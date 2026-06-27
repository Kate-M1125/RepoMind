"""
PR Review Agent 测试

Layer 1 — 纯函数（无需 mock）：parse_pr_url, _should_review, _format_diff, generate_pr_report
Layer 2 — LLM 节点（mock chat/chat_with_tools）：analyze_security, analyze_style, analyze_logic, reflect_pr
Layer 3 — Graph 结构校验（不跑 LLM）
"""
import json
import pytest
from unittest.mock import patch, MagicMock


# ── Helpers ───────────────────────────────────────────────────────────

def _pr_state(**overrides) -> dict:
    base = {
        "pr_url": "https://github.com/requests/requests/pull/42",
        "pr_number": 42,
        "title": "Fix session auth handling",
        "body": "This PR fixes auth handling in sessions.",
        "author": "contributor",
        "base_branch": "main",
        "head_branch": "fix/auth",
        "changed_files": [
            {
                "filename": "requests/sessions.py",
                "status": "modified",
                "additions": 5,
                "deletions": 2,
                "patch": "@@ -10,6 +10,9 @@\n+if auth is None:\n+    raise ValueError('auth required')\n context",
            }
        ],
        "comments": [],
        "security_issues": [],
        "style_issues": [],
        "logic_issues": [],
        "project_map": "",
        "memory_context": "",
        "image_descriptions": "",
        "confidence": 0.85,
        "reflection_note": "",
        "iteration": 0,
        "final_report": "",
        "error": None,
    }
    base.update(overrides)
    return base


def _issue(severity="medium", file="app.py", line=10, description="问题", suggestion="建议"):
    return {"severity": severity, "file": file, "line": line,
            "description": description, "suggestion": suggestion}


# ── Layer 1: tools/github_pr_api.py 纯函数 ───────────────────────────

class TestParsePrUrl:
    def test_valid_pr_url(self):
        from tools.github_pr_api import parse_pr_url
        owner, repo, number = parse_pr_url("https://github.com/fastapi/fastapi/pull/1234")
        assert owner == "fastapi"
        assert repo == "fastapi"
        assert number == 1234

    def test_org_repo_different(self):
        from tools.github_pr_api import parse_pr_url
        owner, repo, number = parse_pr_url("https://github.com/psf/requests/pull/99")
        assert owner == "psf"
        assert repo == "requests"
        assert number == 99

    def test_issue_url_raises(self):
        from tools.github_pr_api import parse_pr_url
        with pytest.raises(ValueError, match="不是合法的 GitHub PR URL"):
            parse_pr_url("https://github.com/fastapi/fastapi/issues/1234")

    def test_invalid_url_raises(self):
        from tools.github_pr_api import parse_pr_url
        with pytest.raises(ValueError):
            parse_pr_url("not-a-url")

    def test_trailing_slash_handled(self):
        from tools.github_pr_api import parse_pr_url
        owner, repo, number = parse_pr_url("https://github.com/psf/requests/pull/100/")
        assert number == 100


class TestShouldReview:
    def test_python_file_reviewed(self):
        from tools.github_pr_api import _should_review
        assert _should_review("src/app.py") is True

    def test_typescript_file_reviewed(self):
        from tools.github_pr_api import _should_review
        assert _should_review("src/index.ts") is True

    def test_lock_file_skipped(self):
        from tools.github_pr_api import _should_review
        assert _should_review("package-lock.json") is False

    def test_poetry_lock_skipped(self):
        from tools.github_pr_api import _should_review
        assert _should_review("poetry.lock") is False

    def test_png_skipped(self):
        from tools.github_pr_api import _should_review
        assert _should_review("docs/logo.png") is False

    def test_svg_skipped(self):
        from tools.github_pr_api import _should_review
        assert _should_review("assets/icon.svg") is False

    def test_requirements_txt_skipped(self):
        from tools.github_pr_api import _should_review
        assert _should_review("requirements.txt") is False

    def test_go_sum_skipped(self):
        from tools.github_pr_api import _should_review
        assert _should_review("go.sum") is False


