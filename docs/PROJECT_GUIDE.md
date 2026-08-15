# RepoMind 项目讲解

> 本文用于快速理解项目、演示项目和准备技术面试。实现细节以当前仓库代码为准。

## 1. 项目是什么

RepoMind 是一个面向 GitHub 仓库的智能代码分析系统。用户输入 Issue 或 Pull Request URL，系统自动拉取仓库与讨论数据，检索相关源码，并让大模型结合工具调用完成分析。

它解决两个问题：

- **Issue 分析**：判断问题类型，定位相关代码与根因，给出严重程度、修改文件和修复建议。
- **PR Review**：从 security、style、logic 三个视角并行审查 diff，输出 `APPROVE` 或 `REQUEST CHANGES`。

这个项目的重点不是简单调用一次 LLM，而是将检索、工具、状态机、并行任务、反思重试和结构化输出组合成可控制的 Agent 工作流。

## 2. 核心架构

```text
GitHub URL
   │
   ├── Issue ──> Issue StateGraph ──> 根因分析报告
   │                │
   │                ├── GitHub Issue / 评论 / 关联 PR
   │                ├── 仓库 clone、代码索引、RAG、调用图
   │                ├── ReAct 代码导航与 Git 工具
   │                ├── Memory、Reflection、HITL
   │                └── SQLite Checkpoint
   │
   └── PR ─────> PR StateGraph ────> Code Review 报告
                    │
                    ├── GitHub PR / diff / 评论
                    ├── security、style、logic 并行审查
                    ├── ReAct 工具与 Reflection
                    └── Memory、SQLite Checkpoint
```

主要模块：

| 目录 | 职责 |
|---|---|
| `workflow/` | LangGraph 状态、节点、路由和工作流定义 |
| `context/` | 仓库加载、代码切片、向量索引、依赖检索、调用图和记忆 |
| `tools/` | GitHub API、代码导航、Git 历史、堆栈解析等工具 |
| `core/llm/` | LLM 客户端、重试、JSON 解析、Pydantic 校验、工具循环 |
| `backend/`、`frontend/` | 异步 Job API 和简单 Web 页面 |
| `agents/` | 跨仓库 Orchestrator 与 RepoAgent 服务 |
| `eval/`、`tests/` | 数据集、LLM Judge、回归评估和自动化测试 |

## 3. Issue 分析链路

主流程定义在 `workflow/graph.py`：

```text
fetch_issue
  → build_project_map
  → describe_images
  → parse_stack_trace
  → route_issue
      ├── bug      → detect_regression
      ├── feature  ─────────────────────┐
      ├── question ─────────────────────┤
      └── security → human_gate ────────┤
                                        ▼
                                  memory_retrieve
                                        → prepare_context
                                        → entry / rag / git 三路并行
                                        → merge_context
      ├── bug/security → classify_fix → analyze_bug
      ├── feature      → detect_existing → analyze_feature
      └── question     → answer_question
                                        → reflect
                                        → generate_report
                                        → memory_save
```

### 3.1 为什么先分类再分析

不同 Issue 需要的证据不同。Bug 关注调用链、回归提交和修复类型；Feature 先确认功能是否已经存在；Question 更重视文档；Security 在继续分析前触发人工确认。提前路由可以让每条分支使用更专门的 prompt 和检索权重，也避免一个“大而全”的 Agent 难以控制。

### 3.2 上下文如何构建

`prepare_context` 先保证仓库已 clone 并建立索引，然后使用 LangGraph `Send` 并行收集三类信息：

1. **Entry**：LLM 从 Issue 中识别入口函数，再用 grep 精确定位定义和附近源码。
2. **RAG**：使用 HyDE、标题、Issue 类型与正文、堆栈信息组成多查询；每路取 top-5，经过 RRF、文件类型加权、CrossEncoder rerank 后保留 top-3；随后补充依赖源码和调用图。
3. **Git**：回归问题根据版本、blame 或近期提交补充相关 diff。

