# RepoMind

输入一个 GitHub Issue URL，自动完成根因分析、代码定位、修复建议，输出结构化报告。

```bash
python main.py https://github.com/fastapi/fastapi/issues/10236
```

```
根因：APIRouter 的 generate_unique_id_function 参数未被传递到子路由器
受影响文件：fastapi/routing.py, fastapi/applications.py
严重程度：medium
修复建议：在 include_router 时显式传递 generate_unique_id_function 参数
置信度：0.90
```

---

## 架构

基于 **LangGraph StateGraph** 的多节点分析流水线，支持 Bug / Feature / Question / Security 四条路径。

```
fetch_issue → build_project_map → describe_images → parse_stack_trace → route_issue
  ├─ bug      → detect_regression → memory_retrieve → retrieve_context
  │             → classify_fix → analyze_bug (ReAct × 12 rounds, 9 tools)
  ├─ feature  → memory_retrieve → retrieve_context
  │             → detect_existing → analyze_feature (ReAct × 12 rounds)
  ├─ question → memory_retrieve → retrieve_context
  │             → answer_question (ReAct × 12 rounds)
  └─ security → human_gate (interrupt) → memory_retrieve → retrieve_context → analyze_bug
  → reflect (retry if confidence < 0.7, max 2x) → generate_report → memory_save
```

### 核心节点

| 节点 | 职责 |
|---|---|
| `build_project_map` | 读仓库 2 层目录树 → LLM 生成项目摘要，注入所有分析节点，减少 ReAct 摸结构的浪费 |
| `route_issue` | 内容信号优先分类（有 traceback → bug；用词含 "how to" → question），label 只作参考 |
| `detect_regression` | 判断是否为回归 bug，标记 `is_regression` 供 git 工具使用 |
| `classify_fix` | 将 bug 细分为 `code_fix / dependency_upgrade / config_fix`，注入差异化提示 |
| `detect_existing` | feature 专用；让 LLM 推理"功能存在的证据是什么"，精确 grep 验证，避免误判 |
| `analyze_*` | ReAct 工具循环，主动追踪代码，最多 12 轮 |
| `reflect` | 独立批评者 LLM 打置信度分，< 0.7 触发重试 |
| `human_gate` | Security 类强制 `interrupt()`，人工确认后 `Command(resume)` 恢复 |
| `memory_retrieve/save` | ChromaDB `issue_memory` collection，历史同类 Issue 根因检索与沉淀 |

---

## 技术要点

### 1. Coding Agent（ReAct 工具循环）

9 个工具构成主动代码追踪能力：

```
代码导航：
  read_file        → 读文件指定行范围
  grep_code        → 全库文本搜索
  list_dir         → 目录结构
  find_definition  → 精确跳到函数/类定义
  find_callers     → 向上追溯调用链

Git 时间轴（回归 bug 专用）：
  git_log_file     → 文件提交历史
  git_blame        → 行级 commit 归属
  search_commits   → 搜 commit message 找历史修复

跨仓库依赖：
  fetch_dependency → RAG 查依赖库源码（含 Rust/C 扩展）
```

### 2. RAG 检索管线

**4 路多查询 + RRF 融合 + CrossEncoder Rerank**：

```
HyDE 假设文档  ┐
Issue 原始标题 ├─→ 各 top-5 → RRF 融合 → top-10 → CrossEncoder → top-3
type + body   │
stack trace帧  ┘
```

- **HyDE**：先让 LLM 生成"假设的相关代码片段"，用代码检索代码，解决自然语言与代码的语义空间不匹配
- **RRF**（Reciprocal Rank Fusion）：`score = Σ 1/(k + rank_i)`，兼顾"出现次数"和"每次排名"
- **文件类型权重**：bug 模式 source ×1.0 / doc ×0.4；question 模式 doc ×1.2 / source ×0.7
- **代码切片**：Python 文件按 AST 函数/类切，保留行号；Markdown 按 `##` 标题切

### 3. Grep 优先 + LLM 入口识别

核心观察：RAG 靠语义相似度，不懂调用链。改造 `retrieve_context`：

1. LLM 读 Issue 输出入口函数名（JSON 数组，能区分函数名/包名/英文词）
2. grep 精确定位入口函数定义，读取上下文代码
3. 入口代码放首位，RAG top-2 补充；无入口时降级 RAG top-3

### 4. Import 驱动依赖索引

RAG 索引时存储每个文件的 `imports` metadata。`retrieve_context` 读取 imports，与 `pyproject.toml` 依赖取交集，自动 clone + 索引依赖库，再对依赖库做 RAG——跨语言（Python → Rust pydantic-core）也能追踪。

### 5. A2A 跨仓库协作（LangGraph Send API）

根因在上游依赖时，OrchestratorAgent 并行调多个 RepoAgent：

```python
# 动态分发，并行执行
def dispatch_to_repo_agents(state):
    return [Send("repo_analysis", {"repo_name": repo}) for repo in state["relevant_repos"]]

# reducer 自动合并并行结果
repo_results: Annotated[list[dict], operator.add]
```

