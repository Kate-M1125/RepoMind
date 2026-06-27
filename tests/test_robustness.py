"""
Layer 4 — 鲁棒性测试

验证边界输入不会抛异常，而是返回有意义的错误字符串或降级结果。
"""
import pytest
from tools.code_nav import (
    _read_file, _grep_code, _list_dir,
    _find_callers, _find_definition,
    _git_log_file, _git_blame, _search_commits,
)

OWNER = "requests"
REPO = "requests"


# ─────────────────────────────────────────────
# 工具层边界：不应抛异常，只返回错误字符串
# ─────────────────────────────────────────────

class TestToolsNeverRaise:
    """所有工具在任何输入下都不应抛出异常，只返回错误字符串。"""

    def test_read_file_empty_path(self, requests_repo):
        out = _read_file(OWNER, REPO, "")
        assert isinstance(out, str)

    def test_read_file_path_traversal_attempt(self, requests_repo):
        out = _read_file(OWNER, REPO, "../../etc/passwd")
        assert isinstance(out, str)

    def test_read_file_start_beyond_end_of_file(self, requests_repo):
        out = _read_file(OWNER, REPO, "src/requests/api.py", start_line=99999, end_line=100000)
        assert isinstance(out, str)

    def test_grep_empty_pattern(self, requests_repo):
        out = _grep_code(OWNER, REPO, "")
        assert isinstance(out, str)

    def test_grep_regex_special_chars(self, requests_repo):
        # Should not crash even if pattern has regex metacharacters
        out = _grep_code(OWNER, REPO, ".*[invalid(")
        assert isinstance(out, str)

    def test_list_dir_deeply_nested_missing_path(self, requests_repo):
        out = _list_dir(OWNER, REPO, "a/b/c/d/e/f/no_such")
        assert isinstance(out, str)

    def test_find_definition_empty_symbol(self, requests_repo):
        out = _find_definition(OWNER, REPO, "")
        assert isinstance(out, str)

    def test_find_callers_empty_func(self, requests_repo):
        out = _find_callers(OWNER, REPO, "")
        assert isinstance(out, str)

    def test_git_log_file_empty_path(self, requests_repo):
        out = _git_log_file(OWNER, REPO, "")
        assert isinstance(out, str)

    def test_git_blame_invalid_range(self, requests_repo):
        out = _git_blame(OWNER, REPO, "src/requests/api.py", 0, 0)
        assert isinstance(out, str)

    def test_git_blame_reversed_range(self, requests_repo):
        out = _git_blame(OWNER, REPO, "src/requests/api.py", 10, 5)
        assert isinstance(out, str)

    def test_search_commits_empty_keyword(self, requests_repo):
        out = _search_commits(OWNER, REPO, "")
        assert isinstance(out, str)

    def test_search_commits_very_long_keyword(self, requests_repo):
        out = _search_commits(OWNER, REPO, "x" * 500)
        assert isinstance(out, str)


class TestToolsWithUnknownRepo:
    """不存在的 owner/repo 不应崩溃，工具应触发 clone 尝试后降级。"""

    def test_read_file_unknown_repo_returns_string(self):
        from unittest.mock import patch
        # ensure_repo_indexed is lazy-imported inside _repo_path, patch at source
        with patch("context.repo_loader.ensure_repo_indexed", return_value=False):
            out = _read_file("nonexistent_org_xyz", "nonexistent_repo_xyz", "file.py")
        assert isinstance(out, str)

    def test_grep_code_unknown_repo_returns_string(self):
        from unittest.mock import patch
        with patch("context.repo_loader.ensure_repo_indexed", return_value=False):
            out = _grep_code("nonexistent_org_xyz", "nonexistent_repo_xyz", "def foo")
        assert isinstance(out, str)


# ─────────────────────────────────────────────
# LLM 输出防御：parse_json 边界
# ─────────────────────────────────────────────

class TestParseJsonRobustness:
    def test_empty_string_raises_value_error(self):
        from core.llm.client import parse_json
        with pytest.raises(ValueError):
            parse_json("")

    def test_just_braces_parses(self):
        from core.llm.client import parse_json
        out = parse_json("{}")
        assert out == {}

    def test_extra_text_before_json(self):
        from core.llm.client import parse_json
        out = parse_json('Sure, here is the result: {"key": "val"}')
        assert out["key"] == "val"

    def test_deeply_nested_json(self):
        from core.llm.client import parse_json
        raw = '{"a": {"b": {"c": [1, 2, 3]}}}'
        out = parse_json(raw)
        assert out["a"]["b"]["c"] == [1, 2, 3]

    def test_json_with_unicode(self):
        from core.llm.client import parse_json
        raw = '{"root_cause": "空指针异常"}'
        out = parse_json(raw)
        assert "空指针" in out["root_cause"]


# ─────────────────────────────────────────────
# state 边界：节点函数处理缺失/None 字段
# ─────────────────────────────────────────────

class TestStateBoundary:
    def test_context_sections_all_empty(self):
        from workflow.nodes.analyze import _context_sections
        state = {
            "code_context": "",
            "memory_context": None,
            "stack_trace_context": "",
            "related_prs": [],
            "image_descriptions": "",
        }
        out = _context_sections(state)
        assert out == ""

    def test_context_sections_none_values_skipped(self):
        from workflow.nodes.analyze import _context_sections
        state = {
            "code_context": None,
            "memory_context": None,
            "stack_trace_context": None,
            "related_prs": None,
            "image_descriptions": None,
        }
        # Should not raise, just skip None fields
        out = _context_sections(state)
        assert isinstance(out, str)

    def test_classify_fix_missing_issue_type_defaults_code_fix(self):
        from workflow.nodes.classify_fix import classify_fix
        state = {
            "issue_type": None,
            "title": "some bug",
            "body": "some body",
        }
        result = classify_fix(state)
        assert result["fix_type"] == "code_fix"
