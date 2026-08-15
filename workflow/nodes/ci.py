"""CI Failure 节点：只读诊断、候选补丁生成、安全校验和报告。"""
from __future__ import annotations

import hashlib
from pathlib import Path

from config import settings
from context.ci_log_parser import parse_ci_log
from core.llm.client import chat, parse_json
from tools.github_actions_api import fetch_actions_run
from tools.ci_sandbox import verify_patch
from tools.github_patch_publisher import publish_draft_pr
from tools.patch_validator import extract_unified_diff, is_protected_path, validate_patch
from tools.patch_review import deterministic_review, review_verdict
from workflow.ci_state import CIState


def fetch_ci_run(state: CIState) -> dict:
    """节点 1：从 GitHub 拉取 CI 运行数据，不做任何远程写操作。"""
    return fetch_actions_run(state["ci_url"])


def parse_failure_logs(state: CIState) -> dict:
    """节点 2：将非结构化日志转换为可路由、可引用的错误证据。"""
    parsed = parse_ci_log(state.get("failure_logs", ""))
    return {
        "failure_logs": parsed["cleaned_log"],
        "error_signatures": parsed["error_signatures"],
        "failing_tests": parsed["failing_tests"],
        "failure_type": parsed["failure_type"],
    }


def _safe_repo_file(repo_root: Path, name: str) -> Path | None:
    """只允许读取 clone 目录内部文件，阻断日志伪造的路径穿越。"""
    if not name:
        return None
    candidate = (repo_root / name).resolve()
    try:
        candidate.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def collect_ci_context(state: CIState) -> dict:
    """读取错误位置和本次提交补丁，给根因分析提供最小且可追溯的上下文。"""
    repo_root = settings.clone_base / f"{state['owner']}__{state['repo']}"
    sections = []
    seen_files = set()

    # 优先读取日志明确指向的源文件，每处保留前后约 20 行。
    for signature in state.get("error_signatures", []):
        name = signature.get("file", "")
        source = _safe_repo_file(repo_root, name)
        if not source or name in seen_files:
            continue
        seen_files.add(name)
        line = max(1, int(signature.get("line", 1) or 1))
        content = source.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = max(0, line - 21), min(len(content), line + 20)
        numbered = "\n".join(f"{i + 1:>6} | {content[i]}" for i in range(start, end))
        sections.append(f"### {name} (around L{line})\n{numbered}")

    # 再加入触发提交的 Diff；最多 15 个文件，避免大提交撑爆上下文。
    for changed in state.get("changed_files", [])[:15]:
        patch = changed.get("patch", "")
        if patch:
            sections.append(f"### Commit diff: {changed.get('filename', '?')}\n{patch[:8000]}")

    return {"code_context": "\n\n".join(sections)[:80_000]}


_ANALYZE_SYSTEM = """你是资深 CI 故障诊断工程师。只根据日志、提交差异和代码证据判断根因。
必须区分根错误与连锁错误；不确定时明确说明，不得编造不存在的文件或提交。"""


def analyze_ci_failure(state: CIState) -> dict:
    """节点 5：基于证据生成结构化诊断；LLM 不可用时退化为首错误报告。"""
    signatures = state.get("error_signatures", [])
    prompt = f"""分析以下 GitHub Actions 失败。

工作流: {state.get('run_info', {}).get('name', '')}
分支: {state.get('run_info', {}).get('head_branch', '')}
提交: {state.get('run_info', {}).get('head_sha', '')}
失败任务: {state.get('failed_jobs', [])}
失败类型: {state.get('failure_type', 'unknown')}
失败测试: {state.get('failing_tests', [])}
错误指纹: {signatures}

项目地图:
{state.get('project_map', '')}

代码和提交上下文:
{state.get('code_context', '')}

关键日志（最多 20000 字符）:
{state.get('failure_logs', '')[-20000:]}

只输出 JSON：
{{
  "root_cause": "有证据的根因",
  "fix_suggestion": "具体但不直接修改代码的修复步骤",
  "suspected_files": ["path/file.py"],
  "suspicious_commit": "commit sha 或空字符串",
  "confidence": 0.0
}}
"""
    try:
        result = parse_json(chat(_ANALYZE_SYSTEM, prompt, max_tokens=1800, json_mode=True))
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
        return {
            "root_cause": str(result.get("root_cause") or "现有证据不足，无法确定根因"),
            "fix_suggestion": str(result.get("fix_suggestion") or "请人工检查首个错误及对应代码"),
            "suspected_files": [str(item) for item in result.get("suspected_files", [])][:20],
            "suspicious_commit": str(result.get("suspicious_commit") or ""),
            "confidence": confidence,
        }
    except Exception as exc:
        # 第一阶段必须始终能产出报告，因此 LLM/JSON 异常不终止整个图。
        first = signatures[0] if signatures else {}
        return {
            "root_cause": first.get("message", "未从 CI 日志中提取到明确错误"),
            "fix_suggestion": "检查失败 Job 的首个错误，并在本地复现对应命令。",
            "suspected_files": [first["file"]] if first.get("file") else [],
            "suspicious_commit": state.get("run_info", {}).get("head_sha", ""),
            "confidence": 0.2,
            "error": f"LLM 分析失败: {exc}",
        }


