from workflow.state import IssueState
from tools.github_api import parse_issue_url
from context.memory_store import get_memory_collection

_memory_collection = get_memory_collection()


def memory_retrieve(state: IssueState) -> dict:
    owner, repo, _ = parse_issue_url(state["issue_url"])
    query = f"{state['issue_type']}: {state['title']}"
    try:
        results = _memory_collection.query(
            query_texts=[query],
            n_results=5,
            where={"repo": f"{owner}/{repo}"},
        )
        docs = results["documents"][0] if results["documents"] else []
    except Exception:
        results = _memory_collection.query(query_texts=[query], n_results=5)
        docs = results["documents"][0] if results["documents"] else []
    return {"memory_context": "\n\n---\n\n".join(docs) if docs else ""}


def memory_save(state: IssueState) -> dict:
    owner, repo, number = parse_issue_url(state["issue_url"])
    affected = ", ".join(state.get("affected_files", [])) or "未确定"
    content = f"""Issue: {state['title']}
类型: {state['issue_type']} | 严重程度: {state.get('severity', 'unknown')}
根因: {state['root_cause']}
修复方案: {state['fix_suggestion']}
涉及文件: {affected}
置信度: {state.get('confidence', 0):.2f}"""

    _memory_collection.upsert(
        ids=[f"{owner}/{repo}#{number}"],
        documents=[content],
        metadatas=[{
            "repo": f"{owner}/{repo}",
            "type": state["issue_type"],
            "severity": state.get("severity", "medium"),
            "confidence": str(round(state.get("confidence", 0), 2)),
        }],
    )
    return {}
