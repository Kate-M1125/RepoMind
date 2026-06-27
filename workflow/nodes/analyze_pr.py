from typing import TypedDict
from langgraph.types import Send
from workflow.pr_state import PRState
from tools.github_pr_api import parse_pr_url
from core.llm.client import chat, chat_with_tools, parse_json
from tools.base import build_tools
from tools.code_nav import READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION
from context.memory_store import get_memory_collection

_memory_collection = get_memory_collection()

# ── Diff 格式化 ──────────────────────────────────────────────────────

_MAX_FILES = 20
_MAX_PATCH_LINES = 120


def _format_diff(changed_files: list[dict]) -> str:
    parts = []
    for f in changed_files[:_MAX_FILES]:
        patch = f.get("patch", "")
        if not patch:
            continue
        lines = patch.splitlines()
        if len(lines) > _MAX_PATCH_LINES:
            patch = "\n".join(lines[:_MAX_PATCH_LINES]) + "\n...(truncated)"
        parts.append(
            f"### {f['filename']} ({f['status']}, +{f['additions']}/-{f['deletions']})\n"
            f"```diff\n{patch}\n```"
        )
    return "\n\n".join(parts)


def _pr_header(state: PRState) -> str:
    owner, repo, number = parse_pr_url(state["pr_url"])
    return (
        f"PR #{number}: {state['title']}\n"
        f"仓库: {owner}/{repo} | {state['head_branch']} → {state['base_branch']}\n"
        f"作者: {state.get('author', '未知')}"
    )


def _shared_context(state: PRState) -> str:
    parts = []
    if pm := state.get("project_map"):
        parts.append(f"【项目地图】\n{pm}")
    if mem := state.get("memory_context"):
        parts.append(f"【历史 Review 经验】\n{mem}")
    if img := state.get("image_descriptions"):
        parts.append(f"【PR 附图描述】\n{img}")
    return "\n\n".join(parts)


# ── 安全审查 ──────────────────────────────────────────────────────────

_SYSTEM_SECURITY = """你是一名安全审查专家，专注审查 PR 代码变更中的安全漏洞。

关注维度（按优先级）：
1. SQL 注入 / XSS / CSRF / 路径遍历 / 命令注入
2. 硬编码密钥、Token、密码（直接写在代码里）
3. 权限验证缺失（未鉴权的端点、越权访问）
4. 不安全的反序列化、eval/exec 滥用
5. 敏感数据泄漏（日志里打印了密码/Token）
6. 不安全的随机数、弱加密算法

工具使用策略：
- read_file：读取变更函数的完整上下文（补丁只有部分，需看完整函数体）
- find_callers：追调用链，验证危险参数是否来自用户输入
- grep_code：搜索类似危险模式，确认是否系统性风险

若没有安全问题，issues 为空列表并在 summary 里说明。"""

_JSON_SECURITY = """输出 JSON，字段如下：
- reasoning: 逐步分析思路（逐文件检查过程）
- issues: 发现的安全问题列表，每项含：
  - severity: critical/high/medium/low
  - file: 涉及文件路径
  - line: 行号（不确定填 0）
  - description: 问题描述（说清楚为什么危险）
  - suggestion: 具体修复建议（可以是代码片段）
- summary: 整体安全评级（safe/low_risk/medium_risk/high_risk）和一句话说明

只输出 JSON，不要任何其他文字。"""


def analyze_security(state: PRState) -> dict:
    owner, repo, _ = parse_pr_url(state["pr_url"])
    diff_text = _format_diff(state["changed_files"])
    prompt = f"""{_pr_header(state)}

{_shared_context(state)}

## 代码变更（待安全审查）
{diff_text}

{_JSON_SECURITY}"""

    tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION)
    raw = chat_with_tools(_SYSTEM_SECURITY, prompt, tools=tools_schema, tool_executor=executors,
                          max_tokens=4000, max_turns=10)
    data = parse_json(raw)
    return {"security_issues": data.get("issues", [])}


# ── 风格审查 ──────────────────────────────────────────────────────────

_SYSTEM_STYLE = """你是一名代码质量专家，专注审查 PR 代码变更中的风格和可读性问题。

关注维度：
1. 命名规范（变量/函数/类名是否语义清晰）
2. 函数/方法过长（超过 50 行建议拆分）
3. 重复代码（DRY 原则违反）
4. 注释缺失（公共 API、复杂逻辑无文档注释）
5. 类型标注缺失（Python 的 type hints）
6. 死代码（注释掉的代码块、未使用的 import）
7. 魔法数字（硬编码数字/字符串应提取为常量）

工具使用策略：
- read_file：读取变更函数的完整上下文（看函数是否过长、有无文档）
- grep_code：检查命名一致性（相似功能是否命名规范一致）

风格问题不影响功能，严重程度一般为 low/medium，不要过度指责。"""

_JSON_STYLE = """输出 JSON，字段如下：
- reasoning: 逐步分析思路
- issues: 发现的风格问题列表，每项含：
  - severity: medium/low（风格问题通常不超过 medium）
  - file: 涉及文件路径
  - line: 行号（不确定填 0）
  - description: 问题描述
  - suggestion: 改进建议
- summary: 整体风格评价（一句话）

只输出 JSON，不要任何其他文字。"""


