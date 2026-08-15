"""GitHub Actions 只读 API：运行、失败任务、日志和对应提交。"""
from __future__ import annotations

import io
import os
import re
import zipfile

import httpx


_CI_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)(?:[/?#].*)?$"
)
# 防止超大日志占满内存或挤占 LLM 上下文：单 Job 50 万字符，总计 150 万字符。
_MAX_JOB_LOG_CHARS = 500_000
_MAX_TOTAL_LOG_CHARS = 1_500_000


def parse_actions_run_url(url: str) -> tuple[str, str, int]:
    """解析 Run URL；同时接受 GitHub 从 Run 页面追加的 /job/<id> 后缀。"""
    match = _CI_URL_RE.match(url.strip())
    if not match:
        raise ValueError(f"不是合法的 GitHub Actions Run URL: {url}")
    owner, repo, run_id = match.groups()
    return owner, repo, int(run_id)


def _headers() -> dict[str, str]:
    """公共仓库允许无 Token 读取；私有仓库由 GITHUB_TOKEN 授权。"""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _api_error(exc: httpx.HTTPStatusError, resource: str) -> ValueError:
    """把底层 HTTP 状态转换为 CLI 可读、且不泄露 Token 的错误。"""
    status = exc.response.status_code
    if status == 404:
        return ValueError(f"{resource} 不存在，或 Token 没有 Actions 读取权限")
    if status == 403:
        return ValueError("GitHub API 权限不足或请求频率受限")
    return ValueError(f"GitHub API 返回 {status}: {resource}")


def _decode_log_response(response: httpx.Response) -> str:
    """兼容 job logs 的纯文本响应和 zip 响应。"""
    payload = response.content
    if payload.startswith(b"PK"):
        # Run/Job 日志在不同 API/重定向响应中可能是 zip 或纯文本。
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            parts = []
            remaining = _MAX_JOB_LOG_CHARS
            for name in archive.namelist():
                if name.endswith("/") or remaining <= 0:
                    continue
                with archive.open(name) as log_file:
                    chunk = log_file.read(remaining)
                remaining -= len(chunk)
                parts.append(chunk.decode("utf-8", errors="replace"))
            return "\n".join(parts)
    return payload[:_MAX_JOB_LOG_CHARS].decode("utf-8", errors="replace")


def _run_source_metadata(run_data: dict, owner: str, repo: str) -> dict:
    """确定补丁基准 Commit 和可发布分支；Fork/来源缺失的 PR 不给发布分支。"""
    pull_requests = run_data.get("pull_requests") or []
    pull_request = pull_requests[0] if pull_requests else {}
    pr_head = pull_request.get("head", {})
    pr_head_repo = pr_head.get("repo", {}) or {}
    source_repo = pr_head_repo.get("full_name", "")
    # Workflow Run 响应有时没有 full_name，只能从官方 API URL 恢复 owner/repo。
    if not source_repo and "/repos/" in pr_head_repo.get("url", ""):
        source_repo = pr_head_repo["url"].split("/repos/", 1)[1].strip("/")
    is_pr_event = str(run_data.get("event", "")).startswith("pull_request")
    # 来源为空时不能视为同仓库；这是面向写操作的 fail-closed 策略。
    same_repo_pr = bool(source_repo) and source_repo.lower() == f"{owner}/{repo}".lower()
    if not is_pr_event:
        same_repo_pr = True
    return {
        "source_sha": pr_head.get("sha") or run_data.get("head_sha", ""),
        "source_repo": source_repo or f"{owner}/{repo}",
        "publish_base_branch": (pr_head.get("ref", "") if same_repo_pr and is_pr_event
                                else run_data.get("head_branch", "") if not is_pr_event else ""),
    }


def fetch_actions_run(url: str) -> dict:
    """一次性收集第一阶段所需的 Run、失败 Job、日志和触发提交差异。"""
    owner, repo, run_id = parse_actions_run_url(url)
    api_base = f"https://api.github.com/repos/{owner}/{repo}"
    headers = _headers()

    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            run = client.get(f"{api_base}/actions/runs/{run_id}", headers=headers)
            run.raise_for_status()
            run_data = run.json()

            # 一个矩阵工作流可能超过 100 个 Job，因此必须分页读取。
            jobs = []
            page = 1
            while True:
                response = client.get(
                    f"{api_base}/actions/runs/{run_id}/jobs",
                    headers=headers,
                    params={"filter": "latest", "per_page": 100, "page": page},
                )
                response.raise_for_status()
                batch = response.json().get("jobs", [])
                jobs.extend(batch)
                if len(batch) < 100:
                    break
                page += 1

            # neutral/skipped/success 不参与根因分析，减少无关日志。
            failed_jobs = [
                job for job in jobs
                if job.get("conclusion") in {"failure", "timed_out", "cancelled", "action_required"}
            ]

            # 按 Job 加标题拼接，后续日志解析器可以保留错误所属任务。
            log_sections = []
            remaining = _MAX_TOTAL_LOG_CHARS
            for job in failed_jobs:
                if remaining <= 0:
                    break
                log_response = client.get(
                    f"{api_base}/actions/jobs/{job['id']}/logs", headers=headers
                )
                # cancelled/action_required 的 Job 可能没有可下载日志；保留 Job 元数据继续分析。
                if log_response.status_code in {404, 410}:
                    continue
                log_response.raise_for_status()
                text = _decode_log_response(log_response)[:_MAX_JOB_LOG_CHARS]
                text = text[:remaining]
                remaining -= len(text)
                log_sections.append(f"===== JOB: {job.get('name', job['id'])} =====\n{text}")

            # 获取触发本次 Run 的提交 Diff，用于判断失败是否由最新改动引入。
            sha = run_data.get("head_sha", "")
            changed_files = []
            if sha:
                commit_response = client.get(f"{api_base}/commits/{sha}", headers=headers)
                if commit_response.status_code == 200:
                    changed_files = [
                        {
                            "filename": item.get("filename", ""),
                            "status": item.get("status", ""),
                            "patch": item.get("patch", ""),
                        }
                        for item in commit_response.json().get("files", [])
                    ]
    except httpx.HTTPStatusError as exc:
        raise _api_error(exc, url) from exc
    except httpx.TimeoutException as exc:
        raise ValueError(f"GitHub Actions API 请求超时: {url}") from exc

    source = _run_source_metadata(run_data, owner, repo)

    return {
        "owner": owner,
        "repo": repo,
        "run_id": run_id,
        "run_info": {
            "name": run_data.get("name", ""),
            "event": run_data.get("event", ""),
            "status": run_data.get("status", ""),
            "conclusion": run_data.get("conclusion", ""),
            "head_branch": run_data.get("head_branch", ""),
            "head_sha": run_data.get("head_sha", ""),
            # PR Run 优先使用真实 head commit；run.head_sha 有时是 GitHub 生成的 merge commit。
            "source_sha": source["source_sha"],
            "source_repo": source["source_repo"],
            # Fork PR 不允许自动发布到原仓库分支，留空让发布门禁明确阻断。
            "publish_base_branch": source["publish_base_branch"],
            "workflow_path": run_data.get("path", ""),
            "html_url": run_data.get("html_url", url),
        },
        "failed_jobs": [
            {
                "id": job.get("id"),
                "name": job.get("name", ""),
                "conclusion": job.get("conclusion", ""),
                "html_url": job.get("html_url", ""),
                "steps": [
                    {
                        "name": step.get("name", ""),
                        "number": step.get("number"),
                        "conclusion": step.get("conclusion", ""),
                    }
                    for step in job.get("steps", [])
                    if step.get("conclusion") not in {"success", "skipped"}
                ],
            }
            for job in failed_jobs
        ],
        "failure_logs": "\n\n".join(log_sections),
        "changed_files": changed_files,
    }
