# RepoMind 开发进度

## 项目简介

输入 GitHub Issue URL，自动完成根因分析、代码定位、修复建议，输出结构化报告。

---

## 已实现功能

### 1. 基础链路（LangGraph StateGraph）

用 LangGraph 搭建主工作流，所有节点共享一个 `IssueState`（TypedDict），节点函数只负责读取和更新 state 的局部字段。

```
fetch_issue → build_project_map → describe_images → parse_stack_trace → route_issue
  → detect_regression / detect_existing → memory_retrieve → retrieve_context
  → classify_fix / detect_existing → analyze_* (ReAct × 12 rounds)
  → reflect → generate_report → memory_save
```

关键文件：`workflow/state.py`、`workflow/graph.py`、`workflow/nodes/`

---

### 2. GitHub Issue 拉取

直接用 `httpx` 调 GitHub REST API，拉取 Issue 的标题、正文、标签、评论。不依赖第三方 Agent 框架，报错透明。

```python
GET /repos/{owner}/{repo}/issues/{number}
GET /repos/{owner}/{repo}/issues/{number}/comments
GET /repos/{owner}/{repo}/issues/{number}/timeline   # 关联 PR
```

**分页评论**：原来只拉第一页（30 条），现在循环 `page=1,2,...` 直到 batch 为空，每页 100 条，对活跃 Issue 不再漏评论。

**关联 PR 拉取**：通过 Timeline API 找 `cross-referenced` 事件，提取关联 PR URL 存入 `state.related_prs`。`analyze_bug` 节点将其注入 prompt，LLM 可参考已知修复 PR 做根因定位。

关键文件：`tools/github_api.py`（`_fetch_all_comments`、`_fetch_related_prs`）、`workflow/nodes/fetch.py`（`_pr_section`）

---

### 3. 条件路由（LangGraph add_conditional_edges）

`route_issue` 节点让 LLM 把 Issue 分类为 bug / feature / question / security，然后通过 `add_conditional_edges` 路由到不同的分析节点。

```python
graph.add_conditional_edges("route_issue", decide_route, {
    "bug":      "detect_regression",  # bug 先检查是否回归
    "feature":  "memory_retrieve",
    "question": "memory_retrieve",
    "security": "human_gate",         # security 先过人工审核
})
```

四种类型对应四个不同的 analyze 节点，prompt 侧重点不同（bug 找根因、feature 评估可行性、question 直接解答、security 走人工审核）。

---

### 4. RAG 代码库检索（ChromaDB + Sentence Transformers）

把目标仓库的代码文件（.py/.md/.ts 等）向量化后存入 ChromaDB 本地数据库。分析前检索最相关的 3 个代码片段，作为上下文注入 analyze 节点的 prompt。

**切片策略（智能分块）：**
- Python 文件 → 用 `ast` 模块按函数/类定义切，每个函数一个 chunk，保留行号
- Markdown 文件 → 按 `##` 标题切，每个章节一个 chunk
- 其他文件 → 500 行滑动窗口，50 行重叠

```python
# Python 文件：AST 解析，按函数切
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
        snippet = lines[node.lineno - 1 : node.end_lineno]
        # chunk id 带行号：path/to/file.py:L10-35
```

**HyDE 检索（Hypothetical Document Embedding）：**

直接用 Issue 文字检索代码存在语义空间不匹配的问题（自然语言 vs 代码）。HyDE 先让 LLM 生成一段"假设的相关代码片段"，用这段代码去做向量检索，代码匹配代码，召回更准确。

```python
# 1. LLM 生成假设文档
hypothetical_doc = _chat("根据 Issue 生成可能相关的代码片段...")

# 2. 用假设文档检索，而不是原始 query
results = _rag_collection.query(query_texts=[hypothetical_doc], n_results=3)
```

建库：
```bash
rm -rf repo_knowledge_base   # 重新建库时先清空
python context/rag_index.py /path/to/repo
```

**多查询检索 + RRF 融合 + Rerank：**

单次 HyDE 检索存在角度单一的问题。升级为 4 路多查询 + RRF 融合 + CrossEncoder 二次排序：

```
4 个查询角度：
  1. HyDE 假设文档（代码对代码）
  2. Issue 原始标题（直接语义）
  3. issue_type + body 前 150 字（类型+描述）
  4. Stack trace 帧文本（有则最精确）

每路 top-5 → RRF 融合 → top-10 候选
  → CrossEncoder rerank → top-3 注入 prompt
```

**RRF（Reciprocal Rank Fusion）**：工业标准多路召回融合算法，`score(doc) = Σ 1/(k+rank_i)`，同时考虑"出现几次"和"每次排第几"，比简单频次计数更精确。

**CrossEncoder Rerank**：`cross-encoder/ms-marco-MiniLM-L-6-v2`，把 query 和 document 一起输入模型计算相关性分数，比 bi-encoder 向量检索更准确，但只在候选集上跑（top-10），保证速度。Reranker 懒加载单例，启动时不阻塞，失败时自动降级到 RRF 结果。

关键文件：`context/rag_index.py`（切片），`workflow/nodes/retrieve.py`（`_rrf_merge` / `_get_reranker` / `retrieve_context`）

---

### 5. Reflection 自我修正（LangGraph 回环边）

analyze 节点输出后，`reflect` 节点让另一个 LLM 实例扮演批评者，对根因分析打置信度分（0-1）。

- 置信度 < 0.7 且重试次数 < 2：回到对应 analyze 节点重新分析
- 置信度 ≥ 0.7 或已重试 2 次：进入报告生成

通过 `add_conditional_edges` 实现回环，`iteration` 字段防止死循环：

