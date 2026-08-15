# RepoMind 详细设计与完整流程文档

> 版本依据：当前仓库代码。本文同时面向项目维护、课程答辩和技术面试；“当前实现”和“建议改进”会明确区分。

## 1. 项目定位

RepoMind 是一个以 GitHub URL 为入口的代码分析 Agent 系统，包含两条主业务链路：

- 输入 Issue URL，输出问题分类、根因、影响文件、严重程度和修复方案。
- 输入 Pull Request URL，从安全、逻辑、风格三个视角审查代码变更并给出合并建议。

系统不把整个仓库直接发送给 LLM，而是先定位资源、拉取 GitHub 数据、clone 和索引仓库、收集候选证据，再允许 LLM 通过工具继续探索。确定性的流程控制由 LangGraph 完成，需要语义理解的环节由 LLM 完成。

## 2. 技术栈

| 技术 | 用途 |
|---|---|
| Python | 主要开发语言 |
| LangGraph | 状态机、条件路由、并行 Send、循环、HITL、checkpoint |
| DeepSeek/OpenAI-compatible API | 分类、分析、Reflection、视觉理解和 Judge |
| ChromaDB | 仓库代码向量索引和长期 Memory |
| Sentence Transformers | `all-MiniLM-L6-v2` embedding |
| CrossEncoder | `ms-marco-MiniLM-L-6-v2` 精排 |
| Python AST | Python 语义切片和轻量调用图 |
| FastAPI | Web Job API 与 RepoAgent 服务 |
| SQLite | LangGraph checkpoint |
| Pydantic | 配置和 LLM 输出校验 |
| httpx | GitHub API 和 Agent 间 HTTP 调用 |
| tenacity | LLM 网络错误指数退避 |

## 3. 目录结构

```text
RepoMind/
├── main.py                    # CLI 统一入口，区分 Issue/PR
├── config.py                  # 环境变量和路径配置
├── workflow/
│   ├── graph.py               # Issue StateGraph
│   ├── pr_graph.py            # PR StateGraph
│   ├── state.py               # IssueState
│   ├── pr_state.py            # PRState
│   └── nodes/                 # 抓取、分类、检索、分析、反思、报告等节点
├── context/                   # clone、切片、索引、Memory、调用图、依赖索引
├── tools/                     # GitHub、代码导航、Git 历史、堆栈工具
├── core/llm/                  # LLM、ReAct、JSON/Pydantic Guardrails
├── backend/                   # 主 Web API
├── frontend/                  # 静态前端
├── agents/                    # 跨仓库 Orchestrator 和 RepoAgent
├── eval/                      # 数据集、Judge、Runner、报告
└── tests/                     # 单元、节点、鲁棒性和回归测试
```

## 4. 从 URL 开始的总流程

```text
用户输入 GitHub URL
        │
        ▼
main.py 用正则判断 URL 路径
        │
        ├── /issues/<number> ──> Issue Graph
        │
        └── /pull/<number>   ──> PR Graph
```

URL 本身只提供 `owner`、`repo`、资源类型和编号。意图不是从 URL 提取，而是在调用 GitHub API 得到标题、正文和 labels 后由 LLM 识别。

每次 CLI 执行生成 UUID 作为 LangGraph `thread_id`。它既隔离不同任务，也用于 checkpoint 和中断恢复。

## 5. Issue 工作流

### 5.1 完整流程图

```text
START
  ↓
fetch_issue
  ↓
build_project_map
  ↓
describe_images
  ↓
parse_stack_trace
  ↓
route_issue
  ├── bug      → detect_regression ──────────┐
  ├── feature  ──────────────────────────────┤
  ├── question ──────────────────────────────┤
  └── security → human_gate ─────────────────┤
                                               ↓
                                        memory_retrieve
                                               ↓
                                        prepare_context
                                               ↓
                         ┌─────────────────────┼────────────────────┐
                         ↓                     ↓                    ↓
                    entry gather          RAG gather           Git gather
                         └─────────────────────┼────────────────────┘
                                               ↓
                                         merge_context
                         ┌─────────────────────┼────────────────────┐
                         ↓                     ↓                    ↓
                   bug/security            feature              question
                   classify_fix       detect_existing       answer_question
                         ↓                     ↓
                   analyze_bug         analyze_feature
                         └─────────────────────┼────────────────────┘
                                               ↓
                                            reflect
                            confidence < 0.7 且 iteration < 2 ?
                                  ├── 是：返回对应 analyze
                                  └── 否：generate_report
                                               ↓
                                          memory_save
                                               ↓
                                              END
```

### 5.2 fetch_issue：获取 GitHub 数据

`parse_issue_url` 用正则提取 owner、repo、number，然后请求 GitHub REST API。节点获取：

- Issue number、标题、完整正文和 labels；
- 所有评论，每页 100 条并持续翻页；
- timeline 中 cross-reference 的 PR；
- 通过 commit 关闭 Issue 的事件。

404、403、429 和 timeout 会转换成更清楚的业务异常。GitHub Token 通过环境变量读取。

### 5.3 build_project_map：项目地图

节点先保证仓库已索引，再读取两层目录树：每层最多查看 50 个 entry，总体传给 LLM 前最多保留 100 行。LLM 将其总结成约 150～250 字的项目地图，描述源码、测试、文档和配置的位置。同一进程中的同一仓库使用内存缓存。

项目地图用于给 Agent 全局视角，减少一开始反复调用 `list_dir`。

### 5.4 图片与堆栈

`describe_images` 从 Issue 正文中发现图片后，下载并转换为 base64 data URL，由视觉模型提取报错文字、UI 异常或图表信息。

`parse_stack_trace` 支持 Python 和 JavaScript 风格的堆栈帧。若能在本地仓库匹配文件，就读取报错行附近源码，形成优先级较高的定位证据。

### 5.5 route_issue：意图识别

LLM 输入：

- Title；
- Labels；
- Body 前 600 个字符。

LLM 只能在四类中选择：

| 类型 | 判定信号 | 后续策略 |
|---|---|---|
| bug | traceback、复现、expected/actual、以前可用 | 回归检测、Bug 根因分析 |
| feature | 新能力或增强请求 | 已存在能力检测、架构分析 |
| question | 纯 how-to/why 且无错误 | 文档偏重检索、技术回答 |
| security | 漏洞与安全风险 | 先人工审批 |

正文信号优先于 labels；输出不在白名单时默认 `bug`。`FORCE_ISSUE_TYPE` 可用于测试指定分支。

### 5.6 Bug 的前置规划
确认是 Bug
   ↓
detect_regression：是不是回归 Bug？
   ↓
收集代码上下文 - memory_retrieve
                      ↓
                  prepare_context
                      ↓
                  entry/rag/git 收集
                      ↓
                  merge_context
                      
                  
   ↓
