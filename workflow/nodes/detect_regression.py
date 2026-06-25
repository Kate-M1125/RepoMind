from workflow.state import IssueState
from core.llm.client import chat, parse_json

_SYSTEM = "你是一名 GitHub Issue 分析专家，判断一个 bug Issue 是否是回归问题（regression）。"

_PROMPT = """判断以下 bug Issue 是否属于回归问题。

回归问题的特征（满足任意一条即是）：
- 描述了"以前能用，现在不行"
- 提到了具体版本号，且在某个版本升级后出现
- 包含"regression"、"broke"、"used to work"、"worked before"等语义
- 任何语言的类似表达

Issue 标题: {title}
Issue 内容:
{body}

只输出 JSON：{{"is_regression": true}} 或 {{"is_regression": false}}"""


def detect_regression(state: IssueState) -> dict:
    if state.get("issue_type") != "bug":
        return {"is_regression": False}

    prompt = _PROMPT.format(
        title=state["title"],
        body=(state.get("body") or "")[:800],
    )
    try:
        result = parse_json(chat(_SYSTEM, prompt, max_tokens=20, json_mode=True))
        return {"is_regression": bool(result.get("is_regression", False))}
    except Exception:
        return {"is_regression": False}
