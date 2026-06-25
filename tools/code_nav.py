"""
代码导航工具：让 LLM 像 coding agent 一样主动读文件、搜符号、追调用链。
"""
import subprocess
from pathlib import Path
from tools.base import Tool


def _repo_path(owner: str, repo: str) -> Path:
    from config import settings
    from context.repo_loader import ensure_repo_indexed
    path = settings.clone_base / f"{owner}__{repo}"
    if not path.exists():
        ensure_repo_indexed(owner, repo)
    return path


def _read_file(owner: str, repo: str, path: str, start_line: int = 1, end_line: int = 120) -> str:
    try:
        full = _repo_path(owner, repo) / path
        if not full.exists():
            return f"文件不存在: {path}"
        lines = full.read_text(errors="ignore").splitlines()
        total = len(lines)
        chunk = lines[start_line - 1:end_line]
        header = f"# {owner}/{repo}: {path} (L{start_line}-{min(end_line, total)}, 共 {total} 行)\n\n"
        print(f"[CodeNav] read_file {owner}/{repo}/{path} L{start_line}-{end_line}")
        return header + "\n".join(chunk)
    except Exception as e:
        return f"读取失败: {e}"


def _grep_code(owner: str, repo: str, pattern: str) -> str:
    try:
        root = str(_repo_path(owner, repo))
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.rs",
             "--include=*.ts", "--include=*.go", "-m", "30", pattern, root],
            capture_output=True, text=True, timeout=15,
        )
        out = result.stdout.replace(root + "/", "")
        print(f"[CodeNav] grep '{pattern}' in {owner}/{repo} → {len(out.splitlines())} 行")
        return out[:3000] if out else f"未找到 '{pattern}'"
    except Exception as e:
        return f"grep 失败: {e}"


def _list_dir(owner: str, repo: str, path: str = "") -> str:
    try:
        base = _repo_path(owner, repo)
        target = base / path if path else base
        if not target.exists():
            return f"目录不存在: {path}"
        entries = sorted(target.iterdir(), key=lambda e: (e.is_file(), e.name))
        lines = [("  " if e.is_file() else "/ ") + e.name for e in entries[:60]]
        print(f"[CodeNav] list_dir {owner}/{repo}/{path}")
        return f"# {owner}/{repo}/{path}\n" + "\n".join(lines)
    except Exception as e:
        return f"列目录失败: {e}"


READ_FILE = Tool(
    name="read_file",
    description=(
        "读取仓库中某个文件的内容。追踪调用链时，用它查看具体实现。"
        "可指定行范围避免读取过长文件。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner":      {"type": "string", "description": "GitHub owner"},
            "repo":       {"type": "string", "description": "仓库名"},
            "path":       {"type": "string", "description": "文件相对路径，如 src/validators/prebuilt.rs"},
            "start_line": {"type": "integer", "description": "起始行（默认 1）"},
            "end_line":   {"type": "integer", "description": "结束行（默认 120）"},
        },
        "required": ["owner", "repo", "path"],
    },
    execute=_read_file,
)

GREP_CODE = Tool(
    name="grep_code",
    description=(
        "在仓库中搜索符号定义或调用。想知道某个函数/类在哪里定义、"
        "或者某个符号在哪里被调用时使用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner":   {"type": "string"},
            "repo":    {"type": "string"},
            "pattern": {"type": "string", "description": "搜索模式，如 'def model_rebuild' 或 'PrebuiltValidator'"},
        },
        "required": ["owner", "repo", "pattern"],
    },
    execute=_grep_code,
)

LIST_DIR = Tool(
    name="list_dir",
    description="列出仓库目录结构。不确定文件在哪个目录时先用它了解项目布局。",
    parameters={
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo":  {"type": "string"},
            "path":  {"type": "string", "description": "目录路径（默认根目录）"},
        },
        "required": ["owner", "repo"],
    },
    execute=_list_dir,
)
