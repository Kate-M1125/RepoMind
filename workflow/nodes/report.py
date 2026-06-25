from workflow.state import IssueState


def generate_report(state: IssueState) -> dict:
    affected = "\n".join(f"- `{f}`" for f in state.get("affected_files", [])) or "- 暂未确定"
    labels = " ".join(f"`{l}`" for l in state.get("suggested_labels", [])) or "无"
    severity = state.get("severity", "medium")

    changes = state.get("code_changes", [])
    if changes:
        change_lines = []
        for c in changes:
            loc = f" L{c['line']}" if c.get("line") else ""
            change_lines.append(f"- `{c.get('file', '?')}`{loc}: {c.get('change', '')}")
        changes_md = "\n".join(change_lines)
    else:
        changes_md = "- 无具体代码改动建议"

    report = f"""# Issue #{state['issue_number']} 分析报告

## {state['title']}

| 字段 | 值 |
|------|-----|
| 类型 | {state['issue_type']} |
| 严重程度 | {severity} |
| 建议标签 | {labels} |

## 根因
{state['root_cause']}

## 受影响文件
{affected}

## 代码改动建议
{changes_md}

## 修复步骤
{state['fix_suggestion']}
"""
    return {"final_report": report}
