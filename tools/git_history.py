"""
Git History Tool：从克隆的仓库里拉取最近的 commit diff，
用于回归 bug 的根因分析。

fetch_recent_diff(repo_path, issue_text, max_commits=20) → str
  返回最相关的 commit diff 片段，注入给 LLM。
"""
import re
import subprocess
from pathlib import Path


def fetch_recent_diff(repo_path: str, issue_text: str, max_commits: int = 20) -> str:
    """
    拉取仓库最近 N 个 commits 的摘要，再找其中与 Issue 最相关的 commit 拉完整 diff。
    返回拼接好的字符串，适合直接注入 LLM prompt。
    """
    if not Path(repo_path).exists():
        return ""

    # Step 1: 拿最近 N 个 commit 的 hash + 标题
    commits = _get_recent_commits(repo_path, max_commits)
    if not commits:
        return ""

    # Step 2: 从 Issue 文字里提取版本号，找最接近的 commit
    version = _extract_version(issue_text)
    relevant = _find_relevant_commits(commits, issue_text, version, top_k=3)

    # Step 3: 拉这几个 commit 的 diff（只看 .py 文件，限长度）
    parts = []
    for commit_hash, commit_title in relevant:
        diff = _get_commit_diff(repo_path, commit_hash)
        if diff:
            parts.append(f"## commit {commit_hash[:8]}: {commit_title}\n{diff}")

    return "\n\n".join(parts)


def _get_recent_commits(repo_path: str, n: int) -> list[tuple[str, str]]:
    """返回 [(hash, title), ...] 列表。"""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={n}", "--pretty=format:%H\t%s"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        commits = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                commits.append((parts[0], parts[1]))
        return commits
    except Exception:
        return []


def _extract_version(text: str) -> str:
    """从文字里提取第一个版本号，如 v0.115 / 0.115.0。"""
    match = re.search(r'v?(\d+\.\d+(?:\.\d+)?)', text)
    return match.group(0) if match else ""


def _find_relevant_commits(
    commits: list[tuple[str, str]],
    issue_text: str,
    version: str,
    top_k: int = 3,
) -> list[tuple[str, str]]:
    """
    简单相关性排序：
    - commit title 含版本号 → 高权重
    - commit title 含 fix/bug/regression → 高权重
    - 其余按时间顺序取最近的
    """
    scored = []
    issue_lower = issue_text.lower()
    for h, title in commits:
        score = 0
        t = title.lower()
        if version and version.lstrip("v") in t:
            score += 3
        if any(kw in t for kw in ["fix", "bug", "regression", "revert", "broke"]):
            score += 2
        # 提取 commit title 里的词和 issue 有多少重叠
        words = set(re.findall(r'\w{4,}', t))
        issue_words = set(re.findall(r'\w{4,}', issue_lower))
        score += len(words & issue_words)
        scored.append((score, h, title))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [(h, title) for _, h, title in scored[:top_k]]


def _get_commit_diff(repo_path: str, commit_hash: str, max_chars: int = 1500) -> str:
    """拉单个 commit 的 diff，只保留 .py 文件，截断到 max_chars。"""
    try:
        result = subprocess.run(
            ["git", "show", commit_hash, "--stat", "--patch", "--", "*.py"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        return result.stdout[:max_chars]
    except Exception:
        return ""


def fetch_blame_diff(repo_path: str, rag_context: str, max_chars: int = 2000) -> str:
    """
    从 RAG 召回的代码片段里提取文件路径和行号，
    用 git blame 找到最后改动这些行的 commit，再 git show 拿 diff。
    适用于没有版本号的回归 bug。
    """
    if not Path(repo_path).exists():
        return ""

    # 从 RAG context 的注释头解析文件路径和行号
    # 格式: "# fastapi/dependencies/utils.py (L695-710)"
    file_ranges = _extract_file_ranges(rag_context)
    if not file_ranges:
        return ""

    seen_commits: set[str] = set()
    parts = []

    for file_path, start_line, end_line in file_ranges[:3]:
        commits = _blame_lines(repo_path, file_path, start_line, end_line)
        for commit_hash, commit_title in commits:
            if commit_hash in seen_commits:
                continue
            seen_commits.add(commit_hash)
            diff = _get_commit_diff(repo_path, commit_hash, max_chars=800)
            if diff:
                parts.append(f"## commit {commit_hash[:8]}: {commit_title}\n{diff}")
        if len(parts) >= 3:
            break

    result = "\n\n".join(parts)
    return result[:max_chars]


def _extract_file_ranges(rag_context: str) -> list[tuple[str, int, int]]:
    """
    从 RAG context 文本里解析出 (file_path, start_line, end_line)。
    匹配格式: "# path/to/file.py (L10-30)" 或 "# path/to/file.py:L10-30"
    """
    pattern = re.compile(
        r'#\s+([\w/./_\-]+\.py)\s*[:(]L?(\d+)[-–](\d+)'
    )
    results = []
    for m in pattern.finditer(rag_context):
        file_path = m.group(1)
        start, end = int(m.group(2)), int(m.group(3))
        results.append((file_path, start, end))
    return results


def _blame_lines(
    repo_path: str,
    file_path: str,
    start_line: int,
    end_line: int,
) -> list[tuple[str, str]]:
    """
    对文件的指定行范围跑 git blame，返回 [(commit_hash, commit_title), ...] 去重列表。
    """
    try:
        result = subprocess.run(
            ["git", "blame", "-L", f"{start_line},{end_line}",
             "--porcelain", file_path],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        # porcelain 格式：hash 行是 40 位十六进制 + 空格 + 数字
        commits: dict[str, str] = {}
        current_hash = ""
        hash_re = re.compile(r'^([0-9a-f]{40}) \d+ \d+')
        for line in result.stdout.splitlines():
            m = hash_re.match(line)
            if m:
                current_hash = m.group(1)
            elif line.startswith("summary ") and current_hash:
                commits[current_hash] = line[8:]
        return list(commits.items())
    except Exception:
        return []