```python
def decide_reflection(state: IssueState) -> str:
    if state["confidence"] < 0.7 and state["iteration"] < 2:
        return state["issue_type"]  # 回到对应分析节点
    return "done"
```

实测：第一次分析质量差时（置信度 0.3），自动重试，第二次结合 RAG 上下文后置信度提升至 0.85 通过。

关键文件：`workflow/graph.py`（`decide_reflection`）、`workflow/nodes/reflect.py`（`reflect`）

---

### 6. Memory 历史模式（ChromaDB issue_memory collection）

用 ChromaDB 单独建一个 `issue_memory` collection，与代码库索引共用同一个数据库路径但不同 collection。

**两个节点：**

- `memory_retrieve`：分析前按 `{owner}/{repo} {issue_type}` 检索历史同类 Issue 的根因摘要，作为先验注入 analyze 节点的 prompt
- `memory_save`：报告生成后把本次根因摘要存入 memory，供后续分析复用

```python
# 存储格式
content = "仓库: fastapi/fastapi | 类型: feature | Issue #1234: ...\n根因: ..."

# 检索
query = f"{owner}/{repo} {issue_type}"
results = _memory_collection.query(query_texts=[query], n_results=3)
```

**存储内容升级**：从 `root_cause[:200]` 扩展为完整解决记录，包含标题、类型、严重程度、根因、修复方案、涉及文件、置信度：

```python
content = f"""Issue: {title}
类型: {issue_type} | 严重程度: {severity}
根因: {root_cause}
修复方案: {fix_suggestion}
涉及文件: {affected_files}
置信度: {confidence}"""
```

**查询升级**：从粗糙的 `owner/repo + issue_type` 改为语义查询 + repo 过滤：

```python
results = _memory_collection.query(
    query_texts=[f"{issue_type}: {title}"],  # 语义更丰富
    n_results=5,
    where={"repo": f"{owner}/{repo}"},       # 只查当前仓库历史
)
```

`where` 过滤确保不同仓库的经验不会互相干扰。空 collection 时自动 fallback 不带过滤。

关键文件：`context/memory_store.py`、`workflow/nodes/memory.py`（`memory_retrieve`、`memory_save`）

---

### 7. InMemorySaver（LangGraph Checkpointer）

给 LangGraph graph 加上 checkpointer，每个节点执行完后自动保存 state 快照，通过 `thread_id` 区分不同执行实例。这是后续 HITL `interrupt()` 的必要前提。

```python
from langgraph.checkpoint.memory import InMemorySaver
app = graph.compile(checkpointer=InMemorySaver())

# 调用时必须传 config
config = {"configurable": {"thread_id": "thread-1"}}
result = app.invoke({...}, config=config)
```

关键文件：`workflow/graph.py`、`main.py`

---

### 8. HITL 人工审核（LangGraph interrupt + Command(resume)）

Security 类 Issue 风险高，在进入分析前强制暂停，等人工确认后继续。

**流程**：`route_issue` 路由到 `human_gate` 节点 → `interrupt(payload)` 抛出特殊异常 → LangGraph 把完整 state 存入 checkpointer，`invoke()` 提前返回 → 检测到 `__interrupt__` 字段 → 人工输入 decision → `Command(resume=decision)` 从断点恢复。

```python
# human_gate 节点
def human_gate(state):
    decision = interrupt({
        "message": "⚠️ Security Issue 需要人工确认",
        "issue": f"#{state['issue_number']} {state['title']}",
    })
    return {"human_decision": decision}

# main.py 处理暂停和恢复
result = app.invoke({...}, config=config)
if result.get("__interrupt__"):
    decision = input("approve/reject: ")
    result = app.invoke(Command(resume=decision), config=config)
```

**关键**：`interrupt()` 是断点，不是结束。`Command(resume=...)` 用同一 `thread_id` 从 checkpointer 恢复，`human_gate` 函数从 `interrupt()` 那行继续执行，不重跑整个 graph。

测试方式：`python main.py --security`（`--security` 参数强制 `route_issue` 返回 security 类型）

关键文件：`workflow/nodes/route.py`（`human_gate`）、`workflow/graph.py`、`main.py`

---

### 9. A2A 跨仓库协作分析（LangGraph Send API）

单 Agent 只有一个 RAG 索引，无法处理根因在上游依赖仓库的 Issue（如 FastAPI 的 Bug 实际出在 Starlette）。A2A 让每个仓库有独立的 RepoAgent 服务，OrchestratorAgent 并行调用多个 RepoAgent，汇总出跨仓库的根因归属。

**架构**：

```
invoke({issue_url})
  │
  ▼
fetch_and_detect
  ├── fetch_issue() → title, body
  └── LLM → relevant_repos: ["fastapi/fastapi", "encode/starlette"]
  │
  ▼ Send API（并行）
  ├──────────────────────────────────────┐
  ▼                                      ▼
repo_analysis                      repo_analysis
repo_name=fastapi/fastapi          repo_name=encode/starlette
  │ httpx.post /analyze               │ httpx.post /analyze
  ▼                                   ▼
RepoAgent（完整 graph）           RepoAgent（完整 graph）
返回 [result_A]                   返回 [result_B]
  │                                   │
  └──────── operator.add 合并 ────────┘
                    │
                    ▼
             merge_reports
             LLM 综合判断根因归属
                    │
                    ▼
             final_report（跨仓库）
```

**Send API**：`dispatch_to_repo_agents` 返回 `Send` 列表，LangGraph 并行启动多个 `repo_analysis` 实例，各自携带独立的 `RepoAnalysisState`：

```python
def dispatch_to_repo_agents(state):
    return [Send("repo_analysis", {"repo_name": repo, ...}) for repo in state["relevant_repos"]]
```

