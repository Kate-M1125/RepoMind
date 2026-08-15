"""
RepoMind 主 API 服务：Issue 分析 + CI 诊断/验证/Draft PR 分阶段授权。
"""
import sys
import uuid
import asyncio
import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from enum import Enum

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from backend.job_store import JobStore

app = FastAPI(title="RepoMind API")


# ── Job 状态 ─────────────────────────────────────────────────────

class Status(str, Enum):
    pending = "pending"
    running = "running"
    done    = "done"
    failed  = "failed"


class Job(BaseModel):
    job_id: str
    task_type: str = "issue"
    source_url: str = ""
    permission_level: str = "analyze"
    status: Status = Status.pending
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    # 记录每次权限阶段及结果，既供 UI 展示，也用于阻止重复发布等非法状态跳转。
    stage_history: list[dict[str, Any]] = Field(default_factory=list)
    # 多 API Key 部署时用于隔离任务；只保存 Key 哈希，绝不落盘原始凭据。
    owner_id: str = "anonymous"


_job_store = JobStore()
_jobs: dict[str, Job] = {}


def _utc_now() -> str:
    """使用带时区的 UTC 时间，避免服务器部署到不同时区后审计时间含糊。"""
    return datetime.now(timezone.utc).isoformat()


def _record_stage(job: Job, stage: str, status: str, **details: Any) -> None:
    """追加而不是覆盖审计事件，保留完整的权限升级轨迹。"""
    job.stage_history.append({
        "stage": stage, "status": status, "timestamp": _utc_now(), **details,
    })
    _save_job(job)


def _save_job(job: Job) -> None:
    """每次关键状态变化后立即落盘；Pydantic 负责把 Enum 转成 JSON 值。"""
    _job_store.save(job.job_id, job.model_dump(mode="json"), _utc_now())


def _load_jobs() -> None:
    """恢复历史任务；进程中断时仍为运行态的任务标记失败，避免永久卡住。"""
    for payload in _job_store.load_all():
        try:
            job = Job.model_validate(payload)
        except Exception:
            continue
        _jobs[job.job_id] = job
        if job.status in {Status.pending, Status.running}:
            job.status = Status.failed
            job.error = "服务重启中断了任务，请重新创建分析任务"
            _record_stage(job, job.permission_level, "interrupted")


def _configured_api_keys() -> set[str]:
    raw = os.getenv("REPOMIND_API_KEYS", os.getenv("REPOMIND_API_KEY", ""))
    return {item.strip() for item in raw.split(",") if item.strip()}


def _principal(authorization: Optional[str] = Header(default=None)) -> str:
    """可选 Bearer 鉴权：未配置 Key 时保持本地开发兼容，配置后默认拒绝匿名访问。"""
    keys = _configured_api_keys()
    if not keys:
        return "anonymous"
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer API key required")
    supplied = authorization.removeprefix("Bearer ").strip()
    matched = next((key for key in keys if secrets.compare_digest(supplied, key)), None)
    if not matched:
        raise HTTPException(status_code=403, detail="invalid API key")
    return hashlib.sha256(matched.encode()).hexdigest()[:16]


def _require_owner(job: Job, principal: str) -> None:
    if job.owner_id != principal:
        # 返回 404，避免向其他租户泄露任务 ID 是否存在。
        raise HTTPException(status_code=404, detail="Job not found")


def _ensure_capacity() -> None:
    try:
        limit = max(1, int(os.getenv("REPOMIND_MAX_ACTIVE_JOBS", "4")))
    except ValueError:
        # 配置错误时使用安全的小默认值，不让每个请求都以 500 失败。
        limit = 4
    active = sum(job.status in {Status.pending, Status.running} for job in _jobs.values())
    if active >= limit:
        raise HTTPException(status_code=429, detail="too many active jobs")


_load_jobs()


# ── 分析任务（同步，在线程池执行）────────────────────────────────