_REFLECT_SYSTEM = "你是独立 CI 诊断审查者，严格检查结论是否由日志、代码或提交差异支持。"


def reflect_ci(state: CIState) -> dict:
    """节点 6：独立检查证据充分性，低置信度时由图决定是否重试。"""
    prompt = f"""评估以下 CI 根因分析。
错误指纹: {state.get('error_signatures', [])}
根因: {state.get('root_cause', '')}
可疑文件: {state.get('suspected_files', [])}
可疑提交: {state.get('suspicious_commit', '')}
修复建议: {state.get('fix_suggestion', '')}

只输出 JSON：{{"confidence": 0到1, "note": "证据充分性意见"}}"""
    try:
        result = parse_json(chat(_REFLECT_SYSTEM, prompt, max_tokens=500, json_mode=True))
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0))))
        note = str(result.get("note", ""))
    except Exception as exc:
        confidence = float(state.get("confidence", 0.0))
        note = f"Reflection 不可用，保留分析置信度: {exc}"
    return {
        "confidence": confidence,
        "reflection_note": note,
        "iteration": state.get("iteration", 0) + 1,
    }


_PATCH_SYSTEM = """你是谨慎的软件修复工程师。根据已确认的 CI 根因生成最小补丁。
只能修改解决当前失败所需的源码或测试，不得修改 CI workflow、密钥、权限或锁文件。
输出必须是从 `diff --git` 开始的标准 Unified Diff，不要解释，不要 Markdown。"""


def _patch_source_context(state: CIState) -> str:
    """为补丁生成读取可疑文件全文；总量受限，避免把整个仓库交给 LLM。"""
    repo_root = settings.clone_base / f"{state['owner']}__{state['repo']}"
    sections, remaining = [], 100_000
    candidates = list(state.get("suspected_files", []))
    candidates.extend(item.get("filename", "") for item in state.get("changed_files", []))
    for name in dict.fromkeys(candidates):
        # 在读取文件前应用保护规则，防止环境密钥或工作流配置进入模型上下文。
        if is_protected_path(name):
            continue
        source = _safe_repo_file(repo_root, name)
        if not source or remaining <= 0:
            continue
        content = source.read_text(encoding="utf-8", errors="replace")[:remaining]
        remaining -= len(content)
        sections.append(f"### FILE: {name}\n{content}")
    return "\n\n".join(sections)