各 RepoAgent 是独立 FastAPI 服务，自动 clone + 索引，Job Queue 模式异步返回结果。

### 6. LLM 输出三层防御

```
response_format=json_object  →  Pydantic Schema 校验  →  修复 LLM 兜底
（语法保证）                      （类型/枚举/必填）        （校验失败重修复，仍失败降级）
```

`severity` 枚举值容错、`root_cause` 空字符串拦截、API 超时指数退避重试（tenacity）。

### 7. HITL（Human-in-the-Loop）

```python
# human_gate 节点
decision = interrupt({"message": "⚠️ Security Issue 需要人工确认", ...})
return {"human_decision": decision}

# main.py 恢复
result = app.invoke(Command(resume=decision), config=config)  # 同 thread_id 从断点继续
```

Checkpoint 持久化到 SQLite，进程重启后暂停状态不丢失。

### 8. Reflection 自我修正

`reflect` 节点是独立的批评者 LLM 实例，不共享 analyze 节点的上下文，打分更客观。置信度 < 0.7 时回到对应分析节点重试，最多 2 次，`iteration` 字段防死循环。

---

## 评估结果

### Bug Eval（9 repos × 68 issues）

| 仓库 | Overall |
|---|---|
| fastapi/fastapi | 0.77 |
| python/mypy | 0.72 |
| psf/requests | 0.64 |
| celery/celery | 0.62 |
| psf/black | 0.58 |
| pydantic/pydantic | 0.51 |
| aio-libs/aiohttp | 0.49 |
| scrapy/scrapy | 0.44 |
| pallets/flask | 0.35 |
| **Macro avg** | **0.570** |

优秀(≥0.8): 31% · 良好(0.6-0.8): 25% · 一般(0.4-0.6): 13% · 较差(<0.4): 31%

**工具升级前后对比（6 rounds → 12 rounds + git tools）**：macro avg 0.521 → 0.570，mypy +0.22，celery +0.16

### Question Eval（pydantic × 5 issues，首次）

macro avg **0.50**，规律：API 用法问题高分（0.89-0.94），设计决策问题低分（LLM 倾向于"修代码"而非"解释意图"）

---

## 测试

```bash
# 151条单元/节点/鲁棒性测试，~13s
pytest tests/ --ignore=tests/test_regression.py

# 8条端到端回归（需 DEEPSEEK_API_KEY + GITHUB_TOKEN）
pytest tests/test_regression.py -m regression -v

# 只跑质量门禁（4条，分数必须 >= 阈值）
pytest tests/test_regression.py -m "regression and quality"
```

---

## 技术栈

| 技术 | 用途 |
|---|---|
| **LangGraph** | StateGraph 工作流，条件路由，回环，Send API 并行，SqliteSaver |
| **ChromaDB** | 向量数据库，per-repo collection 隔离 + Memory collection |
| **Sentence Transformers** | 代码/文档向量化（all-MiniLM-L6-v2） |
| **CrossEncoder** | ms-marco-MiniLM-L-6-v2，Rerank 二次排序 |
| **DeepSeek API** | LLM（deepseek-chat）+ 视觉模型（deepseek-vl2） |
| **FastAPI** | RepoAgent HTTP 服务化，Job Queue 非阻塞 |
| **pydantic-settings** | 统一配置入口，SecretStr 脱敏，启动校验 |
| **tenacity** | API 调用指数退避重试 |
| **Python ast** | Python 文件语义切片（按函数/类） |

---

## 快速开始

```bash
# 1. 安装依赖
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. 配置
cp .env.example .env
# 填入 DEEPSEEK_API_KEY 和 GITHUB_TOKEN

# 3. 分析 Issue
python main.py https://github.com/psf/requests/issues/6859

# 4. 跑评估
python eval/run_eval.py --type bug --owner fastapi --repo fastapi --max 10
python eval/run_eval.py --type question --owner pydantic --repo pydantic --max 10
```

---

## 项目结构

```
workflow/
  nodes/          # 11个节点（fetch/route/analyze/reflect/memory/...）
  graph.py        # LangGraph StateGraph 定义
  state.py        # IssueState TypedDict

tools/
  code_nav.py     # 9个代码导航工具
  git_history.py  # git log/blame/search
  stack_trace.py  # traceback 解析

context/
  rag_index.py    # 代码向量化索引
  repo_loader.py  # 自动 clone + index
  dep_indexer.py  # import 驱动依赖索引
  memory_store.py # Issue 经验记忆

eval/
  dataset.py      # Bug/Feature/Question 数据集构建
  judge.py        # LLM Judge 多维度打分
  runner.py       # 评估运行器（断点续跑）
  report.py       # 汇总报告生成

agents/
  repo_agent_server.py      # RepoAgent FastAPI 服务
  orchestrator/             # A2A OrchestratorAgent

tests/                      # 151条测试（4层）
```
