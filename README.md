# RepoMind

> 完整的项目讲解、运行说明与设计边界见 [`docs/PROJECT_GUIDE.md`](docs/PROJECT_GUIDE.md)；逐节点详细设计见 [`docs/DETAILED_DESIGN.md`](docs/DETAILED_DESIGN.md)；面试准备见 [`docs/INTERVIEW_QA.md`](docs/INTERVIEW_QA.md)。

两个功能，共用同一套底层基础设施（RAG、Memory、LangGraph、工具层）：

```bash
# Issue 根因分析
python main.py https://github.com/fastapi/fastapi/issues/10236

# PR Code Review
python main.py https://github.com/psf/requests/pull/6789
```

`main.py` 自动识别 URL 格式路由到对应工作流。

---

## 功能一：Issue 智能分析

输入 GitHub Issue URL，自动完成根因分析、代码定位、修复建议，输出结构化报告。

```
根因：APIRouter 的 generate_unique_id_function 参数未被传递到子路由器
受影响文件：fastapi/routing.py, fastapi/applications.py
严重程度：medium
修复建议：在 include_router 时显式传递 generate_unique_id_function 参数
置信度：0.90
```

### Issue 分析流程

```
fetch_issue → build_project_map → describe_images → parse_stack_trace → route_issue
  ├─ bug      → detect_regression → memory_retrieve → prepare_context
  ├─ feature  → memory_retrieve → prepare_context            │
  ├─ question → memory_retrieve → prepare_context     ▼ Send × 3（并行）
  └─ security → human_gate → memory_retrieve        gather_context_part
                                               entry/rag/git 三路同时跑
                                                     ▼ merge_context
  ├─ bug      → classify_fix → analyze_bug (ReAct × 12 rounds, 9 tools)
  ├─ feature  → detect_existing → analyze_feature (ReAct × 12 rounds)
  ├─ question → answer_question (ReAct × 12 rounds)
  └─ security → classify_fix → analyze_bug
  → reflect (retry if confidence < 0.7, max 2x) → generate_report → memory_save
```

---

## 功能二：PR Code Review

输入 GitHub PR URL，三路并行分析安全 / 风格 / 逻辑问题，输出 APPROVE / REQUEST CHANGES 报告。

```
✅ APPROVE — PR #6789: Fix timeout handling
置信度：0.88

🔵 [LOW] style — requests/adapters.py L142
  变量名 `t` 含义不明确，建议改为 `timeout_value`

🟡 [MEDIUM] logic — requests/sessions.py L380
  timeout 参数未传递到底层 urllib3 调用
```

### PR Review 流程

```
fetch_pr → build_project_map → describe_pr_images → memory_retrieve_pr
    → prepare_reviews
    │
    ▼ dispatch_reviews（Send × 3）
    ├─ run_review(security)  → ReAct × 10 轮  → security_issues
    ├─ run_review(style)     → ReAct × 6 轮   → style_issues
    └─ run_review(logic)     → ReAct × 10 轮  → logic_issues
    → reflect_pr (retry if confidence < 0.7, 回 prepare_reviews)
    → generate_pr_report (APPROVE / REQUEST CHANGES) → memory_save_pr
```

**Verdict 逻辑**：存在 critical / high 问题 → ❌ REQUEST CHANGES；全部 low / medium → ✅ APPROVE

---

## 技术要点

### 1. Send API 双路并行

**Issue 上下文收集**（三路）：

```python
def dispatch_context_gather(state: IssueState) -> list[Send]:
    return [
        Send("gather_context_part", {**base, "context_type": "entry"}),  # LLM 入口识别 + grep
        Send("gather_context_part", {**base, "context_type": "rag"}),    # HyDE + 4路RAG + dep + callgraph
        Send("gather_context_part", {**base, "context_type": "git"}),    # git history（仅 regression）
    ]
```

**PR 三路 Review**（三路）：

```python
def dispatch_reviews(state: PRState) -> list[Send]:
    return [
        Send("run_review", {**base, "review_type": "security"}),
        Send("run_review", {**base, "review_type": "style"}),
        Send("run_review", {**base, "review_type": "logic"}),
    ]
```

两处都采用单节点名 + 路由字段模式：`gather_context_part` / `run_review` 按类型路由，各路写入不同 State 字段，LangGraph 自动合并，后续节点等全部完成后触发一次。

### 2. ReAct 工具循环（Coding Agent）