def generate_patch(state: CIState) -> dict:
    """节点 7：生成候选补丁；证据不足时安全跳过，不强迫 LLM 猜测。"""
    # Web API 的验证/发布阶段复用上一阶段展示给用户的补丁。哈希不一致说明
    # 状态在阶段间被篡改或损坏，必须阻断，不能让模型重新生成另一份补丁。
    if state.get("patch_locked"):
        patch = state.get("patch", "")
        digest = hashlib.sha256(patch.encode()).hexdigest() if patch else ""
        expected = state.get("expected_patch_sha256", "")
        if not patch or not expected or digest != expected:
            return {
                "patch": "", "patch_status": "blocked", "patch_risk": "high",
                "patch_errors": ["阶段间补丁完整性校验失败"],
                "patch_sha256": digest,
            }
        return {
            "patch": patch, "patch_status": "generated",
            "patch_errors": [], "patch_sha256": digest,
        }
    attempt = state.get("patch_attempt", 0) + 1
    # Reflection 最终仍可能低置信度结束，此处设置第二道安全门。
    if state.get("confidence", 0) < 0.5:
        return {
            "patch": "", "patch_files": [], "patch_risk": "high",
            "patch_status": "skipped", "patch_errors": ["诊断置信度低于 0.5"],
            "patch_explanation": "证据不足，未生成候选补丁。", "patch_attempt": attempt,
            "patch_history": state.get("patch_history", []),
        }

    # 没有真实源码时禁止只凭日志“脑补”补丁。
    source_context = _patch_source_context(state)
    if not source_context:
        return {
            "patch": "", "patch_files": [], "patch_risk": "high",
            "patch_status": "skipped", "patch_errors": ["未找到可疑文件源码"],
            "patch_explanation": "没有足够源码上下文，未生成候选补丁。", "patch_attempt": attempt,
            "patch_history": state.get("patch_history", []),
        }

    prompt = f"""CI 根因：{state.get('root_cause', '')}
修复建议：{state.get('fix_suggestion', '')}
失败测试：{state.get('failing_tests', [])}
错误证据：{state.get('error_signatures', [])}

候选文件源码：
{source_context}

生成最小、可应用的 Unified Diff。"""
    try:
        raw = chat(_PATCH_SYSTEM, prompt, max_tokens=4000)
        patch = extract_unified_diff(raw)
        digest = hashlib.sha256(patch.encode()).hexdigest() if patch else ""
        history = [*state.get("patch_history", [])]
        if digest:
            history.append(digest)
        return {
            "patch": patch,
            "patch_explanation": state.get("fix_suggestion", ""),
            "patch_status": "generated" if patch else "invalid",
            "patch_errors": [] if patch else ["LLM 未返回标准 Unified Diff"],
            "patch_attempt": attempt,
            "patch_history": history,
            "patch_sha256": digest,
        }
    except Exception as exc:
        return {
            "patch": "", "patch_files": [], "patch_risk": "high",
            "patch_status": "invalid", "patch_errors": [f"补丁生成失败: {exc}"],
            "patch_explanation": "", "patch_attempt": attempt,
            "patch_history": state.get("patch_history", []),
        }


def validate_generated_patch(state: CIState) -> dict:
    """节点 8：执行策略检查和只读 git apply --check。"""
    # skipped/生成失败也继续进入报告节点，让用户看到明确原因。
    if not state.get("patch"):
        return {
            "patch_status": state.get("patch_status", "skipped"),
            "patch_files": [], "patch_risk": "high",
            "patch_errors": state.get("patch_errors", ["没有可校验的补丁"]),
        }
    repo_root = settings.clone_base / f"{state['owner']}__{state['repo']}"
    run_info = state.get("run_info", {})
    result = validate_patch(repo_root, state["patch"], run_info.get("source_sha") or run_info.get("head_sha", ""))
    return {
        "patch_status": "valid" if result["valid"] else "invalid",
        "patch_files": result["files"],
        "patch_risk": result["risk"],
        "patch_errors": result["errors"],
    }


def verify_patch_in_sandbox(state: CIState) -> dict:
    """节点 9：在临时 worktree 应用补丁并运行目标/回归测试，结束后强制清理。"""
    # CLI 未提供 --execute-patch 时，本节点理论上不会被路由到；这里仍做纵深校验。
    if not state.get("execute_patch"):
        return {"verification_status": "skipped", "test_command": [],
                "targeted_test_result": {"status": "skipped"},
                "regression_test_result": {"status": "skipped"}}
    # 静态策略或 git apply --check 失败的补丁绝不进入执行器。
    if state.get("patch_status") != "valid":
        return {"verification_status": "blocked", "test_command": [],
                "targeted_test_result": {"status": "blocked", "output": "补丁未通过静态校验"},
                "regression_test_result": {"status": "skipped"}}
    digest = hashlib.sha256(state.get("patch", "").encode()).hexdigest()
    expected = state.get("expected_patch_sha256", "")
    if state.get("patch_locked") and digest != expected:
        return {"verification_status": "blocked", "test_command": [],
                "targeted_test_result": {"status": "blocked", "output": "补丁摘要不一致"},
                "regression_test_result": {"status": "skipped"}}

    repo_root = settings.clone_base / f"{state['owner']}__{state['repo']}"
    result = verify_patch(
        repo_root, (state.get("run_info", {}).get("source_sha")
                    or state.get("run_info", {}).get("head_sha", "")),
        state["patch"], state.get("failing_tests", []),
    )
    return {
        "verification_status": result["status"],
        "test_command": result.get("test_command", []),
        "targeted_test_result": result.get("targeted", {}),
        "regression_test_result": result.get("regression", {}),
    }


