from workflow.state import IssueState
from core.llm.client import chat_json, chat_json_with_tools
from tools.base import build_tools
from tools.fetch_dependency import FETCH_DEPENDENCY
from tools.code_nav import READ_FILE, GREP_CODE, LIST_DIR

_SYSTEM_BUG = """你是一名资深后端工程师，具备 10 年以上源码阅读和 Bug 定位经验。

你有工具可以主动读取仓库文件、搜索符号定义、列出目录结构。不要满足于 context 里现有的代码片段——像真正的 coding agent 一样，沿着调用链主动追踪到根因所在的具体位置。

分析思维链路：
1. 复现路径：从 issue 描述提炼触发 bug 的最短操作序列
2. 入口定位：用 grep_code 找到入口函数的具体位置
3. 调用链追踪：逐层跟进调用，发现跨越依赖库时用 read_file 读依赖源码
4. 根因定位：找到异常分支或错误假设所在的具体文件和行号
5. 影响评估：判断波及范围和严重程度
6. 最小修复：改动最小、副作用最低的方案

输出必须是合法 JSON，reasoning 字段记录你完整的分析推理过程。"""

_SYSTEM_FEATURE = """你是一名资深软件架构师，擅长评估新功能的可行性、实现路径和潜在风险。

分析时严格按照以下思维链路执行：
1. 需求理解：提炼用户真实诉求，区分"想要什么"和"为什么需要"
2. 现状分析：结合代码说明当前实现为什么不满足需求
3. 可行性评估：分析实现复杂度、对现有架构的侵入性、潜在的 breaking change
4. 实现方案：给出具体的代码改动路径，优先选择侵入性最小的方案
5. 风险点：指出实现过程中需要特别注意的兼容性或性能问题

输出必须是合法 JSON，reasoning 字段记录你完整的分析推理过程。"""

_SYSTEM_QUESTION = """你是一名技术文档工程师兼社区维护者，精通框架内部机制，擅长定位用户误解根源。

分析时严格按照以下思维链路执行：
1. 理解困惑：识别用户真正不理解的概念或误用的 API
2. 根因定位：找到导致用户困惑的代码行为或文档表述
3. 清晰解答：结合源码给出准确解释，避免模糊表述
4. 可执行建议：提供用户可以立即执行的解决步骤

输出必须是合法 JSON，reasoning 字段记录你完整的分析推理过程。"""

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
    comments_text = "\n".join(f"- {c['user']}: {c['body']}" for c in state["comments"][:5])
    fix_hint = _FIX_TYPE_HINTS.get(state.get("fix_type", "code_fix"), "")
    prompt = f"""分析以下 Bug Issue。

Title: {state['title']}
Body: {state['body']}
Comments: {comments_text}

{fix_hint}

{_context_sections(state)}

{_JSON_FIELDS}"""

    tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR, FETCH_DEPENDENCY)
    return _result_dict(chat_json_with_tools(
        _SYSTEM_BUG, prompt,
        tools=tools_schema,
        tool_executor=executors,
        max_tokens=3000,
        max_turns=6,
    ))


def analyze_feature(state: IssueState) -> dict:
    prompt = f"""分析以下 Feature Request。

Title: {state['title']}
Body: {state['body']}

{_context_sections(state)}

{_JSON_FIELDS}"""
    return _result_dict(chat_json(_SYSTEM_FEATURE, prompt))


def answer_question(state: IssueState) -> dict:
    prompt = f"""解答以下使用问题。

Title: {state['title']}
Body: {state['body']}

{_context_sections(state)}

{_JSON_FIELDS}"""
    return _result_dict(chat_json(_SYSTEM_QUESTION, prompt))
