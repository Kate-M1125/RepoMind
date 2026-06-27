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


def _find_callers(owner: str, repo: str, func_name: str) -> str:
    """找到函数/方法的所有调用处，附带调用上下文。"""
    try:
        root = str(_repo_path(owner, repo))
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.rs",
             "--include=*.ts", "--include=*.go",
             "-m", "40", f"{func_name}(", root],
            capture_output=True, text=True, timeout=15,
        )
        lines = result.stdout.replace(root + "/", "").splitlines()
        # 过滤掉定义行本身
        callers = [
            l for l in lines
            if f"def {func_name}" not in l and f"class {func_name}" not in l
        ][:25]
        print(f"[CodeNav] find_callers '{func_name}' in {owner}/{repo} → {len(callers)} 处")
        if not callers:
            return f"未找到 '{func_name}' 的调用处（可能名称不对或只在动态调用中使用）"
        return "\n".join(callers)[:3000]
    except Exception as e:
        return f"查找失败: {e}"


def _find_definition(owner: str, repo: str, symbol: str) -> str:
    """精确定位函数/类的定义位置，并返回签名及前 20 行实现。"""
    try:
        root = str(_repo_path(owner, repo))
        # 同时匹配 def / class / async def
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.rs",
             "--include=*.ts", "--include=*.go",
             "-m", "10", f"def {symbol}\\|class {symbol}\\|async def {symbol}", root],
            capture_output=True, text=True, timeout=15,
        )
        hits = result.stdout.replace(root + "/", "").strip().splitlines()
        print(f"[CodeNav] find_definition '{symbol}' in {owner}/{repo} → {len(hits)} 处定义")
        if not hits:
            return f"未找到 '{symbol}' 的定义（检查拼写或尝试 grep_code）"

        # 对每个定义读取签名 + 前 20 行
        out_parts = []
        for hit in hits[:3]:
            # hit 格式: "path/to/file.py:42:def foo(..."
            parts = hit.split(":", 2)
            if len(parts) < 2:
                out_parts.append(hit)
                continue
            fpath, lineno = parts[0], parts[1]
            try:
                start = max(1, int(lineno))
                snippet = _read_file(owner, repo, fpath, start, start + 20)
                out_parts.append(snippet)
            except Exception:
                out_parts.append(hit)

        return "\n\n---\n\n".join(out_parts)[:4000]
    except Exception as e:
        return f"查找失败: {e}"


FIND_CALLERS = Tool(
    name="find_callers",
    description=(
        "找到某个函数/方法在整个仓库中被调用的所有位置。"
        "追踪调用链时使用：先用 find_definition 定位定义，再用 find_callers 看谁调用了它，"
        "逐层向上追溯直到找到异常入口。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner":     {"type": "string"},
            "repo":      {"type": "string"},
            "func_name": {"type": "string", "description": "函数或方法名，如 'process_request' 或 '_check_media'"},
        },
        "required": ["owner", "repo", "func_name"],
    },
    execute=_find_callers,
)

FIND_DEFINITION = Tool(
    name="find_definition",
    description=(
        "精确定位函数/类的定义文件和行号，并返回函数签名及实现开头。"
        "比 grep_code 更准确：直接跳到定义，适合快速了解一个符号的完整签名和入参。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner":  {"type": "string"},
            "repo":   {"type": "string"},
            "symbol": {"type": "string", "description": "函数名或类名，如 'BaseRequest' 或 'validate_model'"},
        },
        "required": ["owner", "repo", "symbol"],
    },
    execute=_find_definition,
)


def _git_log_file(owner: str, repo: str, path: str, n: int = 15) -> str:
    """查看某个文件的 git 提交历史。"""
    try:
        repo_path = str(_repo_path(owner, repo))
        result = subprocess.run(
            ["git", "log", f"--max-count={n}", "--pretty=format:%h %ad %s", "--date=short", "--", path],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        print(f"[CodeNav] git_log_file {owner}/{repo}/{path} → {len(out.splitlines())} commits")
        return out if out else f"文件 '{path}' 无提交历史（路径可能有误）"
    except Exception as e:
        return f"git log 失败: {e}"


def _git_blame(owner: str, repo: str, path: str, start_line: int, end_line: int) -> str:
    """查看文件指定行范围最后是哪个 commit 改的，返回 commit hash + 作者 + 日期 + 代码行。"""
    try:
        repo_path = str(_repo_path(owner, repo))
        result = subprocess.run(
            ["git", "blame", "-L", f"{start_line},{end_line}", "--date=short",
             "-e", path],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        out = result.stdout
        print(f"[CodeNav] git_blame {owner}/{repo}/{path} L{start_line}-{end_line}")
        return out[:3000] if out else f"blame 无结果（路径或行号有误）"
    except Exception as e:
        return f"git blame 失败: {e}"


def _search_commits(owner: str, repo: str, keyword: str, n: int = 20) -> str:
    """在 git commit message 里搜索关键词，找历史修复记录。"""
    try:
        repo_path = str(_repo_path(owner, repo))
        result = subprocess.run(
            ["git", "log", f"--max-count={n}", "--pretty=format:%h %ad %s",
             "--date=short", f"--grep={keyword}", "--regexp-ignore-case"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        out = result.stdout.strip()
        print(f"[CodeNav] search_commits '{keyword}' in {owner}/{repo} → {len(out.splitlines())} commits")
        return out if out else f"未找到包含 '{keyword}' 的 commit"
    except Exception as e:
        return f"搜索失败: {e}"


GIT_LOG_FILE = Tool(
    name="git_log_file",
    description=(
        "查看某个文件的 git 提交历史（最近 N 条）。"
        "用于回归 bug：找出'什么时候引入了这个改动'，确认 bug 是哪个版本引入的。"
        "返回：commit hash、日期、commit message。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo":  {"type": "string"},
            "path":  {"type": "string", "description": "文件相对路径，如 aiohttp/web_request.py"},
            "n":     {"type": "integer", "description": "最多返回几条，默认 15"},
        },
        "required": ["owner", "repo", "path"],
    },
    execute=_git_log_file,
)

GIT_BLAME = Tool(
    name="git_blame",
    description=(
        "查看文件某几行最后是哪个 commit 修改的，以及修改时间和作者。"
        "先用 find_definition 或 read_file 定位可疑行号，再用 git_blame 确认是哪次提交引入的问题。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner":      {"type": "string"},
            "repo":       {"type": "string"},
            "path":       {"type": "string", "description": "文件相对路径"},
            "start_line": {"type": "integer", "description": "起始行号"},
            "end_line":   {"type": "integer", "description": "结束行号"},
        },
        "required": ["owner", "repo", "path", "start_line", "end_line"],
    },
    execute=_git_blame,
)

SEARCH_COMMITS = Tool(
    name="search_commits",
    description=(
        "在 git commit message 里搜索关键词，找相关历史修复或改动记录。"
        "适合：'这个功能是什么时候加的'、'之前有没有修过相关 bug'、找引入 breaking change 的 commit。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "owner":   {"type": "string"},
            "repo":    {"type": "string"},
            "keyword": {"type": "string", "description": "搜索关键词，如 'MediaPipeline' 或 'redirect 303'"},
            "n":       {"type": "integer", "description": "最多返回几条，默认 20"},
        },
        "required": ["owner", "repo", "keyword"],
    },
    execute=_search_commits,
)