三路分别写入 `_entry_ctx`、`_rag_ctx`、`_git_ctx`，最后由 `merge_context` 合并。这样既避免并行写同一字段的覆盖问题，也把总体耗时从近似三路之和降为最慢一路的耗时。

### 3.3 ReAct 如何工作

分析节点通过 OpenAI-compatible function calling 让模型自主调用工具，包括读文件、搜索代码、查定义、找调用者、查看 Git log/blame 和检索依赖源码。`core/llm/client.py` 保存每轮 assistant tool call 和 tool result，直到模型给出最终答案或达到最大轮数。

与纯 RAG 相比，RAG 负责提供候选上下文，ReAct 负责沿着证据继续探索。例如模型可以先读入口函数，再查被调用函数的定义，最后用 blame 确认相关改动。

### 3.4 Reflection 与停止条件

分析完成后，独立的 critic 节点检查证据是否充分并生成置信度。置信度低于 `0.7` 且重试次数小于 2 时，流程回到对应分析节点；否则生成报告。`iteration` 同时承担停止条件，防止图无限循环。

## 4. PR Review 链路

PR 工作流定义在 `workflow/pr_graph.py`：

```text
fetch_pr → build_project_map → describe_pr_images → memory_retrieve_pr
         → prepare_reviews
         → security / style / logic 三路并行 run_review
         → reflect_pr
              ├── confidence < 0.7 且未达上限 → prepare_reviews
              └── 通过 → generate_pr_report → memory_save_pr
```

三类审查写入不同状态字段，因此可以安全合并。报告规则是：存在 `critical` 或 `high` 问题时返回 `REQUEST CHANGES`，只有 `low`、`medium` 或无问题时返回 `APPROVE`。

## 5. RAG 与索引设计

### 5.1 代码切片

- Python：用 AST 按函数、异步函数和类切片，并保留文件与行号。
- Markdown：按二级标题切片。
- TypeScript、JavaScript、Go、Java、Rust 等：使用带重叠的固定窗口。
- 每个仓库使用独立的 Chroma collection，避免不同仓库的向量互相污染。

### 5.2 混合检索

项目没有把向量相似度当成唯一依据：

- grep 提供精确符号定位；
- 多查询扩大召回范围；
- RRF 融合多个排名，降低单次查询偶然性；
- 文件类型权重让 question 偏文档、bug 偏源码；
- CrossEncoder 对 query-document 对做精排；
- 调用图、依赖源码和 Git 历史补足语义检索不擅长的结构与时间信息。

## 6. 可靠性设计

- **结构化状态**：Issue 和 PR 分别使用 `TypedDict`，节点只返回自己更新的字段。
- **输出约束**：JSON mode、Pydantic Schema、字段 validator 和修复 LLM 构成多层校验。
- **网络重试**：超时、连接失败和限流使用指数退避，最多三次。
- **优雅降级**：索引、rerank、调用图或 Git 上下文失败时尽量继续主分析，而不是让整条链路崩溃。
- **并发保护**：仓库首次 clone/index 使用 per-repo lock，并在加锁前后双重检查。
- **检查点**：Issue 与 PR 使用独立 SQLite checkpoint；同一 `thread_id` 可恢复暂停状态。
- **人工介入**：Security 分支通过 `interrupt()` 暂停，并用 `Command(resume=...)` 从断点恢复。

## 7. 运行项目

### 7.1 环境要求

- Python 3.11 或兼容版本
- Git
- DeepSeek API Key（也可改为兼容 OpenAI SDK 的 endpoint/model）
- GitHub Token

### 7.2 安装与配置

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

在项目根目录创建 `.env`：

```dotenv
DEEPSEEK_API_KEY=your_key
GITHUB_TOKEN=your_token

# 可选
LLM_MODEL=deepseek-chat
LLM_BASE_URL=https://api.deepseek.com
DB_PATH=./repo_knowledge_base
CLONE_BASE=/tmp/repomind_repos
REPO_AGENT_URL=http://localhost:8001
```