# ── Layer 1: analyze_pr.py 纯函数 ────────────────────────────────────

class TestFormatDiff:
    def test_empty_list_returns_empty(self):
        from workflow.nodes.analyze_pr import _format_diff
        assert _format_diff([]) == ""

    def test_file_without_patch_skipped(self):
        from workflow.nodes.analyze_pr import _format_diff
        files = [{"filename": "a.py", "status": "modified",
                  "additions": 1, "deletions": 0, "patch": ""}]
        assert _format_diff(files) == ""

    def test_single_file_formatted(self):
        from workflow.nodes.analyze_pr import _format_diff
        files = [{"filename": "app.py", "status": "modified",
                  "additions": 3, "deletions": 1, "patch": "+new line\n-old line"}]
        result = _format_diff(files)
        assert "app.py" in result
        assert "+new line" in result
        assert "```diff" in result

    def test_long_patch_truncated(self):
        from workflow.nodes.analyze_pr import _format_diff, _MAX_PATCH_LINES
        long_patch = "\n".join([f"+line {i}" for i in range(_MAX_PATCH_LINES + 50)])
        files = [{"filename": "big.py", "status": "modified",
                  "additions": 200, "deletions": 0, "patch": long_patch}]
        result = _format_diff(files)
        assert "truncated" in result

    def test_max_files_respected(self):
        from workflow.nodes.analyze_pr import _format_diff, _MAX_FILES
        files = [
            {"filename": f"file{i}.py", "status": "modified",
             "additions": 1, "deletions": 0, "patch": f"+line {i}"}
            for i in range(_MAX_FILES + 5)
        ]
        result = _format_diff(files)
        assert f"file{_MAX_FILES + 1}.py" not in result

    def test_multiple_files_separated(self):
        from workflow.nodes.analyze_pr import _format_diff
        files = [
            {"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "+a"},
            {"filename": "b.py", "status": "added",    "additions": 2, "deletions": 0, "patch": "+b"},
        ]
        result = _format_diff(files)
        assert "a.py" in result and "b.py" in result


class TestPrHeader:
    def test_contains_pr_number(self):
        from workflow.nodes.analyze_pr import _pr_header
        state = _pr_state()
        header = _pr_header(state)
        assert "42" in header

    def test_contains_title(self):
        from workflow.nodes.analyze_pr import _pr_header
        state = _pr_state(title="My PR Title")
        assert "My PR Title" in _pr_header(state)

    def test_contains_repo(self):
        from workflow.nodes.analyze_pr import _pr_header
        state = _pr_state()
        header = _pr_header(state)
        assert "requests/requests" in header


class TestIssuesSection:
    def test_empty_issues_shows_ok(self):
        from workflow.nodes.analyze_pr import _issues_section
        result = _issues_section("🔒 安全审查", [])
        assert "✅" in result
        assert "无问题" in result

    def test_single_issue_rendered(self):
        from workflow.nodes.analyze_pr import _issues_section
        issues = [_issue(severity="high", file="auth.py", line=42, description="SQL注入风险")]
        result = _issues_section("🔒 安全审查", issues)
        assert "auth.py" in result
        assert "SQL注入风险" in result
        assert "HIGH" in result

    def test_severity_emoji_critical(self):
        from workflow.nodes.analyze_pr import _issues_section
        issues = [_issue(severity="critical")]
        result = _issues_section("安全", issues)
        assert "🔴" in result

    def test_severity_emoji_low(self):
        from workflow.nodes.analyze_pr import _issues_section
        issues = [_issue(severity="low")]
        result = _issues_section("风格", issues)
        assert "🔵" in result

    def test_line_number_shown(self):
        from workflow.nodes.analyze_pr import _issues_section
        issues = [_issue(line=99)]
        result = _issues_section("逻辑", issues)
        assert "L99" in result

    def test_line_zero_hidden(self):
        from workflow.nodes.analyze_pr import _issues_section
        issues = [_issue(line=0)]
        result = _issues_section("逻辑", issues)
        assert "L0" not in result


class TestGeneratePrReport:
    def test_no_issues_approve(self):
        from workflow.nodes.analyze_pr import generate_pr_report
        state = _pr_state(security_issues=[], style_issues=[], logic_issues=[], confidence=0.9)
        result = generate_pr_report(state)
        assert "APPROVE" in result["final_report"]
        assert "REQUEST CHANGES" not in result["final_report"]

    def test_high_severity_request_changes(self):
        from workflow.nodes.analyze_pr import generate_pr_report
        state = _pr_state(
            security_issues=[_issue(severity="high")],
            logic_issues=[_issue(severity="critical")],
            style_issues=[],
            confidence=0.8,
        )
        result = generate_pr_report(state)
        assert "REQUEST CHANGES" in result["final_report"]

    def test_only_low_issues_approve(self):
        from workflow.nodes.analyze_pr import generate_pr_report
        state = _pr_state(
            security_issues=[],
            style_issues=[_issue(severity="low"), _issue(severity="medium")],
            logic_issues=[],
            confidence=0.85,
        )
        result = generate_pr_report(state)
        assert "APPROVE" in result["final_report"]

    def test_report_contains_pr_number(self):
        from workflow.nodes.analyze_pr import generate_pr_report
        state = _pr_state(pr_number=99)
        result = generate_pr_report(state)
        assert "99" in result["final_report"]

    def test_report_contains_all_sections(self):
        from workflow.nodes.analyze_pr import generate_pr_report
        state = _pr_state(security_issues=[_issue()], style_issues=[], logic_issues=[])
        report = generate_pr_report(state)["final_report"]
        assert "安全审查" in report
        assert "逻辑审查" in report
        assert "风格审查" in report

    def test_returns_final_report_key(self):
        from workflow.nodes.analyze_pr import generate_pr_report
        result = generate_pr_report(_pr_state())
        assert "final_report" in result
        assert isinstance(result["final_report"], str)


# ── Layer 2: LLM 节点（mock chat_with_tools）───────────────────────

class TestAnalyzeSecurity:
    def _mock_json(self, issues=None):
        return json.dumps({"reasoning": "分析完成", "issues": issues or [], "summary": "safe"})

    def test_returns_security_issues_list(self):
        from workflow.nodes.analyze_pr import analyze_security
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=self._mock_json()):
            result = analyze_security(_pr_state())
        assert "security_issues" in result
        assert isinstance(result["security_issues"], list)

    def test_issues_passed_through(self):
        from workflow.nodes.analyze_pr import analyze_security
        issues = [{"severity": "high", "file": "app.py", "line": 1,
                   "description": "SQL injection", "suggestion": "use parameterized queries"}]
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=self._mock_json(issues)):
            result = analyze_security(_pr_state())
        assert len(result["security_issues"]) == 1
        assert result["security_issues"][0]["severity"] == "high"

    def test_empty_issues_on_safe_code(self):
        from workflow.nodes.analyze_pr import analyze_security
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=self._mock_json([])):
            result = analyze_security(_pr_state())
        assert result["security_issues"] == []

    def test_malformed_json_fallback_to_empty(self):
        from workflow.nodes.analyze_pr import analyze_security
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value='{"reasoning": "ok"}'):
            result = analyze_security(_pr_state())
        assert result["security_issues"] == []


