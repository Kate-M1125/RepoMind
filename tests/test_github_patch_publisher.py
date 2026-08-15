"""第五阶段 Git Data API 发布器的纯单元测试。"""
from unittest.mock import MagicMock, patch

import pytest


def _response(data, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = data
    response.raise_for_status.return_value = None
    return response


def test_requires_github_token():
    from tools.github_patch_publisher import _headers
    with patch.dict("os.environ", {}, clear=True):
        with pytest.raises(ValueError, match="GITHUB_TOKEN"):
            _headers()


def test_collects_modified_file_as_blob(tmp_path):
    """修改文件应上传新 Blob，并保留基准 Tree 中的文件模式。"""
    from tools.github_patch_publisher import _collect_tree_entries
    (tmp_path / "app.py").write_text("value = 2\n")
    client = MagicMock()
    client.get.return_value = _response({
        "tree": [{"path": "app.py", "mode": "100644", "type": "blob"}],
    })
    client.post.return_value = _response({"sha": "blob-sha"}, status=201)
    patch_text = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    entries = _collect_tree_entries(
        client, "https://api.github.test/repos/a/b", "tree-sha",
        tmp_path, patch_text, {"Authorization": "Bearer test"},
    )
    assert entries == [{"path": "app.py", "mode": "100644",
                        "type": "blob", "sha": "blob-sha"}]
    payload = client.post.call_args.kwargs["json"]
    assert payload["encoding"] == "base64"


def test_collects_deleted_file_with_null_sha(tmp_path):
    """删除文件必须生成 sha=null 的 Tree 条目，不能只从新 Tree 中遗漏它。"""
    from tools.github_patch_publisher import _collect_tree_entries
    client = MagicMock()
    client.get.return_value = _response({
        "tree": [{"path": "old.py", "mode": "100644", "type": "blob"}],
    })
    patch_text = """diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-old = True
"""
    entries = _collect_tree_entries(
        client, "https://api.github.test/repos/a/b", "tree-sha",
        tmp_path, patch_text, {},
    )
    assert entries[0]["path"] == "old.py"
    assert entries[0]["sha"] is None


def test_rejects_fork_without_base_branch(tmp_path):
    """无法安全确定基准分支时，发布器必须在任何 GitHub 写入前终止。"""
    from tools.github_patch_publisher import publish_draft_pr
    with pytest.raises(ValueError, match="Fork PR"):
        publish_draft_pr(
            owner="a", repo="b", repo_root=tmp_path, base_sha="abc1234",
            base_branch="", run_id=1, patch="diff --git a/a b/a\n",
            root_cause="x", test_summary="passed", review_summary="approved",
        )


def test_same_repo_pr_has_publish_base_branch():
    from tools.github_actions_api import _run_source_metadata
    run = {"event": "pull_request", "head_sha": "merge", "pull_requests": [{"head": {
        "sha": "source", "ref": "feature", "repo": {"url": "https://api.github.com/repos/a/b"},
    }}]}
    result = _run_source_metadata(run, "a", "b")
    assert result["source_sha"] == "source"
    assert result["publish_base_branch"] == "feature"


def test_fork_pr_has_no_publish_base_branch():
    from tools.github_actions_api import _run_source_metadata
    run = {"event": "pull_request", "head_sha": "merge", "pull_requests": [{"head": {
        "sha": "source", "ref": "feature", "repo": {"url": "https://api.github.com/repos/fork/b"},
    }}]}
    assert _run_source_metadata(run, "a", "b")["publish_base_branch"] == ""


def test_recovers_existing_pull_request():
    from tools.github_patch_publisher import _recover_publication
    client = MagicMock()
    client.get.return_value = _response([{
        "html_url": "https://github.com/a/b/pull/7", "number": 7,
        "head": {"sha": "commit-sha"},
    }])
    result = _recover_publication(
        client, "https://api.github.test/repos/a/b", {}, owner="a",
        branch="repomind/fix", base_branch="main", base_sha="base",
        run_id=1, patch_digest="digest", body="body",
    )
    assert result["status"] == "recovered"
    assert result["number"] == 7
    client.post.assert_not_called()


def test_recovers_branch_only_after_commit_marker_check():
    from tools.github_patch_publisher import _recover_publication
    client = MagicMock()
    client.get.side_effect = [
        _response([]),
        _response({"object": {"sha": "commit-sha"}}),
        _response({
            "message": "fix\n\nRepoMind-Patch-SHA256: digest",
            "parents": [{"sha": "base"}],
        }),
    ]
    client.post.return_value = _response({
        "html_url": "https://github.com/a/b/pull/8", "number": 8,
        "head": {"sha": "commit-sha"},
    }, status=201)
    result = _recover_publication(
        client, "https://api.github.test/repos/a/b", {}, owner="a",
        branch="repomind/fix", base_branch="main", base_sha="base",
        run_id=1, patch_digest="digest", body="body",
    )
    assert result["status"] == "recovered"
    assert result["commit"] == "commit-sha"


def test_rejects_unverifiable_existing_branch():
    from tools.github_patch_publisher import _recover_publication
    client = MagicMock()
    client.get.side_effect = [
        _response([]),
        _response({"object": {"sha": "other"}}),
        _response({"message": "untrusted", "parents": [{"sha": "base"}]}),
    ]
    with pytest.raises(ValueError, match="摘要"):
        _recover_publication(
            client, "https://api.github.test/repos/a/b", {}, owner="a",
            branch="repomind/fix", base_branch="main", base_sha="base",
            run_id=1, patch_digest="digest", body="body",
        )
