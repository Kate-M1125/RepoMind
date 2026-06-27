"""
依赖自动索引：解析目标仓库的 pyproject.toml，把直接依赖的 GitHub 源码也索引进 RAG。

只做一层，不递归。入口：index_dependencies(repo_path, owner, repo)
"""
import re
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MAX_DEPS = 6  # 每个仓库最多索引几个依赖，避免耗时过长

# PyPI metadata 不可靠时的兜底映射（常见包 metadata 错误或指向子目录）
_KNOWN_REPOS: dict[str, tuple[str, str]] = {
    "pydantic-core":      ("pydantic",  "pydantic-core"),
    "starlette":          ("encode",    "starlette"),
    "anyio":              ("agronholm", "anyio"),
    "httpcore":           ("encode",    "httpcore"),
    "h11":                ("python-hyper", "h11"),
    "httpx":              ("encode",    "httpx"),
    "uvicorn":            ("encode",    "uvicorn"),
    "click":              ("pallets",   "click"),
    "werkzeug":           ("pallets",   "werkzeug"),
    "jinja2":             ("pallets",   "jinja2"),
    "pluggy":             ("pytest-dev","pluggy"),
    "urllib3":            ("urllib3",   "urllib3"),
    "certifi":            ("certifi",   "python-certifi"),
}

# 这些 org 的仓库没有意义索引（stdlib、CI 工具等）
_SKIP_ORGS = {"python", "actions", "sponsors", "pypa"}


def parse_dependencies(repo_path: str) -> list[str]:
    """从 pyproject.toml 或 requirements.txt 提取直接依赖的包名。"""
    root = Path(repo_path)
    names: list[str] = []

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore
            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            # PEP 621 标准格式
            for dep in data.get("project", {}).get("dependencies", []):
                name = re.split(r"[>=<!;\[ ]", dep)[0].strip()
                if name:
                    names.append(name)
            # Poetry 格式
            for name in data.get("tool", {}).get("poetry", {}).get("dependencies", {}):
                if name.lower() != "python":
                    names.append(name)
        except Exception as e:
            logger.warning(f"[DepIndexer] 解析 pyproject.toml 失败: {e}")

    # 没有 pyproject.toml 就回退到 requirements.txt
    if not names:
        req = root / "requirements.txt"
        if req.exists():
            for line in req.read_text(errors="ignore").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    name = re.split(r"[>=<!;\[ ]", line)[0].strip()
                    if name:
                        names.append(name)

    return names


def resolve_github_repo(package: str) -> tuple[str, str] | None:
    """查 PyPI JSON API，从 project_urls 里找 GitHub owner/repo。先查兜底映射。"""
    # 先查已知映射，跳过 PyPI 查询
    normalized = package.lower().replace("_", "-")
    if normalized in _KNOWN_REPOS:
        return _KNOWN_REPOS[normalized]

    try:
        resp = httpx.get(f"https://pypi.org/pypi/{package}/json", timeout=10)
        if resp.status_code != 200:
            return None
        urls = resp.json().get("info", {}).get("project_urls") or {}
        for key in ("Source", "Repository", "Homepage", "Source Code", "Code"):
            url = urls.get(key, "")
            if "github.com" not in url:
                continue
            # 跳过子目录引用（指向 /tree/ 或 /blob/ 的不是 repo 根）
            if "/tree/" in url or "/blob/" in url:
                continue
            parts = url.rstrip("/").split("github.com/")[-1].split("/")
            if len(parts) >= 2:
                owner = parts[0]
                if owner in _SKIP_ORGS:
                    continue
                return owner, parts[1].removesuffix(".git")
        return None
    except Exception:
        return None


def _extract_import_packages(code: str) -> list[str]:
    """从代码片段里提取顶层包名（处理 import x / from x.y import z 两种形式）。"""
    packages = set()
    for m in re.finditer(r'^(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)', code, re.MULTILINE):
        pkg = m.group(1).replace("_", "-")  # pydantic_core → pydantic-core
        packages.add(pkg)
    return list(packages)


