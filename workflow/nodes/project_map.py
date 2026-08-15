"""
build_project_map: 分析开始前生成仓库目录结构摘要，给分析 LLM 全局视角，
减少 ReAct 阶段靠 list_dir 盲目摸结构的工具调用浪费。

缓存策略：同一 owner/repo session 内只生成一次。
"""
from pathlib import Path
from workflow.state import IssueState
from core.llm.client import chat
from context import repo_loader
from config import settings

_CACHE: dict[str, str] = {}

_SYSTEM = "你是一名代码导航助手，根据目录结构生成简洁的项目地图。"

_PROMPT = """以下是 {owner}/{repo} 的目录结构（2 层深度）：

{tree}

生成一份简洁的项目地图摘要（150-250 字），说明：
- 核心源码在哪些目录/文件
- 测试代码在哪里
- 文档和配置在哪里（如 setup.py、pyproject.toml、docs/）
- 有哪些关键的顶层文件

纯文本，直接分条说明，不要标题或 JSON。"""


def _walk_tree(path: Path, depth: int, prefix: str = "") -> list[str]:
    """递归收集目录树文本，最深 depth 层。"""
    lines = []
    try:
        entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name))
    except PermissionError:
        return lines
    for entry in entries[:50]:
        if entry.name.startswith("."):
            continue
        marker = "  " if entry.is_file() else "/ "
        lines.append(f"{prefix}{marker}{entry.name}")
        if entry.is_dir() and depth > 1:
            lines.extend(_walk_tree(entry, depth - 1, prefix + "  "))
    return lines


def build_project_map(state) -> dict:
    url = state.get("issue_url") or state.get("pr_url") or state.get("ci_url", "")
    parts = url.rstrip("/").split("/")
    if state.get("owner") and state.get("repo"):
        owner, repo = state["owner"], state["repo"]
    else:
        owner, repo = parts[-4], parts[-3]
    cache_key = f"{owner}/{repo}"

    if cache_key in _CACHE:
        return {"project_map": _CACHE[cache_key]}

    try:
        repo_loader.ensure_repo_indexed(owner, repo)
    except Exception:
        _CACHE[cache_key] = ""
        return {"project_map": ""}

    repo_path = settings.clone_base / f"{owner}__{repo}"
    if not repo_path.exists():
        _CACHE[cache_key] = ""
        return {"project_map": ""}

    tree_lines = _walk_tree(repo_path, depth=2)
    if not tree_lines:
        _CACHE[cache_key] = ""
        return {"project_map": ""}

    tree_text = "\n".join(tree_lines[:100])
    summary = chat(
        _SYSTEM,
        _PROMPT.format(owner=owner, repo=repo, tree=tree_text),
        max_tokens=400,
    )
    _CACHE[cache_key] = summary
    return {"project_map": summary}
