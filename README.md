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

### 1. ReAct 工具循环（Coding Agent）

9 个工具构成主动代码追踪能力，LLM 自主决定下一步操作：

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

### 2. MCP 工具规范

所有工具遵循 **MCP 协议规范**，通过 JSON Schema 定义参数和返回值，统一注册到 `ToolRegistry`：

```python
# tools/base.py — Tool 定义
Tool(name="read_file", description="...", parameters={
    "type": "object",
    "properties": {"owner": ..., "repo": ..., "path": ..., "start_line": ..., "end_line": ...},
    "required": ["owner", "repo", "path"],
})

# build_tools() — 将工具列表转换为 OpenAI function calling 格式
tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR, ...)
```

等价于 MCP 的 `get_file_contents / search_code / list_directory / get_issue`，GitHub REST API 作为 transport。

### 3. Plan-Execute 模式

bug 路径在 ReAct 执行前先完成两步**规划**：

```
detect_regression  →  classify_fix  →  analyze_bug (执行)
  （是否回归？）     （code_fix /        （ReAct 追踪）
                     dependency_upgrade /
                     config_fix）
```

`classify_fix` 根据判断结果注入差异化提示——`dependency_upgrade` 时告知 LLM 把 `affected_files` 写成 `pyproject.toml`，`config_fix` 时关注配置文件路径。规划与执行分离，避免 ReAct 阶段在"这是什么类型的问题"上浪费轮次。

### 4. Reflection / Critic-Actor

`reflect` 是独立的批评者 LLM 实例，不共享 analyze 节点的上下文，驱动 Agent 回头再探：

```python
# reflect 节点 system prompt
"你是严格的代码评审专家，质疑分析结论：证据够吗？还有其他可能原因吗？
 给出 0-1 置信度分，低于 0.7 时说明哪里不足，促使重新分析。"
```

置信度 < 0.7 → 回到对应 analyze 节点重试，最多 2 次，`iteration` 字段防死循环。

### 5. RAG 检索管线

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

### 6. Grep 优先 + LLM 入口识别（ContextBuilder）

核心观察：RAG 靠语义相似度，不懂调用链。`retrieve_context` 节点三步组装上下文：

1. **LLM 识别入口**：读 Issue 标题和 body，输出用户触发问题时调用的函数名（JSON 数组，能区分函数名/包名/英文词）
2. **grep 精确定位**：搜索入口函数定义，读取上下文代码，**入口代码放首位**
3. **RAG 补充**：取 top-2 补充语义相关片段；无入口时降级 RAG top-3

`_context_sections()` 负责最终组装，按 `项目地图 → 代码片段 → 历史记忆 → stack trace → 关联 PR` 顺序排列，控制注入 prompt 的 context 预算。

### 7. Import 驱动依赖索引

RAG 索引时存储每个文件的 `imports` metadata。分析时读取 imports，与 `pyproject.toml` 依赖取交集，自动 clone + 索引依赖库，再对依赖库做 RAG——跨语言（Python → Rust pydantic-core）也能追踪。

### 8. 多模态（Vision 模型提取截图信息）

Issue 里的错误截图比 PR diff 更常见，是 GitHub Issue 分析的刚需：

```python
# describe_images 节点：提取图片 → base64 → 调 DeepSeek-VL2
data_url = f"data:{mime};base64,{b64}"
result = vision_client.chat.completions.create(
    model="deepseek-vl2",
    messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_url}},
        {"type": "text", "text": "描述图片内容，重点关注错误信息、UI 异常、堆栈信息..."},
    ]}],
)
```

图片描述结果注入三个 analyze 节点的 prompt，UI Bug 截图和错误截图中的堆栈信息不再丢失。

### 9. 共享状态/黑板（IssueState）

所有节点读写同一个 `IssueState` TypedDict，LangGraph 确保节点间状态传递是原子的：

```python
class IssueState(TypedDict):
    issue_url: str          # 输入
    issue_type: Literal["bug", "feature", "question", "security"]
    project_map: str        # build_project_map 填入
    is_regression: bool     # detect_regression 填入
    fix_type: str           # classify_fix 填入
    root_cause: str         # analyze_* 填入
    confidence: float       # reflect 填入
    final_report: str       # generate_report 填入
    # ... 共 25 个字段
```

每个节点只负责更新自己的字段，返回 `dict` 局部更新，不覆盖其他节点的结果。

### 10. Checkpointing / 断点续跑

```python
# SqliteSaver：checkpoint 持久化到本地 SQLite
conn = sqlite3.connect("repo_knowledge_base/checkpoints.sqlite")
app = graph.compile(checkpointer=SqliteSaver(conn))

# 每次调用必须携带 thread_id
config = {"configurable": {"thread_id": str(uuid.uuid4())}}
result = app.invoke({...}, config=config)
```

进程重启后 HITL 暂停状态不丢失；eval runner 也利用 checkpoint 实现断点续跑（跳过已评估的 issue）。

### 11. HITL（Human-in-the-Loop）

```python
# human_gate 节点
decision = interrupt({"message": "⚠️ Security Issue 需要人工确认", ...})

# main.py：检测到 interrupt，等待输入后用同一 thread_id 恢复
result = app.invoke(Command(resume=decision), config=config)
```

`interrupt()` 是断点不是结束，`Command(resume)` 从 checkpointer 恢复，`human_gate` 函数从 `interrupt()` 那行继续，不重跑整个 graph。