_REVISE_SYSTEM = """你负责修订一个未通过校验或测试的候选补丁。
必须根据新增失败证据进行最小修改，不得通过跳过测试、关闭检查或修改工作流来规避失败。
只输出从 `diff --git` 开始的完整 Unified Diff，不要解释或 Markdown。"""


def revise_patch(state: CIState) -> dict:
    """节点 10：把静态校验或测试反馈交给修复模型，最多生成三版候选补丁。"""
    # patch_attempt 在首次 generate_patch 时已是 1；修订后依次变为 2、3。
    attempt = state.get("patch_attempt", 0) + 1
    # 只保留测试输出末尾，通常异常总结和失败堆栈集中在这里。
    targeted = dict(state.get("targeted_test_result", {}))
    regression = dict(state.get("regression_test_result", {}))
    targeted["output"] = str(targeted.get("output", ""))[-8000:]
    regression["output"] = str(regression.get("output", ""))[-8000:]
    feedback = {
        "patch_errors": state.get("patch_errors", []),
        "verification_status": state.get("verification_status", ""),
        "targeted_test": targeted,
        "regression_test": regression,
    }
    prompt = f"""根因：{state.get('root_cause', '')}
修复建议：{state.get('fix_suggestion', '')}
当前补丁：
{state.get('patch', '')[:60000]}

失败反馈：
{feedback}

相关源码：
{_patch_source_context(state)[:60000]}

输出修订后的完整 Unified Diff。"""
    try:
        revised = extract_unified_diff(chat(_REVISE_SYSTEM, prompt, max_tokens=4000))
        # 用完整补丁哈希识别循环，避免模型在两个相同方案之间重复消耗测试资源。
        digest = hashlib.sha256(revised.encode()).hexdigest() if revised else ""
        history = list(state.get("patch_history", []))
        errors = []
        status = "generated"
        if not revised:
            status, errors = "invalid", ["修订模型未返回标准 Unified Diff"]
        elif digest in history:
            # 清空 patch 让条件边直接进入报告，不能把重复版本再次送去执行。
            status, errors, revised = "invalid", ["修订补丁与历史版本重复，停止无效循环"], ""
        else:
            history.append(digest)
        return {
            "patch": revised, "patch_status": status, "patch_errors": errors,
            "patch_attempt": attempt, "patch_history": history,
            "patch_sha256": digest if revised else "",
            "verification_status": "pending",
            "targeted_test_result": {}, "regression_test_result": {},
        }
    except Exception as exc:
        return {
            "patch_status": "invalid", "patch_errors": [f"补丁修订失败: {exc}"],
            "patch_attempt": attempt, "verification_status": "failed",
        }


_PATCH_REVIEW_SYSTEM = """你是独立代码审查者。审查用于修复 CI 的候选补丁，关注：
security、logic、style、test_quality、scope_control。不得因为测试通过就忽略安全或逻辑风险。"""