class TestAnalyzeStyle:
    def _mock_json(self, issues=None):
        return json.dumps({"reasoning": "ok", "issues": issues or [], "summary": "clean"})

    def test_returns_style_issues_key(self):
        from workflow.nodes.analyze_pr import analyze_style
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=self._mock_json()):
            result = analyze_style(_pr_state())
        assert "style_issues" in result

    def test_style_issues_populated(self):
        from workflow.nodes.analyze_pr import analyze_style
        issues = [{"severity": "low", "file": "b.py", "line": 5,
                   "description": "函数过长", "suggestion": "拆分函数"}]
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=self._mock_json(issues)):
            result = analyze_style(_pr_state())
        assert result["style_issues"][0]["description"] == "函数过长"


class TestAnalyzeLogic:
    def _mock_json(self, issues=None):
        return json.dumps({"reasoning": "ok", "issues": issues or [], "summary": "looks good"})

    def test_returns_logic_issues_key(self):
        from workflow.nodes.analyze_pr import analyze_logic
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=self._mock_json()):
            result = analyze_logic(_pr_state())
        assert "logic_issues" in result

    def test_critical_logic_issue(self):
        from workflow.nodes.analyze_pr import analyze_logic
        issues = [{"severity": "critical", "file": "auth.py", "line": 20,
                   "description": "空指针未处理", "suggestion": "加 None 检查"}]
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=self._mock_json(issues)):
            result = analyze_logic(_pr_state())
        assert result["logic_issues"][0]["severity"] == "critical"