**reducer 聚合**：并行子节点各自返回 `{"repo_results": [result]}`，通过 `Annotated[list[dict], operator.add]` 自动合并，不会互相覆盖：

```python
class OrchestratorStateWithReducer(OrchestratorState):
    repo_results: Annotated[list[dict], operator.add]
```

**RepoAgent 服务**：现有 RepoMind graph 包成 FastAPI `/analyze` 接口，自动 clone + 建索引，对外暴露为独立服务，可被多个 Orchestrator 复用。

运行方式：
```bash
# 终端 1：启动 RepoAgent
python agents/repo_agent_server.py

# 终端 2：运行 Orchestrator
python main_orchestrator.py
```

关键文件：`agents/repo_agent_server.py`、`agents/orchestrator/graph.py`、`agents/orchestrator/nodes.py`

---

### 10. Tools 工具层

分析链路中注入的增强工具，每个工具对应一个独立节点，无结果时返回空字符串不影响主流程。

#### 多模态图片描述（`describe_images`）

提取 Issue body 里的 Markdown 图片链接和裸图片 URL，下载后 base64 编码，调 `deepseek-vl2` 视觉模型描述图片内容（错误截图、UI 异常、代码片段等），描述结果注入三个 analyze 节点的 prompt。

```python
# 提取图片 URL → 下载 → base64 → 调视觉模型
data_url = f"data:{mime};base64,{b64}"
result = vision_client.chat.completions.create(
    model="deepseek-vl2",
    messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "text", "text": "描述图片内容，重点关注错误信息..."},
    ]}],
)
```

关键文件：`workflow/nodes/fetch.py`（`describe_images`）

#### Stack Trace 精确定位（`parse_stack_trace`）

从 Issue body + 评论里提取 Python / JS traceback，用正则解析出文件路径和行号，在本地克隆的仓库中找到对应文件，读取行号附近 ±15 行的代码片段，直接注入 `analyze_bug` 的 prompt。

```
File "fastapi/routing.py", line 227, in get_request_handler
                ↓
读取 routing.py L212-L242，行号标注，箭头指向出错行
```

比 HyDE + RAG 检索更精确——有 stack trace 时直接定位，不需要语义猜测。

```python
# 解析帧 → 在仓库里搜索文件 → 读取代码片段
pattern = r'File ["\']([^"\']+\.py)["\'],\s*line\s*(\d+)'
snippet = _read_snippet(repo_path, frame["file"], frame["line"], context=15)
```

关键文件：`tools/stack_trace.py`、`workflow/nodes/fetch.py`（`parse_stack_trace`）

---

### 11. Prompt 工程加固（System Prompt + CoT + 结构化输出）

#### System Prompt 专属角色

`_chat()` 新增 `system` 参数，每个节点有专属 system prompt，定义 LLM 的角色和思维框架：

| 节点 | System Prompt 角色 |
|---|---|
| `route_issue` | GitHub Issue 分类专家 |
| `analyze_bug` | 资深后端工程师，10 年源码阅读经验 |
| `analyze_feature` | 资深软件架构师，评估可行性和实现路径 |
| `answer_question` | 技术文档工程师兼社区维护者 |
| `reflect` | 严格的代码评审专家，独立评估分析质量 |
| `retrieve_context`（HyDE） | 擅长预测相关代码形态的工程师 |

#### Chain-of-Thought（CoT）

analyze 节点的 system prompt 规定了强制执行的分步思维链路，例如 `analyze_bug`：

```
1. 复现路径：提炼触发 Bug 的最短操作序列
2. 代码追踪：沿调用栈逐层追踪，找到异常分支
3. 根因定位：区分表象和根因，指出具体文件和行号
4. 影响评估：判断受波及的功能和用户场景
5. 最小修复：改动最小、副作用最低的修复方案
```

JSON 输出中新增 `reasoning` 字段，记录完整推理过程，可审查、可 debug。

#### 结构化输出扩展

analyze 节点从 2 个字段扩展为 7 个：

```json
{
  "reasoning": "分步推理过程...",
  "root_cause": "...",
  "affected_files": ["src/auth.py"],
  "severity": "high",
  "fix_suggestion": "...",
  "code_changes": [{"file": "src/auth.py", "line": 42, "change": "..."}],
  "suggested_labels": ["bug", "auth"]
}
```

关键文件：`workflow/nodes/analyze.py`（`_SYSTEM_BUG` / `_SYSTEM_FEATURE` / `_SYSTEM_QUESTION` / `_SYSTEM_REFLECT`）

---

### 12. LLM 输出防御（三层）

#### 第一层：`response_format` 强制 JSON 语法

`_chat()` 加 `json_mode=True` 参数，传入 `response_format={"type": "json_object"}`，DeepSeek 保证输出合法 JSON，从源头消灭语法错误。

#### 第二层：Pydantic Schema 校验

```python
class AnalyzeResult(BaseModel):
    root_cause: str                          # 必填，不能为空
    affected_files: list[str] = []
    severity: Literal["critical","high","medium","low"] = "medium"
    fix_suggestion: str                      # 必填
    code_changes: list[CodeChange] = []
    suggested_labels: list[str] = []

    @field_validator("severity", mode="before")
    def normalize_severity(cls, v):
        return v if v in (...) else "medium"  # 枚举值容错
```

类型错误、枚举越界、必填字段为空——全部在 Pydantic 校验层被捕获。

#### 第三层：LLM 修复兜底 + 降级

```
_chat_json() 调用
  │ Pydantic 校验失败
  ├─ 把原始输出 + Schema 要求交给修复专用 LLM → 再次校验
  └─ 修复仍失败 → 返回降级结果，流程不中断
```