### 7.3 CLI

```bash
# Issue 根因分析
python main.py https://github.com/fastapi/fastapi/issues/10236

# PR Review
python main.py https://github.com/psf/requests/pull/6789

# 强制走 Security + HITL 分支
python main.py https://github.com/fastapi/fastapi/issues/1234 --security
```

### 7.4 Web API

```bash
bash start_server.sh
```

```bash
curl -X POST http://localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"issue_url":"https://github.com/fastapi/fastapi/issues/10236"}'

curl http://localhost:8000/jobs/<job_id>
```

注意：当前 Web API 只连接 Issue 工作流；PR Review 通过 CLI 使用。

### 7.5 测试与评估

```bash
# 快速单元/节点测试
pytest tests/ --ignore=tests/test_regression.py

# 端到端回归测试需要网络、模型凭证和本地仓库
pytest tests/test_regression.py -m 'regression and quality'

# 构建并运行评估
python eval/run_eval.py --type bug --owner fastapi --repo fastapi --max 10
python eval/run_eval.py --type question --owner pydantic --repo pydantic --max 10
```

评测以真实 Issue 的合并 PR 或维护者回复作为参考答案，由 LLM Judge 从根因、文件、严重程度、修复可执行性等维度评分。评测结果适合做回归比较，但不应被解释为完全客观的正确率。

## 8. 当前边界与可改进项

面试中主动说明边界，通常比声称“已经生产可用”更可信：

1. **Web Job 状态只存在进程内存中**：服务重启后任务列表丢失，也不支持多实例共享；可迁移到 Redis/PostgreSQL 与 Celery/RQ。
2. **API 只暴露 Issue 分析**：CLI 已支持 PR，但后端尚未统一任务类型与 HITL 恢复接口。
3. **索引缺少增量更新策略**：当前 collection 有数据就走快速路径，远端仓库更新后不会自动同步；可按 commit SHA 做版本化和增量 upsert/delete。
4. **静态调用图能力有限**：基于 AST 的名称匹配难以准确处理动态分派、装饰器、反射和跨语言调用；可接入 LSP、tree-sitter 或语言专用分析器。
5. **检索模型偏通用文本**：`all-MiniLM-L6-v2` 和 MS MARCO reranker 不是专门的代码模型，可比较代码 embedding 和混合 BM25 的效果。
6. **成本与延迟仍需治理**：多查询、并行 reviewer、Reflection 和工具循环会增加 token 与调用次数；应增加缓存、预算、超时和可观测性。
7. **安全与权限控制不足**：公开服务前应增加 URL 白名单/严格校验、认证、限流、仓库大小限制、敏感信息过滤和隔离执行环境。
8. **LLM Judge 存在偏差**：需要固定模型版本、盲评、多次采样，并结合人工标注和确定性指标。

## 9. 一分钟项目介绍

> RepoMind 是我做的 GitHub 代码分析 Agent。它支持 Issue 根因分析和 PR Review，不是把整个仓库直接塞给大模型，而是用 LangGraph 把数据获取、问题路由、混合检索、ReAct 工具调用、Reflection 和报告生成组织成可恢复的状态机。检索侧先用 AST 对代码分块，再用 HyDE 多查询、RRF 和 CrossEncoder 精排，同时补充 grep、调用图、依赖源码和 Git 历史。Issue 上下文收集以及 PR 的安全、风格、逻辑审查都通过 Send API 并行执行。为了提高稳定性，我加入了 Pydantic 输出校验、指数退避、降级路径、SQLite checkpoint 和 Security HITL。项目也有基于真实修复 PR/维护者回复的评测和回归测试。目前主要改进方向是增量索引、持久化任务队列、可观测性以及降低 LLM Judge 偏差。
