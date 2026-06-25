"""
仓库自动加载：确保某个 owner/repo 有 RAG 索引，没有就自动 clone + index。

ensure_repo_indexed(owner, repo) 是唯一对外接口，幂等调用。
"""
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 正在索引中的仓库，防止并发重复触发
_indexing: set[str] = set()


def ensure_repo_indexed(owner: str, repo: str, _index_deps: bool = True) -> bool:
    """
    确保 owner/repo 有 RAG 索引。
    - 已有索引 → 直接返回 True
    - 没有索引 → clone + index → 返回 True
    - 失败 → 返回 False（不抛异常，让分析继续，只是 RAG 为空）

    _index_deps=False 由依赖索引流程内部调用，防止无限递归。
    """
    from context.rag_index import get_repo_collection, index_repo
    from config import settings

    key = f"{owner}/{repo}"

    # 已在索引中，跳过
    if key in _indexing:
        logger.info(f"[RepoLoader] {key} 正在索引中，跳过重复触发")
        return False

    # 检查是否已有索引
    try:
        collection = get_repo_collection(owner, repo)
        if collection.count() > 0:
            return True
    except Exception:
        pass

    _indexing.add(key)
    try:
        repo_path = settings.clone_base / f"{owner}__{repo}"

        # clone（如果还没有）
        if not repo_path.exists():
            print(f"[RepoLoader] 开始 clone {key} → {repo_path}")
            _clone_repo(owner, repo, repo_path)
            print(f"[RepoLoader] clone 完成: {key}")

        if not repo_path.exists():
            print(f"[RepoLoader] clone 失败，跳过索引: {key}")
            return False

        # 建索引
        print(f"[RepoLoader] 开始索引 {key} ...")
        index_repo(str(repo_path), owner=owner, repo_name=repo)
        print(f"[RepoLoader] 索引完成: {key}")

        return True

    except Exception as e:
        logger.warning(f"[RepoLoader] {key} 索引失败，分析将在无 RAG 情况下继续: {e}")
        return False
    finally:
        _indexing.discard(key)


def _clone_repo(owner: str, repo: str, target: Path):
    """shallow clone，只拉最新一层历史，速度快。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth=1",
         f"https://github.com/{owner}/{repo}.git",
         str(target)],
        check=True,
        timeout=300,
        capture_output=True,
    )