#### 网络层重试（tenacity）

```python
@retry(
    retry=retry_if_exception_type((APITimeoutError, APIConnectionError, RateLimitError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),  # 2s → 4s → 8s...
    stop=stop_after_attempt(3),
    reraise=True,
)
def _chat(...):
```

API 超时、限流、连接断开自动指数退避重试 3 次，3 次全败后才抛异常。

关键文件：`core/llm/client.py`（`chat` / `parse_json` / `AnalyzeResult` / `CodeChange`）

---

### 13. 代码架构重构

#### 配置单一入口（`config.py`）

用 `pydantic-settings` 的 `BaseSettings` 统一读取环境变量，消灭散落各处的 `os.getenv`。`SecretStr` 防止 token 打印到日志；启动时立刻 validate，`Bearer None` 问题在进程启动时就会报错而不是调用时才发现。

#### LLM 客户端单例（`core/llm/client.py`）

原本 `client = OpenAI(...)` 在 3 个文件重复。现在统一到 `core/llm/client.py`，所有节点 import `chat` / `chat_json`，换模型或 base_url 只改 `config.py` 一行。

#### nodes.py 拆分（618 行 → 11 个职责单一文件）

| 文件 | 职责 |
|---|---|
| `workflow/nodes/fetch.py` | `fetch_issue` / `describe_images` / `parse_stack_trace` |
| `workflow/nodes/project_map.py` | `build_project_map`（目录树→LLM摘要，per-repo 缓存） |
| `workflow/nodes/route.py` | `route_issue` / `human_gate` |
| `workflow/nodes/detect_regression.py` | `detect_regression` |
| `workflow/nodes/detect_existing.py` | `detect_existing`（feature 专用，判断功能是否已实现） |
| `workflow/nodes/classify_fix.py` | `classify_fix`（code_fix / dependency_upgrade / config_fix） |
| `workflow/nodes/retrieve.py` | `retrieve_context` + RRF + Reranker |
| `workflow/nodes/analyze.py` | `analyze_bug` / `analyze_feature` / `answer_question` |
| `workflow/nodes/reflect.py` | `reflect` |
| `workflow/nodes/report.py` | `generate_report` |
| `workflow/nodes/memory.py` | `memory_retrieve` / `memory_save` |

`workflow/nodes/__init__.py` 重导出所有函数，`graph.py` 的 import 路径零改动。

#### Job Queue 模式（`repo_agent_server.py`）

原 `POST /analyze` 同步阻塞（git clone + LLM 链式调用），uvicorn 事件循环被完全卡死。改为：
- `POST /analyze` 立即返回 `{job_id, status_url}`（HTTP 202）
- 任务通过 `loop.run_in_executor` 放进线程池
- `GET /jobs/{job_id}` 轮询状态和结果

#### SqliteSaver 替换 InMemorySaver（`workflow/graph.py`）

Checkpoint 持久化到 `repo_knowledge_base/checkpoints.sqlite`，进程重启后 HITL 暂停状态不丢失，为水平扩展留出升级路径（切 PostgresSaver 只改一行）。

#### 动态 per-repo Collection（`context/rag_index.py`）

`get_repo_collection(owner, repo)` 按仓库动态创建独立 ChromaDB collection，彻底隔离不同仓库的向量数据，`retrieve_context` 从 issue_url 解析 owner/repo 后拿对应 collection 检索。

---

### 14. 路由与前处理加固

#### route_issue 内容信号优先

原来路由逻辑过度依赖 label，label 不准时（如 label=question 但 body 有完整 traceback）会路由错误。
改为**内容信号优先**：先看 body 有没有 traceback / 复现代码 / "预期 vs 实际" 对比，有则判 bug；label 只作中等权重参考；模糊时宁可判 bug（分析比不分析代价更小）。

#### detect_regression 节点

在 route → memory_retrieve 之间插入，只对 bug 类运行。LLM 判断 Issue 是否描述了"以前好用现在坏了"的回归 bug，标记 `is_regression` 供后续 git_history 工具使用。

#### classify_fix 节点

analyze_bug 之前，将 bug 细分为 `code_fix / config_fix / dependency_upgrade`，注入不同的提示：
- `dependency_upgrade` 提示 LLM 把 `affected_files` 写成 `pyproject.toml`
- `config_fix` 提示 LLM 关注配置文件路径

关键文件：`workflow/nodes/route.py`、`workflow/nodes/detect_regression.py`、`workflow/nodes/classify_fix.py`

---

### 15. Git History 工具

回归 bug 专用，两条策略：
- Issue 里有版本号（`v1.x.x` 格式）→ `fetch_recent_diff`：提取版本号附近的 commit，按相关性排序
- 没有版本号 → `fetch_blame_diff`：用 RAG 召回的文件路径 + 行号跑 `git blame`，再 `git show` 该 commit

结果作为 `--- [Git History] ---` 段注入 code_context。

关键文件：`tools/git_history.py`

---

### 16. Call Graph 展开

`context/call_graph.py` 用 `ast` 解析仓库所有 `.py` 文件，构建函数调用关系图，缓存到 `.repomind_callgraph.json`。bug 类 Issue 在 RAG 之后，提取 RAG 召回代码里的函数名，展开这些函数的被调函数，注入 `--- [Call Graph 展开] ---` 段。

关键文件：`context/call_graph.py`

---

### 17. LLM Judge 评估体系（`eval/`）

#### 数据集构建（`eval/dataset.py`）
搜索含 `closes #N` 的合并 PR，过滤纯 CI/docs PR，提取 Issue + Fix PR diff 作为 ground truth。

