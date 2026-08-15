"""候选补丁的确定性审查规则；在 LLM 审查之前提供不可绕过的安全底线。"""
from __future__ import annotations

import re


_SUPPRESSION_PATTERNS = (
    "# noqa", "# type: ignore", "eslint-disable", "@pytest.mark.skip",
    "@pytest.mark.xfail", ".skip(", "test.skip(",
)
# 这些调用不一定永远有漏洞，但由自动修复补丁新增时必须阻断并交给人工确认。
_DANGEROUS_PATTERNS = ("shell=True", "verify=False", "eval(", "exec(", "os.system(")


def _added_lines(patch: str) -> list[str]:
    return [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]


def _deleted_lines(patch: str) -> list[str]:
    return [line[1:] for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")]


def deterministic_review(patch: str, patch_files: list[str], expected_files: list[str]) -> list[dict]:
    """发现测试绕过、危险 API 和明显越界修改，输出与 LLM 审查相同的 issue 结构。"""
    issues = []
    added = _added_lines(patch)
    deleted = _deleted_lines(patch)
    added_text = "\n".join(added)
    deleted_text = "\n".join(deleted)

    # 测试通过不能靠关闭规则、跳过用例或压制类型错误实现。
    for pattern in _SUPPRESSION_PATTERNS:
        if pattern.lower() in added_text.lower():
            issues.append({
                "category": "test_quality", "severity": "high", "file": "",
                "line": 0, "description": f"补丁新增了测试/检查绕过标记: {pattern}",
                "suggestion": "修复真实失败原因，不要跳过测试或静态检查。",
            })

    # 安全规则是确定性的，即使后续 LLM 认为“没有问题”也不会删除这些发现。
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.lower() in added_text.lower():
            issues.append({
                "category": "security", "severity": "high", "file": "",
                "line": 0, "description": f"补丁新增潜在危险调用: {pattern}",
                "suggestion": "使用安全 API，并显式限制输入和执行范围。",
            })

    # 只在测试文件中检查用例/断言删除，避免把生产代码里的普通 assert 误判为删测试。
    test_files = [name for name in patch_files if re.search(r"(^|/)(tests?|specs?)(/|_)|\.(?:test|spec)\.", name)]
    if test_files and re.search(r"\b(?:def test_|it\(|test\(|assert\b|expect\()", deleted_text):
        issues.append({
            "category": "test_quality", "severity": "high", "file": test_files[0],
            "line": 0, "description": "补丁删除或弱化了原有测试断言/测试用例。",
            "suggestion": "保留原测试，并通过修复生产代码使其通过。",
        })

    # expected_files 来自根因分析和触发提交；越界文件先警告，不直接阻断合理的调用链修复。
    expected = {name for name in expected_files if name}
    if expected:
        unrelated = [name for name in patch_files if name not in expected]
        if unrelated:
            issues.append({
                "category": "scope_control", "severity": "medium", "file": unrelated[0],
                "line": 0, "description": f"补丁修改了根因分析未指向的文件: {', '.join(unrelated)}",
                "suggestion": "确认这些文件与当前 CI 失败存在直接调用或依赖关系。",
            })
    return issues


def review_verdict(issues: list[dict]) -> str:
    """critical/high 阻断；只有 medium/low 时允许继续但标记 warning。"""
    # 与现有 PR Review 保持一致：高风险必须人工处理，中低风险可以继续但必须展示。
    severities = {str(item.get("severity", "low")).lower() for item in issues}
    if severities & {"critical", "high"}:
        return "blocked"
    return "warning" if issues else "approved"