class TestReflectPr:
    def test_confidence_extracted(self):
        from workflow.nodes.analyze_pr import reflect_pr
        mock_response = '{"confidence": 0.88, "note": "审查质量良好"}'
        with patch("workflow.nodes.analyze_pr.chat", return_value=mock_response):
            result = reflect_pr(_pr_state())
        assert abs(result["confidence"] - 0.88) < 1e-6

    def test_confidence_clamped_above_one(self):
        from workflow.nodes.analyze_pr import reflect_pr
        with patch("workflow.nodes.analyze_pr.chat", return_value='{"confidence": 1.5, "note": ""}'):
            result = reflect_pr(_pr_state())
        assert result["confidence"] <= 1.0

    def test_confidence_clamped_below_zero(self):
        from workflow.nodes.analyze_pr import reflect_pr
        with patch("workflow.nodes.analyze_pr.chat", return_value='{"confidence": -0.3, "note": ""}'):
            result = reflect_pr(_pr_state())
        assert result["confidence"] >= 0.0

    def test_iteration_incremented(self):
        from workflow.nodes.analyze_pr import reflect_pr
        with patch("workflow.nodes.analyze_pr.chat", return_value='{"confidence": 0.9, "note": ""}'):
            result = reflect_pr(_pr_state(iteration=1))
        assert result["iteration"] == 2

    def test_note_passed_through(self):
        from workflow.nodes.analyze_pr import reflect_pr
        with patch("workflow.nodes.analyze_pr.chat", return_value='{"confidence": 0.7, "note": "逻辑问题存疑"}'):
            result = reflect_pr(_pr_state())
        assert "逻辑问题存疑" in result["reflection_note"]


# ── Layer 3: Graph 结构校验 ───────────────────────────────────────────

