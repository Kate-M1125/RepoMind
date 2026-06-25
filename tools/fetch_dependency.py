"""
fetch_dependency：LLM 发现 bug 根因在依赖库时主动拉取依赖源码。

schema 和 execute 放在一起，改这个 tool 只需改这一个文件。
"""
from tools.base import Tool
from context.repo_loader import ensure_repo_indexed


def _execute(owner: str, repo: str, query: str) -> str:
    print(f"[FetchDependency] 工具被调用: {owner}/{repo}, query='{query[:60]}'")
    ensure_repo_indexed(owner, repo)
    try:
        from context.rag_index import get_repo_collection
        collection = get_repo_collection(owner, repo)
        if collection.count() == 0:
            return f"{owner}/{repo} 索引为空或失败，无法提供代码参考"
        res = collection.query(query_texts=[query], n_results=3, include=["documents"])
        docs = (res["documents"] or [[]])[0]
        if not docs:
            return f"在 {owner}/{repo} 中未找到与 '{query}' 相关的代码"
        return f"=== {owner}/{repo} 相关代码 ===\n\n" + "\n\n---\n\n".join(docs)
    except Exception as e:
        return f"查询 {owner}/{repo} 失败: {e}"


FETCH_DEPENDENCY = Tool(
    name="fetch_dependency",
    description=(
        "当你怀疑 bug 根因在某个依赖库（而非当前项目本身）时，调用此工具。"
        "它会自动拉取该依赖的源码并返回相关代码片段，帮助你定位跨库问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "GitHub owner，例如 'encode'"},
            "repo":  {"type": "string", "description": "GitHub 仓库名，例如 'starlette'"},
            "query": {"type": "string", "description": "在该依赖中要搜索的关键词，如函数名或错误描述"},
        },
        "required": ["owner", "repo", "query"],
    },
    execute=_execute,
)