#### 4 维度自动打分（`eval/judge.py`）
用 LLM 对比 AI 分析结果与 ground truth，逐条打分：
- `root_cause_accuracy`：根因方向是否正确
- `file_accuracy`：受影响文件命中率
- `severity_fit`：严重程度是否合理
- `fix_actionability`：修复建议是否可执行

#### 断点续跑 + 汇总报告
- `eval/runner.py`：已评估的 issue 跳过，避免重复消耗 token
- `eval/report.py`：汇总各维度均分、分布、逐条明细

---

### 18. Import 驱动依赖索引（`context/dep_indexer.py`）

RAG 召回的每个文档在索引时都存储了该文件的 `imports` 字段（ChromaDB metadata）。`retrieve_from_imported_deps` 读取这些 imports，与 `pyproject.toml` 的依赖列表取交集，自动 clone + 索引相关依赖库，再对依赖库做 RAG 查询。

跨语言 dep 支持：识别到 `pydantic-core` 这类 Rust 库时，仍能从该 repo 的 `.rs` 文件中检索到相关代码。

关键文件：`context/dep_indexer.py`、`context/rag_index.py`（`imports` 存储）

---

### 19. Coding Agent 工具层（`tools/code_nav.py`）

原先 analyze_bug 只能被动使用 retrieve_context 给的代码片段；改造后 LLM 具备主动追踪代码的能力，通过 ReAct 循环自主决定下一步操作。

**工具箱（9 个）：**

```
代码导航：
  read_file(owner, repo, path, start_line, end_line)  → 读文件指定行
  grep_code(owner, repo, pattern)                      → 全库文本搜索
  list_dir(owner, repo, path)                          → 目录结构
  find_definition(owner, repo, symbol)                 → 精确跳到函数/类定义，返回签名+前20行
  find_callers(owner, repo, func_name)                 → 找函数所有调用处，向上追溯调用链

Git 时间轴：
  git_log_file(owner, repo, path, n)                   → 文件提交历史，找引入改动的 commit
  git_blame(owner, repo, path, start, end)             → 指定行最后是哪个 commit 改的
  search_commits(owner, repo, keyword)                 → 搜 commit message，找历史修复记录

依赖库：
  fetch_dependency(owner, repo, query)                 → RAG 查依赖库源码
```

**迭代历史：**
- v1（6轮）：基础 read_file + grep_code + list_dir，pydantic overall 0.71
- v2（12轮）：加 find_callers + find_definition，能追 4-6 层调用链
- v3（12轮）：加 git_log_file + git_blame + search_commits，支持回归 bug 溯源

**关键**：max_turns 从 6 升到 12 后，复杂仓库（celery/mypy）分数显著提升，原因是 6 轮不够完成深层调用链追踪，JSON 被截断。

关键文件：`tools/code_nav.py`、`tools/base.py`、`core/llm/client.py`（`chat_json_with_tools`）

---

### 20. Grep 优先 + LLM 入口识别（`workflow/nodes/retrieve.py`）

核心观察：RAG 靠语义相似度，不懂调用链；代码 bug 需要从入口函数出发，沿执行路径追踪。

改造 `retrieve_context`：
1. **LLM 识别入口**：`_llm_identify_entry_points` 读 Issue 标题和 body，让 LLM 输出用户触发 bug 时调用的函数/方法名（JSON 数组）
2. **grep 精确定位**：`_grep_entry_points` 搜索入口函数的定义位置，读取上下文代码
3. **RAG 补充**：入口代码放最前，RAG 取 top-2 补充；无入口时降级 RAG 取 top-3

LLM 能正确区分函数名（`model_rebuild`）、包名（`pydantic`）、英文词（`Exponential`），不像正则会把包名当符号搜索，避免 grep 返回 2000+ 噪声行。

---

### 21. Feature 分析流程（`detect_existing` + `analyze_feature` 升级）

**背景**：feature 和 question 路径原来只用 `chat_json`（单次无工具调用），分析质量远低于 bug 路径的 ReAct 循环。

#### detect_existing 节点（新增）

feature 路径在进入完整分析前，先判断用户想要的功能是否已存在。两种结果：
- 已存在 → 直接告知用法，跳过架构分析
- 不存在 → 进入完整 `analyze_feature`

**v1 实现（有缺陷）**：
LLM 提取功能关键词 → grep → LLM 判断"有代码 = 已存在"

问题：搜的是**主语**（功能名词），而不是**宾语**（缺失的那个东西）。
- "给 MemoryUsage 补测试" → 提取 `MemoryUsage` → grep 找到类定义 → 误判"已存在" ❌

**v2 实现（当前）**：
让 LLM 直接推理"如果这个功能已实现，codebase 里应该有什么具体证据"，并指定搜索路径：

```json
[
  {"what": "tests/ 里有 MemoryUsage 的测试文件", "term": "test_memusage", "path_hint": "tests/"},
  {"what": "get_retry_request 的 give_up_log_level 参数", "term": "give_up_log_level", "path_hint": ""}
]
```

grep 支持 `path_hint` 子路径过滤，判断时严格要求"找到核心实现证据"而非"找到相关代码"。

关键文件：`workflow/nodes/detect_existing.py`

#### analyze_feature 升级

从 `chat_json`（单次） → `chat_json_with_tools`（ReAct × 12 轮），system prompt 改为架构师视角：
1. 需求理解：提炼真实诉求
2. **横向探索**：用 `grep_code` 找类似已有功能，理解实现模式（区别于 bug 的纵向追踪）
3. 架构定位：`list_dir` / `read_file` 了解模块结构，判断新功能放哪里
4. 可行性评估：侵入性、breaking change 风险
5. 实现方案：具体文件路径和改动思路