def _run_issue(job_id: str, issue_url: str) -> None:
    """Issue 工作流按需导入，避免启动 API 时初始化所有 LangGraph 依赖。"""
    job = _jobs[job_id]
    job.status = Status.running
    _save_job(job)
    try:
        from workflow.graph import app as langgraph_app
        config = {"configurable": {"thread_id": job_id}}
        state = langgraph_app.invoke(
            {"issue_url": issue_url, "iteration": 0, "error": None},
            config=config,
        )
        job.result = {
            "issue_number":  state.get("issue_number"),
            "title":         state.get("title", ""),
            "issue_type":    state.get("issue_type", ""),
            "severity":      state.get("severity", ""),
            "root_cause":    state.get("root_cause", ""),
            "fix_suggestion":state.get("fix_suggestion", ""),
            "affected_files":state.get("affected_files", []),
            "code_changes":  state.get("code_changes", []),
            "suggested_labels": state.get("suggested_labels", []),
            "final_report":  state.get("final_report", ""),
            "confidence":    state.get("confidence", 0.0),
        }
        job.status = Status.done
        _save_job(job)
    except Exception as e:
        job.status  = Status.failed
        job.error   = str(e)
        _save_job(job)


def _ci_result(state: dict) -> dict[str, Any]:
    """仅返回前端需要且可序列化的 CI 字段，不暴露 checkpoint 内部状态。"""
    keys = (
        "run_id", "run_info", "failed_jobs", "failure_type", "failing_tests",
        "error_signatures", "root_cause", "fix_suggestion", "suspected_files",
        "confidence", "patch", "patch_files", "patch_risk", "patch_status",
        "patch_errors", "patch_attempt", "verification_status", "test_command",
        "patch_sha256",
        "targeted_test_result", "regression_test_result", "patch_review_issues",
        "patch_review_status", "patch_review_confidence", "publish_status",
        "published_branch", "published_commit", "draft_pr_url", "publish_error",
        "final_report",
    )
    return {key: state.get(key) for key in keys}


def _run_ci(job_id: str, ci_url: str, *, execute_patch: bool, create_pr: bool,
            locked_result: Optional[dict[str, Any]] = None) -> None:
    """CI 工作流在线程池执行；create_pr 始终隐式启用补丁验证。"""
    job = _jobs[job_id]
    job.status = Status.running
    job.error = None
    _save_job(job)
    try:
        from workflow.ci_graph import ci_app
        config = {"configurable": {"thread_id": f"{job_id}-{uuid.uuid4()}"}}
        locked_result = locked_result or {}
        state = ci_app.invoke({
            "ci_url": ci_url, "iteration": 0, "patch_attempt": 0,
            "patch_history": [], "execute_patch": execute_patch or create_pr,
            "create_pr": create_pr, "error": None,
            # 权限升级后复用原补丁，并由工作流节点再次核对 SHA-256。
            "patch": locked_result.get("patch", ""),
            "patch_locked": bool(locked_result),
            "expected_patch_sha256": locked_result.get("patch_sha256", ""),
        }, config=config)
        job.result = _ci_result(state)
        job.status = Status.done
        _record_stage(
            job, job.permission_level, "done",
            patch_sha256=job.result.get("patch_sha256", ""),
            publish_status=job.result.get("publish_status", ""),
        )
    except Exception as exc:
        job.status = Status.failed
        job.error = str(exc)
        _record_stage(job, job.permission_level, "failed", error=str(exc)[:1000])


# ── 路由 ─────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    issue_url: str


class JobCreated(BaseModel):
    job_id: str
    status_url: str


@app.post("/analyze", status_code=202, response_model=JobCreated)
async def analyze(req: AnalyzeRequest, authorization: Optional[str] = Header(default=None)):
    principal = _principal(authorization)
    _ensure_capacity()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = Job(job_id=job_id, task_type="issue", source_url=req.issue_url,
                        owner_id=principal)
    _save_job(_jobs[job_id])
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run_issue, job_id, req.issue_url)
    return JobCreated(job_id=job_id, status_url=f"/jobs/{job_id}")


class CIAnalyzeRequest(BaseModel):
    ci_url: str