classify_fix：大概率需要哪种修复？
   ↓
analyze_bug
正式分析：Bug 为什么发生、发生在哪里、应该怎么修
        ↓
reflect
检查：根因证据是否充分
        ↓
generate_report
输出最终报告

`detect_regression` 使用标题和正文前 800 字符判断是否满足“升级后出现、以前可用、worked before、regression”等信号。输出布尔值，解析失败降级为 `False`。
// 800- 为了控制 Prompt 长度和调用成本。回归信号一般出现在 Issue 开头，例如版本号、升级说明、复现描述。
`classify_fix` 使用标题和正文前 600 字符分为：


- `code_fix`：修改源码逻辑，默认类别；
表示项目自身的源码逻辑有问题，需要修改代码。
表示项目自身代码可能没问题，问题来自第三方依赖版本。
表示代码本身可能正确，但配置、环境变量或项目设置错误。
- `dependency_upgrade`：升级或降级第三方依赖；
- `config_fix`：修改配置、环境变量或项目设置。

类型会改变 `analyze_bug` 的提示重点，但不会替代后续证据分析。
如果 is_regression=True，上下文收集阶段会额外查 Git 历史：
普通 Bug：
入口代码 + RAG + 调用图 + 依赖源码

回归 Bug：
入口代码 + RAG + 调用图 + 依赖源码
                             + Git log
                             + git blame
                             + commit diff

analyze_bug 如何分析成因
analyze_bug 会收到前面收集的全部证据：
Issue 标题、正文、评论
项目目录地图
入口函数源码
RAG 相关源码 top-3
依赖源码 top-3
调用图补充函数
Git log、blame、commit diff
Stack Trace
历史相似 Issue
关联 PR
修复类型提示
然后使用 ReAct 工具继续调查，最多 12 轮。
例如：
LLM 阅读入口函数
    ↓
发现 generate_unique_id_function 参数
    ↓
调用 find_callers 查谁调用该函数
    ↓
调用 read_file 查看 include_router 完整实现
    ↓
发现父路由器没有把参数传给子路由器
    ↓
调用 git_blame 查看这行何时引入
    ↓
确定根因及受影响文件
最后输出：
{
  "root_cause": "include_router 在注册子路由时没有传递 generate_unique_id_function，导致子路由退回默认 ID 生成逻辑",
  "affected_files": [
    "fastapi/routing.py"
  ],
  "severity": "medium",
  "fix_suggestion": "在 include_router 调用中显式传递该参数",
  "code_changes": [
    {
      "file": "fastapi/routing.py",
      "line": 0,
      "change": "向子路由注册逻辑传递 generate_unique_id_function"
    }
  ]
}
之后 reflect 会审查这个成因：
有没有代码证据？
文件是否定位正确？
有没有遗漏其他原因？
修复方案是否可执行？
严重程度是否合理？
如果可信度低于 0.7，就返回 analyze_bug 重新分析，最多重试两次。
所以准确流程是：
detect_regression 和 classify_fix 只是正式分析前的辅助判断；真正的成因分析发生在 analyze_bug，并由 reflect 对分析结果进行复核。

### 5.7 Feature 与 Question

Feature 在分析前使用 grep 检查请求能力是否已经存在。已存在时短路为用法说明，不再假设必须修改源码；不存在时从架构、实现位置、兼容性和测试角度给出方案。

Question 使用更高的文档检索权重，回答重点是正确用法或设计原因，而不是默认建议改库代码。

Feature 和 Question 在前半段共用同一套上下文收集流程，但在检索权重、专用节点、Prompt、工具和最终输出语义上不同。

```text
route_issue
   ├── feature
   │      ↓
   │   memory_retrieve
   │      ↓
   │   entry / RAG / git 三路收集
   │      ↓
   │   merge_context
   │      ↓
   │   detect_existing
   │      ├── 已存在 → 直接说明用法
   │      └── 不存在 → ReAct 探索实现方案
   │
   └── question
          ↓
       memory_retrieve
          ↓
       entry / RAG / git 三路收集
          ↓
       merge_context
          ↓
       answer_question
```

其中 Feature 和 Question 都会派发 Git 分支，但因为它们的 `is_regression=False`，Git 分支直接返回空内容。

# 一、Feature 详细实现

Feature 路径的核心问题是：

> 用户要求增加的功能，代码库里真的不存在吗？

所以在正式分析前加入了 `detect_existing`。

## 1. 先收集通用上下文

Feature 也会经过 Entry 和 RAG。

Feature 的 RAG 文件类型权重是：

```python
"feature": {
    "source": 1.0,
    "test": 0.8,
    "doc": 0.6,
}
```

意味着：

- 源码权重最高；
- 测试代码其次；
- 文档再次。

因为分析新功能时，既需要找到核心实现，也需要查看相似功能的测试方式。

RAG 仍然执行：

```text
最多 4 路查询，每路 top-5
    ↓
RRF 融合 top-10
    ↓
文件类型加权
    ↓
CrossEncoder 精排 top-3
```

Feature 不会执行调用图扩展，因为当前代码只对 Bug 执行：

```python
if state.get("issue_type") == "bug":
    call_graph_context = _expand_call_graph(...)
```

但 Feature 后面的 ReAct 可以通过 `find_callers` 自己查看调用关系。

## 2. detect_existing 第一步：推理“存在的证据”

系统没有直接拿 Feature 标题全文进行 grep，因为这样容易找到“相关对象”，却不能证明功能已经存在。

例如用户请求：

```text
给 MemoryUsage 增加测试
```

如果直接搜索：

```text
MemoryUsage
```

肯定可以找到这个类，但只能证明类存在，不能证明测试已经存在。

所以系统先让 LLM 推理：

> 如果这个功能已经完整实现，代码库中应该出现什么具体证据？

要求输出 1～3 个搜索目标：

```json
[
  {
    "what": "MemoryUsage 的测试函数",
    "term": "test_memory_usage",
    "path_hint": "tests/"
  }
]
```

又比如 Feature 是：

```text
允许用户修改 give-up 日志等级
```

LLM 可能返回：

```json
[
  {
    "what": "控制 give-up 日志等级的参数",
    "term": "give_up_log_level",
    "path_hint": ""
  }
]
```

这一步使用：

- Feature 标题；
- 正文前 600 字符；
- 最大输出 300 tokens。

如果 LLM 没有返回有效搜索目标，系统直接认为：

```python
feature_exists = False
existing_api = ""
```

注意：这不是确定功能不存在，而是没有找到足够证据，按照“不存在”继续分析。

## 3. detect_existing 第二步：精确 grep

系统最多取前三个搜索目标：