9 个工具，LLM 自主决定下一步操作，最多 12 轮：

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
  search_commits   → 搜 commit message

跨仓库依赖：
  fetch_dependency → RAG 查依赖库源码（含 Rust/C 扩展）
```

### 3. MCP 工具规范

所有工具遵循 **MCP 协议规范**，通过 JSON Schema 定义参数和返回值，统一注册到 `ToolRegistry`：

```python
# tools/base.py
Tool(name="read_file", parameters={
    "type": "object",
    "properties": {"owner": ..., "repo": ..., "path": ..., "start_line": ..., "end_line": ...},
    "required": ["owner", "repo", "path"],
})

tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR, ...)
```

等价于 MCP 的 `get_file_contents / search_code / list_directory`，GitHub REST API 作为 transport。

### 4. Plan-Execute 模式

bug 路径在 ReAct 执行前先完成两步规划：

```
detect_regression  →  classify_fix  →  analyze_bug (执行)
  （是否回归？）     （code_fix /        （ReAct 追踪）
                     dependency_upgrade /
                     config_fix）
```

`classify_fix` 根据判断结果注入差异化提示——`dependency_upgrade` 时告知 LLM 关注 `pyproject.toml`，`config_fix` 时关注配置文件路径。规划与执行分离，避免 ReAct 阶段浪费轮次在"这是什么类型"上。

### 5. Reflection / Critic-Actor

`reflect` 是独立批评者 LLM，不共享 analyze 节点上下文：

```python
"你是严格的代码评审专家，质疑分析结论：证据够吗？还有其他可能原因吗？
 给出 0-1 置信度分，低于 0.7 时说明哪里不足，促使重新分析。"
```

置信度 < 0.7 → 回到对应节点重试，最多 2 次，`iteration` 防死循环。PR Review 中，retry 回到 `prepare_reviews` 跳过 memory 重查，直接重新并行 review。

### 6. RAG 检索管线

**4 路多查询 + RRF 融合 + CrossEncoder Rerank**：

```
HyDE 假设文档  ┐
Issue 原始标题 ├─→ 各 top-5 → RRF 融合 → top-10 → CrossEncoder → top-3
type + body   │
stack trace帧  ┘
```

- **HyDE**：先让 LLM 生成"假设的相关代码片段"，用代码检索代码，解决语义空间不匹配
- **RRF**：`score = Σ 1/(k + rank_i)`，兼顾"出现次数"和"每次排名"
- **文件类型权重**：bug 模式 source ×1.0 / doc ×0.4；question 模式 doc ×1.2 / source ×0.7
- **代码切片**：Python 文件按 AST 函数/类切，保留行号；Markdown 按 `##` 标题切

### 7. ContextBuilder：Grep 优先 + LLM 入口识别

核心观察：RAG 靠语义相似度，不懂调用链。上下文组装三步：

1. **LLM 识别入口**：读 Issue 标题和 body，输出用户触发问题时调用的函数名（JSON 数组，能区分函数名/包名/英文词）
2. **grep 精确定位**：搜索入口函数定义，读取上下文代码，**入口代码放首位**
3. **RAG 补充**：入口有结果时 top-2 补充；无入口时降级 RAG top-3

现在这三步在 `gather_context_part` 三路并行中分别执行，耗时从串行求和变为取最大值。

### 8. Import 驱动依赖索引

RAG 索引时存储每个文件的 `imports` metadata。分析时读取 imports，与 `pyproject.toml` 依赖取交集，自动 clone + 索引依赖库，再对依赖库做 RAG——跨语言（Python → Rust pydantic-core）也能追踪。

### 9. 多模态（Vision 模型提取截图信息）

```python
# describe_images / describe_pr_images 节点
data_url = f"data:{mime};base64,{b64}"
vision_client.chat.completions.create(model="deepseek-vl2", messages=[
    {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "text", "text": "描述图片内容，重点关注错误信息、UI 异常..."},
    ]}
])
```

Issue 和 PR 都复用同一套视觉描述逻辑，图片描述注入对应 analyze 节点的 prompt。

### 10. 共享状态/黑板

两套 TypedDict 分别作为 Issue 和 PR 的黑板：