已存在短路：若 `feature_exists=True`，走简化 prompt，直接解释用法，不进入 ReAct 循环。

新增 State 字段：`feature_exists: bool`、`existing_api: str`

**完整 graph feature 路径**：
```
route_issue(feature) → memory_retrieve → retrieve_context
  → detect_existing → analyze_feature(ReAct × 12) → reflect → generate_report
```

关键文件：`workflow/nodes/analyze.py`、`workflow/state.py`、`workflow/graph.py`

---

### 22. 测试体系（`tests/`）

四层测试，151 条，全部自动化：

| 层级 | 文件 | 内容 | 特点 |
|---|---|---|---|
| 工具单测 | `test_code_nav.py` | 36条，9工具函数覆盖 | 需要本地克隆仓库，无 LLM |
| LLM工具单测 | `test_llm_utils.py` | 34条，parse_json / AnalyzeResult / judge_question / question report | 纯函数，mock LLM，0 API 调用 |
| 节点测试 | `test_nodes.py` | 67条，所有 analyze/route/reflect 节点 | mock LLM，有真实仓库 grep 测试 |
| 鲁棒性测试 | `test_robustness.py` | 18条，边界输入、空路径、特殊字符 | 验证不崩溃 |
| 端到端回归 | `test_regression.py` | 8条 | 真实 API（`-m regression`） |

**端到端回归分两类**：
- `quality`（4条）：psf/requests / pydantic / fastapi / black，答案明确，分数必须 ≥ 阈值（0.75-0.85）
- `smoke`（4条）：celery / aiohttp / mypy / scrapy，链路复杂，只验结构合法，不卡分数

```bash
pytest tests/ --ignore=tests/test_regression.py  # 151条，~13s
pytest tests/test_regression.py -m regression    # 8条端到端（需 API key，~10min）
pytest tests/test_regression.py -m "regression and quality"  # 只跑质量门禁
```

---

### 23. Feature / Question Eval 基础设施

#### 数据集策略（`eval/dataset.py`）

新增 `fetch_feature_issues_with_prs`：
- 按 label 找 feature issue（enhancement / feature / feature request / Feature Request / improvement…）
- 通过 timeline + **search API 双策略**找关联合并 PR（timeline 靠 cross-referenced 事件；search API 搜 PR body 里提到 `#N` 的记录）

踩坑：原 `_find_fix_pr` 只用 timeline，scrapy 的 PR 用 "Resolves #N" 写法没触发 cross-referenced 事件，换成 search API 后解决。

#### Feature Judge（`eval/judge.py`）

`judge_feature` 新增 4 个维度（对应 bug 的 4 维完全不同）：
- `requirement_understanding`：是否理解了用户真实诉求
- `architecture_fit`：建议实现位置/模块是否与实际 PR 一致
- `implementation_feasibility`：方案方向是否与实际 PR 一致
- `exists_detection_accuracy`：detect_existing 是否做出正确判断

#### Question Eval（`eval/dataset.py` / `eval/judge.py`）

ground truth 是维护者/贡献者的权威回复（非 PR diff）：

```python
# dataset：找带 "question"/"help wanted" 等标签的已关闭 issue，提取贡献者回复
fetch_question_issues_with_answers(owner, repo, max_issues=20)
# → {"issue_url", "title", "body", "resolution_comments", "key_answer"}

# judge：3维度打分
judge_question(title, body, ai_result, key_answer, n=3)
# → confusion_identified / answer_accuracy / actionability / overall
```

`_extract_key_answers` 优先取贡献者/维护者回复，过滤 <30 字短评和 issue 作者自问自答，按正文长度降序取最多 3 条。

#### 评估入口（`eval/run_eval.py`）

新增 `--type bug/feature/question` 参数，结果文件命名加 type 前缀：
```bash
python eval/run_eval.py --type feature --owner scrapy --repo scrapy --max 10
python eval/run_eval.py --type question --owner pydantic --repo pydantic --max 10
```

`report.py` 按 `eval_type` 字段（或 scores 字段）自动识别 bug/feature/question 类型，输出对应格式报告。

---

## 完整节点流程（当前）

```
fetch_issue → build_project_map → describe_images → parse_stack_trace → route_issue
  ├─ bug      → detect_regression → memory_retrieve → retrieve_context
  │             → classify_fix → analyze_bug（ReAct × 12轮，9工具）
  ├─ feature  → memory_retrieve → retrieve_context
  │             → detect_existing → analyze_feature（ReAct × 12轮，9工具）
  ├─ question → memory_retrieve → retrieve_context → answer_question（ReAct × 12轮，9工具）
  └─ security → human_gate → memory_retrieve → retrieve_context → classify_fix → analyze_bug
  → reflect（置信度<0.7 最多重试2次）→ generate_report → memory_save
```

**`build_project_map`**：每次分析前，读仓库 2 层目录树 → LLM 生成 ~200 字摘要（核心源码/测试/文档在哪），注入所有分析节点的 prompt context，session 内 per-repo 缓存（~3s 首次，之后 0ms）。

---

## 评估结果

### Bug Eval — 多仓库（2026-06-28，v2，9 repos × 68 issues）

