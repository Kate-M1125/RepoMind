"""在临时 Git worktree 中应用候选补丁并运行受限测试。"""
from __future__ import annotations

import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath


TEST_TIMEOUT_SECONDS = 180
MAX_OUTPUT_CHARS = 30_000
# 这里只允许测试路径、pytest 的 :: 节点和参数化用的 []，不接受空格、分号或 Shell 元字符。
_SAFE_TEST_SPEC = re.compile(r"^[A-Za-z0-9_./:\[\]-]+$")


def _safe_test_name(value: str) -> bool:
    """测试名称来自不可信 CI 日志，只接受相对路径式字符集合。"""
    if not value or not _SAFE_TEST_SPEC.fullmatch(value):
        return False
    path_part = value.split("::", 1)[0]
    path = PurePosixPath(path_part)
    return not path.is_absolute() and ".." not in path.parts


def detect_test_commands(repo_root: Path, failing_tests: list[str]) -> dict:
    """从仓库清单和失败测试中构造 argv；从不执行日志或 LLM 提供的任意命令。"""
    safe_tests = [item for item in failing_tests if _safe_test_name(item)]

    # Python 始终通过当前解释器的 -m pytest 启动，避免 PATH 中同名可执行文件被劫持。
    if (repo_root / "pyproject.toml").exists() or (repo_root / "pytest.ini").exists() or safe_tests:
        target = [sys.executable, "-m", "pytest", "-q"]
        if safe_tests:
            target.extend(safe_tests[:10])
        regression = [sys.executable, "-m", "pytest", "-q"] if safe_tests else []
        return {"targeted": target, "regression": regression}

    # 只认可 package.json 中已存在的 test script，不执行日志中出现的 npm 命令。
    package_json = repo_root / "package.json"
    if package_json.exists():
        try:
            scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        if "test" in scripts:
            manager = "npm"
            if (repo_root / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
                manager = "pnpm"
            elif (repo_root / "yarn.lock").exists() and shutil.which("yarn"):
                manager = "yarn"
            targeted = [manager, "test"]
            js_tests = [item.split("::", 1)[0] for item in safe_tests]
            if js_tests:
                targeted.extend(["--", *js_tests[:10]])
            regression = [manager, "test"] if js_tests else []
            return {"targeted": targeted, "regression": regression}

    # Go/Cargo 暂不从日志推导包名，第一版使用工具自身的全仓测试入口。
    if (repo_root / "go.mod").exists():
        return {"targeted": ["go", "test", "./..."], "regression": []}
    if (repo_root / "Cargo.toml").exists():
        return {"targeted": ["cargo", "test"], "regression": []}
    return {"targeted": [], "regression": []}


def _clean_environment(home_dir: Path, temp_dir: Path) -> dict[str, str]:
    """使用白名单环境，避免把 GitHub/LLM Token 传给不可信测试进程。"""
    # 不继承用户完整环境；尤其不传递包含 TOKEN/KEY/SECRET 的变量。
    allowed = {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "COMSPEC", "PATHEXT"}
    env = {key: value for key, value in os.environ.items() if key in allowed and value}
    # HOME/TMPDIR 指向任务临时目录，避免测试工具读取用户级配置或写入真实 HOME。
    env.update({
        "HOME": str(home_dir),
        "TMPDIR": str(temp_dir),
        "CI": "true",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_INDEX": "1",
        "npm_config_offline": "true",
    })
    return env


def _resource_limits():
    """子进程启动前设置 CPU、内存、输出文件和进程数上限。"""
    # timeout 控制墙上时间；RLIMIT_CPU 单独限制实际占用 CPU 的时间。
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    except (ValueError, OSError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
    except (ValueError, OSError):
        pass


def _sandbox_command(command: list[str], cwd: Path | None = None) -> tuple[list[str], str]:
    """macOS 使用 sandbox-exec，Linux 优先使用 bubblewrap 创建无网络命名空间。"""
    # macOS 原生 profile 明确拒绝所有网络操作，依赖必须提前安装在本机。
    if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
        profile = "(version 1) (allow default) (deny network*)"
        return ["sandbox-exec", "-p", profile, *command], "macos-network-denied"
    if platform.system() == "Linux" and shutil.which("bwrap"):
        writable = (cwd or Path.cwd()).resolve().parent
        return [
            "bwrap", "--die-with-parent", "--unshare-net",
            "--ro-bind", "/", "/", "--bind", str(writable), str(writable),
            "--dev", "/dev", "--proc", "/proc", "--", *command,
        ], "linux-bwrap-network-denied"
    # 其他平台不会静默降级；必须由操作者再次显式接受“仅进程限制”的风险。
    if os.getenv("REPOMIND_ALLOW_UNSANDBOXED_TESTS", "").lower() in {"1", "true", "yes"}:
        return command, "process-limits-only"
    return [], "unavailable"


def _run_test(command: list[str], cwd: Path, env: dict[str, str]) -> dict:
    """执行单条已识别测试命令，并把所有异常转换为可写入报告的结构化结果。"""
    wrapped, mode = _sandbox_command(command, cwd)
    if not wrapped:
        return {"status": "blocked", "exit_code": None, "duration": 0.0,
                "output": "当前平台没有可用的网络隔离器。", "sandbox_mode": mode}
    started = time.monotonic()
    try:
        result = subprocess.run(
            wrapped, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=TEST_TIMEOUT_SECONDS, preexec_fn=_resource_limits,
        )
        output = (result.stdout + "\n" + result.stderr)[-MAX_OUTPUT_CHARS:]
        return {"status": "passed" if result.returncode == 0 else "failed",
                "exit_code": result.returncode, "duration": round(time.monotonic() - started, 2),
                "output": output.strip(), "sandbox_mode": mode}
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + "\n" + (exc.stderr or ""))[-MAX_OUTPUT_CHARS:]
        return {"status": "timeout", "exit_code": None,
                "duration": round(time.monotonic() - started, 2),
                "output": output.strip(), "sandbox_mode": mode}
    except OSError as exc:
        return {"status": "blocked", "exit_code": None,
                "duration": round(time.monotonic() - started, 2),
                "output": f"无法启动测试命令: {exc}", "sandbox_mode": mode}


def verify_patch(repo_root: Path, base_sha: str, patch: str, failing_tests: list[str]) -> dict:
    """完整验证生命周期：临时 worktree → apply → 目标测试 → 回归测试 → 清理。"""
    with tempfile.TemporaryDirectory(prefix="repomind-ci-verify-") as parent:
        parent_path = Path(parent)
        worktree = parent_path / "worktree"
        home_dir = parent_path / "home"
        temp_dir = parent_path / "tmp"
        home_dir.mkdir()
        temp_dir.mkdir()
        added = False
        try:
            # detached worktree 固定在 CI 的 head_sha，不影响 clone 当前分支。
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), base_sha or "HEAD"],
                cwd=repo_root, check=True, capture_output=True, text=True, timeout=60,
            )
            added = True
            # 此处是第一次真正应用补丁，但目标仅是稍后整体删除的临时 worktree。
            subprocess.run(
                ["git", "apply", "--whitespace=error-all", "-"], cwd=worktree,
                input=patch, check=True, capture_output=True, text=True, timeout=30,
            )
            commands = detect_test_commands(worktree, failing_tests)
            if not commands["targeted"]:
                return {"status": "blocked", "test_command": [],
                        "targeted": {"status": "blocked", "output": "未识别到安全测试命令"},
                        "regression": {"status": "skipped"}}

            env = _clean_environment(home_dir, temp_dir)
            # 先运行最小失败用例；只有它通过才值得支付全量回归测试成本。
            targeted = _run_test(commands["targeted"], worktree, env)
            regression = {"status": "skipped"}
            if targeted["status"] == "passed" and commands["regression"]:
                regression = _run_test(commands["regression"], worktree, env)
            if targeted["status"] != "passed":
                final_status = targeted["status"]
            else:
                final_status = "passed" if regression.get("status") in {"passed", "skipped"} else regression["status"]
            return {"status": final_status, "test_command": commands["targeted"],
                    "targeted": targeted, "regression": regression}
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            message = getattr(exc, "stderr", "") or str(exc)
            return {"status": "failed", "test_command": [],
                    "targeted": {"status": "failed", "output": message.strip()[:MAX_OUTPUT_CHARS]},
                    "regression": {"status": "skipped"}}
        finally:
            # 清理放在 finally，补丁应用失败、测试异常和超时都不能跳过。
            if added:
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_root,
                        capture_output=True, text=True, timeout=30,
                    )
                except (OSError, subprocess.SubprocessError):
                    # TemporaryDirectory 仍会回收文件；下次 git worktree prune 可清理残留元数据。
                    pass
