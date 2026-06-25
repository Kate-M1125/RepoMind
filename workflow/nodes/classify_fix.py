from workflow.state import IssueState
from core.llm.client import chat, parse_json

_SYSTEM = "你是一名 GitHub Bug 分析专家，判断一个 bug 需要什么类型的修复。"

_PROMPT = """判断以下 bug 需要什么类型的修复，只输出 JSON。

修复类型定义：
- code_fix：需要修改源码逻辑（绝大多数 bug）
- dependency_upgrade：需要升级或降级某个第三方依赖库的版本
- config_fix：需要修改配置文件、环境变量或项目设置

判断信号：
- "after upgrading X"、"incompatible with vX"、"X version broke"、依赖库名+版本号 → dependency_upgrade
- 配置项、.env、settings、环境变量相关 → config_fix
- 其他 → code_fix

Issue 标题: {title}
Issue 内容:
{body}

只输出 JSON：{{"fix_type": "code_fix"}} 或 {{"fix_type": "dependency_upgrade"}} 或 {{"fix_type": "config_fix"}}"""


def classify_fix(state: IssueState) -> dict:
    if state.get("issue_type") != "bug":
        return {"fix_type": "code_fix"}

    prompt = _PROMPT.format(
        title=state["title"],
        body=(state.get("body") or "")[:600],
    )
    try:
        result = parse_json(chat(_SYSTEM, prompt, max_tokens=20, json_mode=True))
        fix_type = result.get("fix_type", "code_fix")
        if fix_type not in ("code_fix", "dependency_upgrade", "config_fix"):
            fix_type = "code_fix"
        return {"fix_type": fix_type}
    except Exception:
        return {"fix_type": "code_fix"}