```python
for target in targets[:3]:
```

每个目标包含：

```text
term：具体搜索词
path_hint：建议搜索目录
```

执行系统 grep：

```bash
grep -rn \
  --include=*.py \
  --include=*.rs \
  --include=*.ts \
  --include=*.go \
  -m 20 \
  <term> <path>
```

支持的语言是：

- Python
- Rust
- TypeScript
- Go

每次搜索：

- 最多返回 20 个命中；
- 最终文本最多保留 2000 字符；
- 如果指定目录不存在，则退回整个仓库；
- 超时时间为 15 秒。

系统将每个搜索结果整理为：

```text
[目标] MemoryUsage 的测试函数
[搜索] term='test_memory_usage' path='tests/'
[结果] 找到
tests/test_memory.py:120:def test_memory_usage():
```

这里的“找到”暂时只是 grep 命中，不等于最终认定功能存在。

## 4. detect_existing 第三步：基于证据判断

然后将搜索结果再次交给 LLM，要求严格判断：

```text
只有找到功能核心实现的直接证据，才算已存在。
找到相关但不同的代码，不算已存在。
只找到部分实现，也算未实现。
```

例如：

```text
Feature：给 MemoryUsage 增加测试
搜索结果：只找到了 MemoryUsage 类，没有测试函数
```

应该输出：

```json
{
  "exists": false,
  "usage": ""
}
```

如果确实找到了完整实现：

```json
{
  "exists": true,
  "usage": "可以通过 give_up_log_level 参数设置日志等级"
}
```

结果写入 State：

```python
{
    "feature_exists": True,
    "existing_api": "可以通过 give_up_log_level 参数设置日志等级"
}
```

这形成了一个两阶段 LLM 流程：

```text
第一次 LLM：
如果功能存在，应该搜索什么？
        ↓
代码执行 grep
        ↓
第二次 LLM：
这些 grep 结果能否证明功能已经存在？
```

比“直接问 LLM 功能存不存在”更可靠，因为第二次判断能够看到仓库证据。

## 5. 功能已经存在：短路处理

如果：

```python
feature_exists is True
and existing_api 不为空
```

`analyze_feature` 不再进入 ReAct 工具循环，而是调用一次普通的结构化 LLM：

```python
chat_json(_SYSTEM_FEATURE_EXISTS, prompt)
```

Prompt 包含：

- Feature 标题；
- 正文前 400 字符；
- `existing_api`；
- 项目地图；
- 已收集的代码上下文；
- Memory、堆栈、关联 PR、图片描述。

模型角色变成“社区维护者”，目标是：

```text
告诉用户功能已经存在
    ↓
说明正确使用方式
    ↓
指出相关源码位置
```

例如输出：

```json
{
  "root_cause": "该需求对应的能力已经由 give_up_log_level 参数提供",
  "affected_files": ["scrapy/core/downloader.py"],
  "severity": "low",
  "fix_suggestion": "创建处理器时传入 give_up_log_level=logging.DEBUG",
  "code_changes": [
    {
      "file": "scrapy/core/downloader.py",
      "line": 0,
      "change": "Handler(give_up_log_level=logging.DEBUG)"
    }
  ],
  "suggested_labels": ["documentation", "usage"]
}
```

这里的“短路”是指：

> 不再执行最多 12 轮的 Feature ReAct 探索，而是利用已有证据直接生成用法说明。

它并不是跳过整个工作流。生成结果后仍会经过：

```text
reflect → generate_report → memory_save
```

## 6. 功能不存在：分析实现方案

如果：

```python
feature_exists = False
```

则进入完整的 `analyze_feature` ReAct 分析。

它可以使用六个工具：

- `read_file`
- `grep_code`
- `list_dir`
- `find_callers`
- `find_definition`
- `fetch_dependency`

最多 12 轮。

系统要求 Agent 按下面的思路分析：

```text
1. 理解真实需求
2. 搜索最相似的已有功能
3. 阅读相似功能完整实现
4. 查看相似功能的调用者
5. 确定新功能接入位置
6. 分析兼容性和 breaking change 风险
7. 给出实现文件、函数和测试建议
```

例如用户请求：

```text
支持自定义重试回调
```

Agent 可能这样探索：

```text
grep_code("retry")
    ↓
找到现有 RetryPolicy
    ↓
find_definition("RetryPolicy")
    ↓
read_file 读取完整实现
    ↓
find_callers("RetryPolicy")
    ↓
确定创建位置和调用方
    ↓
grep_code("test_retry")
    ↓
找到现有测试模式
    ↓
生成实现建议
```

最终的字段语义是：

- `root_cause`：当前实现为什么不满足需求；
- `affected_files`：需要修改或新增哪些文件；
- `severity`：需求缺失对用户的影响；
- `fix_suggestion`：具体实现路径；
- `code_changes`：建议修改；
- `suggested_labels`：如 `enhancement`、`good first issue`。

# 二、Question 详细实现

Question 没有 `detect_existing`，因为它的目标不是判断“功能存不存在”，而是：

> 理解用户为什么困惑，并给出现有框架中的正确答案。

## 1. RAG 如何偏向文档

Question 的文件类型权重是：

```python
"question": {
    "source": 0.7,
    "test": 0.5,
    "doc": 1.2,
}
```

假设 RRF 后得到：

```text
排名 1：source
排名 2：doc
排名 3：test
```

代码按照下面的公式重新评分：

```python
score = (1 / rank) * file_type_weight
```

大致得到：

```text
source：1 / 1 × 0.7 = 0.7
doc：   1 / 2 × 1.2 = 0.6
test：  1 / 3 × 0.5 = 0.167
```

文档即使原始排名稍低，也可能被提升。

不过它不是“只搜索文档”。Question 仍然检索：

- 文档；
- 源码；
- 测试。

因为回答 API 用法时，文档适合说明使用方式，源码适合确认真实行为，测试适合提供可运行示例。

精排后最终仍保留 top-3。

## 2. Question 的 Entry 检索

系统同样先让 LLM 从 Issue 中识别 1～3 个 API 或函数名，再使用 grep 定位定义。

例如：

```text
How do I use model_validate_strings?
```

提取：

```json
["model_validate_strings"]
```

然后读取该函数附近约 60 行代码。

所以 Question 的上下文可能是：

```text
入口 API 定义
+ 文档 RAG
+ 源码 RAG
+ 测试示例
+ 依赖源码
```

Question 不补充调用图，也不查询 Git History。

## 3. answer_question 的角色约束

`answer_question` 使用“技术文档工程师兼社区维护者”角色，Prompt 明确要求：

```text
目标是解释 API 的实际行为，
帮助用户在现有框架内正确使用，
而不是建议修改库代码。
```

它先区分两种问题。

### 使用问题

