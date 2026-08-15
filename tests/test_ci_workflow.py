"""CI Failure：URL、日志解析、API 映射、节点与图结构测试。"""
import io
import os
import zipfile
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DEEPSEEK_API_KEY", "test")
os.environ.setdefault("GITHUB_TOKEN", "test")


def _ci_state(**overrides):
    state = {
        "ci_url": "https://github.com/acme/widget/actions/runs/12345",
        "owner": "acme",
        "repo": "widget",
        "run_id": 12345,
        "run_info": {
            "name": "tests", "head_branch": "main", "head_sha": "abc123",
            "conclusion": "failure",
        },
        "failed_jobs": [{"name": "pytest", "conclusion": "failure"}],
        "failure_logs": "FAILED tests/test_api.py::test_login - AssertionError",
        "changed_files": [],
        "error_signatures": [],
        "failing_tests": [],
        "failure_type": "unknown",
        "project_map": "",
        "code_context": "",
        "root_cause": "assertion mismatch",
        "fix_suggestion": "fix status code",
        "suspected_files": ["app/api.py"],
        "suspicious_commit": "abc123",
        "confidence": 0.8,
        "reflection_note": "证据充分",
        "iteration": 0,
        "patch": "",
        "patch_files": [],
        "patch_explanation": "",
        "patch_risk": "unknown",
        "patch_status": "",
        "patch_errors": [],
        "patch_attempt": 0,
        "patch_history": [],
        "execute_patch": False,
        "verification_status": "skipped",
        "test_command": [],
        "targeted_test_result": {"status": "skipped"},
        "regression_test_result": {"status": "skipped"},
        "patch_review_issues": [],
        "patch_review_status": "",
        "patch_review_confidence": 0.0,
        "create_pr": False,
        "publish_status": "",
        "published_branch": "",
        "published_commit": "",
        "draft_pr_url": "",
        "publish_error": "",
        "final_report": "",
        "error": None,
    }
    state.update(overrides)
    return state


class TestParseActionsUrl:
    def test_valid_url(self):
        from tools.github_actions_api import parse_actions_run_url
        assert parse_actions_run_url(
            "https://github.com/fastapi/fastapi/actions/runs/98765"
        ) == ("fastapi", "fastapi", 98765)

    def test_job_suffix_is_accepted(self):
        from tools.github_actions_api import parse_actions_run_url
        assert parse_actions_run_url(
            "https://github.com/org/repo/actions/runs/42/job/99"
        ) == ("org", "repo", 42)

    @pytest.mark.parametrize("url", [
        "https://github.com/org/repo/issues/42",
        "https://example.com/org/repo/actions/runs/42",
        "not-a-url",
    ])
    def test_invalid_url(self, url):
        from tools.github_actions_api import parse_actions_run_url
        with pytest.raises(ValueError, match="Actions Run URL"):
            parse_actions_run_url(url)


class TestCiLogParser:
    def test_pytest_failure_and_location(self):
        from context.ci_log_parser import parse_ci_log
        result = parse_ci_log(
            "FAILED tests/test_auth.py::test_expired\n"
            "tests/test_auth.py:48: AssertionError\n"
        )
        assert result["failure_type"] == "test_failure"
        assert result["failing_tests"] == ["tests/test_auth.py::test_expired"]
        assert result["error_signatures"][0]["file"] == "tests/test_auth.py"
        assert result["error_signatures"][0]["line"] == 48

    def test_typescript_error(self):
        from context.ci_log_parser import parse_ci_log
        result = parse_ci_log("src/index.ts(12,4): error TS2322: Type string is not assignable")
        assert result["failure_type"] == "type_check"
        assert result["error_signatures"][0]["file"] == "src/index.ts"

    def test_ansi_and_timestamps_are_removed(self):
        from context.ci_log_parser import clean_actions_log
        cleaned = clean_actions_log("2026-01-01T01:02:03Z \x1b[31mERROR\x1b[0m")
        assert cleaned == "ERROR"

    def test_duplicate_signatures_are_deduplicated(self):
        from context.ci_log_parser import parse_ci_log
        result = parse_ci_log("Error: boom\nError: boom\nError: boom")
        assert len(result["error_signatures"]) == 1


class TestLogResponse:
    def test_plain_text(self):
        from tools.github_actions_api import _decode_log_response
        response = MagicMock(content=b"plain log")
        assert _decode_log_response(response) == "plain log"

    def test_zip(self):
        from tools.github_actions_api import _decode_log_response
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("1_test.txt", "failed log")
        response = MagicMock(content=buffer.getvalue())
        assert "failed log" in _decode_log_response(response)


class TestCiNodes:
    def test_parse_failure_logs(self):
        from workflow.nodes.ci import parse_failure_logs
        result = parse_failure_logs(_ci_state())
        assert result["failure_type"] == "test_failure"
        assert result["error_signatures"]

    def test_analyze_structured_result(self):
        from workflow.nodes.ci import analyze_ci_failure
        response = (
            '{"root_cause":"bad status","fix_suggestion":"return 401",'
            '"suspected_files":["app/api.py"],"suspicious_commit":"abc123",'
            '"confidence":0.9}'
        )
        with patch("workflow.nodes.ci.chat", return_value=response):
            result = analyze_ci_failure(_ci_state())
        assert result["root_cause"] == "bad status"
        assert result["confidence"] == 0.9

    def test_analyze_falls_back_to_first_signature(self):
        from workflow.nodes.ci import analyze_ci_failure
        state = _ci_state(error_signatures=[{
            "message": "AssertionError", "file": "tests/test_api.py"
        }])
        with patch("workflow.nodes.ci.chat", side_effect=RuntimeError("offline")):
            result = analyze_ci_failure(state)
        assert result["root_cause"] == "AssertionError"
        assert result["confidence"] == 0.2

    def test_report_contains_evidence(self):
        from workflow.nodes.ci import generate_ci_report
        report = generate_ci_report(_ci_state())["final_report"]
        assert "CI Run #12345" in report
        assert "app/api.py" in report
        assert "abc123" in report
        assert "assertion mismatch" in report


class TestCiGraph:
    def test_graph_contains_first_phase_nodes(self):
        """防止后续重构意外删除诊断、修订、审查或报告节点。"""
        from workflow.ci_graph import ci_app
        nodes = set(ci_app.get_graph().nodes)
        expected = {
            "fetch_ci_run", "parse_failure_logs", "build_project_map",
            "collect_ci_context", "analyze_ci_failure", "reflect_ci",
            "generate_patch", "validate_patch", "verify_patch", "revise_patch",
            "review_patch", "publish_patch", "generate_ci_report",
        }
        assert expected.issubset(nodes)
