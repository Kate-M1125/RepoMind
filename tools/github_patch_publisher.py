"""通过 GitHub Git Data API 发布已验证补丁，并创建 Draft PR。"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

import httpx

from tools.patch_validator import patch_path_changes


MAX_PUBLISHED_FILE_BYTES = 2 * 1024 * 1024


def _headers() -> dict[str, str]:
    """发布属于 GitHub 写操作，必须提供 Token；不允许像只读分析一样匿名降级。"""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise ValueError("创建 Draft PR 需要 GITHUB_TOKEN")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api_json(response: httpx.Response, operation: str) -> dict:
    """统一检查 GitHub 响应，并限制错误正文长度，避免报告中泄露过多服务端信息。"""
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text[:500]
        raise ValueError(f"{operation}失败（HTTP {response.status_code}）: {detail}") from exc
    return response.json()


def _materialize_patch(repo_root: Path, base_sha: str, patch: str, destination: Path):
    """在临时 detached worktree 应用最终补丁，供发布器读取准确文件内容。"""
    # detached worktree 固定在已经测试过的 source_sha，避免默认分支后续更新造成漂移。
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(destination), base_sha], cwd=repo_root,
        check=True, capture_output=True, text=True, timeout=60,
    )
    # 发布前再次应用最终补丁；这里失败说明验证后状态发生变化，必须停止写 GitHub。
    subprocess.run(
        ["git", "apply", "--whitespace=error-all", "-"], cwd=destination,
        input=patch, check=True, capture_output=True, text=True, timeout=30,
    )


def _collect_tree_entries(client: httpx.Client, api_base: str, base_tree: str,
                          worktree: Path, patch: str, headers: dict) -> list[dict]:
    # 读取完整基准 Tree 是为了保留可执行位，并正确处理修改、删除和 rename。
    tree_data = _api_json(
        client.get(f"{api_base}/git/trees/{base_tree}", headers=headers, params={"recursive": "1"}),
        "读取基准 Git Tree",
    )
    modes = {item["path"]: item.get("mode", "100644") for item in tree_data.get("tree", [])}
    entries = []
    seen = set()

    for old_path, new_path in patch_path_changes(patch):
        # rename 需要显式删除旧路径；普通修改 old/new 相同，只写一次新 Blob。
        if old_path != new_path and old_path not in seen:
            entries.append({"path": old_path, "mode": modes.get(old_path, "100644"),
                            "type": "blob", "sha": None})
            seen.add(old_path)

        if new_path in seen:
            continue
        seen.add(new_path)
        local_file = worktree / new_path
        # 应用补丁后文件不存在表示删除，Git Trees API 用 sha=null 表示删除条目。
        if not local_file.exists():
            entries.append({"path": new_path, "mode": modes.get(new_path, "100644"),
                            "type": "blob", "sha": None})
            continue
        if local_file.is_symlink() or not local_file.is_file():
            raise ValueError(f"自动发布不支持符号链接或特殊文件: {new_path}")
        content = local_file.read_bytes()
        if len(content) > MAX_PUBLISHED_FILE_BYTES:
            raise ValueError(f"自动发布文件超过 2MB 限制: {new_path}")
        # 每个新文件内容先成为不可变 Blob，随后由新 Tree 引用。
        blob = _api_json(
            client.post(f"{api_base}/git/blobs", headers=headers, json={
                "content": base64.b64encode(content).decode(), "encoding": "base64",
            }),
            f"创建 Blob {new_path}",
        )
        mode = modes.get(new_path) or modes.get(old_path) or "100644"
        if mode not in {"100644", "100755"}:
            raise ValueError(f"自动发布不支持文件模式 {mode}: {new_path}")
        entries.append({"path": new_path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    return entries


def _pr_body(run_id: int, root_cause: str, test_summary: str, review_summary: str) -> str:
    return f"""## CI failure

Generated from Actions Run #{run_id}.

## Root cause

{root_cause}

## Validation

{test_summary}

## Patch review

{review_summary}

