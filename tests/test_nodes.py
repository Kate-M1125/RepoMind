"""
Layer 2 — 节点级测试 (workflow/nodes/)

策略：
- 纯函数直接测，不需要 mock
- 依赖 LLM 的节点用 unittest.mock.patch 替换 chat()，验证路由/格式逻辑
- 不测 LLM 输出质量（那是 eval 的职责）
"""
import os
import pytest
from unittest.mock import patch
from workflow.state import IssueState


def _state(**overrides) -> dict:
    """最小合法 IssueState，可按需覆盖字段。"""
    base = {
        "issue_url": "https://github.com/requests/requests/issues/1",
        "issue_number": 1,
        "title": "TypeError in Session.send",
        "body": "Getting TypeError when calling session.send(). Traceback:\n  File ...",
        "labels": ["bug"],
        "comments": [],
        "related_prs": [],
        "issue_type": "bug",
        "is_regression": False,
        "fix_type": "code_fix",
        "feature_exists": False,
        "existing_api": "",
        "root_cause": "null pointer in send()",
        "fix_suggestion": "add None check",
        "affected_files": ["requests/sessions.py"],
        "severity": "high",
        "code_changes": [],
        "suggested_labels": [],
        "final_report": "",
        "confidence": 0.0,
        "reflection_note": "",
        "image_descriptions": "",
        "stack_trace_context": "",
        "code_context": "",
        "memory_context": "",
        "human_decision": "",
        "iteration": 0,
        "error": None,
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────
# analyze.py — pure helper functions
# ─────────────────────────────────────────────

class TestContextSections:
    def test_empty_state_returns_empty_string(self):
        from workflow.nodes.analyze import _context_sections
        state = _state(code_context="", memory_context="", stack_trace_context="",
                       related_prs=[], image_descriptions="")
        assert _context_sections(state) == ""

    def test_includes_code_context(self):
        from workflow.nodes.analyze import _context_sections
        state = _state(code_context="def foo(): pass")
        assert "def foo(): pass" in _context_sections(state)

    def test_includes_memory_context(self):
        from workflow.nodes.analyze import _context_sections
        state = _state(memory_context="历史相似 bug: #123")
        assert "#123" in _context_sections(state)

    def test_includes_related_prs(self):
        from workflow.nodes.analyze import _context_sections
        state = _state(related_prs=["https://github.com/org/repo/pull/42"])
        out = _context_sections(state)
        assert "pull/42" in out

    def test_sections_joined_by_double_newline(self):
        from workflow.nodes.analyze import _context_sections
        state = _state(code_context="code here", memory_context="memory here")
        out = _context_sections(state)
        assert "\n\n" in out

    def test_multiple_prs_all_included(self):
        from workflow.nodes.analyze import _context_sections
        state = _state(related_prs=["url/1", "url/2", "url/3"])
        out = _context_sections(state)
        assert "url/1" in out and "url/2" in out and "url/3" in out


class TestResultDict:
    def test_converts_analyze_result_to_dict(self):
        from workflow.nodes.analyze import _result_dict
        from core.llm.client import AnalyzeResult
        result = AnalyzeResult(
            root_cause="missing check",
            fix_suggestion="add check",
            severity="medium",
            affected_files=["a.py"],
            code_changes=[],
            suggested_labels=["bug"],
        )
        d = _result_dict(result)
        assert d["root_cause"] == "missing check"
        assert d["severity"] == "medium"
        assert d["affected_files"] == ["a.py"]
        assert isinstance(d["code_changes"], list)

    def test_code_changes_are_serialized_to_dicts(self):
        from workflow.nodes.analyze import _result_dict
        from core.llm.client import AnalyzeResult, CodeChange
        result = AnalyzeResult(
            root_cause="x",
            fix_suggestion="y",
            code_changes=[CodeChange(file="a.py", line=10, change="fix")],
        )
        d = _result_dict(result)
        assert d["code_changes"][0] == {"file": "a.py", "line": 10, "change": "fix"}


# ─────────────────────────────────────────────
# route.py — FORCE_ISSUE_TYPE shortcut (no LLM)
# ─────────────────────────────────────────────

class TestRouteIssue:
    def test_force_env_var_bypasses_llm(self):
        from workflow.nodes.route import route_issue
        for t in ("bug", "feature", "question", "security"):
            with patch.dict(os.environ, {"FORCE_ISSUE_TYPE": t}):
                result = route_issue(_state())
                assert result["issue_type"] == t

    def test_force_env_var_case_insensitive(self):
        from workflow.nodes.route import route_issue
        with patch.dict(os.environ, {"FORCE_ISSUE_TYPE": "BUG"}):
            result = route_issue(_state())
            assert result["issue_type"] == "bug"

    def test_llm_result_validated_to_known_type(self):
        from workflow.nodes.route import route_issue
        with patch.dict(os.environ, {"FORCE_ISSUE_TYPE": ""}):
            with patch("workflow.nodes.route.chat", return_value="bug"):
                result = route_issue(_state())
                assert result["issue_type"] == "bug"

    def test_unknown_llm_output_falls_back_to_bug(self):
        from workflow.nodes.route import route_issue
        with patch.dict(os.environ, {"FORCE_ISSUE_TYPE": ""}):
            with patch("workflow.nodes.route.chat", return_value="unknown_type"):
                result = route_issue(_state())
                assert result["issue_type"] == "bug"

    @pytest.mark.parametrize("issue_type", ["bug", "feature", "question", "security"])
    def test_all_valid_types_accepted(self, issue_type):
        from workflow.nodes.route import route_issue
        with patch.dict(os.environ, {"FORCE_ISSUE_TYPE": ""}):
            with patch("workflow.nodes.route.chat", return_value=issue_type):
                result = route_issue(_state())
                assert result["issue_type"] == issue_type


# ─────────────────────────────────────────────
# classify_fix.py
# ─────────────────────────────────────────────

class TestClassifyFix:
    def test_non_bug_always_returns_code_fix(self):
        from workflow.nodes.classify_fix import classify_fix
        for t in ("feature", "question", "security"):
            result = classify_fix(_state(issue_type=t))
            assert result["fix_type"] == "code_fix"

    def test_bug_calls_llm_and_returns_valid_type(self):
        from workflow.nodes.classify_fix import classify_fix
        with patch("workflow.nodes.classify_fix.chat",
                   return_value='{"fix_type": "dependency_upgrade"}'):
            result = classify_fix(_state(issue_type="bug"))
            assert result["fix_type"] == "dependency_upgrade"

    def test_invalid_llm_fix_type_falls_back_to_code_fix(self):
        from workflow.nodes.classify_fix import classify_fix
        with patch("workflow.nodes.classify_fix.chat",
                   return_value='{"fix_type": "some_invalid_type"}'):
            result = classify_fix(_state(issue_type="bug"))
            assert result["fix_type"] == "code_fix"

    def test_llm_exception_falls_back_to_code_fix(self):
        from workflow.nodes.classify_fix import classify_fix
        with patch("workflow.nodes.classify_fix.chat", side_effect=Exception("timeout")):
            result = classify_fix(_state(issue_type="bug"))
            assert result["fix_type"] == "code_fix"

    @pytest.mark.parametrize("fix_type", ["code_fix", "dependency_upgrade", "config_fix"])
    def test_all_valid_fix_types_accepted(self, fix_type):
        from workflow.nodes.classify_fix import classify_fix
        with patch("workflow.nodes.classify_fix.chat",
                   return_value=f'{{"fix_type": "{fix_type}"}}'):
            result = classify_fix(_state(issue_type="bug"))
            assert result["fix_type"] == fix_type


# ─────────────────────────────────────────────
# reflect.py
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# detect_regression.py
# ─────────────────────────────────────────────

class TestDetectRegression:
    def test_non_bug_always_false(self):
        from workflow.nodes.detect_regression import detect_regression
        for t in ("feature", "question", "security"):
            result = detect_regression(_state(issue_type=t))
            assert result["is_regression"] is False

    def test_bug_with_regression_signal(self):
        from workflow.nodes.detect_regression import detect_regression
        with patch("workflow.nodes.detect_regression.chat",
                   return_value='{"is_regression": true}'):
            result = detect_regression(_state(issue_type="bug"))
            assert result["is_regression"] is True

    def test_bug_without_regression_signal(self):
        from workflow.nodes.detect_regression import detect_regression
        with patch("workflow.nodes.detect_regression.chat",
                   return_value='{"is_regression": false}'):
            result = detect_regression(_state(issue_type="bug"))
            assert result["is_regression"] is False

    def test_llm_exception_falls_back_to_false(self):
        from workflow.nodes.detect_regression import detect_regression
        with patch("workflow.nodes.detect_regression.chat", side_effect=Exception("timeout")):
            result = detect_regression(_state(issue_type="bug"))
            assert result["is_regression"] is False

    def test_result_is_bool_not_string(self):
        from workflow.nodes.detect_regression import detect_regression
        with patch("workflow.nodes.detect_regression.chat",
                   return_value='{"is_regression": true}'):
            result = detect_regression(_state(issue_type="bug"))
            assert isinstance(result["is_regression"], bool)


# ─────────────────────────────────────────────
# report.py — generate_report (100% pure)
# ─────────────────────────────────────────────

class TestGenerateReport:
    def test_report_contains_issue_number(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(issue_number=9999))
        assert "9999" in result["final_report"]

    def test_report_contains_title(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(title="my special bug title"))
        assert "my special bug title" in result["final_report"]

    def test_report_contains_root_cause(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(root_cause="null pointer in send()"))
        assert "null pointer in send()" in result["final_report"]

    def test_report_contains_fix_suggestion(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(fix_suggestion="add None check"))
        assert "add None check" in result["final_report"]

    def test_report_contains_affected_files(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(affected_files=["requests/sessions.py"]))
        assert "requests/sessions.py" in result["final_report"]

    def test_report_contains_severity(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(severity="critical"))
        assert "critical" in result["final_report"]

    def test_report_with_code_changes(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(
            code_changes=[{"file": "a.py", "line": 42, "change": "add check"}]
        ))
        assert "a.py" in result["final_report"]
        assert "42" in result["final_report"]

    def test_empty_affected_files_shows_placeholder(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(affected_files=[]))
        assert "暂未确定" in result["final_report"]

    def test_empty_code_changes_shows_placeholder(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(code_changes=[]))
        assert "无具体代码改动" in result["final_report"]

    def test_report_is_markdown(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state())
        report = result["final_report"]
        assert report.startswith("#")
        assert "##" in report
        assert "|" in report  # 表格

    def test_multiple_labels_all_shown(self):
        from workflow.nodes.report import generate_report
        result = generate_report(_state(suggested_labels=["bug", "regression", "high-priority"]))
        report = result["final_report"]
        assert "bug" in report
        assert "regression" in report
        assert "high-priority" in report


# ─────────────────────────────────────────────
# retrieve.py — pure helper functions
# ─────────────────────────────────────────────

class TestRrfMerge:
    def test_single_list_preserves_order(self):
        from workflow.nodes.retrieve import _rrf_merge
        docs = ["a", "b", "c"]
        result = _rrf_merge([docs])
        assert result == docs

    def test_merges_two_lists(self):
        from workflow.nodes.retrieve import _rrf_merge
        result = _rrf_merge([["a", "b"], ["b", "c"]])
        # "b" appears in both lists so should rank higher
        assert "b" in result
        assert result[0] == "b"

    def test_returns_unique_docs(self):
        from workflow.nodes.retrieve import _rrf_merge
        result = _rrf_merge([["a", "b"], ["a", "b"]])
        assert len(result) == len(set(result))

    def test_empty_lists_returns_empty(self):
        from workflow.nodes.retrieve import _rrf_merge
        assert _rrf_merge([]) == []
        assert _rrf_merge([[]]) == []

    def test_doc_in_more_lists_ranks_higher(self):
        from workflow.nodes.retrieve import _rrf_merge
        # "shared" appears in 3 lists, "solo" only in 1
        result = _rrf_merge([
            ["shared", "solo"],
            ["shared", "other"],
            ["shared", "another"],
        ])
        assert result.index("shared") < result.index("solo")


class TestApplyFiletypeWeights:
    def test_bug_type_ranks_source_over_doc(self):
        from workflow.nodes.retrieve import _apply_filetype_weights
        docs = ["src_doc", "doc_doc"]
        metas = {"src_doc": "source", "doc_doc": "doc"}
        result = _apply_filetype_weights(docs, metas, "bug")
        assert result.index("src_doc") < result.index("doc_doc")

    def test_question_type_can_boost_doc_over_lower_ranked_source(self):
        from workflow.nodes.retrieve import _apply_filetype_weights
        # question: source_weight=0.7, doc_weight=1.2
        # doc at rank 5 score = (1/5)*1.2 = 0.24
        # source at rank 4 score = (1/4)*0.7 = 0.175  → doc wins
        docs = ["A", "B", "C", "src_doc", "doc_doc"]
        metas = {d: "source" for d in ["A", "B", "C", "src_doc"]}
        metas["doc_doc"] = "doc"
        result = _apply_filetype_weights(docs, metas, "question")
        assert result.index("doc_doc") < result.index("src_doc")

    def test_unknown_issue_type_uses_bug_weights(self):
        from workflow.nodes.retrieve import _apply_filetype_weights
        docs = ["a", "b"]
        metas = {"a": "source", "b": "doc"}
        result_unknown = _apply_filetype_weights(docs, metas, "unknown_type")
        result_bug = _apply_filetype_weights(docs, metas, "bug")
        assert result_unknown == result_bug

    def test_empty_docs_returns_empty(self):
        from workflow.nodes.retrieve import _apply_filetype_weights
        assert _apply_filetype_weights([], {}, "bug") == []

    def test_preserves_all_docs(self):
        from workflow.nodes.retrieve import _apply_filetype_weights
        docs = ["a", "b", "c"]
        metas = {"a": "source", "b": "test", "c": "doc"}
        result = _apply_filetype_weights(docs, metas, "bug")
        assert set(result) == set(docs)


class TestGrepEntryPoints:
    def test_finds_known_symbol(self, requests_repo):
        from workflow.nodes.retrieve import _grep_entry_points
        result = _grep_entry_points("requests", "requests", ["HTTPAdapter"])
        assert result  # non-empty
        assert "HTTPAdapter" in result

    def test_unknown_symbol_returns_empty(self, requests_repo):
        from workflow.nodes.retrieve import _grep_entry_points
        result = _grep_entry_points("requests", "requests", ["__no_such_func_xyz__"])
        assert result == ""

    def test_empty_list_returns_empty(self, requests_repo):
        from workflow.nodes.retrieve import _grep_entry_points
        result = _grep_entry_points("requests", "requests", [])
        assert result == ""

    def test_multiple_symbols_finds_at_least_one(self, requests_repo):
        from workflow.nodes.retrieve import _grep_entry_points
        result = _grep_entry_points("requests", "requests", ["Session", "HTTPAdapter"])
        assert result


class TestReflect:
    def test_confidence_clamped_to_0_1(self):
        from workflow.nodes.reflect import reflect
        with patch("workflow.nodes.reflect.chat",
                   return_value='{"confidence": 1.5, "note": "looks good"}'):
            result = reflect(_state())
            assert result["confidence"] == 1.0

    def test_negative_confidence_clamped_to_zero(self):
        from workflow.nodes.reflect import reflect
        with patch("workflow.nodes.reflect.chat",
                   return_value='{"confidence": -0.3, "note": "bad"}'):
            result = reflect(_state())
            assert result["confidence"] == 0.0

    def test_iteration_increments(self):
        from workflow.nodes.reflect import reflect
        with patch("workflow.nodes.reflect.chat",
                   return_value='{"confidence": 0.8, "note": "ok"}'):
            result = reflect(_state(iteration=2))
            assert result["iteration"] == 3

    def test_reflection_note_captured(self):
        from workflow.nodes.reflect import reflect
        with patch("workflow.nodes.reflect.chat",
                   return_value='{"confidence": 0.7, "note": "file path missing"}'):
            result = reflect(_state())
            assert "file path missing" in result["reflection_note"]

    def test_missing_note_defaults_to_empty(self):
        from workflow.nodes.reflect import reflect
        with patch("workflow.nodes.reflect.chat",
                   return_value='{"confidence": 0.9}'):
            result = reflect(_state())
            assert result["reflection_note"] == ""
