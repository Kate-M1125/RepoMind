from typing import TypedDict, Optional
from langgraph.types import Send

from workflow.state import IssueState
from tools.github_api import parse_issue_url
from core.llm.client import chat
from context.call_graph import extract_func_names, expand_with_callees
from context.repo_loader import ensure_repo_indexed
from context.dep_indexer import retrieve_from_imported_deps
from tools.git_history import fetch_recent_diff, fetch_blame_diff
from tools.code_nav import _grep_code, _read_file

_reranker = None


def get_repo_collection(owner: str, repo: str):
    """保留可 Mock 的轻量代理，真正调用时再加载 ChromaDB 实现。"""
    from context.rag_index import get_repo_collection as load_collection
    return load_collection(owner, repo)


def _get_reranker():
    global _reranker
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder
            _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            print("[RAG] Reranker 加载完成: cross-encoder/ms-marco-MiniLM-L-6-v2")
        except Exception as e:
            print(f"[RAG] Reranker 加载失败，将跳过 rerank 步骤: {e}")
            _reranker = False
    return _reranker if _reranker is not False else None


def _rrf_merge(ranked_lists: list[list[str]], k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc in enumerate(ranked, start=1):
            scores[doc] = scores.get(doc, 0.0) + 1.0 / (k + rank)
    return [doc for doc, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True)]


def _llm_identify_entry_points(title: str, body: str) -> list[str]:
    prompt = f"""以下是一个 GitHub Issue。请找出用户触发这个 bug 时直接调用的函数名或方法名（1-3个）。

Issue: {title}
{body[:400]}

只输出 JSON 数组，如 ["model_rebuild", "model_validate_strings"]。
没有明确函数调用时返回 []。只输出数组，不要任何解释。"""
    try:
        import json
        raw = chat("你是代码分析助手，只输出 JSON。", prompt, max_tokens=80)
        import re as _re
        m = _re.search(r'\[.*?\]', raw, _re.DOTALL)
        if m:
            return [s for s in json.loads(m.group()) if isinstance(s, str)]
    except Exception:
        pass
    return []


def _grep_entry_points(owner: str, repo: str, symbols: list[str]) -> str:
    sections = []
    seen_files: set[str] = set()
    for sym in symbols:
        out = _grep_code(owner, repo, f"def {sym}|class {sym}|fn {sym}|func {sym}")
        if not out or "未找到" in out:
            out = _grep_code(owner, repo, sym)
        if not out or "未找到" in out:
            continue
        for line in out.splitlines()[:3]:
            parts = line.split(":")
            if len(parts) < 2:
                continue
            rel_path, lineno = parts[0], parts[1]
            if rel_path in seen_files:
                continue
            seen_files.add(rel_path)
            try:
                start = max(1, int(lineno) - 2)
                content = _read_file(owner, repo, rel_path, start, start + 60)
                sections.append(content)
            except Exception:
                pass
        if len(sections) >= 3:
            break
    return "\n\n---\n\n".join(sections)


def _generate_hyde(state: dict) -> Optional[str]:
    try:
        hyde_prompt = (
            f"根据以下 GitHub Issue，写出最可能引发该 bug 的 Python/Rust 代码片段（20 行内）：\n"
            f"{state['title']}\n{state.get('body', '')[:300]}"
        )
        return chat("你是一名擅长预测 bug 位置的工程师，只输出代码。", hyde_prompt, max_tokens=200)
    except Exception:
        return None


_FILETYPE_WEIGHTS = {
    "bug":      {"source": 1.0, "test": 0.7, "doc": 0.4},
    "question": {"source": 0.7, "test": 0.5, "doc": 1.2},
    "feature":  {"source": 1.0, "test": 0.8, "doc": 0.6},
    "security": {"source": 1.0, "test": 0.7, "doc": 0.4},
}


def _apply_filetype_weights(docs: list[str], doc_metas: dict[str, str], issue_type: str) -> list[str]:
    weights = _FILETYPE_WEIGHTS.get(issue_type, _FILETYPE_WEIGHTS["bug"])
    scored = []
    for rank, doc in enumerate(docs, start=1):
        file_type = doc_metas.get(doc, "source")
        w = weights.get(file_type, 1.0)
        score = (1.0 / rank) * w
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [doc for _, doc in scored]


