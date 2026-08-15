"""GitHub Actions 日志的确定性清洗、分类和错误指纹提取。"""
from __future__ import annotations

import hashlib
import re


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")
# 覆盖常见编译器的位置格式：file.py:12:3 和 index.ts(12,4)。
_LOCATION_PATTERNS = [
    re.compile(r"(?P<file>[\w./\\-]+\.py):(?P<line>\d+)(?::\d+)?"),
    re.compile(r"(?P<file>[\w./\\-]+\.(?:ts|tsx|js|jsx|go|rs|java|kt|rb|php))[:(](?P<line>\d+)"),
]
_SIGNAL_RE = re.compile(
    r"(?:FAILED|ERROR|Error:|Exception|AssertionError|Traceback|"
    r"error TS\d+|fatal error|BUILD FAILED|npm ERR!|panic:|timed out|TimeoutError)",
    re.IGNORECASE,
)


def clean_actions_log(text: str) -> str:
    """去颜色、统一换行、移除 Actions 时间戳并压缩连续重复行。"""
    text = _ANSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = []
    previous = None
    repeated = 0
    for raw_line in text.splitlines():
        line = _TIMESTAMP_RE.sub("", raw_line).rstrip()
        if line == previous:
            repeated += 1
            if repeated > 2:
                continue
        else:
            previous, repeated = line, 0
        lines.append(line)
    return "\n".join(lines)


def _failure_type(text: str) -> str:
    """规则优先分类；未知类型保留为 runtime_error，避免让 LLM 决定路由。"""
    lowered = text.lower()
    if "pytest" in lowered or re.search(r"\bfailed\b.*\btest", lowered):
        return "test_failure"
    if "error ts" in lowered or "typecheck" in lowered or "mypy" in lowered:
        return "type_check"
    if "eslint" in lowered or "ruff" in lowered or "flake8" in lowered:
        return "lint_failure"
    if "npm err!" in lowered or "could not resolve" in lowered or "no matching distribution" in lowered:
        return "dependency_failure"
    if "docker" in lowered and ("build failed" in lowered or "error" in lowered):
        return "build_failure"
    if "timed out" in lowered or "timeouterror" in lowered:
        return "timeout"
    return "runtime_error"


def parse_ci_log(text: str, max_signatures: int = 12) -> dict:
    """提取有限数量的稳定错误指纹、位置和失败测试名称。"""
    cleaned = clean_actions_log(text)
    lines = cleaned.splitlines()
    signatures = []
    seen = set()

    # 每个信号携带前 2 后 3 行，兼顾错误位置和异常消息。
    for index, line in enumerate(lines):
        if not _SIGNAL_RE.search(line):
            continue
        window = "\n".join(lines[max(0, index - 2): min(len(lines), index + 4)]).strip()
        normalized = re.sub(r"\s+", " ", line).strip()[:500]
        # 归一化错误行后哈希，可消除日志中完全重复的连锁报错。
        digest = hashlib.sha256(normalized.encode()).hexdigest()[:12]
        if digest in seen:
            continue
        seen.add(digest)

        file_name, line_number = "", 0
        for pattern in _LOCATION_PATTERNS:
            match = pattern.search(window)
            if match:
                file_name = match.group("file").replace("\\", "/")
                line_number = int(match.group("line"))
                break

        signatures.append({
            "id": digest,
            "type": _failure_type(window),
            "message": normalized,
            "file": file_name,
            "line": line_number,
            "context": window[:2000],
        })
        if len(signatures) >= max_signatures:
            break

    # pytest/Jest 风格的 file::test_name 单独收集，供后续目标测试阶段复用。
    failing_tests = []
    for match in re.finditer(r"(?:FAILED\s+)?([\w./\\-]+\.(?:py|ts|tsx|js|jsx)::[\w.\[\]-]+)", cleaned):
        value = match.group(1).replace("\\", "/")
        if value not in failing_tests:
            failing_tests.append(value)

    return {
        "cleaned_log": cleaned,
        "error_signatures": signatures,
        "failing_tests": failing_tests[:20],
        "failure_type": signatures[0]["type"] if signatures else "unknown",
    }
