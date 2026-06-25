import os
from langgraph.types import interrupt
from workflow.state import IssueState
from core.llm.client import chat


def route_issue(state: IssueState) -> dict:
    forced = os.getenv("FORCE_ISSUE_TYPE", "").lower()
    if forced in ("bug", "feature", "question", "security"):
        return {"issue_type": forced}

    system = """你是一名 GitHub Issue 分类专家。判断 Issue 类型：
- bug：已存在的错误行为（有报错/traceback/复现步骤/预期vs实际描述）
- feature：新功能请求
- question：使用疑问（纯粹问"how to"或"why"，没有报错）
- security：安全漏洞

判断规则（按优先级）：
1. 内容信号优先：有 traceback、复现代码、"expected X got Y"、"worked before" → bug；纯问法 → question
2. GitHub label 仅作参考，权重中等，可能不准确
3. 内容信号与 label 冲突时，优先相信内容信号
4. 模糊时宁可判断为 bug"""

    prompt = f"""判断这个 GitHub Issue 的类型，只输出一个词：bug / feature / question / security

Title: {state['title']}
Labels: {state['labels']}
Body:
{state['body'][:600]}

注意：label 仅供参考，以 Issue 内容为主要判断依据。"""

    issue_type = chat(system, prompt, max_tokens=10).lower()
    if issue_type not in ("bug", "feature", "question", "security"):
        issue_type = "bug"
    return {"issue_type": issue_type}


def human_gate(state: IssueState) -> dict:
    decision = interrupt({
        "message": "⚠️ Security Issue 需要人工确认",
        "issue": f"#{state['issue_number']} {state['title']}",
        "body": state["body"][:300],
    })
    return {"human_decision": decision}
