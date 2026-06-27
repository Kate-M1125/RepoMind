"""
从 GitHub 拉取已关闭 Issue + 关联 Fix PR，构建评估数据集。

Ground truth 来源：
  bug/feature：已关闭 Issue → 关联合并 PR → PR diff
  question：已关闭 Issue → 维护者/贡献者的回复 → 解答文本
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


def fetch_feature_issues_with_prs(
    owner: str,
    repo: str,
    max_issues: int = 20,
) -> list[dict]:
    """
    抓取带有 feature/enhancement 标签的已关闭 Issue，
    通过 timeline 找关联合并 PR，构建 feature eval 数据集。
    """
    base = f"https://api.github.com/repos/{owner}/{repo}"
    seen_issues: set[int] = set()
    dataset: list[dict] = []

    for label in ["enhancement", "feature", "feature request", "Feature Request",
                  "new feature", "type: feature", "type:feature", "improvement"]:
        if len(dataset) >= max_issues:
            break
        try:
            issues = _get(
                f"{base}/issues",
                params={"state": "closed", "labels": label,
                        "per_page": 50, "sort": "updated", "direction": "desc"},
            )
        except Exception as e:
            print(f"  [warn] 拉取 label={label} 失败: {e}")
            continue

        for issue in issues:
            if len(dataset) >= max_issues:
                break
            issue_number = issue["number"]
            if issue_number in seen_issues or "pull_request" in issue:
                continue
            seen_issues.add(issue_number)

            pr_url = _find_fix_pr(base, issue_number)
            if not pr_url:
                continue

            pr_info = _fetch_pr_info(pr_url)
            if not pr_info or not pr_info["files"]:
                continue

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
                "labels":          [l["name"] for l in issue.get("labels", [])],
                "pr_url":          pr_url,
                "pr_title":        pr_info["title"],
                "pr_diff_summary": pr_info["diff_summary"],
                "pr_files":        pr_info["files"],
            })
            print(f"  ✓ Issue #{issue_number}: {issue['title'][:60]}")

    return dataset


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
    """
    通过两种方式找关联 PR URL：
    1. GitHub search API（更可靠）：搜 PR body 里提到 issue 编号
    2. issue timeline（兜底）：找 cross-referenced 事件
    """
    # 从 base URL 解析 owner/repo
    parts = base.rstrip("/").split("/")
    owner, repo = parts[-2], parts[-1]

    # 方法1：search API，覆盖 closes/fixes/resolves/#N 各种写法
    try:
        result = _get(
            "https://api.github.com/search/issues",
            headers=_headers(),
            params={
                "q": f"repo:{owner}/{repo} is:pr is:merged #{issue_number} in:body",
                "sort": "updated",
                "order": "desc",
                "per_page": 5,
            },
        )
        for item in result.get("items", []):
            body = item.get("body") or ""
            # 确认 body 里确实引用了这个 issue（避免误匹配更大数字里的子串）
            import re as _re
            if _re.search(rf'(?:clos|fix|resolv|implement|address)[a-z]*\s+#?{issue_number}\b', body, _re.IGNORECASE) \
               or _re.search(rf'#\s*{issue_number}\b', body):
                return item["html_url"]
    except Exception:
        pass

    # 方法2：timeline 兜底
    try:
        timeline = _get(f"{base}/issues/{issue_number}/timeline")
        for event in timeline:
            if event.get("event") == "cross-referenced":
                src = event.get("source", {}).get("issue", {})
                if "pull_request" in src and src.get("state") == "closed":
                    return src["html_url"]
    except Exception:
        pass

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


# ── Question Dataset ──────────────────────────────────────────────────

_QUESTION_LABELS = [
    "question", "help wanted", "support", "how-to", "howto",
    "type: question", "type:question", "kind/question", "❓ question",
    "usage", "help", "help needed",
]


def _fetch_contributors(owner: str, repo: str) -> set[str]:
    """拉取仓库贡献者列表，用于识别权威回复。"""
    try:
        items = _get(
            f"https://api.github.com/repos/{owner}/{repo}/contributors",
            params={"per_page": 100},
        )
        return {item["login"] for item in items if isinstance(item, dict)}
    except Exception:
        return set()


def _extract_key_answers(
    comments: list[dict],
    contributors: set[str],
    author_login: str,
) -> list[dict]:
    """
    从评论列表里提取"权威回答"：
    1. 贡献者/维护者的回复（优先）
    2. 排除 issue 作者自己的追问
    3. 最多取 3 条，按正文长度降序（短回复通常是"谢谢"类噪声）
    """
    authoritative = []
    others = []
    for c in comments:
        user = c.get("user", {}).get("login", "")
        body = (c.get("body") or "").strip()
        if len(body) < 30:
            continue
        if user == author_login:
            continue
        if user in contributors:
            authoritative.append({"user": user, "body": body, "is_contributor": True})
        else:
            others.append({"user": user, "body": body, "is_contributor": False})

    candidates = (authoritative or others)
    candidates.sort(key=lambda x: len(x["body"]), reverse=True)
    return candidates[:3]


def fetch_question_issues_with_answers(
    owner: str,
    repo: str,
    max_issues: int = 20,
) -> list[dict]:
    """
    抓取已关闭的 question 类型 Issue，以维护者回复作为 ground truth。

    返回字段：
      issue_url, issue_number, title, body, labels
      resolution_comments  — 维护者/贡献者回复列表（最多 3 条）
      key_answer           — 最长/最权威的回复（judge 主要对比此项）
    """
    base = f"https://api.github.com/repos/{owner}/{repo}"
    contributors = _fetch_contributors(owner, repo)
    seen: set[int] = set()
    dataset: list[dict] = []

    for label in _QUESTION_LABELS:
        if len(dataset) >= max_issues:
            break
        try:
            issues = _get(
                f"{base}/issues",
                params={
                    "state": "closed",
                    "labels": label,
                    "per_page": 50,
                    "sort": "updated",
                    "direction": "desc",
                },
            )
        except Exception as e:
            print(f"  [warn] label={label} 拉取失败: {e}")
            continue

        for issue in issues:
            if len(dataset) >= max_issues:
                break
            number = issue["number"]
            if number in seen or "pull_request" in issue:
                continue
            seen.add(number)

            # 拉评论
            try:
                comments_raw = _get(
                    f"{base}/issues/{number}/comments",
                    params={"per_page": 100},
                )
            except Exception:
                continue

            author = (issue.get("user") or {}).get("login", "")
            answers = _extract_key_answers(comments_raw, contributors, author)
            if not answers:
                continue

            key_answer = answers[0]["body"]
            dataset.append({
                "issue_url":            issue["html_url"],
                "issue_number":         number,
                "title":                issue["title"],
                "body":                 (issue["body"] or "")[:1000],
                "labels":               [l["name"] for l in issue.get("labels", [])],
                "resolution_comments":  answers,
                "key_answer":           key_answer,
            })
            print(f"  ✓ Issue #{number}: {issue['title'][:60]}")

    return dataset