```python
class IssueState(TypedDict):    # Issue 分析共享状态（~28 字段）
    issue_url: str
    issue_type: Literal["bug", "feature", "question", "security"]
    _entry_ctx: str             # gather_context_part 并行写入
    _rag_ctx: str
    _git_ctx: str
    code_context: str           # merge_context 组装后的最终上下文
    confidence: float
    final_report: str
    # ...

class PRState(TypedDict):       # PR Review 共享状态（~20 字段）
    pr_url: str
    changed_files: list[dict]
    security_issues: list[dict] # run_review(security) 写入
    style_issues: list[dict]    # run_review(style) 写入
    logic_issues: list[dict]    # run_review(logic) 写入
    final_report: str
    # ...
```

每个节点只返回自己更新的字段，LangGraph 原子合并，并行节点写不同 key 无冲突。

### 11. Checkpointing / 断点续跑

```python
# Issue graph → checkpoints.sqlite
# PR graph   → pr_checkpoints.sqlite（独立文件，互不干扰）
conn = sqlite3.connect("repo_knowledge_base/checkpoints.sqlite")
app = graph.compile(checkpointer=SqliteSaver(conn))
```

进程重启后 HITL 暂停状态不丢失；eval runner 利用 checkpoint 断点续跑（跳过已评估的 issue）。

### 12. HITL（Human-in-the-Loop）

```python
# human_gate 节点
decision = interrupt({"message": "⚠️ Security Issue 需要人工确认", ...})

# main.py：检测到 interrupt，等待输入后用同一 thread_id 恢复
result = app.invoke(Command(resume=decision), config=config)
```

`interrupt()` 是断点不是结束，`Command(resume)` 从 checkpointer 恢复，`human_gate` 从 `interrupt()` 那行继续，不重跑整个 graph。

### 13. 层级编排（3 层）

```
main.py（用户入口）
  └─ OrchestratorAgent（识别跨仓库依赖，分发子任务）
       └─ RepoAgent × N（各自独立 graph，clone + index + 分析）
```

- **OrchestratorAgent**：LLM 判断根因可能跨哪些仓库 → `Send API` 动态并行分发
- **RepoAgent**：独立 FastAPI 服务，Job Queue 非阻塞，`GET /jobs/{id}` 轮询结果
- **reducer 聚合**：`Annotated[list[dict], operator.add]`，并行子节点结果自动追加不覆盖

### 14. A2A + ANP 协议

```python
# A2A：Agent 间通过 HTTP 通信
httpx.post(f"{repo_agent_url}/analyze", json={"issue_url": ...})

# ANP：地址自知，无需服务发现
REPO_AGENT_URLS = {
    "fastapi/fastapi": "http://localhost:8001",
    "encode/starlette": "http://localhost:8002",
}
```

OrchestratorAgent 直接持有各 RepoAgent 的 URL，无中间注册表，可独立部署和横向扩展。

### 15. 角色专业化

| 节点 | 角色 |
|---|---|
| `analyze_bug` | 资深后端工程师，10 年源码阅读经验，擅长调用链追踪 |
| `analyze_feature` | 资深软件架构师，评估可行性、侵入性、breaking change 风险 |
| `answer_question` | 技术文档工程师，区分"使用问题"vs"设计决策" |
| `analyze_security` | 安全工程师，OWASP Top 10 视角，ReAct × 10 轮 |
| `analyze_style` | 资深工程师，代码规范和可维护性，ReAct × 6 轮 |
| `analyze_logic` | 资深工程师，业务逻辑正确性，ReAct × 10 轮 |
| `reflect / reflect_pr` | 严格评审专家，独立评估，质疑证据充分性 |

### 16. LLM 输出三层防御（Guardrails）

```
response_format=json_object  →  Pydantic Schema 校验  →  修复 LLM 兜底
（语法保证）                      （类型/枚举/必填）        （校验失败重修复，仍失败降级）
```

`severity` 枚举值容错、`root_cause` 空字符串拦截、API 超时指数退避重试（tenacity 3次，2s→4s→8s）。

### 17. Memory（跨 Issue / PR 经验积累）

```python
# Issue memory
content = f"仓库: fastapi/fastapi | 类型: bug\n根因: {root_cause}\n修复: {fix_suggestion}"
_memory_collection.add(documents=[content], metadatas=[{"repo": "fastapi/fastapi", "type": "issue"}])

# PR memory（独立 metadata 隔离）
_memory_collection.add(documents=[report], metadatas=[{"repo": "...", "type": "pr_review"}])

# 检索时按 type 过滤，Issue 和 PR 经验不互相污染
results = _memory_collection.query(query_texts=[...], where={"type": "issue", "repo": ...})
```

