"""候选 Unified Diff 的纯解析与只读安全校验。"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath


# 候选补丁只是供人工审阅的第一版，主动限制规模，避免 LLM 做大范围重构。
MAX_PATCH_FILES = 5
MAX_CHANGED_LINES = 400
MAX_PATCH_CHARS = 100_000

# 第一版不允许 Agent 改 CI 权限、密钥或依赖锁文件，避免通过“关闭检查”修复 CI。
_PROTECTED_PREFIXES = (".github/workflows/", ".github/actions/")
_PROTECTED_NAMES = {
    ".env", ".env.local", ".env.production", "credentials.json",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
}


def extract_unified_diff(text: str) -> str:
    """从 LLM 文本或 Markdown 代码块中提取标准 git Unified Diff。"""
    text = text.strip()
    fenced = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    # 丢弃模型可能添加的解释文字，只保留 Git 能识别的补丁主体。
    start = text.find("diff --git ")
    return text[start:].strip() + "\n" if start >= 0 else ""


def _diff_paths(patch: str) -> list[tuple[str, str]]:
    return re.findall(r"^diff --git a/(.+?) b/(.+?)$", patch, re.MULTILINE)


def patch_path_changes(patch: str) -> list[tuple[str, str]]:
    """返回补丁 old/new 路径，供发布器正确处理 rename 和 delete。"""
    return _diff_paths(patch)


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\x00" not in value


def is_protected_path(value: str) -> bool:
    """判断文件是否禁止读取给补丁模型或禁止被候选补丁修改。"""
    if not _safe_relative_path(value):
        return True
    normalized = str(PurePosixPath(value))
    name = PurePosixPath(normalized).name
    return normalized.startswith(_PROTECTED_PREFIXES) or name in _PROTECTED_NAMES or name.startswith(".env.")


def inspect_patch(patch: str) -> dict:
    """不访问文件系统的策略检查，返回文件列表、规模、风险和错误。"""
    errors = []
    paths = _diff_paths(patch)
    files = []

    if not patch:
        errors.append("未生成标准 Unified Diff")
    if len(patch) > MAX_PATCH_CHARS:
        errors.append(f"补丁超过 {MAX_PATCH_CHARS} 字符限制")
    if not paths and patch:
        errors.append("缺少 diff --git 文件头")
    if len(paths) > MAX_PATCH_FILES:
        errors.append(f"补丁修改 {len(paths)} 个文件，超过 {MAX_PATCH_FILES} 个限制")

    # old/new 两侧都要检查；只检查新路径会漏掉 rename/delete 的路径攻击。
    for old_path, new_path in paths:
        for value in (old_path, new_path):
            if not _safe_relative_path(value):
                errors.append(f"不安全的补丁路径: {value}")
                continue
            normalized = str(PurePosixPath(value))
            if is_protected_path(normalized):
                errors.append(f"禁止修改受保护文件: {normalized}")
        if new_path not in files:
            files.append(new_path)

    # 文件头的 +++/--- 不属于代码改动，统计时显式排除。
    changed_lines = sum(
        1 for line in patch.splitlines()
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    if changed_lines > MAX_CHANGED_LINES:
        errors.append(f"补丁改动 {changed_lines} 行，超过 {MAX_CHANGED_LINES} 行限制")
    if "GIT binary patch" in patch or "Binary files " in patch:
        errors.append("不支持二进制补丁")

    # 任一策略错误均为 high；合法但范围较大的补丁标记为 medium，留给人工重点检查。
    risk = "high" if errors else ("medium" if changed_lines > 100 or len(files) > 2 else "low")
    return {
        "files": files,
        "changed_lines": changed_lines,
        "risk": risk,
        "errors": list(dict.fromkeys(errors)),
    }


def validate_patch(repo_root: Path, patch: str, base_sha: str = "") -> dict:
    """通过临时 Git Index 执行 apply --check；不触碰工作树和真实 Index。"""
    inspection = inspect_patch(patch)
    if inspection["errors"]:
        return {**inspection, "valid": False}
    if not (repo_root / ".git").exists():
        return {**inspection, "valid": False, "errors": ["本地仓库不存在，无法校验补丁"]}

    target = base_sha or "HEAD"
    if base_sha and not re.fullmatch(r"[0-9a-fA-F]{7,64}", base_sha):
        return {**inspection, "valid": False, "errors": ["CI 基准提交 SHA 格式非法"]}

    if base_sha:
        # Actions 常由 PR 分支触发，浅克隆未必包含 head_sha。
        exists = subprocess.run(
            ["git", "cat-file", "-e", f"{base_sha}^{{commit}}"], cwd=repo_root,
            capture_output=True, text=True, timeout=15,
        )
        if exists.returncode != 0:
            try:
                # 浅克隆通常没有 PR 分支提交；只获取单个对象，不切换分支或工作树。
                subprocess.run(
                    ["git", "fetch", "--depth=1", "origin", base_sha], cwd=repo_root,
                    check=True, capture_output=True, text=True, timeout=120,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                message = getattr(exc, "stderr", "") or str(exc)
                return {
                    **inspection, "valid": False,
                    "errors": [f"无法获取 CI 基准提交: {message.strip()[:500]}"],
                }

    # GIT_INDEX_FILE 指向临时文件，所以 read-tree/apply 都不会污染仓库真实暂存区。
    with tempfile.TemporaryDirectory(prefix="repomind-patch-check-") as temp_dir:
        index_file = Path(temp_dir) / "index"
        env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
        try:
            # 先把 CI 提交快照写入临时 Index，再只检查补丁能否应用。
            subprocess.run(
                ["git", "read-tree", target], cwd=repo_root, env=env,
                check=True, capture_output=True, text=True, timeout=30,
            )
            subprocess.run(
                ["git", "apply", "--cached", "--check", "-"],
                cwd=repo_root, env=env, input=patch,
                check=True, capture_output=True, text=True, timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            message = getattr(exc, "stderr", "") or str(exc)
            return {
                **inspection,
                "valid": False,
                "errors": [f"git apply --check 失败: {message.strip()[:500]}"],
            }

    return {**inspection, "valid": True}
