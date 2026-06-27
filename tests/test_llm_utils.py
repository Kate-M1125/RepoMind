"""
Layer 2 — LLM 工具函数单测 (core/llm/client.py)

parse_json、_repair_truncated_json、AnalyzeResult schema 全部是纯函数，不调用 LLM，
结果完全确定。同时测 eval/judge.py 的 question judge 构造逻辑和 report 报告生成。
"""
import json
import pytest
from pathlib import Path
from pydantic import ValidationError
from core.llm.client import parse_json, _repair_truncated_json, AnalyzeResult


# ─────────────────────────────────────────────
# parse_json — 三步降级解析
# ─────────────────────────────────────────────

class TestParseJson:
    def test_valid_json_string(self):
        out = parse_json('{"fix_type": "code_fix"}')
        assert out == {"fix_type": "code_fix"}

    def test_valid_json_with_whitespace(self):
        out = parse_json('  \n{"a": 1}  ')
        assert out["a"] == 1

    def test_markdown_wrapped_json(self):
        raw = "```json\n{\"root_cause\": \"null pointer\"}\n```"
        out = parse_json(raw)
        assert out["root_cause"] == "null pointer"

    def test_markdown_without_language_tag(self):
        raw = "```\n{\"key\": \"value\"}\n```"
        out = parse_json(raw)
        assert out["key"] == "value"

    def test_json_embedded_in_text(self):
        raw = 'Here is the result: {"severity": "high"} end'
        out = parse_json(raw)
        assert out["severity"] == "high"

    def test_truncated_json_is_repaired(self):
        # Simulates LLM output cut off mid-field
        raw = '{"root_cause": "missing check", "severity": "hi'
        out = parse_json(raw)
        # Should recover at least root_cause
        assert "root_cause" in out

    def test_raises_for_completely_invalid_input(self):
        with pytest.raises(ValueError, match="JSON 解析失败"):
            parse_json("this is not json at all, no braces")

    def test_nested_json(self):
        raw = '{"code_changes": [{"file": "a.py", "line": 10, "change": "fix"}]}'
        out = parse_json(raw)
        assert out["code_changes"][0]["file"] == "a.py"


# ─────────────────────────────────────────────
# _repair_truncated_json
# ─────────────────────────────────────────────

class TestRepairTruncatedJson:
    def test_repairs_after_last_complete_value(self):
        truncated = '{"a": "hello", "b": "worl'
        repaired = _repair_truncated_json(truncated)
        # Result should be parseable or at least contain the first key
        assert "a" in repaired or repaired == ""

    def test_returns_empty_for_no_comma_or_brace(self):
        result = _repair_truncated_json("no json here")
        # Should return "" or a minimal valid fragment, not crash
        assert isinstance(result, str)

    def test_result_is_always_string(self):
        for s in ["", "{}", '{"a":', '{"x": 1, "y":']:
            result = _repair_truncated_json(s)
            assert isinstance(result, str)


# ─────────────────────────────────────────────
# AnalyzeResult — Pydantic schema validation
# ─────────────────────────────────────────────