def analyze_style(state: PRState) -> dict:
    diff_text = _format_diff(state["changed_files"])
    prompt = f"""{_pr_header(state)}

{_shared_context(state)}

## 代码变更（待风格审查）
{diff_text}

{_JSON_STYLE}"""

    tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR)
    raw = chat_with_tools(_SYSTEM_STYLE, prompt, tools=tools_schema, tool_executor=executors,
                          max_tokens=3000, max_turns=6)
    data = parse_json(raw)
    return {"style_issues": data.get("issues", [])}


# ── 逻辑审查 ──────────────────────────────────────────────────────────

_SYSTEM_LOGIC = """你是一名资深代码审查工程师，专注审查 PR 代码变更中的逻辑正确性问题。

关注维度：
1. 边界条件处理（空值/零值/超长输入/并发竞争）
2. 错误处理遗漏（未捕获异常、静默吞掉错误）
3. 逻辑死区（永远不会执行的代码分支）
4. Off-by-one 错误（索引、循环范围）
5. 状态一致性（并发操作下的状态竞争）
6. 资源泄漏（未关闭的文件/连接/锁）
7. 回归风险（改动是否可能破坏现有功能）

工具使用策略：
- read_file：读取变更函数的完整上下文，理解函数整体逻辑
- find_callers：检查函数调用方，评估改动的影响范围
- find_definition：追踪依赖函数/类的定义，确认行为假设
- grep_code：搜索测试文件，检查是否有对应的测试覆盖

优先使用工具确认上下文，避免仅凭 diff 片段做出错误判断。"""

_JSON_LOGIC = """输出 JSON，字段如下：
- reasoning: 逐步分析思路（逐改动点分析逻辑正确性）
- issues: 发现的逻辑问题列表，每项含：
  - severity: critical/high/medium/low
  - file: 涉及文件路径
  - line: 行号（不确定填 0）
  - description: 问题描述（说明为什么有问题）
  - suggestion: 具体修复建议
- summary: 整体逻辑质量评价和是否建议 approve（一句话）

只输出 JSON，不要任何其他文字。"""


def analyze_logic(state: PRState) -> dict:
    diff_text = _format_diff(state["changed_files"])
    prompt = f"""{_pr_header(state)}

{_shared_context(state)}

## 代码变更（待逻辑审查）
{diff_text}

{_JSON_LOGIC}"""

    tools_schema, executors = build_tools(READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION)
    raw = chat_with_tools(_SYSTEM_LOGIC, prompt, tools=tools_schema, tool_executor=executors,
                          max_tokens=4000, max_turns=10)
    data = parse_json(raw)
    return {"logic_issues": data.get("issues", [])}


# ── Reflect ───────────────────────────────────────────────────────────

_SYSTEM_REFLECT_PR = """你是一名首席工程师，负责评估 PR 自动审查报告的质量和可信度。

评估维度：
- 安全问题：是否有误报？严重问题是否都被识别到？
- 逻辑问题：是否有代码依据，还是仅凭 diff 片段猜测？
- 风格问题：是否过于苛刻或遗漏重要问题？
- 整体：建议是否具体可操作？"""


def reflect_pr(state: PRState) -> dict:
    def _fmt_issues(issues: list[dict]) -> str:
        if not issues:
            return "无"
        return "\n".join(
            f"- [{i.get('severity', '?')}] {i.get('file', '?')}:{i.get('line', 0)} — {i.get('description', '')}"
            for i in issues
        )

    prompt = f"""评估以下 PR 自动审查报告的质量。

PR: {state['title']}

安全问题（{len(state.get('security_issues', []))} 条）:
{_fmt_issues(state.get('security_issues', []))}

逻辑问题（{len(state.get('logic_issues', []))} 条）:
{_fmt_issues(state.get('logic_issues', []))}

风格问题（{len(state.get('style_issues', []))} 条）:
{_fmt_issues(state.get('style_issues', []))}

只输出 JSON，两个字段：
- confidence: 0-1 的浮点数（高于 0.7 表示审查可信，可以输出报告）
- note: 评审意见，指出具体的质疑点或确认理由

只输出 JSON，不要任何其他文字。"""

    result = parse_json(chat(_SYSTEM_REFLECT_PR, prompt, max_tokens=600))
    confidence = max(0.0, min(1.0, float(result.get("confidence", 0.8))))
    return {
        "confidence": confidence,
        "reflection_note": result.get("note", ""),
        "iteration": state.get("iteration", 0) + 1,
    }


# ── Report ────────────────────────────────────────────────────────────

_SEVERITY_EMOJI = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}