例如：

```text
How do I validate JSON strings?
```

回答重点：

- 应该调用哪个 API；
- 参数如何传递；
- 提供可运行示例；
- 指出常见误用。

### 设计决策问题

例如：

```text
Why doesn't FastAPI support this behavior?
```

回答重点：

- 框架为什么这样设计；
- 当前限制是什么；
- 是否有兼容性或安全原因；
- 现有框架下有什么替代方案。

除非维护者明确接受 PR，否则不应直接建议用户修改框架源码。

## 4. Question 的 ReAct 调查过程

Question 可以使用与 Feature 相同的六个工具，最多 12 轮：

```text
find_definition
    ↓
找到用户提到的 API 定义
    ↓
read_file
    ↓
确认参数、默认值和实际行为
    ↓
grep_code，在 docs/ 或 tests/ 搜使用示例
    ↓
find_callers
    ↓
理解框架内部如何调用这个 API
    ↓
必要时 fetch_dependency
    ↓
形成解释和正确示例
```

系统 Prompt 给出的分析顺序是：

```text
1. 识别用户误解的概念或 API
2. 找到 API 定义
3. 阅读核心实现
4. 在 tests/docs 中寻找真实示例
5. 对比用户预期和真实行为
6. 给出解释、用法或替代方案
```

## 5. Question 的输出字段如何复用

为了让不同分支能够共用 State 和报告生成器，Question 仍然输出与 Bug 相同的字段，但字段含义发生了变化：

| 字段 | Question 中的含义 |
|---|---|
| `root_cause` | 用户困惑或误解的根源 |
| `affected_files` | 相关源码文件，不代表需要修改 |
| `severity` | 这个误解影响用户的程度 |
| `fix_suggestion` | 正确用法或替代方案 |
| `code_changes` | 可运行的正确使用示例 |
| `suggested_labels` | `usage`、`documentation`、`wont-fix` 等 |

例如：

```json
{
  "root_cause": "用户混淆了 Python 字符串输入和 JSON 字符串输入",
  "affected_files": ["pydantic/main.py"],
  "severity": "low",
  "fix_suggestion": "JSON 字符串应使用 model_validate_json，而不是 model_validate_strings",
  "code_changes": [
    {
      "file": "docs/concepts/models.md",
      "line": 0,
      "change": "User.model_validate_json('{\"id\": 1}')"
    }
  ],
  "suggested_labels": ["question", "usage"]
}
```

这不是建议修改 `docs/concepts/models.md`，而是说明示例来源。当前共用字段名 `code_changes` 容易让人误会，未来可以把 Question 单独设计为 `usage_examples`。

# 三、两条分支的核心区别

| 对比项 | Feature | Question |
|---|---|---|
| 目标 | 判断并设计新能力 | 解答现有能力的用法或设计 |
| 专用前置节点 | `detect_existing` | 无 |
| 源码权重 | 1.0 | 0.7 |
| 测试权重 | 0.8 | 0.5 |
| 文档权重 | 0.6 | 1.2 |
| 已存在时 | 普通 LLM 直接说明用法 | 不适用 |
| 不存在时 | ReAct 探索架构与实现方案 | 不适用 |
| Agent 重点 | 相似实现、扩展点、兼容性 | API 定义、真实行为、示例 |
| 是否默认改源码 | 可能需要 | 默认不需要 |
| 工具轮数 | 最多 12 | 最多 12 |
| Git History | 不收集 | 不收集 |
| 自动调用图扩展 | 不执行 | 不执行 |

# 四、当前实现的局限

1. `detect_existing` 的 grep 只包含 Python、Rust、TypeScript 和 Go，没有 Java、JavaScript、文档与配置文件。

2. 搜索目标由 LLM 生成。如果 LLM 猜错标识符，可能把已经存在的功能判断为不存在。

3. “已存在”分支不会继续调用工具验证，而是依赖 `existing_api` 和之前收集的上下文直接生成答案。

4. Question 虽然提高文档权重，但 CrossEncoder 精排统一使用 Issue 标题，没有针对“用法问题”和“设计问题”使用不同 query。

5. Question 复用了 Bug 的 State 字段和报告模板，`affected_files`、`code_changes` 容易被理解成需要修改代码。

6. Feature 与 Question 不会自动补充调用图和 Git 历史，只能在 ReAct 阶段通过工具进一步探索。

一句话概括：

> Feature 会先让 LLM提出“功能已存在时应看到什么证据”，再执行最多三个定向 grep，并由第二次 LLM 严格判断。功能已存在就直接说明用法，不存在才启动 12 轮 ReAct 做架构分析。Question 不检查功能是否存在，而是通过文档权重 1.2 的混合检索以及源码、测试工具调查，定位用户误解并给出现有框架中的正确用法或设计解释。

### 5.8 Security 与 HITL

Security 路径调用 LangGraph `interrupt()`，保存当前图状态并等待人工决定。CLI 使用同一个 thread_id 调用 `Command(resume=decision)`，从中断行继续执行，不重复前面的抓取和建图。

当前代码会把 decision 记录到状态后继续流程；若要真正支持 reject 中止，还应增加基于 `human_decision` 的条件边。

Security 路径实现的是一个 HITL（Human-in-the-Loop，人工介入）暂停点。

它的目标是：

```text
发现 Security Issue
    ↓
暂停自动工作流
    ↓
将 Issue 信息展示给人工
    ↓
人工输入 approve / reject
    ↓
使用原任务 ID 恢复工作流
```

不过当前代码中，`approve` 和 `reject` 都会继续后续分析；`reject` 还没有真正连接到终止分支。

# 1. Security 类型是怎么进入的

Issue 首先经过 `route_issue` 分类：

```python
issue_type = chat(system, prompt, max_tokens=10).lower()
```

如果结果是：

```python
{
    "issue_type": "security"
}
```

LangGraph 条件边会进入 `human_gate`：

```python
graph.add_conditional_edges(
    "route_issue",
    decide_route,
    {
        "bug": "detect_regression",
        "feature": "memory_retrieve",
        "question": "memory_retrieve",
        "security": "human_gate",
    }
)
```

完整路由是：

```text
route_issue
    ├── bug      → detect_regression
    ├── feature  → memory_retrieve
    ├── question → memory_retrieve
    └── security → human_gate
```

CLI 还提供了测试参数：

```bash
python main.py <issue-url> --security
```

运行后设置环境变量：

```python
os.environ["FORCE_ISSUE_TYPE"] = "security"
```

`route_issue` 发现这个变量后跳过 LLM 分类：

```python
forced = os.getenv("FORCE_ISSUE_TYPE", "").lower()

if forced in ("bug", "feature", "question", "security"):
    return {"issue_type": forced}
```