### 12. 层级编排（3 层）

```
main.py（用户入口）
  └─ OrchestratorAgent（识别跨仓库依赖，分发子任务）
       └─ RepoAgent × N（各自独立 graph，clone + index + 分析）
```

- **OrchestratorAgent**：LLM 判断根因可能跨哪些仓库 → `Send API` 动态并行分发
- **RepoAgent**：独立 FastAPI 服务，Job Queue 非阻塞，`GET /jobs/{id}` 轮询结果
- **reducer 聚合**：`Annotated[list[dict], operator.add]`，并行子节点结果自动追加不覆盖

### 13. A2A + ANP 协议

```python
# A2A：Agent 间通过 HTTP 通信
httpx.post(f"{repo_agent_url}/analyze", json={"issue_url": ...})

# ANP：地址自知，无需服务发现
# OrchestratorAgent 直接持有 RepoAgent 的 URL，无中间注册表
REPO_AGENT_URLS = {
    "fastapi/fastapi": "http://localhost:8001",
    "encode/starlette": "http://localhost:8002",
}
```

3 个 A2AServer（TriageAgent / CodeSearchAgent / FixSuggestionAgent）是真实 HTTP 服务，可独立部署和横向扩展。

### 14. 角色专业化

每个分析节点有独立的 system prompt 定义角色，防止角色混淆：

| 节点 | 角色 |
|---|---|
| `analyze_bug` | 资深后端工程师，10 年源码阅读经验，擅长调用链追踪 |
| `analyze_feature` | 资深软件架构师，评估可行性、侵入性、breaking change 风险 |
| `answer_question` | 技术文档工程师，区分"使用问题"vs"设计决策"，不建议修改库代码 |
| `reflect` | 严格的代码评审专家，独立评估，质疑证据充分性 |
| `route_issue` | GitHub Issue 分类专家，内容信号优先于 label |

### 15. LLM 输出三层防御（Guardrails）

```
response_format=json_object  →  Pydantic Schema 校验  →  修复 LLM 兜底
（语法保证）                      （类型/枚举/必填）        （校验失败重修复，仍失败降级）
```

`severity` 枚举值容错、`root_cause` 空字符串拦截、API 超时指数退避重试（tenacity 3次，2s→4s→8s）。

### 16. MemoryTool（跨 Issue 经验积累）

```python
# memory_save：分析完成后沉淀经验
content = f"仓库: fastapi/fastapi | 类型: bug | Issue #1234\n根因: {root_cause}\n修复: {fix_suggestion}"
_memory_collection.add(documents=[content], metadatas=[{"repo": "fastapi/fastapi"}])

# memory_retrieve：分析前检索同仓库历史模式
results = _memory_collection.query(
    query_texts=[f"{issue_type}: {title}"],
    where={"repo": f"{owner}/{repo}"},   # 只查当前仓库
    n_results=5,
)
```

记忆模式如"auth 模块经常出现权限问题"，为新 Issue 分析提供先验。

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

### LLM Judge 评估体系（多路打分）

```
AI 分析结果
    │
    ├─ Bug Judge   → root_cause_accuracy / file_accuracy / severity_fit / fix_actionability
    ├─ Feature Judge → requirement_understanding / architecture_fit / implementation_feasibility / exists_detection_accuracy
    └─ Question Judge → confusion_identified / answer_accuracy / actionability
```

每个维度 LLM 打分 n=3 次取均值，减少单次方差。ground truth 来源：Bug/Feature 用合并 PR diff，Question 用维护者权威回复。

### Question Eval（pydantic × 5 issues，首次）

macro avg **0.50**，规律：API 用法问题高分（0.89-0.94），设计决策问题低分（LLM 倾向于"修代码"而非"解释意图"）

---

## 测试（151 条，4 层）

| 层级 | 文件 | 内容 |
|---|---|---|
| 工具单测 | `test_code_nav.py` | 36 条，9 个工具函数，需本地克隆仓库，无 LLM |
| LLM工具单测 | `test_llm_utils.py` | 34 条，parse_json / Guardrails / Judge / Report，mock LLM |
| 节点测试 | `test_nodes.py` | 67 条，所有节点函数，mock LLM |
| 鲁棒性测试 | `test_robustness.py` | 18 条，空路径、特殊字符、边界输入 |
| 端到端回归 | `test_regression.py` | 8 条，4 quality（分数门禁）+ 4 smoke（结构验证） |

```bash
pytest tests/ --ignore=tests/test_regression.py   # 151 条，~13s
pytest tests/test_regression.py -m "regression and quality"  # 端到端质量门禁
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
  state.py        # IssueState TypedDict（共享状态/黑板）

tools/
  code_nav.py     # 9个代码导航工具（MCP 规范）
  git_history.py  # git log/blame/search
  stack_trace.py  # traceback 解析

context/
  rag_index.py    # 代码向量化索引
  repo_loader.py  # 自动 clone + index
  dep_indexer.py  # import 驱动依赖索引
  memory_store.py # Issue 经验记忆（MemoryTool）

eval/
  dataset.py      # Bug/Feature/Question 数据集构建
  judge.py        # LLM Judge 多维度打分
  runner.py       # 评估运行器（断点续跑）
  report.py       # 汇总报告生成

agents/
  repo_agent_server.py   # RepoAgent FastAPI 服务（A2A）
  orchestrator/          # OrchestratorAgent（层级编排）

tests/                   # 151条测试（4层）
```