def _issues_section(title: str, issues: list[dict]) -> str:
    if not issues:
        return f"### {title}\n✅ 无问题\n"
    lines = [f"### {title}\n"]
    for i in issues:
        emoji = _SEVERITY_EMOJI.get(i.get("severity", "low"), "⚪")
        loc = f"`{i.get('file', '?')}`"
        if i.get("line"):
            loc += f" L{i['line']}"
        lines.append(f"- {emoji} **[{i.get('severity', '?').upper()}]** {loc}")
        lines.append(f"  - **问题**: {i.get('description', '')}")
        lines.append(f"  - **建议**: {i.get('suggestion', '')}")
    return "\n".join(lines) + "\n"


def generate_pr_report(state: PRState) -> dict:
    security = state.get("security_issues", [])
    style = state.get("style_issues", [])
    logic = state.get("logic_issues", [])
    all_issues = security + logic + style
    critical_count = sum(1 for i in all_issues if i.get("severity") in ("critical", "high"))

    verdict = "✅ APPROVE" if critical_count == 0 else f"❌ REQUEST CHANGES（{critical_count} 个 high/critical 问题）"

    changed = state.get("changed_files", [])
    files_summary = ", ".join(f["filename"] for f in changed[:5])
    if len(changed) > 5:
        files_summary += f" 等共 {len(changed)} 个文件"

    report = f"""# PR #{state['pr_number']} 审查报告

## {state['title']}

| 字段 | 值 |
|---|---|
| 作者 | {state.get('author', '未知')} |
| 分支 | `{state.get('head_branch', '?')}` → `{state.get('base_branch', '?')}` |
| 变更文件 | {files_summary} |
| 审查结论 | {verdict} |
| 置信度 | {state.get('confidence', 0):.0%} |

---

{_issues_section("🔒 安全审查", security)}

{_issues_section("🧠 逻辑审查", logic)}

{_issues_section("🎨 风格审查", style)}

---

## 总结

{state.get('reflection_note', '')}
"""
    return {"final_report": report}


# ── Memory ────────────────────────────────────────────────────────────

def memory_retrieve_pr(state: PRState) -> dict:
    owner, repo, _ = parse_pr_url(state["pr_url"])
    query = f"PR review: {state.get('title', '')}"
    try:
        results = _memory_collection.query(
            query_texts=[query],
            n_results=3,
            where={"repo": f"{owner}/{repo}", "type": "pr_review"},
        )
        docs = results["documents"][0] if results["documents"] else []
    except Exception:
        docs = []
    return {"memory_context": "\n\n---\n\n".join(docs) if docs else ""}


def memory_save_pr(state: PRState) -> dict:
    owner, repo, number = parse_pr_url(state["pr_url"])
    all_issues = (
        state.get("security_issues", []) +
        state.get("logic_issues", []) +
        state.get("style_issues", [])
    )
    issue_summary = "; ".join(
        f"[{i.get('severity')}] {i.get('file')}:{i.get('description', '')[:50]}"
        for i in all_issues[:5]
    ) or "无问题"

    content = f"""PR: {state['title']}
仓库: {owner}/{repo} | PR #{number}
问题摘要: {issue_summary}
总问题数: 安全={len(state.get('security_issues', []))} 逻辑={len(state.get('logic_issues', []))} 风格={len(state.get('style_issues', []))}
置信度: {state.get('confidence', 0):.2f}"""

    _memory_collection.upsert(
        ids=[f"{owner}/{repo}#pr{number}"],
        documents=[content],
        metadatas=[{"repo": f"{owner}/{repo}", "type": "pr_review"}],
    )
    return {}


# ── 并行分发（Send API） ───────────────────────────────────────────────

class ReviewTaskState(TypedDict):
    """Send API 子节点的状态：PRState 的子集 + review_type 标记"""
    pr_url: str
    pr_number: int
    title: str
    body: str
    author: str
    base_branch: str
    head_branch: str
    changed_files: list[dict]
    comments: list[dict]
    project_map: str
    memory_context: str
    image_descriptions: str
    review_type: str   # "security" | "style" | "logic"


def dispatch_reviews(state: PRState) -> list[Send]:
    """从 PRState 提取子状态，向三个并行 run_review 节点各发一个 Send。"""
    base = {
        "pr_url":            state["pr_url"],
        "pr_number":         state.get("pr_number", 0),
        "title":             state.get("title", ""),
        "body":              state.get("body", ""),
        "author":            state.get("author", ""),
        "base_branch":       state.get("base_branch", ""),
        "head_branch":       state.get("head_branch", ""),
        "changed_files":     state.get("changed_files", []),
        "comments":          state.get("comments", []),
        "project_map":       state.get("project_map", ""),
        "memory_context":    state.get("memory_context", ""),
        "image_descriptions":state.get("image_descriptions", ""),
    }
    return [
        Send("run_review", {**base, "review_type": "security"}),
        Send("run_review", {**base, "review_type": "style"}),
        Send("run_review", {**base, "review_type": "logic"}),
    ]


def run_review(state: ReviewTaskState) -> dict:
    """单个 review 子任务，结果直接写回父 PRState 对应字段。"""
    t = state["review_type"]
    if t == "security":
        return analyze_security(state)   # → {"security_issues": [...]}
    elif t == "style":
        return analyze_style(state)      # → {"style_issues": [...]}
    else:
        return analyze_logic(state)      # → {"logic_issues": [...]}