def review_patch(state: CIState) -> dict:
    """节点 11：确定性规则优先，再用独立 LLM 审查五个维度。"""
    # 规则结果先写入 issues，后续 LLM 只能补充，不能覆盖或降低确定性问题的等级。
    expected_files = list(state.get("suspected_files", []))
    expected_files.extend(item.get("filename", "") for item in state.get("changed_files", []))
    issues = deterministic_review(state.get("patch", ""), state.get("patch_files", []), expected_files)
    targeted_summary = dict(state.get("targeted_test_result", {}))
    regression_summary = dict(state.get("regression_test_result", {}))
    targeted_summary["output"] = str(targeted_summary.get("output", ""))[-5000:]
    regression_summary["output"] = str(regression_summary.get("output", ""))[-5000:]
    prompt = f"""根因：{state.get('root_cause', '')}
候选补丁：
{state.get('patch', '')}

测试结果：
目标测试：{targeted_summary}
回归测试：{regression_summary}

只输出 JSON：
{{"confidence": 0到1, "issues": [{{
  "category": "security/logic/style/test_quality/scope_control",
  "severity": "critical/high/medium/low",
  "file": "path", "line": 0,
  "description": "问题", "suggestion": "建议"
}}]}}"""
    # LLM 不可用时仍保留规则结果，并使用中等置信度提醒人工审查。
    confidence = 0.5
    try:
        result = parse_json(chat(_PATCH_REVIEW_SYSTEM, prompt, max_tokens=1800, json_mode=True))
        confidence = max(0.0, min(1.0, float(result.get("confidence", 0.5))))
        # 对模型输出做枚举归一化，防止任意类别或严重等级影响 Verdict 路由。
        valid_categories = {"security", "logic", "style", "test_quality", "scope_control"}
        valid_severities = {"critical", "high", "medium", "low"}
        for item in result.get("issues", [])[:30]:
            category = str(item.get("category", "logic")).lower()
            severity = str(item.get("severity", "medium")).lower()
            issues.append({
                "category": category if category in valid_categories else "logic",
                "severity": severity if severity in valid_severities else "medium",
                "file": str(item.get("file", "")), "line": int(item.get("line", 0) or 0),
                "description": str(item.get("description", "")),
                "suggestion": str(item.get("suggestion", "")),
            })
    except Exception as exc:
        issues.append({
            "category": "logic", "severity": "medium", "file": "", "line": 0,
            "description": f"LLM 补丁审查不可用: {exc}", "suggestion": "提交前进行人工代码审查。",
        })

    # 以关键字段去重，避免规则与 LLM 重复报告同一问题。
    deduplicated = []
    seen = set()
    for item in issues:
        key = (item.get("category"), item.get("severity"), item.get("file"), item.get("description"))
        if key not in seen:
            seen.add(key)
            deduplicated.append(item)
    return {
        "patch_review_issues": deduplicated,
        "patch_review_status": review_verdict(deduplicated),
        "patch_review_confidence": confidence,
    }


def publish_patch(state: CIState) -> dict:
    """节点 12：通过全部门禁后创建修复分支和 Draft PR；任何失败都不自动重试写操作。"""
    if not state.get("create_pr"):
        return {"publish_status": "skipped", "publish_error": ""}
    # 在真正调用写 API 前重新检查全部状态，不能只依赖 LangGraph 上游路由。
    blockers = []
    if state.get("patch_status") != "valid":
        blockers.append("补丁未通过静态校验")
    if state.get("verification_status") != "passed":
        blockers.append("隔离测试未全部通过")
    if state.get("patch_review_status") not in {"approved", "warning"}:
        blockers.append("补丁审查未明确允许发布")
    digest = hashlib.sha256(state.get("patch", "").encode()).hexdigest()
    if not state.get("expected_patch_sha256") or digest != state.get("expected_patch_sha256"):
        blockers.append("待发布补丁与已授权补丁摘要不一致")
    run_info = state.get("run_info", {})
    base_branch = run_info.get("publish_base_branch", "")
    if not base_branch:
        blockers.append("无法确定同仓库 PR 基准分支，可能来自 Fork PR")
    if blockers:
        # 返回 blocked 而不是抛异常，最终报告会完整展示未发布原因。
        return {"publish_status": "blocked", "publish_error": "；".join(blockers)}

    repo_root = settings.clone_base / f"{state['owner']}__{state['repo']}"
    base_sha = run_info.get("source_sha") or run_info.get("head_sha", "")
    test_summary = (
        f"Targeted tests: {state.get('targeted_test_result', {}).get('status', 'unknown')}\n\n"
        f"Regression tests: {state.get('regression_test_result', {}).get('status', 'unknown')}"
    )
    review_summary = (
        f"Verdict: {state.get('patch_review_status', 'unknown')}\n\n"
        f"Issues: {state.get('patch_review_issues', [])}"
    )
    try:
        result = publish_draft_pr(
            owner=state["owner"], repo=state["repo"], repo_root=repo_root,
            base_sha=base_sha, base_branch=base_branch, run_id=state["run_id"],
            patch=state["patch"], root_cause=state.get("root_cause", ""),
            test_summary=test_summary, review_summary=review_summary,
        )
        return {"publish_status": result["status"], "published_branch": result["branch"],
                "published_commit": result["commit"], "draft_pr_url": result["url"],
                "publish_error": ""}
    except Exception as exc:
        # 写操作不自动重试：可能已经创建 Blob/Tree/Commit/Ref，盲目重试会产生重复资源。
        return {"publish_status": "failed", "published_branch": "",
                "published_commit": "", "draft_pr_url": "", "publish_error": str(exc)}