class TestDispatchReviews:
    def test_returns_three_sends(self):
        from langgraph.types import Send
        from workflow.nodes.analyze_pr import dispatch_reviews
        sends = dispatch_reviews(_pr_state())
        assert len(sends) == 3
        assert all(isinstance(s, Send) for s in sends)

    def test_all_review_types_covered(self):
        from workflow.nodes.analyze_pr import dispatch_reviews
        sends = dispatch_reviews(_pr_state())
        types = {s.arg["review_type"] for s in sends}
        assert types == {"security", "style", "logic"}

    def test_all_sends_target_run_review(self):
        from workflow.nodes.analyze_pr import dispatch_reviews
        sends = dispatch_reviews(_pr_state())
        assert all(s.node == "run_review" for s in sends)

    def test_pr_url_propagated_to_each_send(self):
        from workflow.nodes.analyze_pr import dispatch_reviews
        sends = dispatch_reviews(_pr_state(pr_url="https://github.com/psf/requests/pull/99"))
        assert all("psf/requests/pull/99" in s.arg["pr_url"] for s in sends)

    def test_changed_files_propagated(self):
        from workflow.nodes.analyze_pr import dispatch_reviews
        files = [{"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0, "patch": "+x"}]
        sends = dispatch_reviews(_pr_state(changed_files=files))
        assert all(s.arg["changed_files"] == files for s in sends)


class TestRunReview:
    def _mock_json(self, key, issues=None):
        return {"issues": issues or [], "reasoning": "ok", "summary": "done"}

    def test_security_type_calls_analyze_security(self):
        from workflow.nodes.analyze_pr import run_review
        state = {**_pr_state(), "review_type": "security"}
        with patch("workflow.nodes.analyze_pr.chat_with_tools",
                   return_value='{"issues": [], "reasoning": "", "summary": ""}'):
            result = run_review(state)
        assert "security_issues" in result

    def test_style_type_calls_analyze_style(self):
        from workflow.nodes.analyze_pr import run_review
        state = {**_pr_state(), "review_type": "style"}
        with patch("workflow.nodes.analyze_pr.chat_with_tools",
                   return_value='{"issues": [], "reasoning": "", "summary": ""}'):
            result = run_review(state)
        assert "style_issues" in result

    def test_logic_type_calls_analyze_logic(self):
        from workflow.nodes.analyze_pr import run_review
        state = {**_pr_state(), "review_type": "logic"}
        with patch("workflow.nodes.analyze_pr.chat_with_tools",
                   return_value='{"issues": [], "reasoning": "", "summary": ""}'):
            result = run_review(state)
        assert "logic_issues" in result

    def test_each_type_writes_different_key(self):
        from workflow.nodes.analyze_pr import run_review
        mock_empty = '{"issues": [], "reasoning": "", "summary": ""}'
        with patch("workflow.nodes.analyze_pr.chat_with_tools", return_value=mock_empty):
            r_sec = run_review({**_pr_state(), "review_type": "security"})
            r_sty = run_review({**_pr_state(), "review_type": "style"})
            r_log = run_review({**_pr_state(), "review_type": "logic"})
        assert set(r_sec.keys()) == {"security_issues"}
        assert set(r_sty.keys()) == {"style_issues"}
        assert set(r_log.keys()) == {"logic_issues"}


class TestPrGraphStructure:
    def test_all_expected_nodes_present(self):
        from workflow.pr_graph import pr_app
        nodes = list(pr_app.get_graph().nodes.keys())
        expected = [
            "fetch_pr", "build_project_map", "describe_pr_images",
            "memory_retrieve_pr", "prepare_reviews", "run_review",
            "reflect_pr", "generate_pr_report", "memory_save_pr",
        ]
        for node in expected:
            assert node in nodes, f"缺少节点: {node}"

    def test_sequential_nodes_removed(self):
        from workflow.pr_graph import pr_app
        nodes = list(pr_app.get_graph().nodes.keys())
        assert "analyze_security" not in nodes
        assert "analyze_style" not in nodes
        assert "analyze_logic" not in nodes

    def test_graph_has_start_and_end(self):
        from workflow.pr_graph import pr_app
        nodes = list(pr_app.get_graph().nodes.keys())
        assert "__start__" in nodes
        assert "__end__" in nodes

    def test_pr_state_required_fields(self):
        from workflow.pr_state import PRState
        required = {"pr_url", "pr_number", "title", "changed_files",
                    "security_issues", "style_issues", "logic_issues",
                    "confidence", "iteration", "final_report"}
        annotations = PRState.__annotations__
        for field in required:
            assert field in annotations, f"PRState 缺少字段: {field}"


# ── URL 路由检测（main.py 逻辑） ──────────────────────────────────────

class TestUrlRouting:
    def test_pr_url_detected(self):
        import re
        url = "https://github.com/fastapi/fastapi/pull/567"
        assert re.search(r"github\.com/[^/]+/[^/]+/pull/\d+", url)

    def test_issue_url_not_detected_as_pr(self):
        import re
        url = "https://github.com/fastapi/fastapi/issues/1234"
        assert not re.search(r"github\.com/[^/]+/[^/]+/pull/\d+", url)

    def test_pr_url_with_extra_path_not_detected(self):
        import re
        url = "https://github.com/fastapi/fastapi/pull/567/files"
        # 带 /files 后缀的 PR 链接也应能识别
        assert re.search(r"github\.com/[^/]+/[^/]+/pull/\d+", url)
