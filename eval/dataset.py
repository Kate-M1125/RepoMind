"""
从 GitHub 拉取已关闭 Issue + 关联 Fix PR，构建评估数据集。

Ground truth 来源：
  1. 已关闭 Issue 的 timeline 中找 cross-referenced PR
  2. 拉取 PR diff，作为"真实修改了什么"的依据
"""
import os
import httpx
from typing import Optional


def _headers(extra: dict = None) -> dict:
    token = os.getenv("GITHUB_TOKEN", "")
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    if extra:
        h.update(extra)
    return h


def _get(url: str, headers: dict = None, params: dict = None) -> dict | list:
    resp = httpx.get(url, headers=headers or _headers(), params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_closed_issues_with_prs(
    owner: str,
    repo: str,
    max_issues: int = 20,
    labels: Optional[list[str]] = None,
) -> list[dict]:
    """
    策略：搜索含 'closes #N' 的合并 PR，从 PR body 提取 Issue 编号，
    再拉取 Issue 内容作为评估样本。
    """
    import re
    base = f"https://api.github.com/repos/{owner}/{repo}"

    # 搜索含明确 issue 引用的合并 PR，覆盖常见写法
    queries = [
        f'repo:{owner}/{repo} is:pr is:merged "closes #" in:body',
        f'repo:{owner}/{repo} is:pr is:merged "fixes #" in:body',
        f'repo:{owner}/{repo} is:pr is:merged "resolves #" in:body',
    ]

    seen_issues: set[int] = set()
    dataset: list[dict] = []

    for q in queries:
        if len(dataset) >= max_issues:
            break
        result = _get(
            "https://api.github.com/search/issues",
            params={"q": q, "sort": "updated", "order": "desc", "per_page": 50},
            headers=_headers(),
        )
        for pr in result.get("items", []):
            if len(dataset) >= max_issues:
                break
            pr_body = pr.get("body") or ""
            pr_html_url = pr["html_url"]

            # 找 "closes #N" / "fixes #N" / "resolves #N"
            refs = re.findall(
                r'(?:clos(?:es|ed)|fix(?:es|ed)?|resolv(?:es|ed))\s+#(\d+)',
                pr_body, re.IGNORECASE
            )
            if not refs:
                continue

            issue_number = int(refs[0])
            if issue_number in seen_issues:
                continue
            seen_issues.add(issue_number)

            try:
                issue = _get(f"{base}/issues/{issue_number}", headers=_headers())
                # 必须是 Issue（非 PR），且已关闭
                if issue.get("state") != "closed" or "pull_request" in issue:
                    continue
            except Exception:
                continue

            pr_info = _fetch_pr_info(pr_html_url)
            if not pr_info or not pr_info["files"]:
                continue

            # 过滤掉纯 CI/docs PR（所有改动文件都是配置文件）
            src_files = [f for f in pr_info["files"]
                         if not any(f["filename"].startswith(p)
                                    for p in [".github/", "docs/", "uv.lock", ".pre-commit"])]
            if not src_files:
                continue

            dataset.append({
                "issue_url":       issue["html_url"],
                "issue_number":    issue_number,
                "title":           issue["title"],
                "body":            (issue["body"] or "")[:1000],
                "pr_url":          pr_html_url,
                "pr_title":        pr_info["title"],
                "pr_diff_summary": pr_info["diff_summary"],
                "pr_files":        pr_info["files"],
            })
            print(f"  ✓ Issue #{issue_number}: {issue['title'][:60]}")

    return dataset


def _find_fix_pr(base: str, issue_number: int) -> Optional[str]:
    """从 Issue timeline 找关联 PR URL。"""
    try:
        timeline = _get(
            f"{base}/issues/{issue_number}/timeline",
            headers=_headers({"Accept": "application/vnd.github.mockingbird-preview+json"}),
        )
        for event in timeline:
            if event.get("event") == "cross-referenced":
                src = event.get("source", {}).get("issue", {})
                if "pull_request" in src and src.get("state") == "closed":
                    return src["html_url"]
        return None
    except Exception:
        return None


def _fetch_pr_info(pr_html_url: str) -> Optional[dict]:
    """拉取 PR 的文件变更列表和摘要。"""
    try:
        # html_url: https://github.com/owner/repo/pull/123
        parts = pr_html_url.rstrip("/").split("/")
        owner, repo, pr_number = parts[-4], parts[-3], parts[-1]
        api_base = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"

        files_raw = _get(f"{api_base}/files", headers=_headers())
        files = [
            {
                "filename": f["filename"],
                "status":   f["status"],          # added/modified/removed
                "additions": f["additions"],
                "deletions": f["deletions"],
                "patch":    (f.get("patch") or "")[:500],  # 只取前 500 字
            }
            for f in files_raw[:10]  # 最多 10 个文件
        ]

        diff_summary = "; ".join(
            f"{f['filename']} ({f['status']}, +{f['additions']}/-{f['deletions']})"
            for f in files
        )
        pr_data = _get(api_base, headers=_headers())
        return {
            "title": pr_data.get("title", ""),
            "files": files,
            "diff_summary": diff_summary,
        }
    except Exception as e:
        print(f"    [warn] 无法拉取 PR info: {e}")
        return None