因此 `--security` 主要用于稳定测试人工暂停流程。

# 2. human_gate 如何暂停

节点定义在 `workflow/nodes/route.py`：

```python
def human_gate(state: IssueState) -> dict:
    decision = interrupt({
        "message": "⚠️ Security Issue 需要人工确认",
        "issue": f"#{state['issue_number']} {state['title']}",
        "body": state["body"][:300],
    })

    return {"human_decision": decision}
```

关键是：

```python
interrupt(...)
```

它不是普通的 `input()`，也不是异常终止，而是 LangGraph 提供的工作流暂停机制。

传给 `interrupt` 的内容是一个字典：

```python
{
    "message": "⚠️ Security Issue 需要人工确认",
    "issue": "#1234 Security vulnerability",
    "body": "Issue 正文前 300 字符"
}
```

这些数据会返回给调用工作流的外部程序，用于展示审批信息。

执行到这里时：

```python
decision = interrupt(...)
```

不会立即得到 `decision`，工作流会先暂停。

# 3. Checkpointer 为什么是必须的

Issue Graph 编译时配置了 SQLite checkpointer：

```python
db_path = settings.db_path / "checkpoints.sqlite"
db_path.parent.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(
    str(db_path),
    check_same_thread=False,
)

checkpointer = SqliteSaver(conn)

app = graph.compile(checkpointer=checkpointer)
```

默认保存位置类似：

```text
repo_knowledge_base/checkpoints.sqlite
```

当执行到 `interrupt()` 时，LangGraph 会将当前执行状态与 `thread_id` 关联保存。

可以概念化为：

```text
thread_id: 81c845b4-...

当前节点:
human_gate

当前 State:
{
    "issue_url": "...",
    "issue_number": 1234,
    "title": "Security vulnerability",
    "body": "...",
    "labels": [...],
    "issue_type": "security",
    "project_map": "...",
    "image_descriptions": "...",
    "stack_trace_context": "...",
    "iteration": 0
}

中断数据:
{
    "message": "Security Issue 需要人工确认",
    "issue": "#1234 Security vulnerability",
    "body": "..."
}
```

因此暂停以后，前面已经得到的数据不会丢失。

# 4. thread_id 如何关联任务

CLI 开始执行时生成 UUID：

```python
config = {
    "configurable": {
        "thread_id": str(uuid.uuid4())
    }
}
```

例如：

```text
thread_id = 81c845b4-cf63-4f3d-9f55-f0d7d237a3c1
```

第一次调用：

```python
result = app.invoke(
    {
        "issue_url": url,
        "iteration": 0,
        "error": None,
    },
    config=config,
)
```

LangGraph 保存 checkpoint 时会关联这个 ID：

```text
81c845b4-... → Security Issue 的状态和暂停位置
```

之后恢复时仍然传入同一个 `config`，LangGraph 才知道要恢复哪一个任务。

# 5. CLI 如何知道工作流暂停了

第一次 `invoke()` 返回后，CLI 检查：

```python
if result.get("__interrupt__"):
```

如果发生暂停，`result` 中会带有特殊字段：

```python
result["__interrupt__"]
```

项目读取第一个 interrupt 的 value：

```python
info = result["__interrupt__"][0].value
```

得到的就是 `human_gate` 传出的数据：

```python
{
    "message": "⚠️ Security Issue 需要人工确认",
    "issue": "#1234 Security vulnerability",
    "body": "..."
}
```

然后展示给用户：

```python
print("⚠️ 工作流暂停，等待人工确认")
print(f"Issue: {info['issue']}")
print(f"内容: {info['body']}")
```

# 6. 用户如何输入决定

CLI 使用普通终端输入：

```python
decision = input(
    "\n输入决定 (approve/reject): "
).strip()
```

例如用户输入：

```text
approve
```

此时变量为：

```python
decision = "approve"
```

注意，当前代码没有做严格白名单校验。用户输入：

```text
yes
abc
继续
```

也会作为 decision 传给工作流。

更严谨的实现应该校验：

```python
if decision not in {"approve", "reject"}:
    # 提示重新输入
```

# 7. Command(resume) 如何恢复

CLI 使用：

```python
result = app.invoke(
    Command(resume=decision),
    config=config,
)
```

如果用户输入 `approve`，等价于：

```python
Command(resume="approve")
```

这里没有重新传入完整 IssueState，因为状态已经保存在 checkpoint 中。

LangGraph 根据相同的 thread_id：

```text
81c845b4-...
```

找到之前暂停的任务，然后把：

```text
"approve"
```

作为 `interrupt()` 的返回值。

从节点代码的角度，可以理解为暂停前：

```python
decision = interrupt(...)
```

恢复后：

```python
decision = "approve"
```

然后继续执行下一行：

```python
return {"human_decision": decision}
```

所以 State 更新为：

```python
{
    ...
    "human_decision": "approve"
}
```

# 8. 为什么不会重新执行前面的节点

暂停前已经执行：

```text
fetch_issue
    ↓
build_project_map
    ↓
describe_images
    ↓
parse_stack_trace
    ↓
route_issue
    ↓
human_gate：暂停
```

恢复时 LangGraph 从 checkpoint 知道当前暂停位置，不会重新从 START 调用：

- `fetch_issue`
- `build_project_map`
- `describe_images`
- `parse_stack_trace`
- `route_issue`

恢复后的流程是：

```text
human_gate 得到 resume 值
    ↓
返回 human_decision
    ↓
memory_retrieve
    ↓
prepare_context
    ↓
后续安全分析
```

这样可以避免重复：

- 请求 GitHub API；
- clone 和索引仓库；
- 调用图片模型；
- 重新进行 Issue 分类。

# 9. human_gate 后面的当前连接

Graph 当前定义：

```python
graph.add_edge("human_gate", "memory_retrieve")
```

也就是无条件连接：

```text
human_gate → memory_retrieve
```

这意味着只要恢复工作流，无论 decision 是什么，都会继续。

例如：

```text
Command(resume="approve")
    ↓
human_decision = approve
    ↓
memory_retrieve
```

而：

```text
Command(resume="reject")
    ↓
human_decision = reject
    ↓
memory_retrieve
```

两者结果一样。

这就是文档中所说：

> 当前代码会把 decision 记录到状态后继续流程，但 reject 还不能真正中止。

# 10. Approve 后如何分析 Security

Security 批准后进入公共上下文流程：

```text
memory_retrieve
    ↓
prepare_context
    ↓
entry / RAG / git
    ↓
merge_context
```

因为 Security 不是 regression，Git 分支通常为空。

合并上下文后再次根据类型路由：

```python
graph.add_conditional_edges(
    "merge_context",
    decide_route,
    {
        "bug": "classify_fix",
        "feature": "detect_existing",
        "question": "answer_question",
        "security": "classify_fix",
    }
)
```