| 仓库 | Overall | root | file | sev | fix | n |
|---|---|---|---|---|---|---|
| fastapi/fastapi | **0.77** | 0.76 | 0.81 | 0.94 | 0.71 | 8 |
| python/mypy | 0.72 | 0.76 | 0.75 | 0.86 | 0.64 | 6 |
| psf/requests | 0.64 | 0.67 | 0.55 | 0.83 | 0.56 | 8 |
| celery/celery | 0.62 | 0.60 | 0.63 | 0.81 | 0.54 | 8 |
| psf/black | 0.58 | 0.58 | 0.50 | 0.86 | 0.48 | 6 |
| pydantic/pydantic | 0.51 | 0.50 | 0.48 | 0.79 | 0.45 | 8 |
| aio-libs/aiohttp | 0.49 | 0.55 | 0.44 | 0.69 | 0.45 | 8 |
| scrapy/scrapy | 0.44 | 0.40 | 0.48 | 0.69 | 0.44 | 8 |
| pallets/flask | 0.35 | 0.38 | 0.25 | 0.58 | 0.32 | 8 |
| **Macro avg** | **0.570** | 0.574 | 0.540 | 0.780 | 0.507 | 68 |

分布：优秀≥0.8: 21(31%) · 良好0.6-0.8: 17(25%) · 一般0.4-0.6: 9(13%) · 较差<0.4: 21(31%)

**v1→v2 提升（加 find_callers/find_definition/git工具 + max_turns 6→12）：**
- Macro avg: 0.521 → 0.570（+0.049）
- 最显著：mypy +0.22、celery +0.16、aiohttp +0.11
- 较差比例：40% → 31%

**较差的 21 条失败原因分类：**
- detect_existing 时态错位（9条）：eval 数据集天然限制，非系统问题
- 复杂运行时 bug（6条）：跨进程/异步时序，静态分析的结构性上限
- 设计决策/revert/test-only（6条）：LLM 默认"改代码"逻辑无法覆盖

### pydantic 迭代历史

| 方案 | Overall | 教训 |
|---|---|---|
| 纯 RAG 基线 | 0.64 | — |
| dep 无过滤 | 0.59 | 加 Rust 代码反而干扰 |
| CrossEncoder reranker 过滤 dep | 0.57 | 模型不懂代码，打负分 |
| 正则提取符号 grep | 0.57 | 把包名当符号，返回 2000+ 行噪声，#12718 崩溃 |
| prompt 限制 dep 文件 | 0.59 | 硬编码，有些 bug 真在 dep |
| coding agent 工具 | 0.71 | LLM 主动追调用链，首次突破基线 |
| grep 正则提取符号（重试） | 0.66 | 同上问题，#12718 从 0.97 回退到 0.32 |
| **LLM 入口识别 + coding agent** | **0.77** | 正确架构，无硬编码 |

**核心规律**：代码 bug（有调用链可追）得分 0.68-1.00；文档/配置/typo 类得分 0.10-0.35。

### Feature Eval — scrapy/scrapy（2026-06-27，6条）

| Issue | Overall | 需求理解 | 架构适配 | 可行性 | exists 检测 |
|---|---|---|---|---|---|
| #6958 Review _SettingsKeyT type | 0.78 | 0.80 | 0.67 | 0.80 | 0.97 |
| #4622 Retry give-up log level | 0.63 | 0.87 | 0.50 | 0.47 | 1.00 |
| #7252 httpx handler production-ready | 0.42 | 0.67 | 0.37 | 0.30 | 0.83 |
| #5297 get_retry_request log level | 0.23 | 0.83 | 0.00 | 0.17 | 0.00 |
| #4421 Implement static checks | 0.20 | 0.50 | 0.00 | 0.00 | 0.33 |
| #7002 Cover memusage with tests | 0.13 | 0.27 | 0.23 | 0.10 | 0.00 |

**平均 overall: 0.40**

**重要说明**：feature eval 存在**时序缺陷**——数据集要求 issue 有已合并 PR，意味着功能已在当前 codebase 实现。`detect_existing` 在当前代码上运行，正确找到实现并返回"已存在"，但 judge 对比原始 PR（新增了大量代码）打低分，判断为误判。这是 eval 流程设计问题，不是系统问题。真正有效的 feature eval 需要在 pre-merge 快照上运行。

### Question Eval — pydantic/pydantic（2026-06-28，5条，首次）

| Issue | Overall | 问题识别 | 技术准确 | 可操作 |
|---|---|---|---|---|
| #11023 PostgresDsn 主机为空 | 0.94 | 1.00 | 1.00 | 0.83 |
| #9557 SecretStr 类型错误 | 0.89 | 1.00 | 0.97 | 0.73 |
| #1223 Optional 字段 v1/v2 | 0.37 | 0.50 | 0.27 | 0.33 |
| #7581 model_copy extra=forbid | 0.27 | 0.43 | 0.20 | 0.27 |
| #8187 frozen model set 序列化 | 0.03 | 0.17 | 0.00 | 0.00 |

**平均 overall: 0.50**

**规律**：两个集群——
- **高分（0.89-0.94）**：答案是具体 API 用法或配置项，LLM 查到代码就能准确解释
- **低分（0.03-0.37）**：维护者的回答是"这是设计决策，不改"，LLM 找到代码位置后倾向于提议修改，而不是解释设计意图并给出变通方案

`answer_question` 节点已针对此问题优化 prompt（区分"使用问题"vs"设计决策问题"，使用独立的 `_JSON_FIELDS_QUESTION` 字段语义），待重测验证效果。

### 当前能力边界
- **擅长**：有具体函数调用的代码 bug，尤其跨语言（Python → Rust dep）；feature 需求理解准确（0.7+）
- **不擅长**：文档/typo/配置类 bug；大型跨模块架构重构的实现方向（#7252 httpx handler）
- **feature eval 受限**：时序问题导致 exists_detection_accuracy 失真，暂不作为参考维度

---

## 技术栈