def _expand_call_graph(rag_context: str, owner: str, repo: str) -> str:
    from config import settings
    repo_path = str(settings.clone_base / f"{owner}__{repo}")
    if not __import__("pathlib").Path(repo_path).exists():
        return ""
    try:
        func_names = extract_func_names(rag_context)
        if not func_names:
            return ""
        return expand_with_callees(func_names, repo_path)
    except Exception as e:
        print(f"[CallGraph] 展开失败，跳过: {e}")
        return ""


def _fetch_git_history(owner: str, repo: str, state: dict, rag_context: str) -> str:
    from config import settings
    from pathlib import Path
    import re
    repo_path = str(settings.clone_base / f"{owner}__{repo}")
    if not Path(repo_path).exists():
        return ""
    try:
        issue_text = f"{state['title']}\n{state.get('body', '')[:500]}"
        has_version = bool(re.search(r'v?\d+\.\d+', issue_text))
        if has_version:
            result = fetch_recent_diff(repo_path, issue_text)
        else:
            result = fetch_blame_diff(repo_path, rag_context)
        return result
    except Exception as e:
        print(f"[GitHistory] 获取失败，跳过: {e}")
        return ""


# ── Sub-state for Send API parallel gather ──────────────────────────────────

class GatherContextState(TypedDict):
    issue_url: str
    title: str
    body: str
    issue_type: str
    is_regression: bool
    stack_trace_context: str
    context_type: str  # "entry" | "rag" | "git"


# ── 三个独立收集函数 ─────────────────────────────────────────────────────────

def _gather_entry_context(state: dict) -> dict:
    """LLM 识别入口函数 + grep 定位调用链起点（~1.5s LLM + fast grep）"""
    owner, repo, _ = parse_issue_url(state["issue_url"])
    entry_points = _llm_identify_entry_points(state["title"], state.get("body", ""))
    if not entry_points:
        print("[Gather/Entry] LLM 未识别到入口函数")
        return {"_entry_ctx": ""}
    ctx = _grep_entry_points(owner, repo, entry_points)
    if ctx:
        print(f"[Gather/Entry] 入口 {entry_points} grep 成功")
    else:
        print(f"[Gather/Entry] 入口 {entry_points} 仓库中未找到定义")
    return {"_entry_ctx": ctx}


def _gather_rag_context(state: dict) -> dict:
    """HyDE + RAG 四路查询 + rerank + dep_indexer + call_graph（~2-7s）"""
    owner, repo, _ = parse_issue_url(state["issue_url"])
    rag_collection = get_repo_collection(owner, repo)

    hyde_doc = _generate_hyde(state)
    title_query = state["title"]
    type_query = f"{state.get('issue_type', '')} {state.get('body', '')[:150]}"
    stack_ctx = state.get("stack_trace_context", "")
    stack_query = stack_ctx[:300] if stack_ctx else None

    queries = [q for q in [hyde_doc, title_query, type_query, stack_query] if q]

    per_query_results: list[list[str]] = []
    per_query_metadatas: list[list[dict]] = []
    for q in queries:
        try:
            res = rag_collection.query(query_texts=[q], n_results=5,
                                       include=["documents", "metadatas"])
            docs = res["documents"][0] if res["documents"] else []
            metas = res["metadatas"][0] if res["metadatas"] else []
            per_query_results.append(docs)
            per_query_metadatas.append(metas)
        except Exception as e:
            print(f"[RAG] 查询失败，跳过: {e}")

    final_docs: list[str] = []
    if per_query_results:
        merged = _rrf_merge(per_query_results)[:10]
        doc_metas: dict[str, str] = {}
        for docs, metas in zip(per_query_results, per_query_metadatas):
            for doc, meta in zip(docs, metas):
                if doc not in doc_metas:
                    doc_metas[doc] = meta.get("file_type", "source")
        merged = _apply_filetype_weights(merged, doc_metas, state.get("issue_type", "bug"))
        reranker = _get_reranker()
        if reranker and len(merged) > 1:
            try:
                pairs = [(state["title"], doc) for doc in merged]
                scores = reranker.predict(pairs)
                ranked = sorted(zip(scores, merged), key=lambda x: x[0], reverse=True)
                final_docs = [doc for _, doc in ranked[:3]]
            except Exception:
                final_docs = merged[:3]
        else:
            final_docs = merged[:3]

    rag_context = "\n\n---\n\n".join(final_docs)

    doc_paths: dict[str, str] = {}
    for docs, metas in zip(per_query_results, per_query_metadatas):
        for doc, meta in zip(docs, metas):
            if doc not in doc_paths and meta.get("path"):
                doc_paths[doc] = meta["path"]

    from config import settings
    repo_path = str(settings.clone_base / f"{owner}__{repo}")
    dep_docs = retrieve_from_imported_deps(final_docs, doc_paths, repo_path, owner, repo, queries)
    dep_context = "\n\n---\n\n".join(dep_docs[:3]) if dep_docs else ""
    if dep_context:
        print(f"[Gather/RAG] DepIndexer 补充依赖（前 100 字）: {dep_context[:100]}...")

    call_graph_context = ""
    if state.get("issue_type") == "bug":
        call_graph_context = _expand_call_graph(rag_context, owner, repo)
        if call_graph_context:
            print(f"[Gather/RAG] CallGraph 展开:\n{call_graph_context[:200]}...")

    sections = []
    if rag_context:
        sections.append(rag_context)
    if dep_context:
        sections.append("--- [依赖源码] ---\n\n" + dep_context)
    if call_graph_context:
        sections.append("--- [Call Graph 展开] ---\n\n" + call_graph_context)

    return {"_rag_ctx": "\n\n".join(sections)}