---

## 评估结果

### Bug Eval（4 repos × 68 issues）

| 仓库 | Overall |
|---|---|
| fastapi/fastapi | 0.77 |
| python/mypy | 0.72 |
| psf/requests | 0.64 |
| celery/celery | 0.62 |
| **Macro avg** | **0.69** |

4 个仓库均处于 0.6-0.8 良好区间。

**工具升级前后对比（6 rounds → 12 rounds + git tools）**：macro avg 0.521 → 0.570，mypy +0.22，celery +0.16

### LLM Judge 评估体系

```
AI 分析结果
    ├─ Bug Judge   → root_cause_accuracy / file_accuracy / severity_fit / fix_actionability
    ├─ Feature Judge → requirement_understanding / architecture_fit / implementation_feasibility
    └─ Question Judge → confusion_identified / answer_accuracy / actionability
```

每维度 LLM 打分 n=3 次取均值，ground truth：Bug/Feature 用合并 PR diff，Question 用维护者权威回复。

### Question Eval（pydantic × 5 issues）

macro avg **0.50**。规律：API 用法问题高分（0.89-0.94），设计决策问题低分（LLM 倾向于"修代码"而非"解释意图"）。

---

## 测试（227 条，5 层）

| 层级 | 文件 | 条数 | 特点 |
|---|---|---|---|
| 工具单测 | `test_code_nav.py` | 36 | 9 个工具函数，需本地克隆仓库，无 LLM |
| LLM工具单测 | `test_llm_utils.py` | 34 | parse_json / Guardrails / Judge，mock LLM |
| 节点测试（Issue） | `test_nodes.py` | 76 | 所有节点 + 并行上下文收集，mock LLM |
| 节点测试（PR） | `test_pr_review.py` | 63 | PR fetch / 三路并行 / graph 结构，mock LLM |
| 鲁棒性测试 | `test_robustness.py` | 18 | 空路径、特殊字符、边界输入 |
| 端到端回归 | `test_regression.py` | 8 | 4 quality（分数门禁）+ 4 smoke（结构验证） |

```bash
pytest tests/ --ignore=tests/test_regression.py          # 227 条，~20s
pytest tests/test_regression.py -m "regression and quality"  # 端到端质量门禁
```

---

## 技术栈

| 技术 | 用途 |
|---|---|
| **LangGraph** | StateGraph 工作流，条件路由，回环，SqliteSaver |
| **LangGraph Send API** | Issue 上下文三路并行收集 + PR Review 三路并行审查 |
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

# 4. Review PR
python main.py https://github.com/psf/requests/pull/6789

# 5. Security Issue（触发 HITL 人工审核）
python main.py https://github.com/fastapi/fastapi/issues/1234 --security

# 6. 跑评估
python eval/run_eval.py --type bug --owner fastapi --repo fastapi --max 10
python eval/run_eval.py --type question --owner pydantic --repo pydantic --max 10
```

---

## 项目结构

```
workflow/
  nodes/              # Issue 分析节点（fetch/route/retrieve/analyze/reflect/memory/...）
  nodes/analyze_pr.py # PR Review 节点（三路并行 + reflect + report）
  nodes/fetch_pr.py   # PR 数据拉取 + 图片描述
  graph.py            # Issue LangGraph StateGraph
  pr_graph.py         # PR Review LangGraph StateGraph
  state.py            # IssueState TypedDict（共享黑板）
  pr_state.py         # PRState TypedDict

tools/
  code_nav.py         # 9个代码导航工具（MCP 规范）
  github_api.py       # Issue REST API
  github_pr_api.py    # PR REST API + diff 过滤
  git_history.py      # git log/blame/search
  stack_trace.py      # traceback 解析

context/
  rag_index.py        # 代码向量化索引
  repo_loader.py      # 自动 clone + index
  dep_indexer.py      # import 驱动依赖索引
  memory_store.py     # Issue/PR 经验记忆

eval/
  dataset.py          # Bug/Feature/Question 数据集构建
  judge.py            # LLM Judge 多维度打分
  runner.py           # 评估运行器（断点续跑）
  report.py           # 汇总报告生成

agents/
  repo_agent_server.py    # RepoAgent FastAPI 服务（A2A）
  orchestrator/           # OrchestratorAgent（层级编排）

tests/                    # 227条测试（5层）
```