Security 会进入：

```text
classify_fix → analyze_bug
```

不过 `classify_fix` 中存在判断：

```python
if state.get("issue_type") != "bug":
    return {"fix_type": "code_fix"}
```

因为 Security 不等于 Bug，所以它会直接得到：

```python
fix_type = "code_fix"
```

随后使用普通 `analyze_bug` Agent 分析。

也就是说，当前 Security 分支的特殊之处主要是“增加人工暂停”，还没有单独的 Security 分析角色或安全专用工具策略。

# 11. 如何正确实现 reject 终止

应该把：

```python
graph.add_edge("human_gate", "memory_retrieve")
```

改成条件路由。

先增加判断函数：

```python
def decide_human_gate(state: IssueState) -> str:
    decision = state.get("human_decision", "").strip().lower()

    if decision == "approve":
        return "approve"

    return "reject"
```

然后配置条件边：

```python
graph.add_conditional_edges(
    "human_gate",
    decide_human_gate,
    {
        "approve": "memory_retrieve",
        "reject": END,
    },
)
```

这样流程变为：

```text
human_gate
    ├── approve → memory_retrieve → 继续分析
    └── reject  → END
```

但直接连接 END 后，最终 State 中可能没有 `final_report`。CLI 最后执行：

```python
print(result["final_report"])
```

就可能触发 `KeyError`。

因此更完整的实现应该增加拒绝报告节点。

```python
def generate_rejected_report(state: IssueState) -> dict:
    return {
        "final_report": (
            f"# Issue #{state['issue_number']} 分析已终止\n\n"
            "该 Security Issue 未通过人工审批，系统没有继续执行自动分析。"
        )
    }
```

Graph 配置：

```python
graph.add_node(
    "generate_rejected_report",
    generate_rejected_report,
)

graph.add_conditional_edges(
    "human_gate",
    decide_human_gate,
    {
        "approve": "memory_retrieve",
        "reject": "generate_rejected_report",
    },
)

graph.add_edge("generate_rejected_report", END)
```

最终流程：

```text
human_gate
    ├── approve
    │      ↓
    │   memory_retrieve
    │      ↓
    │   Security 分析
    │
    └── reject
           ↓
       generate_rejected_report
           ↓
          END
```

# 12. CLI 输入也应该校验

可以把当前的一次输入改成循环：

```python
while True:
    decision = input(
        "\n输入决定 (approve/reject): "
    ).strip().lower()

    if decision in {"approve", "reject"}:
        break

    print("输入无效，只能输入 approve 或 reject")
```

避免错误输入被默认当成 reject 或继续处理。

# 13. 当前恢复能力的边界

当前同一次 CLI 运行中：

```text
invoke
    ↓
interrupt
    ↓
input
    ↓
Command(resume)
```

可以正常复用 `config` 中的 thread_id。

但是如果用户在暂停时关闭程序：

```text
程序关闭
    ↓
重新执行 python main.py ...
    ↓
main.py 生成新的 UUID
```

虽然 SQLite 中可能仍有旧 checkpoint，但新进程不知道旧 thread_id，无法通过现有 CLI 恢复。

若要支持跨进程恢复，需要：

1. 首次运行时打印或保存 thread_id；
2. CLI 支持传入旧 thread_id；
3. 提供独立的 resume 命令。

例如：

```bash
python main.py <url> --thread-id <uuid>
```

恢复：

```bash
python main.py \
  --resume <uuid> \
  --decision approve
```

# 14. Web API 当前也没有接入 HITL 恢复

`backend/app.py` 在线程池中直接执行：

```python
state = langgraph_app.invoke(...)
```

它没有像 CLI 一样检查：

```python
state.get("__interrupt__")
```

也没有：

```text
POST /jobs/{id}/resume
```

因此 Web API 如果遇到 Security interrupt，目前不能完成完整的人工审批闭环。

生产化应增加：

```text
POST /analyze
    ↓
job.status = waiting_approval
    ↓
GET /jobs/{id}
    返回 interrupt 信息
    ↓
POST /jobs/{id}/decision
    {"decision": "approve"}
    ↓
Command(resume="approve")
```

# 15. 面试回答

> Security Issue 分类后会进入 human_gate。该节点调用 LangGraph interrupt，将审批消息作为 interrupt value 返回，同时 SqliteSaver 按 thread_id 保存当前 State 和执行位置。CLI 检测到 `__interrupt__` 后显示 Issue 信息，让用户输入 approve 或 reject，再使用相同 thread_id 调用 `Command(resume=decision)`。LangGraph 会把 decision 作为 interrupt 的返回值，从 human_gate 继续执行，而不是重跑 GitHub 抓取和前置节点。当前 Graph 将 human_gate 无条件连接到 memory_retrieve，因此 reject 也会继续，这是现有实现的缺口；正确方式是增加基于 human_decision 的条件边，让 approve 继续、reject 进入拒绝报告节点并结束。
## 6. 仓库索引

### 6.1 clone 与并发保护

仓库保存为：

```text
CLONE_BASE/<owner>__<repo>
```

首次使用 shallow clone，深度为 200，兼顾 clone 成本与 Git log/blame。`ensure_repo_indexed` 是幂等入口：先无锁检查 collection，再获取 per-repo lock，持锁后再次检查，防止并发任务重复 clone/index。

索引或 clone 失败会返回 `False`，尽量让工作流在缺少 RAG 的情况下继续。

### 6.2 文件范围与切片

索引扩展名包括 `.py`、`.ts`、`.js`、`.md`、`.go`、`.java`、`.rs`。忽略 `.git`、虚拟环境、缓存和 node_modules。

| 文件 | 切片方式 |
|---|---|
| Python | AST 按函数、异步函数和类切片 |
| Markdown | 按 `##` 标题切片 |
| 其他语言 | 500 行窗口，50 行 overlap |

每个 chunk 保存路径、行号、owner/repo、file_type 和 Python 顶层 imports。每个仓库一个独立 Chroma collection。
RepoMind 的索引采用分语言切片。Python 使用 AST 按函数和类切片，Markdown 按二级标题切片，其他语言暂时使用 500 行、50 行重叠的窗口作为降级方案。每个 chunk 保存路径、行号、文件类型、仓库来源和 Python imports，写入仓库独立的 Chroma collection。检索时根据 Issue 类型调整 source、test、doc 权重，imports 则用于进一步定位依赖仓库。当前主要不足是其他语言缺少语义切片、固定窗口实际又截断到 2000 字符、Python 类和方法可能重复，以及索引缺少 commit 级增量更新。
## 7. Bug 上下文收集细节

### 7.1 为什么并行

