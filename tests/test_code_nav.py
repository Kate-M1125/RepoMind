"""
工具层单测 — tools/code_nav.py

使用真实克隆的 requests/requests 仓库，不调用 LLM，结果确定。
repo path: /tmp/repomind_repos/requests__requests
"""
import pytest
from tools.code_nav import (
    _read_file, _grep_code, _list_dir,
    _find_callers, _find_definition,
    _git_log_file, _git_blame, _search_commits,
    READ_FILE, GREP_CODE, LIST_DIR,
    FIND_CALLERS, FIND_DEFINITION,
    GIT_LOG_FILE, GIT_BLAME, SEARCH_COMMITS,
)
from tools.base import Tool, build_tools

OWNER = "requests"
REPO = "requests"
API_FILE = "src/requests/api.py"
ADAPTER_FILE = "src/requests/adapters.py"


# ─────────────────────────────────────────────
# read_file
# ─────────────────────────────────────────────

class TestReadFile:
    def test_reads_known_file(self, requests_repo):
        out = _read_file(OWNER, REPO, API_FILE)
        assert "requests.api" in out
        assert f"L1-" in out

    def test_line_range_is_respected(self, requests_repo):
        out = _read_file(OWNER, REPO, API_FILE, start_line=1, end_line=5)
        assert "L1-5" in out
        lines = out.splitlines()
        # header + ≤5 content lines
        assert len(lines) <= 7

    def test_returns_error_for_missing_file(self, requests_repo):
        out = _read_file(OWNER, REPO, "nonexistent/path.py")
        assert "不存在" in out or "失败" in out

    def test_header_contains_owner_repo_path(self, requests_repo):
        out = _read_file(OWNER, REPO, API_FILE, 1, 3)
        assert f"{OWNER}/{REPO}" in out
        assert API_FILE in out


# ─────────────────────────────────────────────
# grep_code
# ─────────────────────────────────────────────

class TestGrepCode:
    def test_finds_known_function_definition(self, requests_repo):
        out = _grep_code(OWNER, REPO, "def get(")
        assert "def get(" in out
        assert "api.py" in out or "sessions.py" in out

    def test_returns_multiple_matches(self, requests_repo):
        out = _grep_code(OWNER, REPO, "def get(")
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) >= 2

    def test_returns_not_found_for_missing_pattern(self, requests_repo):
        out = _grep_code(OWNER, REPO, "def __this_never_exists_xyz_abc__")
        assert "未找到" in out

    def test_output_within_size_limit(self, requests_repo):
        # grep caps at 3000 chars
        out = _grep_code(OWNER, REPO, "import")
        assert len(out) <= 3000


# ─────────────────────────────────────────────
# list_dir
# ─────────────────────────────────────────────

class TestListDir:
    def test_root_contains_expected_dirs(self, requests_repo):
        out = _list_dir(OWNER, REPO)
        assert "src" in out
        assert "tests" in out

    def test_subdirectory_lists_files(self, requests_repo):
        out = _list_dir(OWNER, REPO, "src/requests")
        assert "api.py" in out
        assert "adapters.py" in out

    def test_nonexistent_dir_returns_error(self, requests_repo):
        out = _list_dir(OWNER, REPO, "no/such/path")
        assert "不存在" in out or "失败" in out

    def test_header_shows_path(self, requests_repo):
        out = _list_dir(OWNER, REPO, "src/requests")
        assert "src/requests" in out


# ─────────────────────────────────────────────
# find_definition
# ─────────────────────────────────────────────

class TestFindDefinition:
    def test_finds_known_class(self, requests_repo):
        out = _find_definition(OWNER, REPO, "HTTPAdapter")
        assert "HTTPAdapter" in out
        assert "adapters.py" in out

    def test_returns_signature_and_body(self, requests_repo):
        out = _find_definition(OWNER, REPO, "HTTPAdapter")
        # should include class definition line
        assert "class HTTPAdapter" in out

    def test_unknown_symbol_returns_not_found(self, requests_repo):
        out = _find_definition(OWNER, REPO, "__no_such_symbol_xyz__")
        assert "未找到" in out

    def test_finds_function_in_module(self, requests_repo):
        out = _find_definition(OWNER, REPO, "Session")
        assert "Session" in out


# ─────────────────────────────────────────────
# find_callers
# ─────────────────────────────────────────────

class TestFindCallers:
    def test_finds_call_sites(self, requests_repo):
        out = _find_callers(OWNER, REPO, "HTTPAdapter")
        assert "HTTPAdapter(" in out

    def test_filters_definition_line(self, requests_repo):
        out = _find_callers(OWNER, REPO, "HTTPAdapter")
        # definition line should be excluded
        for line in out.splitlines():
            assert "class HTTPAdapter" not in line

    def test_returns_multiple_callers(self, requests_repo):
        out = _find_callers(OWNER, REPO, "HTTPAdapter")
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) >= 2

    def test_unknown_function_returns_not_found(self, requests_repo):
        out = _find_callers(OWNER, REPO, "__xyz_no_such_func__")
        assert "未找到" in out