class ActionConfirmation(BaseModel):
    confirmation: str


def _require_ci_job(job_id: str, principal: str = "anonymous") -> Job:
    job = _jobs.get(job_id)
    if not job or job.task_type != "ci":
        raise HTTPException(status_code=404, detail="CI job not found")
    _require_owner(job, principal)
    if job.status in {Status.pending, Status.running}:
        raise HTTPException(status_code=409, detail="CI job is still running")
    return job


def _restart_ci(job: Job, permission_level: str, *, execute_patch: bool, create_pr: bool) -> None:
    """升级同一个 UI Job 的权限并重新运行，旧结果会被新阶段结果替换。"""
    previous_result = dict(job.result or {})
    if not previous_result.get("patch") or not previous_result.get("patch_sha256"):
        raise HTTPException(status_code=409, detail="no integrity-checked patch is available")
    job.permission_level = permission_level
    job.status = Status.pending
    job.error = None
    _save_job(job)
    _record_stage(
        job, permission_level, "accepted",
        patch_sha256=previous_result["patch_sha256"],
    )
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        None, lambda: _run_ci(job.job_id, job.source_url,
                              execute_patch=execute_patch, create_pr=create_pr,
                              locked_result=previous_result)
    )


@app.post("/api/ci/analyze", status_code=202, response_model=JobCreated)
async def analyze_ci(req: CIAnalyzeRequest, authorization: Optional[str] = Header(default=None)):
    principal = _principal(authorization)
    _ensure_capacity()
    from tools.github_actions_api import parse_actions_run_url
    try:
        parse_actions_run_url(req.ci_url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    job_id = str(uuid.uuid4())
    job = Job(job_id=job_id, task_type="ci", source_url=req.ci_url, owner_id=principal)
    _record_stage(job, "analyze", "accepted")
    _jobs[job_id] = job
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        None, lambda: _run_ci(job_id, req.ci_url, execute_patch=False, create_pr=False)
    )
    return JobCreated(job_id=job_id, status_url=f"/jobs/{job_id}")


@app.post("/api/ci/tasks/{job_id}/verify", status_code=202, response_model=JobCreated)
async def verify_ci_patch(job_id: str, req: ActionConfirmation,
                          authorization: Optional[str] = Header(default=None)):
    job = _require_ci_job(job_id, _principal(authorization))
    if req.confirmation != "RUN_PATCH":
        raise HTTPException(status_code=400, detail="confirmation must be RUN_PATCH")
    if job.permission_level != "analyze":
        raise HTTPException(status_code=409, detail="patch verification has already been requested")
    _restart_ci(job, "verify", execute_patch=True, create_pr=False)
    return JobCreated(job_id=job_id, status_url=f"/jobs/{job_id}")


@app.post("/api/ci/tasks/{job_id}/publish", status_code=202, response_model=JobCreated)
async def publish_ci_patch(job_id: str, req: ActionConfirmation,
                           authorization: Optional[str] = Header(default=None)):
    job = _require_ci_job(job_id, _principal(authorization))
    if req.confirmation != "CREATE_DRAFT_PR":
        raise HTTPException(status_code=400, detail="confirmation must be CREATE_DRAFT_PR")
    if job.permission_level != "verify":
        detail = ("Draft PR has already been requested" if job.permission_level == "publish"
                  else "verify the patch before publishing")
        raise HTTPException(status_code=409, detail=detail)
    result = job.result or {}
    if result.get("verification_status") != "passed":
        raise HTTPException(status_code=409, detail="verify the patch successfully before publishing")
    if result.get("patch_review_status") not in {"approved", "warning"}:
        raise HTTPException(status_code=409, detail="patch review has not approved publishing")
    _restart_ci(job, "publish", execute_patch=True, create_pr=True)
    return JobCreated(job_id=job_id, status_url=f"/jobs/{job_id}")


@app.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str, authorization: Optional[str] = Header(default=None)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    _require_owner(job, _principal(authorization))
    return job


# 挂载前端静态文件
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
