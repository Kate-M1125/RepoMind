"""CI Failure 第一阶段节点：获取、解析、上下文、分析、反思和报告。"""
from __future__ import annotations

from pathlib import Path

from config import settings
from context.ci_log_parser import parse_ci_log
from core.llm.client import chat, parse_json
from tools.github_actions_api import fetch_actions_run
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


def generate_ci_report(state: CIState) -> dict:
    """节点 7：纯格式化 Markdown，不再调用外部服务。"""
    run = state.get("run_info", {})
    jobs = "\n".join(
        f"- `{job.get('name', '?')}`：{job.get('conclusion', 'unknown')}"
        for job in state.get("failed_jobs", [])
    ) or "- 未识别到失败 Job"
    files = "\n".join(f"- `{name}`" for name in state.get("suspected_files", [])) or "- 暂未确定"
    tests = "\n".join(f"- `{name}`" for name in state.get("failing_tests", [])) or "- 未提取到测试名称"
    commit = state.get("suspicious_commit") or run.get("head_sha", "暂未确定")
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
"""
    return {"final_report": report}