`dispatch_context_gather` 返回三个 `Send`，分别携带 `context_type=entry/rag/git`。三路写不同字段，因此不会覆盖：

```text
entry → _entry_ctx
rag   → _rag_ctx
git   → _git_ctx
```

LangGraph 在三路完成后执行一次 `merge_context`。并行后的延迟近似最慢分支，而不是三路耗时相加。

### 7.2 Entry 分支

LLM 根据标题和正文前 400 字识别 1～3 个入口函数名。随后 grep `def/class/fn/func symbol`，失败时再搜普通文本。

限制：每个符号检查前 3 条结果，整体最多读取 3 个不同文件，每段从命中附近开始约 60 行。因此初始入口上下文最多约 180 行。

### 7.3 RAG 分支

最多使用四个 query：HyDE 假设代码、标题、类型加正文前 150 字、堆栈前 300 字。每路 top-5，最多形成 20 个粗召回结果：

```text
4 × top-5
  → RRF 去重融合
  → top-10
  → file_type 权重重排
  → CrossEncoder(query, document)
  → top-3 主仓库片段
```

Bug 的权重为 source 1.0、test 0.7、doc 0.4。Reranker 加载或推理失败时直接使用加权后的前三条。

### 7.4 依赖源码

系统从召回源码 metadata 读取 imports，并与项目依赖声明取交集。包名解析到 GitHub 仓库后并行 clone/index；使用前两个 query，每个依赖每个 query top-2。最终去重并只将前三个依赖片段加入上下文。

### 7.5 调用图

索引完成后，系统用 Python AST 建立：

```python
callee_map = {"function": ["called_a", "called_b"]}
location_map = {"function": "path.py:L10-40"}
```

RAG 代码中的函数名作为起点，向下查一层 callee，最多读取 5 个可定位函数，每个最多约 60 行。它用于扩展候选证据，不是精确的运行时调用证明；同名函数、多态、反射和动态分派可能误判。

### 7.6 Git 分支

仅 regression 执行。存在版本号时倾向于查近期相关提交和 diff，否则使用 blame 思路定位变更。上下文通常限制在约 1500～2000 字符。为了并行，Git 分支使用 Issue 文本作为锚点，而不是等待 RAG 结果。

### 7.7 合并和实际 Prompt

`merge_context` 按 Entry、RAG、Git 顺序组成 `code_context`。`analyze_bug` 还会加入完整正文与评论、项目地图、Memory、stack trace、关联 PR、图片描述以及 fix_type 提示。

当前没有全局 token budget，正文和评论也没有统一截断。这是长 Issue 的上下文溢出风险，建议按 token 对各 section 分配预算并做 chunk 去重。
Entry 定义 grep 使用 |，但没有 grep -E，定义模式可能无法按预期匹配，通常依靠普通符号搜索降级。

RRF 用完整 document 文本作为身份，不是 chunk ID；重叠但不完全相同的代码不会去重。

file_type 加权只保留 RRF 排名，没有保留 RRF 原始分数。

CrossEncoder 只使用标题作为 query，没有综合正文、HyDE 和 stack trace。

依赖检索函数虽然接收 final_docs 和 doc_paths，当前实际会重新查询 top-8 metadata，并非只读取最终 top-3 的 imports。

MAX_DEPS=6 只在另一个批量 index_dependencies 入口中使用；当前 retrieve_from_imported_deps 路径没有应用这个上限。依赖很多时可能 clone/index 过多仓库。

依赖结果没有 CrossEncoder 精排，只按并发完成后的汇总顺序去重，再取前三条。并发完成顺序并不代表相关性。

依赖检索只覆盖 Python imports，其他语言的包依赖尚未解析。

面试时可以概括为：
prepare_context 先串行保证仓库和索引存在，随后 Send API 并行派发 Entry、RAG 和 Git。Entry 用 LLM 提取入口符号，再 grep 并读取最多三个约 60 行的源码片段。RAG 使用 HyDE、标题、类型正文和堆栈最多四个 query，每路 top-5，经 RRF top-10、文件类型加权和 CrossEncoder 精排得到主仓库 top-3。依赖检索从相关 source metadata 中读取 imports，与 pyproject 或 requirements 的直接依赖取交集，通过已知映射或 PyPI 元数据定位 GitHub 仓库，并行索引后每个依赖用两个 query 各取 top-2，最终只加入三个依赖片段。三路分别写不同 State 字段，全部结束后由 merge_context 统一合并。

## 8. ReAct 分析

Bug Agent 可使用 9 个工具：

| 工具 | 作用 |
|---|---|
| `read_file` | 读取文件指定行 |
| `grep_code` | 全仓库搜索文本/模式 |
| `list_dir` | 查看目录 |
| `find_definition` | 查函数或类定义 |
| `find_callers` | 查上游调用者 |
| `git_log_file` | 查文件提交历史 |
| `git_blame` | 查行级归属 |
| `search_commits` | 搜索提交信息 |
| `fetch_dependency` | 检索依赖仓库源码 |

工具由名称、描述、JSON Schema 和 executor 组成。LLM 返回 tool call，应用执行后将结果追加为 tool message。Bug 最多 12 轮；达到上限后追加“基于现有信息输出最终 JSON”的指令。

最终字段包括 root cause、affected files、severity、fix suggestion、code changes 和 suggested labels。

## 9. 输出可靠性与 Reflection

### 9.1 Guardrails

```text
JSON response format
  → parse_json：直接解析/去 code fence/提取对象/有限截断修复
  → Pydantic AnalyzeResult
  → severity 归一化、必填字段非空校验
  → 失败时 LLM 修复一次
  → 再失败返回明确降级结果
```

LLM 网络超时、连接失败或 rate limit 最多重试 3 次，等待使用指数退避。

### 9.2 Reflection 分数

独立 critic 只阅读分析结论，检查代码依据、文件、修复可执行性和严重度，输出 `confidence ∈ [0,1]`。

```text
confidence < 0.7 且 iteration < 2 → 返回分析节点
否则 → 生成报告
```

因此初始分析 1 次，加上最多 2 次重试，总共最多执行 3 次分析。达到上限后即使分数仍低于 0.7，也会继续报告，防止死循环。

该 confidence 是运行时控制信号；它与 `eval/judge.py` 的离线 Judge overall 完全不同。

## 10. Memory 与 Checkpoint

Issue Memory 按仓库查询 top-5，相似内容加入分析上下文；分析完成后以 `owner/repo#issue` 为 ID upsert 根因、修复、文件、类型和 confidence。

PR Memory 按 `repo + type=pr_review` 查询 top-3，与 Issue 经验隔离。

Issue 和 PR 分别使用 `checkpoints.sqlite`、`pr_checkpoints.sqlite`。Memory 保存知识内容；checkpoint 保存一次工作流执行状态，两者用途不同。