> This is an AI-generated draft. Human review is required before merge.
"""


def _create_pr(client: httpx.Client, api_base: str, headers: dict, *, owner: str,
               branch: str, base_branch: str, run_id: int, body: str) -> dict:
    pr = _api_json(
        client.post(f"{api_base}/pulls", headers=headers, json={
            "title": f"Fix CI failure in run #{run_id}", "head": branch,
            "base": base_branch, "body": body, "draft": True,
            "maintainer_can_modify": True,
        }),
        "创建 Draft PR",
    )
    return {"status": "created", "branch": branch,
            "commit": pr.get("head", {}).get("sha", ""),
            "url": pr["html_url"], "number": pr["number"]}


def _recover_publication(client: httpx.Client, api_base: str, headers: dict, *,
                         owner: str, branch: str, base_branch: str, base_sha: str,
                         run_id: int, patch_digest: str, body: str) -> dict | None:
    """恢复“分支/PR 已创建但响应丢失”的发布，避免重复资源。"""
    pulls = _api_json(
        client.get(f"{api_base}/pulls", headers=headers, params={
            "state": "all", "head": f"{owner}:{branch}", "base": base_branch,
        }),
        "查询已有修复 PR",
    )
    if pulls:
        pr = pulls[0]
        return {"status": "recovered", "branch": branch,
                "commit": pr.get("head", {}).get("sha", ""),
                "url": pr["html_url"], "number": pr["number"]}

    ref = client.get(f"{api_base}/git/ref/heads/{branch}", headers=headers)
    if ref.status_code == 404:
        return None
    ref_data = _api_json(ref, "读取已有修复分支")
    commit_sha = ref_data.get("object", {}).get("sha", "")
    commit = _api_json(
        client.get(f"{api_base}/git/commits/{commit_sha}", headers=headers),
        "验证已有修复 Commit",
    )
    marker = f"RepoMind-Patch-SHA256: {patch_digest}"
    parents = [item.get("sha") for item in commit.get("parents", [])]
    if marker not in commit.get("message", "") or base_sha not in parents:
        raise ValueError("同名修复分支已存在，但补丁摘要或基准提交不匹配")
    result = _create_pr(
        client, api_base, headers, owner=owner, branch=branch,
        base_branch=base_branch, run_id=run_id, body=body,
    )
    result["status"] = "recovered"
    result["commit"] = commit_sha
    return result


def publish_draft_pr(*, owner: str, repo: str, repo_root: Path, base_sha: str,
                     base_branch: str, run_id: int, patch: str, root_cause: str,
                     test_summary: str, review_summary: str) -> dict:
    """创建 Blob/Tree/Commit/Ref/Draft PR；从不覆盖已有分支或自动合并。"""
    # Fork PR 无法安全判断应向哪个可写分支提 PR，因此宁可阻断也不猜测。
    if not base_branch:
        raise ValueError("无法确定安全的 PR 基准分支（Fork PR 默认禁止自动发布）")
    # 补丁哈希进入分支名：同一 Run 的不同修订版不会互相覆盖。
    full_digest = hashlib.sha256(patch.encode()).hexdigest()
    branch = f"repomind/fix-ci-{run_id}-{full_digest[:8]}"
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = _headers()

    body = _pr_body(run_id, root_cause, test_summary, review_summary)
    with httpx.Client(timeout=60) as client:
        recovered = _recover_publication(
            client, api_base, headers, owner=owner, branch=branch,
            base_branch=base_branch, base_sha=base_sha, run_id=run_id,
            patch_digest=full_digest, body=body,
        )
        if recovered:
            return recovered

    # 只有确认远端既无分支也无 PR 后，才开始创建新的 Git 对象。
    with tempfile.TemporaryDirectory(prefix="repomind-ci-publish-") as temp_dir:
        worktree = Path(temp_dir) / "worktree"
        added = False
        try:
            _materialize_patch(repo_root, base_sha, patch, worktree)
            added = True
            with httpx.Client(timeout=60) as client:
                # Git Data API 的写入顺序必须是 Blob → Tree → Commit → Ref。
                base_commit = _api_json(
                    client.get(f"{api_base}/git/commits/{base_sha}", headers=headers),
                    "读取基准 Commit",
                )
                entries = _collect_tree_entries(
                    client, api_base, base_commit["tree"]["sha"], worktree, patch, headers,
                )
                if not entries:
                    raise ValueError("候选补丁没有可发布的文件变化")
                tree = _api_json(
                    client.post(f"{api_base}/git/trees", headers=headers, json={
                        "base_tree": base_commit["tree"]["sha"], "tree": entries,
                    }),
                    "创建修复 Git Tree",
                )
                commit = _api_json(
                    client.post(f"{api_base}/git/commits", headers=headers, json={
                        "message": (f"fix CI run {run_id}\n\n"
                                    f"RepoMind-Patch-SHA256: {full_digest}"),
                        "tree": tree["sha"],
                        "parents": [base_sha],
                    }),
                    "创建修复 Commit",
                )
                # 使用 Create Ref 而不是 Update Ref，分支已存在时 API 会失败，避免强制覆盖。
                ref_response = client.post(f"{api_base}/git/refs", headers=headers, json={
                    "ref": f"refs/heads/{branch}", "sha": commit["sha"],
                })
                _api_json(ref_response, "创建修复分支")

                # 永远创建 Draft；即使自动测试和审查通过，也必须由人决定是否合并。
                result = _create_pr(
                    client, api_base, headers, owner=owner, branch=branch,
                    base_branch=base_branch, run_id=run_id, body=body,
                )
                result["commit"] = commit["sha"]
                return result
        finally:
            # GitHub API 是否成功不影响本地清理；临时 worktree 不应残留在 clone 元数据中。
            if added:
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_root,
                        capture_output=True, text=True, timeout=30,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
