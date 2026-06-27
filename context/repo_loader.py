"""
仓库自动加载：确保某个 owner/repo 有 RAG 索引，没有就自动 clone + index。

ensure_repo_indexed(owner, repo) 是唯一对外接口，幂等调用。
"""
import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# per-repo 锁，防止并行线程重复 clone/index 同一仓库
_repo_locks: dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()


def _get_lock(key: str) -> threading.Lock:
    with _locks_mutex:
        if key not in _repo_locks:
            _repo_locks[key] = threading.Lock()
        return _repo_locks[key]


def ensure_repo_indexed(owner: str, repo: str, _index_deps: bool = True) -> bool:
    """
    确保 owner/repo 有 RAG 索引。
    - 已有索引 → 直接返回 True
    - 没有索引 → clone + index → 返回 True
    - 失败 → 返回 False（不抛异常，让分析继续，只是 RAG 为空）

    _index_deps=False 由依赖索引流程内部调用，防止无限递归。
    线程安全：per-repo 锁确保同一仓库不被多个线程并发 clone/index。
    """
    from context.rag_index import get_repo_collection, index_repo
    from config import settings

    key = f"{owner}/{repo}"

    # 快速路径：已有索引直接返回（不持锁，最常见路径）
    try:
        collection = get_repo_collection(owner, repo)
        if collection.count() > 0:
            return True
    except Exception:
        pass

    # 慢路径：需要 clone/index，持 per-repo 锁
    with _get_lock(key):
        # 持锁后再检查一次（可能刚被另一个线程完成了）
        try:
            collection = get_repo_collection(owner, repo)
            if collection.count() > 0:
                return True
        except Exception:
            pass

        try:
            repo_path = settings.clone_base / f"{owner}__{repo}"

            if not repo_path.exists():
                print(f"[RepoLoader] 开始 clone {key} → {repo_path}")
                _clone_repo(owner, repo, repo_path)
                print(f"[RepoLoader] clone 完成: {key}")

            if not repo_path.exists():
                print(f"[RepoLoader] clone 失败，跳过索引: {key}")
                return False

            print(f"[RepoLoader] 开始索引 {key} ...")
            index_repo(str(repo_path), owner=owner, repo_name=repo)
            print(f"[RepoLoader] 索引完成: {key}")
            return True

        except Exception as e:
            logger.warning(f"[RepoLoader] {key} 索引失败，分析将在无 RAG 情况下继续: {e}")
            return False


def _clone_repo(owner: str, repo: str, target: Path):
    """shallow clone 拉最近 200 条历史，兼顾速度与 git blame/log 可用性。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--depth=200",
         f"https://github.com/{owner}/{repo}.git",
         str(target)],
        check=True,
        timeout=300,
        capture_output=True,
    )
