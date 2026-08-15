"""第三阶段临时 worktree、测试命令和环境隔离测试。"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch


PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""


def _git_repo(path: Path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "app.py").write_text("value = 1\n")
    (path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=path, check=True)


def test_detects_pytest_target_and_regression(tmp_path):
    from tools.ci_sandbox import detect_test_commands
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    result = detect_test_commands(tmp_path, ["tests/test_api.py::test_login"])
    assert result["targeted"][:4] == [sys.executable, "-m", "pytest", "-q"]
    assert "tests/test_api.py::test_login" in result["targeted"]
    assert result["regression"]


def test_full_pytest_is_not_run_twice_without_target(tmp_path):
    from tools.ci_sandbox import detect_test_commands
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    result = detect_test_commands(tmp_path, [])
    assert result["targeted"]
    assert result["regression"] == []


def test_rejects_untrusted_test_name(tmp_path):
    from tools.ci_sandbox import detect_test_commands
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    result = detect_test_commands(tmp_path, ["../../secret.py::test_x", "x; touch /tmp/pwn"])
    assert "../../secret.py::test_x" not in result["targeted"]
    assert "touch" not in " ".join(result["targeted"])


def test_clean_environment_drops_secrets(tmp_path):
    from tools.ci_sandbox import _clean_environment
    with patch.dict(os.environ, {"GITHUB_TOKEN": "secret", "OPENAI_API_KEY": "secret"}):
        env = _clean_environment(tmp_path / "home", tmp_path / "tmp")
    assert "GITHUB_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["CI"] == "true"


def test_non_macos_execution_is_blocked_by_default():
    from tools.ci_sandbox import _sandbox_command
    with patch("tools.ci_sandbox.platform.system", return_value="Linux"), \
         patch.dict(os.environ, {}, clear=True):
        command, mode = _sandbox_command(["python", "-V"])
    assert command == []
    assert mode == "unavailable"


def test_verify_patch_uses_and_cleans_worktree(tmp_path):
    from tools.ci_sandbox import verify_patch
    _git_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                         capture_output=True, text=True).stdout.strip()
    passed = {"status": "passed", "exit_code": 0, "duration": 0.1,
              "output": "ok", "sandbox_mode": "mock"}
    with patch("tools.ci_sandbox._run_test", return_value=passed):
        result = verify_patch(tmp_path, sha, PATCH, ["tests/test_x.py::test_x"])
    assert result["status"] == "passed"
    assert result["targeted"]["status"] == "passed"
    assert result["regression"]["status"] == "passed"
    assert (tmp_path / "app.py").read_text() == "value = 1\n"
    worktrees = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=tmp_path,
                               check=True, capture_output=True, text=True).stdout
    assert "repomind-ci-verify-" not in worktrees