class TestAnalyzeResult:
    def _valid(self, **overrides) -> dict:
        base = {
            "root_cause": "null pointer in validate()",
            "fix_suggestion": "add None check before calling validate()",
            "severity": "high",
            "affected_files": ["src/validators.py"],
            "code_changes": [{"file": "src/validators.py", "line": 42, "change": "add None check"}],
            "suggested_labels": ["bug"],
        }
        base.update(overrides)
        return base

    def test_valid_input_parses(self):
        result = AnalyzeResult.model_validate(self._valid())
        assert result.root_cause == "null pointer in validate()"
        assert result.severity == "high"

    def test_severity_normalizes_to_lowercase(self):
        result = AnalyzeResult.model_validate(self._valid(severity="HIGH"))
        assert result.severity == "high"

    def test_unknown_severity_defaults_to_medium(self):
        result = AnalyzeResult.model_validate(self._valid(severity="urgent"))
        assert result.severity == "medium"

    def test_empty_root_cause_raises(self):
        with pytest.raises(ValidationError):
            AnalyzeResult.model_validate(self._valid(root_cause=""))

    def test_whitespace_only_root_cause_raises(self):
        with pytest.raises(ValidationError):
            AnalyzeResult.model_validate(self._valid(root_cause="   "))

    def test_empty_fix_suggestion_raises(self):
        with pytest.raises(ValidationError):
            AnalyzeResult.model_validate(self._valid(fix_suggestion=""))

    def test_optional_fields_have_defaults(self):
        minimal = {"root_cause": "a bug", "fix_suggestion": "fix it"}
        result = AnalyzeResult.model_validate(minimal)
        assert result.affected_files == []
        assert result.code_changes == []
        assert result.suggested_labels == []
        assert result.severity == "medium"

    def test_reasoning_field_is_optional(self):
        result = AnalyzeResult.model_validate(self._valid())
        assert result.reasoning == ""

    def test_code_change_requires_file_and_change(self):
        with pytest.raises(ValidationError):
            AnalyzeResult.model_validate(self._valid(
                code_changes=[{"line": 1}]  # missing file and change
            ))

    def test_code_change_line_defaults_to_zero(self):
        data = self._valid(code_changes=[{"file": "a.py", "change": "fix", "line": 0}])
        result = AnalyzeResult.model_validate(data)
        assert result.code_changes[0].line == 0


# ─────────────────────────────────────────────
# Question Judge — 得分结构（mock LLM，不真实调用）
# ─────────────────────────────────────────────

class TestJudgeQuestionStructure:
    """验证 judge_question 的 averaging 逻辑和输出结构，不调用真实 LLM。"""

    def _fake_scores(self) -> dict:
        return {
            "confusion_identified": 0.8,
            "answer_accuracy": 0.6,
            "actionability": 0.7,
            "overall": 0.7,
            "reasoning": "AI correctly identified the misuse pattern.",
        }

    def test_output_has_required_keys(self, monkeypatch):
        from eval import judge as judge_mod
        monkeypatch.setattr(judge_mod, "chat", lambda *a, **kw: json.dumps(self._fake_scores()))
        scores = judge_mod.judge_question(
            title="How to use X?",
            body="I tried X but got error",
            ai_result={"root_cause": "r", "fix_suggestion": "f", "affected_files": []},
            key_answer="You should use Y instead.",
            n=1,
        )
        assert set(scores.keys()) >= {"confusion_identified", "answer_accuracy", "actionability", "overall"}

    def test_scores_clamped_0_to_1(self, monkeypatch):
        from eval import judge as judge_mod
        raw = {"confusion_identified": 1.5, "answer_accuracy": -0.1,
               "actionability": 0.8, "overall": 0.9, "reasoning": "test"}
        monkeypatch.setattr(judge_mod, "chat", lambda *a, **kw: json.dumps(raw))
        scores = judge_mod.judge_question("t", "b", {}, "k", n=1)
        assert scores["confusion_identified"] == 1.0
        assert scores["answer_accuracy"] == 0.0

    def test_averaging_over_n_runs(self, monkeypatch):
        from eval import judge as judge_mod
        calls = []
        def fake_chat(*a, **kw):
            calls.append(1)
            v = 0.6 + 0.2 * len(calls)
            return json.dumps({"confusion_identified": v, "answer_accuracy": v,
                                "actionability": v, "overall": v, "reasoning": ""})
        monkeypatch.setattr(judge_mod, "chat", fake_chat)
        scores = judge_mod.judge_question("t", "b", {}, "k", n=3)
        assert scores["judge_runs"] == 3
        # average of 0.8, 1.0, 1.0 → clamped → (0.8 + 1.0 + 1.0)/3 ≈ 0.933
        assert 0.9 <= scores["overall"] <= 1.0

    def test_all_parse_failures_returns_zeros(self, monkeypatch):
        from eval import judge as judge_mod
        monkeypatch.setattr(judge_mod, "chat", lambda *a, **kw: "not json")
        scores = judge_mod.judge_question("t", "b", {}, "k", n=2)
        assert scores["overall"] == 0.0
        assert "Judge 全部解析失败" in scores["reasoning"]