def generate_ci_report(state: CIState) -> dict:
    """最终节点：格式化诊断与候选补丁，不再调用外部服务。"""
    run = state.get("run_info", {})
    jobs = "\n".join(
        f"- `{job.get('name', '?')}`：{job.get('conclusion', 'unknown')}"
        for job in state.get("failed_jobs", [])
    ) or "- 未识别到失败 Job"
    files = "\n".join(f"- `{name}`" for name in state.get("suspected_files", [])) or "- 暂未确定"
    tests = "\n".join(f"- `{name}`" for name in state.get("failing_tests", [])) or "- 未提取到测试名称"
    commit = state.get("suspicious_commit") or run.get("head_sha", "暂未确定")
    patch_errors = "\n".join(f"- {item}" for item in state.get("patch_errors", [])) or "- 无"
    patch = state.get("patch", "")
    patch_block = f"```diff\n{patch.rstrip()}\n```" if patch else "未生成候选补丁。"
    targeted = state.get("targeted_test_result", {})
    regression = state.get("regression_test_result", {})
    test_output = targeted.get("output", "")[-5000:]
    # 把统一 issue 结构渲染成人类可读列表，同时保留严重级别和类别。
    review_issues = state.get("patch_review_issues", [])
    if review_issues:
        review_lines = []
        for item in review_issues:
            location = f" `{item.get('file')}`" if item.get("file") else ""
            review_lines.append(
                f"- **{item.get('severity', 'medium').upper()}** "
                f"[{item.get('category', 'logic')}]{location}: {item.get('description', '')} "
                f"建议：{item.get('suggestion', '')}"
            )
        review_text = "\n".join(review_lines)
    else:
        review_text = "- 未发现问题"
    publish_url = state.get("draft_pr_url", "")
    report = f"""# CI Run #{state.get('run_id', '')} 失败分析报告

| 字段 | 值 |
|------|-----|
| 工作流 | {run.get('name', '')} |
| 分支 | {run.get('head_branch', '')} |
| 结论 | {run.get('conclusion', '')} |
| 失败类型 | {state.get('failure_type', 'unknown')} |
| 置信度 | {state.get('confidence', 0.0):.2f} |

## 失败任务
{jobs}

## 失败测试
{tests}

## 根因
{state.get('root_cause', '暂未确定')}

## 可疑文件
{files}

## 可疑提交
`{commit}`

## 修复建议
{state.get('fix_suggestion', '请人工分析')}

## 独立评审
{state.get('reflection_note', '无')}

## 候选补丁

| 字段 | 值 |
|------|-----|
| 状态 | {state.get('patch_status', '未执行')} |
| 风险 | {state.get('patch_risk', 'unknown')} |
| 文件 | {', '.join(state.get('patch_files', [])) or '无'} |

{patch_block}

### 补丁校验信息
{patch_errors}

## 补丁审查

| 字段 | 值 |
|------|-----|
| 结论 | {state.get('patch_review_status', '未执行')} |
| 置信度 | {state.get('patch_review_confidence', 0.0):.2f} |
| 补丁版本 | {state.get('patch_attempt', 0)} |

{review_text}

## 隔离验证

| 字段 | 值 |
|------|-----|
| 是否授权执行 | {'是' if state.get('execute_patch') else '否'} |
| 验证状态 | {state.get('verification_status', 'skipped')} |
| 目标测试 | {targeted.get('status', 'skipped')} |
| 回归测试 | {regression.get('status', 'skipped')} |
| 命令 | `{' '.join(state.get('test_command', [])) or '未执行'}` |

```text
{test_output or '无测试输出'}
```

## Draft PR 发布

| 字段 | 值 |
|------|-----|
| 是否授权 | {'是' if state.get('create_pr') else '否'} |
| 状态 | {state.get('publish_status', 'skipped')} |
| 分支 | `{state.get('published_branch', '') or '无'}` |
| Commit | `{state.get('published_commit', '') or '无'}` |
| PR | {f'[打开 Draft PR]({publish_url})' if publish_url else '未创建'} |

{state.get('publish_error', '') or '无发布错误'}
"""
    return {"final_report": report}
