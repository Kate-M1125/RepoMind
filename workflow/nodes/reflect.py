from workflow.state import IssueState
from core.llm.client import chat, parse_json

_SYSTEM_REFLECT = """你是一名严格的代码评审专家，负责独立评估 Issue 分析报告的质量和可信度。

评估维度：
- 根因是否有代码依据，还是仅凭猜测
- 受影响文件是否准确，有无遗漏
- 修复方案是否具体可执行，有无引入新问题
- 严重程度评级是否与实际影响匹配

你的评估必须独立、严格，不受原始分析的语气影响。"""


def reflect(state: IssueState) -> dict:
    prompt = f"""独立评估以下 Issue 分析报告的质量。

Issue: {state['title']}
类型: {state.get('issue_type', '未知')}
根因: {state['root_cause']}
受影响文件: {state.get('affected_files', [])}
严重程度: {state.get('severity', '未知')}
修复建议: {state['fix_suggestion']}

只输出 JSON，两个字段：
- confidence: 0-1 的浮点数，代表分析可信度（低于 0.7 表示分析不可信，需要重做）
- note: 你的评审意见，指出具体的质疑点或确认理由

只输出 JSON，不要任何其他文字。"""

    result = parse_json(chat(_SYSTEM_REFLECT, prompt, max_tokens=600))
    raw_conf = result.get("confidence", 0)
    confidence = max(0.0, min(1.0, float(raw_conf)))
    return {
        "confidence": confidence,
        "reflection_note": result.get("note", ""),
        "iteration": state.get("iteration", 0) + 1,
    }