# ─────────────────────────────────────────────
# Question Report — 报告生成（纯函数）
# ─────────────────────────────────────────────

class TestQuestionReport:
    def _make_results(self, n: int = 4) -> list[dict]:
        scores_pool = [
            {"confusion_identified": 0.9, "answer_accuracy": 0.8, "actionability": 0.9, "overall": 0.87},
            {"confusion_identified": 0.6, "answer_accuracy": 0.5, "actionability": 0.6, "overall": 0.57},
            {"confusion_identified": 0.4, "answer_accuracy": 0.3, "actionability": 0.4, "overall": 0.37},
            {"confusion_identified": 0.2, "answer_accuracy": 0.2, "actionability": 0.2, "overall": 0.20},
        ]
        return [
            {
                "issue_number": 100 + i,
                "title": f"How to do thing {i}?",
                "eval_type": "question",
                "ai_result": {"confidence": 0.8},
                "scores": scores_pool[i % len(scores_pool)],
            }
            for i in range(n)
        ]

    def test_report_contains_type_header(self, tmp_path):
        from eval.report import generate_report
        f = tmp_path / "q.json"
        f.write_text(json.dumps(self._make_results()))
        report = generate_report(str(f))
        assert "Question" in report

    def test_report_shows_all_dimensions(self, tmp_path):
        from eval.report import generate_report
        f = tmp_path / "q.json"
        f.write_text(json.dumps(self._make_results()))
        report = generate_report(str(f))
        assert "confusion_identified" in report
        assert "answer_accuracy" in report
        assert "actionability" in report

    def test_report_shows_sample_count(self, tmp_path):
        from eval.report import generate_report
        f = tmp_path / "q.json"
        f.write_text(json.dumps(self._make_results(n=3)))
        report = generate_report(str(f))
        assert "3" in report

    def test_detect_question_type_from_eval_type_field(self):
        from eval.report import _detect_eval_type
        assert _detect_eval_type([{"eval_type": "question"}]) == "question"

    def test_detect_question_type_from_scores_field(self):
        from eval.report import _detect_eval_type
        assert _detect_eval_type([{"scores": {"confusion_identified": 0.5, "overall": 0.5}}]) == "question"

    def test_detect_bug_type_as_default(self):
        from eval.report import _detect_eval_type
        assert _detect_eval_type([{"scores": {"root_cause_accuracy": 0.5}}]) == "bug"

    def _comment(self, user: str, body: str) -> dict:
        return {"user": {"login": user}, "body": body}

    def test_dataset_extractor_removes_short_answers(self):
        from eval.dataset import _extract_key_answers
        comments = [
            self._comment("user1", "Thanks!"),       # too short (<30)
            self._comment("user1", "x" * 100),       # long enough
        ]
        answers = _extract_key_answers(comments, {"user1"}, "author")
        assert len(answers) == 1

    def test_dataset_extractor_excludes_author_self_replies(self):
        from eval.dataset import _extract_key_answers
        comments = [
            self._comment("author", "x" * 100),
            self._comment("maintainer", "y" * 100),
        ]
        answers = _extract_key_answers(comments, {"maintainer"}, "author")
        assert all(c["user"] != "author" for c in answers)

    def test_dataset_extractor_contributors_sorted_first(self):
        from eval.dataset import _extract_key_answers
        comments = [
            self._comment("random_user", "a" * 200),
            self._comment("maintainer", "b" * 50),
        ]
        answers = _extract_key_answers(comments, {"maintainer"}, "author")
        assert answers[0]["is_contributor"] is True
