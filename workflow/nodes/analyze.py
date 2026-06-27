from workflow.state import IssueState
from core.llm.client import chat_json, chat_json_with_tools
from tools.base import build_tools
from tools.fetch_dependency import FETCH_DEPENDENCY
from tools.code_nav import READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION, GIT_LOG_FILE, GIT_BLAME, SEARCH_COMMITS

_SYSTEM_BUG = """你是一名资深后端工程师，具备 10 年以上源码阅读和 Bug 定位经验。

你有八个工具：read_file、grep_code、list_dir、find_definition、find_callers、git_log_file、git_blame、search_commits。

**第一步：先判断这个 issue 需要什么性质的动作，再决定分析方向。**

不要默认所有 issue 都需要改代码逻辑。常见性质：
- 改代码逻辑 → 追调用链，找出错的那一行
- 回滚 commit → 先 search_commits / git_log_file 找引入问题的提交
- 废弃 API → 找定义位置，给出加 deprecation 标记的步骤
- 补回归测试 → 验证代码逻辑已对，找对应测试文件给出用例
- 改文档/配置 → 找相关文件位置
- 其他 → 自行推断

用第一轮工具调用确认性质（如 search_commits 看最近改动、grep 看是否有测试），再展开对应方向的分析。

**确认性质后，按对应策略深入：**
- 追代码：find_definition 入口 → read_file 实现 → find_definition 下一层 → 循环，发现可疑时 find_callers 向上溯源
- 追 git：git_log_file 看文件历史 → git_blame 锁定行 → search_commits 找相关 commit
- 其他性质：灵活组合工具

你有 12 轮工具调用。先花 1-2 轮判断性质，剩余轮次深入分析。

输出必须是合法 JSON，reasoning 字段记录你完整的分析推理过程。"""

_SYSTEM_FEATURE = """你是一名资深软件架构师，擅长评估新功能的可行性、实现路径和潜在风险。

你有五个工具：read_file、grep_code、list_dir、find_definition、find_callers。
核心策略：**先找最相似的已有功能，读懂它的实现模式，再推断新功能的实现路径**。

分析思维链路：
1. 需求理解：提炼用户真实诉求，区分"想要什么"和"为什么需要"
2. 找相似功能：用 grep_code 找已有的类似实现，用 find_definition 读其签名
3. 追实现模式：read_file 读相似功能的完整实现，理解扩展点在哪里
4. 架构定位：用 find_callers 了解相似功能的调用方，判断新功能接入点
5. 实现方案：基于真实代码结构给出具体文件路径和改动思路
6. 风险点：兼容性、breaking change、API 稳定性

你有 12 轮工具调用，优先深读 1-2 个最相关的实现，胜过浅读 5 个文件。

输出必须是合法 JSON，reasoning 字段记录你完整的分析推理过程。"""

_SYSTEM_FEATURE_EXISTS = """你是一名资深社区维护者，用户请求的功能在代码库中已经存在。
你的任务是清晰地告知用户该功能的正确用法，并指向相关源码。
输出必须是合法 JSON，reasoning 字段说明功能在哪里、怎么用。"""

_SYSTEM_QUESTION = """你是一名技术文档工程师兼社区维护者，精通框架内部机制，擅长定位用户误解根源。

你有五个工具：read_file、grep_code、list_dir、find_definition、find_callers。

核心策略：**你的目标是解释 API 的实际行为，帮助用户在现有框架内正确使用，而不是修改库代码。**

⚠️ 关键区分：
- 如果这是**使用问题**（怎么用 X？）→ 解释正确用法，给可运行示例
- 如果这是**设计决策问题**（为什么不支持 Y？）→ 解释设计意图和限制，给合理的变通方案
- 不要建议修改库的源码，除非维护者明确表示愿意接受 PR

分析思维链路：
1. 理解困惑：识别用户真正不理解的概念，或误用的 API
2. 定位 API：用 find_definition 找到相关 API 的定义，读取函数签名和实现
3. 读实现：read_file 读核心逻辑，理解它为什么这样设计
4. 找示例：在 tests/ 或 docs/ 里找真实使用示例印证理解
5. 对比预期：找到实际行为和用户预期不符的具体位置
6. 给出答案：解释实际行为的原因，给出用户可以立即使用的正确写法

你有 12 轮工具调用。优先深度理解 API 的实现，不要浅尝辄止。

输出必须是合法 JSON，reasoning 字段记录你完整的分析推理过程。"""

_JSON_FIELDS_QUESTION = """输出 JSON，包含以下字段（注意：这是用法解答，不是 bug 修复报告）：
- reasoning: 按照思维链路逐步写出分析推理过程
- root_cause: 用户困惑的根本原因（误解了哪个概念 / 使用了哪个错误的 API 模式）
- affected_files: 与这个问题直接相关的源码文件（供参考，非修复对象）
- severity: 这个误解的影响程度（low/medium/high，反映有多少用户容易踩坑）
- fix_suggestion: 正确的使用方式（用户应该怎么写代码，而非库应该怎么改）
- code_changes: 正确用法示例列表，每项含 file（示例来自哪个文件）/line/change（可运行的正确代码片段）
- suggested_labels: 建议标签（如 "usage", "documentation", "wont-fix"）

只输出 JSON，不要任何其他文字。"""

_JSON_FIELDS = """输出 JSON，包含以下字段：
- reasoning: 按照思维链路逐步写出分析推理过程
- root_cause: 最终结论，结合代码指出具体文件或逻辑
- affected_files: 受影响的文件路径列表（根据根因分析判断，bug 在哪里就填哪里，可以是主库文件也可以是依赖库文件）
- severity: 只能是 critical / high / medium / low 之一
- fix_suggestion: 具体的修复步骤
- code_changes: 建议改动列表，每项含 file/line/change，行号不确定填 0
- suggested_labels: 建议打的标签列表

只输出 JSON，不要任何其他文字。"""