def _gather_git_context(state: dict) -> dict:
    """Git history（仅 regression，使用 issue body 作锚点并行化）"""
    if not state.get("is_regression"):
        return {"_git_ctx": ""}
    owner, repo, _ = parse_issue_url(state["issue_url"])
    # 并行模式下无法使用 RAG 结果，改用 issue body 作为 blame 锚点
    issue_text = f"{state['title']}\n{state.get('body', '')[:500]}"
    git_ctx = _fetch_git_history(owner, repo, state, rag_context=issue_text)
    if git_ctx:
        print(f"[Gather/Git] 找到相关 commit diff（前 200 字）:\n{git_ctx[:200]}...")
    return {"_git_ctx": git_ctx}


# ── Send API dispatch ────────────────────────────────────────────────────────

def dispatch_context_gather(state: IssueState) -> list[Send]:
    """返回三个 Send，并行启动 entry / rag / git 收集。"""
    base = {
        "issue_url":           state["issue_url"],
        "title":               state["title"],
        "body":                state.get("body", ""),
        "issue_type":          state.get("issue_type", "bug"),
        "is_regression":       state.get("is_regression", False),
        "stack_trace_context": state.get("stack_trace_context", ""),
    }
    return [
        Send("gather_context_part", {**base, "context_type": "entry"}),
        Send("gather_context_part", {**base, "context_type": "rag"}),
        Send("gather_context_part", {**base, "context_type": "git"}),
    ]


def gather_context_part(state: GatherContextState) -> dict:
    """单节点，按 context_type 路由到三个收集函数。"""
    t = state["context_type"]
    if t == "entry":
        return _gather_entry_context(state)
    elif t == "rag":
        return _gather_rag_context(state)
    else:
        return _gather_git_context(state)


# ── 准备和合并节点 ──────────────────────────────────────────────────────────

def prepare_context(state: IssueState) -> dict:
    """在并行收集前确保仓库已克隆并索引（阻塞操作，必须串行）。"""
    owner, repo, _ = parse_issue_url(state["issue_url"])
    ensure_repo_indexed(owner, repo)
    return {}


def merge_context(state: IssueState) -> dict:
    """三路并行结果全部完成后，组装 code_context。"""
    entry_ctx = state.get("_entry_ctx", "")
    rag_ctx   = state.get("_rag_ctx", "")
    git_ctx   = state.get("_git_ctx", "")

    sections = []
    if entry_ctx:
        sections.append("--- [入口代码（grep 定位）] ---\n\n" + entry_ctx)
    if rag_ctx:
        sections.append(rag_ctx)
    if git_ctx:
        sections.append("--- [Git History] ---\n\n" + git_ctx)

    return {"code_context": "\n\n".join(sections)}