| 技术 | 用途 |
|------|------|
| LangGraph | 工作流编排，条件路由，回环控制，SqliteSaver checkpointer |
| ChromaDB | 本地向量数据库，per-repo collection 隔离 + Memory collection |
| Sentence Transformers | 代码/文档向量化（all-MiniLM-L6-v2） |
| Python ast 模块 | Python 文件语义切片（按函数/类）|
| HyDE | 假设文档嵌入，改善代码检索匹配 |
| DeepSeek API | LLM（deepseek-chat）+ 视觉模型（deepseek-vl2） |
| LangGraph Send API | 动态并行分发，A2A 跨仓库子任务 |
| FastAPI + uvicorn | RepoAgent HTTP 服务化，Job Queue 非阻塞模式 |
| pydantic-settings | 统一配置入口，启动校验，SecretStr 脱敏 |
| httpx | GitHub REST API + Agent 间 HTTP 调用 |
| Python TypedDict | LangGraph State 定义 |
| Pydantic | LLM 输出 Schema 校验 + 字段容错 |
| tenacity | API 调用指数退避重试 |
| subprocess grep | 精确符号定位，优先于 RAG 语义搜索 |

---

## 踩坑记录

### feature eval 时序问题
**现象**：feature eval 中 `exists_detection_accuracy` 普遍低分。
**根因**：eval 数据集要求 issue 有合并 PR，意味着所有 feature 在当前 codebase 里已实现。`detect_existing` 在当前代码上跑，正确判断"已存在"，但 judge 用 PR（新增了大量代码）打低分，认为系统误判。
**教训**：feature eval 需要在 pre-merge 快照上运行才有意义；当前 eval 仅参考需求理解/架构适配/可行性三个维度，`exists_detection_accuracy` 暂弃用。

### detect_existing 搜"主语"而非"宾语"
**现象**：`#7002` "给 MemoryUsage 补测试" → 提取 `MemoryUsage` → grep 找到类定义 → 误判功能已存在。
**根因**：v1 提取的是"功能操作对象"（MemoryUsage 类），而不是"功能本身"（test_memusage 测试文件）。
**修法**：v2 让 LLM 推理"功能存在的证据是什么"（term + path_hint），精确搜索目标证据。

### eval `_find_fix_pr` timeline 策略失效
**现象**：scrapy feature eval 找到 0 条 issue。
**根因**：`_find_fix_pr` 依赖 timeline `cross-referenced` 事件，但 scrapy 的 PR 用 "Resolves #N" 写法，GitHub 没有把它记录为 cross-referenced 事件。
**修法**：加 search API 作为主策略（`repo:owner/repo is:pr is:merged #{N} in:body`），timeline 作兜底。

### 各仓库 feature label 命名不统一
**现象**：pydantic feature eval 返回 0 条 issue。
**根因**：代码写死了 `["enhancement", "feature", "new feature"]`，pydantic 用 "feature request"，requests 用 "Feature Request"。
**修法**：扩充 label 列表，覆盖 8 种常见写法。

### main.py issue URL 硬编码
**现象**：`python main.py https://github.com/scrapy/scrapy/issues/5297` 实际跑的是 fastapi #1234。
**修法**：改为 `sys.argv` 读取，保留 fastapi #1234 作默认值。

### shallow clone 导致 git 工具失效
**现象**：`git_log_file` 只返回 1 条 commit，`search_commits` 返回空。
**根因**：`_clone_repo` 用 `--depth=1`，本地仓库没有历史记录，git blame/log 全部失效。
**修法**：改为 `--depth=200`，并对现有 40 个已克隆仓库执行 `git fetch --deepen=200` 批量加深。

### LLM 忽略提示词里的"软建议"
**现象**：系统提示加了"先花 1-2 轮判断 fix 性质"，但 LLM 遇到有函数名的 issue 直接追代码，跳过 git 诊断。
**根因**：提示词只是建议，LLM 根据 issue 内容自行判断"性质显而易见"而跳过步骤。执行顺序应通过图结构强制，而非提示词软指令。
**教训**：想强制 LLM 做某个步骤，必须把它做成独立节点，而不是写进 system prompt。

---

## 待实现

- [x] Memory：ChromaDB issue_memory collection，历史模式检索与沉淀
- [x] InMemorySaver / SqliteSaver：LangGraph checkpointer
- [x] HITL：`interrupt()` + `Command(resume)` + `--security` 模拟参数
- [x] A2A 跨仓库：Send API 并行 RepoAgent + operator.add reducer + OrchestratorAgent
- [x] LLM Judge 评估：4 维度自动打分 + 断点续跑
- [x] Coding agent：ReAct 工具循环（read_file / grep_code / list_dir）
- [x] Import 驱动依赖索引：ChromaDB imports metadata 自动 clone dep
- [x] Grep 优先：LLM 识别入口函数 → grep 定位 → RAG 补充
- [x] Feature 分析流程：detect_existing + analyze_feature ReAct 升级
- [x] Feature Eval：dataset / judge / runner / report 全套
- [x] answer_question 升级为 ReAct 工具循环（同 analyze_bug）
- [ ] FastAPI + GitHub Webhook：Issue 创建自动触发
- [ ] Feature eval pre-merge 快照：checkout 到 PR 合并前 commit 再跑分析
- [x] 项目地图：`build_project_map` 节点，目录树 2 层 → LLM 摘要 → 注入所有分析节点 context，session 内 per-repo 缓存

---

## 快速开始

```bash
# 1. 建虚拟环境
python3.11 -m venv .venv && source .venv/bin/activate

# 2. 装依赖
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env  # 填入 DEEPSEEK_API_KEY 和 GITHUB_TOKEN

# 4. 克隆目标仓库并建索引
git clone https://github.com/fastapi/fastapi /tmp/fastapi
python context/rag_index.py /tmp/fastapi

# 5. 运行
python main.py
```
