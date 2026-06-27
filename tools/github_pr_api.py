import re
import httpx
import os


def parse_pr_url(url: str) -> tuple[str, str, int]:
    pattern = r"github\.com/([^/]+)/([^/]+)/pull/(\d+)"
    match = re.search(pattern, url)
    if not match:
        raise ValueError(f"不是合法的 GitHub PR URL: {url}")
    owner, repo, number = match.groups()
    return owner, repo, int(number)


def _fetch_all_pages(client: httpx.Client, url: str, headers: dict) -> list:
    items, page = [], 1
    while True:
        batch = client.get(url, headers=headers, params={"page": page, "per_page": 100}).raise_for_status().json()
        if not batch:
            break
        items.extend(batch)
        page += 1
    return items


_SKIP_EXTENSIONS = {".lock", ".sum", ".mod", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2"}
_SKIP_PATTERNS = {"package-lock.json", "yarn.lock", "poetry.lock", "requirements.txt"}


def _should_review(filename: str) -> bool:
    if any(filename.endswith(ext) for ext in _SKIP_EXTENSIONS):
        return False
    if any(filename.endswith(p) for p in _SKIP_PATTERNS):
        return False
    return True


def fetch_pr(url: str) -> dict:
    owner, repo, number = parse_pr_url(url)
    token = os.getenv("GITHUB_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}"

    try:
        with httpx.Client(timeout=30) as client:
            pr = client.get(base, headers=headers).raise_for_status().json()

            all_files = _fetch_all_pages(client, f"{base}/files", headers)
            changed_files = [
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "additions": f["additions"],
                    "deletions": f["deletions"],
                    "patch": f.get("patch", ""),
                }
                for f in all_files
                if _should_review(f["filename"]) and f.get("patch")
            ]

            comments_raw = _fetch_all_pages(
                client, f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments", headers
            )

    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            raise ValueError(f"PR 不存在或仓库为私有: {url}") from e
        elif status == 403:
            raise ValueError("GitHub API 权限不足") from e
        elif status == 429:
            raise ValueError("GitHub API 请求超限，请稍后重试") from e
        else:
            raise ValueError(f"GitHub API 返回 {status}: {url}") from e
    except httpx.TimeoutException as e:
        raise ValueError(f"GitHub API 请求超时: {url}") from e

    return {
        "pr_number": pr["number"],
        "title": pr["title"],
        "body": pr["body"] or "",
        "author": pr["user"]["login"],
        "base_branch": pr["base"]["ref"],
        "head_branch": pr["head"]["ref"],
        "changed_files": changed_files,
        "comments": [{"user": c["user"]["login"], "body": c["body"]} for c in comments_raw],
    }