## 11. PR Review 工作流

GitHub API 拉取 PR 元数据、评论和所有 changed files；过滤 lock、图片、字体等文件，并要求 patch 存在。

给 reviewer 的 diff 最多取 20 个文件，每个 patch 最多 120 行。三个 reviewer 并行：

| Reviewer | 工具轮次 | 工具 | 关注点 |
|---|---:|---|---|
| Security | 10 | read/grep/list/callers/definition | 注入、鉴权、密钥、反序列化、数据泄漏 |
| Style | 6 | read/grep/list | 命名、复杂度、重复、类型、死代码 |
| Logic | 10 | read/grep/list/callers/definition | 边界、错误处理、状态、资源、回归 |

三路结果进入 `reflect_pr`。低于 0.7 时回到 `prepare_reviews`，跳过 Memory 查询，重新并行 review；最多重试两次。

报告将 critical/high 计为阻塞问题：只要存在一个就 `REQUEST CHANGES`，否则 `APPROVE`。这是当前硬编码策略，生产中应改成按类别和团队规范配置。

## 12. Web API 与跨仓库 Agent

主 API：

```text
POST /analyze       → 202 + job_id
GET /jobs/{job_id}  → pending/running/done/failed
```

分析在线程池运行，避免阻塞 HTTP 请求。当前 `_jobs` 是内存字典，只支持 Issue，进程重启或多 worker 会丢失/不共享状态。

跨仓库 Orchestrator 让 LLM 从 Issue 中识别最多 3 个可能相关仓库，保证主仓库存在，规范化 full_name 后通过 Send 并行调用 RepoAgent。`repo_results` 使用 `operator.add` reducer 聚合，最后选择最高 confidence 的候选根因仓库并生成综合报告。

## 13. 离线评估

RepoMind 的分析完成后才进入 LLM-as-a-Judge：

- Bug/Feature 以真实合并 PR 的文件和 patch 为 ground truth；
- Question 以维护者/贡献者回复为 ground truth；
- 各评分范围为 0～1；
- 每个样本默认 Judge 3 次，各维度取有效结果的平均值并保留 3 位小数；
- 输出异常分数会 clamp 到 `[0,1]`。

Bug 维度为根因、文件、严重度、修复可执行性和 overall。Judge overall 只用于离线报告，不会控制线上节点，也不会触发重试。目前没有实现统计置信区间；严谨比较可对每个 Issue 的 overall 做 bootstrap 95% CI，版本比较使用 paired bootstrap。

## 14. 核心数据存储

| 数据 | 位置 | 生命周期 |
|---|---|---|
| 克隆仓库 | `/tmp/repomind_repos` 默认 | 本机文件 |
| 仓库向量索引 | `repo_knowledge_base` Chroma | 持久化 |
| Issue/PR Memory | Chroma memory collection | 持久化 |
| Issue checkpoint | SQLite | 持久化 |
| PR checkpoint | 独立 SQLite | 持久化 |
| 项目地图缓存 | 进程内字典 | 进程生命周期 |
| Call graph | 仓库内 JSON + 内存 | 文件与进程缓存 |
| Web jobs | 进程内字典 | 进程生命周期 |
| Eval results | JSON 文件 | 持久化 |

## 15. 失败与降级

| 场景 | 当前行为 |
|---|---|
| LLM timeout/rate limit | 指数退避，最多 3 次 |
| LLM JSON 不合法 | 多级解析、修复 LLM、降级结果 |
| Reranker 无法加载 | 使用 RRF/权重后的 top-3 |
| clone/index 失败 | 记录警告，尽可能无 RAG 继续 |
| 调用图失败 | 跳过调用图 |
| Git 证据失败 | 跳过 Git 增强 |
| 分类输出非法 | 默认 bug |
| regression/fix 分类失败 | 默认 false/code_fix |
| GitHub 必要数据失败 | 任务失败并返回明确错误 |

## 16. 当前不足与改进优先级

1. 按 commit SHA 做增量、版本化索引，避免仓库更新后仍使用旧 collection。
2. 对 Prompt 实施全局 token budget、去重、优先级裁剪和评论摘要。
3. 用 Redis/PostgreSQL 加任务队列替换 Web 内存 Job，并支持取消、超时、进度和幂等。
4. 给 Web API 补齐 PR、Security resume、认证、限流和严格 URL 校验。
5. 对 Issue、评论和源码中的 prompt injection 做数据隔离与最小权限工具控制。
6. 用 tree-sitter/LSP/语言专用分析增强跨语言切片和调用图。
7. 加入 OpenTelemetry/LangSmith，记录节点耗时、token、成本、检索命中和工具轨迹。
8. 固定评测模型，加入人工抽样、消融实验和 bootstrap 置信区间。

## 17. 一条 Bug 的完整示例

```text
1. 输入 https://github.com/fastapi/fastapi/issues/10236
2. 正则识别为 Issue，解析 fastapi/fastapi 和编号 10236
3. GitHub API 拉取标题、正文、labels、评论、关联 PR
4. clone/index 仓库并生成项目地图
5. 提取图片和 stack trace
6. LLM 分类为 bug
7. LLM 判断是否 regression
8. Memory 查询同仓库历史问题 top-5
9. 并行：
   - 识别入口符号并 grep 最多 3 段源码
   - 4 路 RAG，各 top-5，RRF top-10，rerank top-3
   - regression 时获取 Git diff/blame
10. RAG 再补充依赖源码 top-3、callee 最多 5 个
11. 合并代码上下文
12. 判断 code_fix/dependency_upgrade/config_fix
13. Bug Agent 最多 12 轮调用代码和 Git 工具
14. Pydantic 校验结构化分析结果
15. Critic 评分；低于 0.7 最多返回重做 2 次
16. 生成 Markdown 报告
17. 将经验写入 Memory，checkpoint 保存执行状态
```

## 18. 面试版总结

> RepoMind 用 LangGraph 把 GitHub 代码分析拆成确定性流程和 LLM 语义节点。URL 只负责定位资源；GitHub API 获取内容后，LLM 对 Issue 做 bug、feature、question、security 分类。Bug 分支会进行回归检测、混合检索和修复类型规划。上下文由入口 grep、四路 RAG、RRF、CrossEncoder、依赖源码、调用图和 Git 历史组成，然后交给最多 12 轮工具调用的 ReAct Agent。结果经过 JSON/Pydantic Guardrails 和独立 Reflection，低于 0.7 最多重试两次。PR 则并行执行安全、逻辑、风格三路审查。项目还包含 Memory、SQLite checkpoint、HITL、跨仓库 Agent 和基于真实 PR 的离线 LLM Judge 评测。
