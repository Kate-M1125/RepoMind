"""
Stack Trace 解析工具

从 Issue body 提取 Python/JS/Java 的 stack trace，
定位具体文件和行号，读取本地仓库对应的代码片段。
"""
import re
from pathlib import Path

# 本地仓库可能存放的路径前缀
_REPO_SEARCH_ROOTS = [
    Path("/tmp/repomind_repos"),
    Path("/tmp"),
]


def _find_local_repo(repo_name: str) -> Path | None:
    """在本地找到已 clone 的仓库目录"""
    safe = repo_name.replace("/", "__")
    repo_slug = repo_name.split("/")[-1]  # e.g. "fastapi"

    candidates = []
    for root in _REPO_SEARCH_ROOTS:
        candidates += [
            root / safe,
            root / repo_slug,
        ]

    for path in candidates:
        if path.exists() and path.is_dir():
            return path
    return None


def _extract_python_frames(text: str) -> list[dict]:
    """提取 Python traceback 中的文件帧"""
    # 匹配: File "path/to/file.py", line 42, in func_name
    pattern = r'File ["\']([^"\']+\.py)["\'],\s*line\s*(\d+)'
    frames = []
    for match in re.finditer(pattern, text):
        frames.append({
            "file": match.group(1),
            "line": int(match.group(2)),
            "lang": "python",
        })
    return frames


def _extract_js_frames(text: str) -> list[dict]:
    """提取 JS/TS stack trace 中的文件帧"""
    # 匹配: at funcName (path/to/file.js:42:10) 或 at path/to/file.js:42:10
    pattern = r'at\s+(?:\S+\s+)?\(?([\w./\\-]+\.[jt]sx?):(\d+):\d+\)?'
    frames = []
    for match in re.finditer(pattern, text):
        frames.append({
            "file": match.group(1),
            "line": int(match.group(2)),
            "lang": "javascript",
        })
    return frames


def _read_snippet(repo_path: Path, file_hint: str, line: int, context: int = 15) -> str | None:
    """在仓库里找到文件，读取指定行附近的代码片段"""
    # file_hint 可能是绝对路径或相对路径片段，尝试在仓库里匹配
    hint = Path(file_hint)

    # 优先：hint 相对于仓库根
    candidate = repo_path / hint
    if not candidate.exists():
        # 搜索仓库内匹配文件名的文件
        matches = list(repo_path.rglob(hint.name))
        # 选路径最短的（最可能是源码而不是缓存）
        matches = [m for m in matches if ".git" not in m.parts and "__pycache__" not in m.parts]
        if not matches:
            return None
        candidate = min(matches, key=lambda p: len(p.parts))

    try:
        lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = max(0, line - context - 1)
        end = min(len(lines), line + context)
        snippet = "\n".join(
            f"{i+1:4d} {'→' if i+1 == line else ' '} {l}"
            for i, l in enumerate(lines[start:end], start=start)
        )
        rel = candidate.relative_to(repo_path)
        return f"# {rel}  (附近 L{line})\n{snippet}"
    except Exception:
        return None


def extract_stack_context(issue_body: str, repo_name: str) -> str:
    """
    主入口：从 issue body 提取 stack trace，返回相关代码片段字符串。
    无 stack trace 或找不到本地仓库时返回空字符串。
    """
    all_frames = _extract_python_frames(issue_body) + _extract_js_frames(issue_body)
    if not all_frames:
        return ""

    repo_path = _find_local_repo(repo_name)
    if not repo_path:
        # 没有本地仓库，只返回帧摘要
        lines = [f"Stack trace 定位到以下位置（未找到本地仓库，无法读取代码）："]
        for f in all_frames[:5]:
            lines.append(f"  {f['file']}:{f['line']}")
        return "\n".join(lines)

    snippets = []
    seen_files = set()
    for frame in all_frames[:5]:  # 最多处理 5 帧
        key = (frame["file"], frame["line"])
        if key in seen_files:
            continue
        seen_files.add(key)

        snippet = _read_snippet(repo_path, frame["file"], frame["line"])
        if snippet:
            snippets.append(snippet)

    if not snippets:
        return ""

    return "## Stack Trace 精确定位\n\n" + "\n\n---\n\n".join(snippets)