def _context_sections(state: IssueState) -> str:
    parts = []
    if pm := state.get("project_map"):
        parts.append(f"【项目地图】（仓库目录结构概览，帮助你快速定位代码）：\n{pm}")
    if ctx := state.get("code_context"):
        parts.append(f"相关代码片段：\n{ctx}")
    if mem := state.get("memory_context"):
        parts.append(f"历史同类 Issue 经验：\n{mem}")
    if stack := state.get("stack_trace_context"):
        parts.append(stack)
    if prs := state.get("related_prs"):
        pr_list = "\n".join(f"- {pr}" for pr in prs)
        parts.append(f"关联 PR（可能包含此问题的修复）：\n{pr_list}")
    if img := state.get("image_descriptions"):
        parts.append(f"Issue 附图描述：\n{img}")
    return "\n\n".join(parts)


def _result_dict(result) -> dict:
    return {
        "root_cause": result.root_cause,
        "affected_files": result.affected_files,
        "severity": result.severity,
        "fix_suggestion": result.fix_suggestion,
        "code_changes": [c.model_dump() for c in result.code_changes],
        "suggested_labels": result.suggested_labels,
    }


_FIX_TYPE_HINTS = {
    "dependency_upgrade": (
        "⚠️ 此 bug 已被判断为【依赖升级类】问题。\n"
        "分析重点：找出需要升级/降级哪个依赖库，从什么版本升到什么版本。\n"
        "affected_files 应包含 pyproject.toml 或 requirements.txt。\n"
        "fix_suggestion 应直接说明版本升级命令或版本号范围。"
    ),
    "config_fix": (
        "⚠️ 此 bug 已被判断为【配置修复类】问题。\n"
        "分析重点：找出需要修改哪个配置项或环境变量。\n"
        "affected_files 应包含相关配置文件路径。"
    ),
}


def analyze_bug(state: IssueState) -> dict:
    comments_text = "\n".join(f"- {c['user']}: {c['body']}" for c in state["comments"])
    fix_hint = _FIX_TYPE_HINTS.get(state.get("fix_type", "code_fix"), "")
    prompt = f"""分析以下 Bug Issue。

Title: {state['title']}
Body: {state['body']}
Comments: {comments_text}

{fix_hint}

{_context_sections(state)}

{_JSON_FIELDS}"""

    tools_schema, executors = build_tools(
        READ_FILE, GREP_CODE, LIST_DIR,
        FIND_CALLERS, FIND_DEFINITION,
        GIT_LOG_FILE, GIT_BLAME, SEARCH_COMMITS,
        FETCH_DEPENDENCY,
    )
    return _result_dict(chat_json_with_tools(
        _SYSTEM_BUG, prompt,
        tools=tools_schema,
        tool_executor=executors,
        max_tokens=4000,
        max_turns=12,
    ))


_JSON_FIELDS_FEATURE = """输出 JSON，包含以下字段：
- reasoning: 按照思维链路逐步写出分析推理过程
- root_cause: 当前为什么不满足需求（现有实现的缺口）
- affected_files: 实现该功能需要新增或修改的文件路径列表
- severity: 对用户的影响程度，只能是 critical / high / medium / low 之一
- fix_suggestion: 具体实现路径（文件、函数、改动思路）
- code_changes: 建议改动列表，每项含 file/line/change，行号不确定填 0
- suggested_labels: 建议打的标签列表（如 enhancement、good first issue 等）

只输出 JSON，不要任何其他文字。"""


def analyze_feature(state: IssueState) -> dict:
    # 已存在短路：功能已有，直接解释用法
    if state.get("feature_exists") and state.get("existing_api"):
        prompt = f"""用户提交了一个 Feature Request，但该功能已在代码库中存在。

Title: {state['title']}
Body: {state['body'][:400]}

已存在的实现：{state['existing_api']}

{_context_sections(state)}

请告知用户该功能已存在，并给出正确用法和相关源码位置。
{_JSON_FIELDS_FEATURE}"""
        return _result_dict(chat_json(_SYSTEM_FEATURE_EXISTS, prompt))

    # 功能不存在：ReAct 工具循环主动探索架构
    comments_text = "\n".join(f"- {c['user']}: {c['body']}" for c in state["comments"])
    prompt = f"""分析以下 Feature Request，主动探索代码库后给出实现方案。

Title: {state['title']}
Body: {state['body']}
Comments: {comments_text}

{_context_sections(state)}

{_JSON_FIELDS_FEATURE}"""

    tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION, FETCH_DEPENDENCY)
    return _result_dict(chat_json_with_tools(
        _SYSTEM_FEATURE, prompt,
        tools=tools_schema,
        tool_executor=executors,
        max_tokens=4000,
        max_turns=12,
    ))


def answer_question(state: IssueState) -> dict:
    comments_text = "\n".join(f"- {c['user']}: {c['body']}" for c in state["comments"])
    prompt = f"""解答以下使用问题，主动查阅源码后给出准确解释。

⚠️ 记住：你的目标是帮助用户理解正确用法，不是提议修改库代码。
如果这个问题的答案是"框架设计如此，不会改变"，请解释设计意图并给出现有框架下的变通方案。

Title: {state['title']}
Body: {state['body']}
Comments: {comments_text}

{_context_sections(state)}

{_JSON_FIELDS_QUESTION}"""

    tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION, FETCH_DEPENDENCY)
    return _result_dict(chat_json_with_tools(
        _SYSTEM_QUESTION, prompt,
        tools=tools_schema,
        tool_executor=executors,
        max_tokens=4000,
        max_turns=12,
    ))
