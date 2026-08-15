"""第二阶段候选补丁生成与安全校验测试。"""
import subprocess
from pathlib import Path
from unittest.mock import patch


VALID_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


# 纯文本解析与策略测试不依赖 LLM、GitHub 或项目配置。
def test_extract_diff_from_markdown():
    from tools.patch_validator import extract_unified_diff
    result = extract_unified_diff(f"说明\n```diff\n{VALID_PATCH}```")
    assert result.startswith("diff --git")
    assert "+value = 2" in result


def test_rejects_workflow_change():
    from tools.patch_validator import inspect_patch
    unsafe = VALID_PATCH.replace("app.py", ".github/workflows/ci.yml")
    result = inspect_patch(unsafe)
    assert result["risk"] == "high"
    assert any("受保护文件" in error for error in result["errors"])


def test_env_variants_are_protected_before_generation():
    from tools.patch_validator import is_protected_path
    assert is_protected_path(".env") is True
    assert is_protected_path("config/.env.staging") is True
    assert is_protected_path("src/settings.py") is False


def test_rejects_path_traversal():
    from tools.patch_validator import inspect_patch
    unsafe = VALID_PATCH.replace("app.py", "../outside.py")
    assert any("不安全" in error for error in inspect_patch(unsafe)["errors"])


def test_git_apply_check_uses_temporary_index(tmp_path: Path):
    """校验成功后真实文件和真实暂存区都必须保持干净。"""
    from tools.patch_validator import validate_patch
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    result = validate_patch(tmp_path, VALID_PATCH, head_sha)
    assert result["valid"] is True
    # 校验不能真正修改工作树。
    assert (tmp_path / "app.py").read_text() == "value = 1\n"
    assert subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path,
                          check=True, capture_output=True, text=True).stdout == ""


def test_rejects_invalid_base_sha(tmp_path: Path):
    from tools.patch_validator import validate_patch
    (tmp_path / ".git").mkdir()
    result = validate_patch(tmp_path, VALID_PATCH, "HEAD; rm -rf /")
    assert result["valid"] is False
    assert any("SHA 格式非法" in error for error in result["errors"])


def test_generate_patch_skips_low_confidence():
    # workflow.nodes 包依赖完整项目环境；测试只验证节点的安全门逻辑。
    from workflow.nodes.ci import generate_patch
    result = generate_patch({"confidence": 0.3, "patch_attempt": 0})
    assert result["patch_status"] == "skipped"


def test_generate_patch_reuses_locked_patch_without_llm():
    """阶段升级必须复用用户已看到的补丁，不能再次调用生成模型。"""
    import hashlib
    from workflow.nodes.ci import generate_patch

    patch_text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
    digest = hashlib.sha256(patch_text.encode()).hexdigest()
    result = generate_patch({
        "patch_locked": True, "patch": patch_text,
        "expected_patch_sha256": digest,
    })
    assert result["patch"] == patch_text
    assert result["patch_sha256"] == digest


def test_generate_patch_blocks_tampered_locked_patch():
    from workflow.nodes.ci import generate_patch

    result = generate_patch({
        "patch_locked": True, "patch": "changed", "expected_patch_sha256": "bad",
    })
    assert result["patch_status"] == "blocked"
    assert result["patch_risk"] == "high"
    assert result["patch"] == ""


def test_generate_patch_extracts_llm_diff(tmp_path: Path):
    from workflow.nodes.ci import generate_patch
    (tmp_path / "app.py").write_text("value = 1\n")
    state = {
        "owner": "acme", "repo": "widget", "confidence": 0.9,
        "suspected_files": ["app.py"], "changed_files": [],
        "root_cause": "wrong value", "fix_suggestion": "use 2",
        "failing_tests": [], "error_signatures": [], "patch_attempt": 0,
        "patch_history": [],
    }
    with patch("workflow.nodes.ci.settings.clone_base", tmp_path.parent), \
         patch("workflow.nodes.ci._safe_repo_file", return_value=tmp_path / "app.py"), \
         patch("workflow.nodes.ci.chat", return_value=VALID_PATCH):
        result = generate_patch(state)
    assert result["patch_status"] == "generated"
    assert result["patch"].startswith("diff --git")