# ─────────────────────────────────────────────
# git_log_file
# ─────────────────────────────────────────────

class TestGitLogFile:
    def test_returns_commits_for_known_file(self, requests_repo):
        out = _git_log_file(OWNER, REPO, ADAPTER_FILE)
        assert out  # non-empty
        assert "未找到" not in out and "失败" not in out

    def test_output_has_hash_date_message_format(self, requests_repo):
        out = _git_log_file(OWNER, REPO, ADAPTER_FILE)
        first_line = out.splitlines()[0]
        parts = first_line.split(" ", 2)
        assert len(parts) == 3
        # hash is 7 hex chars
        assert len(parts[0]) == 7
        # date is YYYY-MM-DD
        assert len(parts[1]) == 10 and parts[1][4] == "-"

    def test_respects_n_limit(self, requests_repo):
        out = _git_log_file(OWNER, REPO, ADAPTER_FILE, n=3)
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) <= 3

    def test_missing_file_returns_no_history(self, requests_repo):
        out = _git_log_file(OWNER, REPO, "this_file_does_not_exist.py")
        assert "无提交历史" in out or len(out.strip()) == 0


# ─────────────────────────────────────────────
# git_blame
# ─────────────────────────────────────────────

class TestGitBlame:
    def test_returns_blame_for_valid_range(self, requests_repo):
        out = _git_blame(OWNER, REPO, ADAPTER_FILE, 1, 5)
        assert out and "失败" not in out

    def test_output_contains_commit_hash(self, requests_repo):
        out = _git_blame(OWNER, REPO, ADAPTER_FILE, 1, 5)
        # blame output has commit hashes at start of each line
        first_line = out.splitlines()[0]
        assert len(first_line.split()[0]) >= 7  # short or full hash

    def test_output_contains_line_numbers(self, requests_repo):
        out = _git_blame(OWNER, REPO, ADAPTER_FILE, 1, 5)
        # git blame output includes line number
        assert "1)" in out or "1 " in out


# ─────────────────────────────────────────────
# search_commits
# ─────────────────────────────────────────────

class TestSearchCommits:
    def test_finds_commits_with_known_keyword(self, requests_repo):
        out = _search_commits(OWNER, REPO, "redirect")
        assert out and "未找到" not in out

    def test_output_format_is_hash_date_message(self, requests_repo):
        out = _search_commits(OWNER, REPO, "redirect")
        first_line = out.splitlines()[0]
        parts = first_line.split(" ", 2)
        assert len(parts) == 3
        assert len(parts[0]) == 7

    def test_returns_not_found_for_no_match(self, requests_repo):
        out = _search_commits(OWNER, REPO, "__xyzzy_no_match_in_commits__")
        assert "未找到" in out

    def test_respects_n_limit(self, requests_repo):
        out = _search_commits(OWNER, REPO, "fix", n=2)
        lines = [l for l in out.splitlines() if l.strip()]
        assert len(lines) <= 2


# ─────────────────────────────────────────────
# Tool dataclass & build_tools
# ─────────────────────────────────────────────

class TestToolSchema:
    def test_tool_as_openai_has_required_fields(self):
        schema = READ_FILE.as_openai()
        assert schema["type"] == "function"
        fn = schema["function"]
        assert "name" in fn and "description" in fn and "parameters" in fn

    def test_all_tools_have_names(self):
        tools = [READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION,
                 GIT_LOG_FILE, GIT_BLAME, SEARCH_COMMITS]
        names = {t.name for t in tools}
        assert names == {
            "read_file", "grep_code", "list_dir", "find_callers", "find_definition",
            "git_log_file", "git_blame", "search_commits",
        }

    def test_build_tools_returns_schemas_and_executors(self):
        schemas, executors = build_tools(READ_FILE, GREP_CODE)
        assert len(schemas) == 2
        assert set(executors.keys()) == {"read_file", "grep_code"}
        assert callable(executors["read_file"])

    def test_build_tools_executor_is_callable(self):
        _, executors = build_tools(LIST_DIR)
        fn = executors["list_dir"]
        assert callable(fn)

    def test_required_params_declared(self):
        for tool in [READ_FILE, GREP_CODE, LIST_DIR, FIND_CALLERS, FIND_DEFINITION,
                     GIT_LOG_FILE, GIT_BLAME, SEARCH_COMMITS]:
            params = tool.parameters
            assert "required" in params
            assert "owner" in params["required"]
            assert "repo" in params["required"]
