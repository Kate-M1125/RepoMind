import re
import httpx
import os


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """从 URL 解析 owner/repo/issue_number"""
    pattern = r"github\.com/([^/]+)/([^/]+)/issues/(\d+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"不是合法的 GitHub Issue URL: {url}")
    owner, repo, number = match.groups()
    return owner, repo, int(number)


def resolve_repo(repo_name: str) -> str:
    """解析仓库的真实 full_name，处理仓库迁移/重命名（如 tiangolo/fastapi → fastapi/fastapi）"""
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        resp = httpx.get(f"https://api.github.com/repos/{repo_name}", headers=headers)
        resp.raise_for_status()
        return resp.json()["full_name"]
    except Exception:
        return repo_name  # 解析失败时返回原值


def _fetch_all_comments(client: httpx.Client, base_url: str, headers: dict) -> list[dict]:
    """分页拉取所有评论，每页 100 条直到无更多数据"""
    comments = []
    page = 1
    while True:
        batch = client.get(
            f"{base_url}/comments",
            headers=headers,
            params={"page": page, "per_page": 100},
        ).raise_for_status().json()
        if not batch:
            break
        comments.extend(batch)
        page += 1
    return comments


def _fetch_related_prs(client: httpx.Client, base_url: str, headers: dict) -> list[str]:
    """
    通过 timeline API 找关联 PR。
    当有人在 PR 里提到这个 Issue（cross-referenced），
    或 Issue 被 PR 关闭（closed via PR），都会出现在 timeline 里。
    """
    timeline_headers = {**headers, "Accept": "application/vnd.github.mockingbird-preview+json"}
    try:
        timeline = client.get(f"{base_url}/timeline", headers=timeline_headers).raise_for_status().json()
    except Exception:
        return []

    pr_urls = []
    for event in timeline:
        # cross-referenced：某个 PR/Issue 提到了当前 Issue
        if event.get("event") == "cross-referenced":
            source_issue = event.get("source", {}).get("issue", {})
            if "pull_request" in source_issue:
                pr_urls.append(source_issue["pull_request"]["html_url"])
        # closed：Issue 被 commit 或 PR 关闭
        elif event.get("event") == "closed" and event.get("commit_id"):
            pr_urls.append(f"closed via commit: {event['commit_id'][:7]}")

    return list(dict.fromkeys(pr_urls))  # 去重保序


def fetch_issue(url: str) -> dict:
    """拉取 Issue 主体 + 全部评论（分页）+ 关联 PR"""
    owner, repo, number = parse_issue_url(url)
    token = os.getenv("GITHUB_TOKEN")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    base = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(base, headers=headers)
            resp.raise_for_status()
            issue = resp.json()
            comments_raw = _fetch_all_comments(client, base, headers)
            related_prs = _fetch_related_prs(client, base, headers)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            raise ValueError(f"Issue 不存在或仓库为私有: {url}") from e
        elif status == 403:
            raise ValueError(f"GitHub API 权限不足（Token 无效或权限缺失）") from e
        elif status == 429:
            raise ValueError(f"GitHub API 请求超限（rate limit），请稍后重试") from e
        else:
            raise ValueError(f"GitHub API 返回 {status}: {url}") from e
    except httpx.TimeoutException as e:
        raise ValueError(f"GitHub API 请求超时: {url}") from e

    return {
        "issue_number": issue["number"],
        "title": issue["title"],
        "body": issue["body"] or "",
        "labels": [l["name"] for l in issue["labels"]],
        "comments": [{"user": c["user"]["login"], "body": c["body"]} for c in comments_raw],
        "related_prs": related_prs,
    }