def retrieve_from_imported_deps(
    final_docs: list[str],
    doc_paths: dict[str, str],
    repo_path: str,
    owner: str,
    repo: str,
    queries: list[str],
) -> list[str]:
    """
    从 RAG 召回文件的 ChromaDB metadata 里读取 imports 字段，找到用了哪些依赖，
    clone + index 那些依赖，再对它们跑 RAG，返回候选文档列表。
    不做任何过滤，由 LLM 自己判断哪些 dep 代码与 bug 相关。
    """
    from context.repo_loader import ensure_repo_indexed
    from context.rag_index import get_repo_collection

    # 解析主库 pyproject.toml 得到允许的依赖集合
    allowed = {
        pkg.lower().replace("_", "-"): pkg
        for pkg in parse_dependencies(repo_path)
    }
    if not allowed:
        return []

    # 从 ChromaDB metadata 的 imports 字段读取（索引时存入，不依赖磁盘 clone）
    imported: set[str] = set()
    try:
        col = get_repo_collection(owner, repo)
        # 先从召回文档的 metadata 里取（通过 ids 查）
        for q in queries[:2]:
            res = col.query(
                query_texts=[q], n_results=8,
                where={"file_type": "source"},
                include=["metadatas"],
            )
            for meta in (res["metadatas"] or [[]])[0]:
                for pkg in meta.get("imports", "").split(","):
                    pkg = pkg.strip()
                    if pkg:
                        imported.add(pkg)
    except Exception as e:
        logger.warning(f"[DepIndexer] 读取 imports metadata 失败: {e}")
        return []

    # 只保留出现在 pyproject.toml 依赖列表里的包
    to_index = [pkg for pkg in imported if pkg in allowed]

    if not to_index:
        return ""

    # 解析每个 pkg 对应的 GitHub repo（网络请求，可并行）
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _resolve(pkg):
        result = resolve_github_repo(pkg)
        if not result:
            return None
        dep_owner, dep_repo = result
        if dep_owner == owner and dep_repo == repo:
            return None
        return pkg, dep_owner, dep_repo

    resolved = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in as_completed([ex.submit(_resolve, p) for p in to_index]):
            val = r.result()
            if val:
                resolved.append(val)

    if not resolved:
        return []

    # 并行 clone + index 每个 dep（各 dep 目录独立，无竞争）
    def _index_and_query(pkg, dep_owner, dep_repo):
        print(f"[DepIndexer] RAG 发现 import {pkg} → 索引 {dep_owner}/{dep_repo}")
        ensure_repo_indexed(dep_owner, dep_repo, _index_deps=False)
        try:
            col = get_repo_collection(dep_owner, dep_repo)
            if col.count() == 0:
                return []
            docs = []
            for q in queries[:2]:
                res = col.query(query_texts=[q], n_results=2, include=["documents"])
                docs.extend((res["documents"] or [[]])[0])
            return docs
        except Exception as e:
            print(f"[DepIndexer] 查询 {dep_owner}/{dep_repo} 失败: {e}")
            return []

    extra_docs: list[str] = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_index_and_query, *r): r for r in resolved}
        for fut in as_completed(futures):
            extra_docs.extend(fut.result())

    seen: set[str] = set()
    return [d for d in extra_docs if not (d in seen or seen.add(d))]


def index_dependencies(repo_path: str, owner: str, repo: str) -> None:
    """
    解析 repo_path 的直接依赖，自动 clone + index 找到的 GitHub 依赖。
    通过 _index_deps=False 调用 ensure_repo_indexed，不递归。
    """
    from context.repo_loader import ensure_repo_indexed

    packages = parse_dependencies(repo_path)
    if not packages:
        return

    print(f"[DepIndexer] {owner}/{repo} 解析到 {len(packages)} 个依赖，查找 GitHub 地址...")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    candidates = []
    for pkg in packages[:MAX_DEPS * 2]:
        result = resolve_github_repo(pkg)
        if not result:
            continue
        dep_owner, dep_repo = result
        if dep_owner == owner and dep_repo == repo:
            continue
        candidates.append((pkg, dep_owner, dep_repo))
        if len(candidates) >= MAX_DEPS:
            break

    def _do_index(pkg, dep_owner, dep_repo):
        print(f"[DepIndexer] {pkg} → {dep_owner}/{dep_repo}")
        return ensure_repo_indexed(dep_owner, dep_repo, _index_deps=False)

    indexed = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_do_index, *c): c for c in candidates}
        for fut in as_completed(futures):
            if fut.result():
                indexed += 1

    if indexed:
        print(f"[DepIndexer] 完成，共索引 {indexed} 个依赖")
