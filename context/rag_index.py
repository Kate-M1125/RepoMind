import sys
import ast
import re
from pathlib import Path
from typing import Any, Optional

EXTENSIONS = {".py", ".ts", ".js", ".md", ".go", ".java", ".rs"}

# 文件路径前缀 → file_type
_DOC_PREFIXES  = ("docs/", "doc/", "README", "CHANGELOG", "CONTRIBUTING")
_TEST_PREFIXES = ("tests/", "test/", "spec/", "e2e/", "__tests__/")


def _classify_file(rel_path: str) -> str:
    p = rel_path.lower()
    if any(p.startswith(pfx.lower()) or p == pfx.lower() for pfx in _DOC_PREFIXES):
        return "doc"
    if any(p.startswith(pfx.lower()) for pfx in _TEST_PREFIXES):
        return "test"
    # docs_src 是 FastAPI 的文档示例，算 doc
    if p.startswith("docs_src/"):
        return "doc"
    return "source"

# 懒加载 ChromaDB 客户端单例
_chroma_client: Optional[Any] = None


def _get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        from config import settings
        _chroma_client = chromadb.PersistentClient(path=str(settings.db_path))
    return _chroma_client


def _make_ef():
    from chromadb.utils import embedding_functions
    return embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-MiniLM-L6-v2"
    )


def get_repo_collection(owner: str, repo: str):
    """按 owner/repo 动态创建独立 collection，彻底隔离不同仓库的向量数据。"""
    # ChromaDB collection 名称只允许 [a-zA-Z0-9_-]，长度 3-63
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", f"{owner}__{repo}")[:63]
    return _get_chroma_client().get_or_create_collection(name, embedding_function=_make_ef())


def get_collection(owner: str = "default", repo: str = "repo"):
    """向后兼容接口，无 owner/repo 时使用默认 collection。"""
    return get_repo_collection(owner, repo)


def chunk_python(rel_path: str, source: str) -> list[dict]:
    """按函数/类切片，每个定义一个 chunk"""
    chunks = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return chunk_fixed(rel_path, source)

    lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if not hasattr(node, "lineno"):
            continue
        start = node.lineno - 1
        end = getattr(node, "end_lineno", start + 30)
        snippet = "\n".join(lines[start:end])
        if len(snippet.strip()) < 30:
            continue
        chunks.append({
            "id": f"{rel_path}:L{node.lineno}-{end}",
            "text": f"# {rel_path} (L{node.lineno}-{end})\n\n{snippet}",
            "metadata": {"path": rel_path, "start_line": node.lineno, "end_line": end},
        })
    return chunks or chunk_fixed(rel_path, source)


def chunk_markdown(rel_path: str, source: str) -> list[dict]:
    """按 ## 标题切片"""
    chunks = []
    sections = []
    current_title = ""
    current_lines = []

    for line in source.splitlines():
        if line.startswith("## "):
            if current_lines:
                sections.append((current_title, "\n".join(current_lines)))
            current_title = line
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_title, "\n".join(current_lines)))

    for i, (title, content) in enumerate(sections):
        if len(content.strip()) < 30:
            continue
        chunk_id = f"{rel_path}#section-{i}"
        chunks.append({
            "id": chunk_id,
            "text": f"# {rel_path}\n\n{content[:2000]}",
            "metadata": {"path": rel_path, "section": title},
        })
    return chunks or chunk_fixed(rel_path, source)


def chunk_fixed(rel_path: str, source: str, window: int = 500, overlap: int = 50) -> list[dict]:
    """固定行数滑动窗口切片"""
    lines = source.splitlines()
    chunks = []
    step = window - overlap
    for i in range(0, len(lines), step):
        snippet = "\n".join(lines[i:i + window])
        if len(snippet.strip()) < 30:
            continue
        chunks.append({
            "id": f"{rel_path}:L{i+1}-{i+window}",
            "text": f"# {rel_path} (L{i+1}-{i+window})\n\n{snippet[:2000]}",
            "metadata": {"path": rel_path, "start_line": i + 1},
        })
    return chunks


def chunk_file(rel_path: str, source: str) -> list[dict]:
    suffix = Path(rel_path).suffix
    if suffix == ".py":
        return chunk_python(rel_path, source)
    elif suffix == ".md":
        return chunk_markdown(rel_path, source)
    else:
        return chunk_fixed(rel_path, source)


def index_repo(repo_path: str, owner: str = "default", repo_name: str = "repo"):
    """索引仓库到独立 collection。owner/repo_name 决定 collection 名称。"""
    collection = get_repo_collection(owner, repo_name)
    repo = Path(repo_path)
    file_count = 0
    chunk_count = 0

    for file in repo.rglob("*"):
        if file.suffix not in EXTENSIONS:
            continue
        if any(p in file.parts for p in [".git", "node_modules", "__pycache__", ".venv"]):
            continue
        try:
            source = file.read_text(encoding="utf-8", errors="ignore").strip()
            if len(source) < 50:
                continue
            rel_path = str(file.relative_to(repo))
            chunks = chunk_file(rel_path, source)
            # .py 文件：从文件顶部提取 import 包名存进 metadata
            top_imports = ""
            if file.suffix == ".py":
                import re as _re
                head = source[:2000]
                pkgs = {
                    m.group(1).replace("_", "-")
                    for m in _re.finditer(r'^(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)', head, _re.MULTILINE)
                }
                top_imports = ",".join(sorted(pkgs))
            for chunk in chunks:
                meta = {**chunk["metadata"], "owner": owner, "repo": repo_name,
                        "file_type": _classify_file(rel_path),
                        "imports": top_imports}
                collection.upsert(
                    ids=[chunk["id"]],
                    documents=[chunk["text"]],
                    metadatas=[meta],
                )
                chunk_count += 1
            file_count += 1
            print(f"indexed {len(chunks):3d} chunks: {rel_path}")
        except Exception as e:
            print(f"skip {file}: {e}")

    print(f"\n完成：{file_count} 个文件，{chunk_count} 个 chunks")

    # 顺带构建 Call Graph（结果缓存到磁盘，后续检索直接复用）
    try:
        from context.call_graph import build_call_graph
        build_call_graph(str(repo))
        print("Call Graph 构建完成")
    except Exception as e:
        print(f"Call Graph 构建失败（不影响 RAG）: {e}")


if __name__ == "__main__":
    repo_path = sys.argv[1] if len(sys.argv) > 1 else "."
    index_repo(repo_path)
