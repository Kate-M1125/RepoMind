"""
Call Graph 构建与查询。

build_call_graph(repo_path) → 解析所有 .py 文件，返回两张表：
  callee_map:  { "func_name": ["called_func1", "called_func2", ...] }
  location_map: { "func_name": "path/to/file.py:L10-30" }

expand_with_callees(func_names, repo_path, source_lines=80) → str
  给定一组函数名，查 callee_map 找它们调用的函数，
  再从源码里截取那些被调函数的代码，拼成字符串返回给 LLM。
"""
import ast
import json
import re
from pathlib import Path
from typing import Optional

_CACHE: dict[str, dict] = {}  # repo_path → {"callee_map": ..., "location_map": ...}


# ── 构建 ─────────────────────────────────────────────────────────────

def build_call_graph(repo_path: str) -> dict:
    """解析仓库所有 .py 文件，返回 callee_map + location_map。结果缓存在内存。"""
    if repo_path in _CACHE:
        return _CACHE[repo_path]

    # 尝试从磁盘加载缓存
    cache_file = Path(repo_path) / ".repomind_callgraph.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            _CACHE[repo_path] = data
            return data
        except Exception:
            pass

    callee_map: dict[str, list[str]] = {}
    location_map: dict[str, str] = {}  # func_name → "file.py:Lstart-end"

    for py_file in Path(repo_path).rglob("*.py"):
        if any(p in py_file.parts for p in [".git", "__pycache__", ".venv", "node_modules"]):
            continue
        try:
            source = py_file.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(source)
        except Exception:
            continue

        rel = str(py_file.relative_to(repo_path))
        _extract_from_tree(tree, rel, callee_map, location_map)

    data = {"callee_map": callee_map, "location_map": location_map}
    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        pass

    _CACHE[repo_path] = data
    return data


def _extract_from_tree(tree, rel_path: str, callee_map: dict, location_map: dict):
    """遍历 AST，记录每个函数的定义位置和它调用的函数名。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_name = node.name
        end = getattr(node, "end_lineno", node.lineno + 20)
        location_map[func_name] = f"{rel_path}:L{node.lineno}-{end}"

        # 收集这个函数体内所有的 Call 节点
        callees: list[str] = []
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            name = _call_name(child)
            if name and name != func_name:
                callees.append(name)

        if callees:
            callee_map[func_name] = list(dict.fromkeys(callees))  # 去重保序


def _call_name(node: ast.Call) -> Optional[str]:
    """从 Call AST 节点提取被调用的函数名（简单名或最后一段属性名）。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ── 查询 ─────────────────────────────────────────────────────────────

def extract_func_names(text: str) -> list[str]:
    """从代码片段或 Issue 文本中提取疑似函数/方法名（snake_case 或 camelCase）。"""
    # 匹配 Python 风格函数名（至少含一个下划线或全小写 3+ 字符）
    candidates = re.findall(r'\b([a-z_][a-z0-9_]{2,})\s*[\(\[]', text)
    # 去掉 Python 关键字和常见内置
    SKIP = {"for", "if", "while", "with", "not", "and", "or", "in", "len",
            "str", "int", "list", "dict", "set", "type", "print", "return",
            "import", "from", "def", "class", "try", "except", "raise"}
    return [c for c in dict.fromkeys(candidates) if c not in SKIP]


def expand_with_callees(
    func_names: list[str],
    repo_path: str,
    max_funcs: int = 5,
) -> str:
    """
    给定一组函数名，查 callee_map 找它们调用的函数，
    再从源文件里读取那些被调函数的完整代码，返回拼接字符串。
    """
    cg = build_call_graph(repo_path)
    callee_map = cg["callee_map"]
    location_map = cg["location_map"]

    # 找所有 callee
    callee_names: list[str] = []
    for fn in func_names:
        callee_names.extend(callee_map.get(fn, []))
    callee_names = list(dict.fromkeys(callee_names))  # 去重

    # 过滤掉没有 location 的（外部库函数）
    callee_names = [c for c in callee_names if c in location_map][:max_funcs]

    if not callee_names:
        return ""

    snippets: list[str] = []
    for callee in callee_names:
        loc = location_map[callee]  # "path/file.py:L10-30"
        snippet = _read_func_source(repo_path, loc)
        if snippet:
            snippets.append(f"# {loc} [{callee}]\n{snippet}")

    return "\n\n---\n\n".join(snippets)


def _read_func_source(repo_path: str, location: str) -> str:
    """按 location 字符串读取源码片段，最多 60 行。"""
    try:
        # location 格式: "fastapi/routing.py:L10-30"
        match = re.match(r"(.+):L(\d+)-(\d+)", location)
        if not match:
            return ""
        rel_path, start, end = match.group(1), int(match.group(2)), int(match.group(3))
        lines = (Path(repo_path) / rel_path).read_text(encoding="utf-8", errors="ignore").splitlines()
        snippet_lines = lines[start - 1: min(end, start + 60)]
        return "\n".join(snippet_lines)
    except Exception:
        return ""
