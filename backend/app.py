"""
RepoMind 主 API 服务
- POST /analyze        提交分析任务，返回 job_id
- GET  /jobs/{job_id}  查询任务状态和结果
- GET  /               前端静态页面
"""
import sys
import uuid
import asyncio
from pathlib import Path
from typing import Any, Optional
from enum import Enum

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from workflow.graph import app as langgraph_app

app = FastAPI(title="RepoMind API")


# ── Job 状态 ─────────────────────────────────────────────────────

class Status(str, Enum):
    pending = "pending"
    running = "running"
    done    = "done"
    failed  = "failed"


class Job(BaseModel):
    job_id: str
    status: Status = Status.pending
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


_jobs: dict[str, Job] = {}


# ── 分析任务（同步，在线程池执行）────────────────────────────────

def _run(job_id: str, issue_url: str) -> None:
    job = _jobs[job_id]
    job.status = Status.running
    try:
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
    except Exception as e:
        job.status  = Status.failed
        job.error   = str(e)


# ── 路由 ─────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    issue_url: str


class JobCreated(BaseModel):
    job_id: str
    status_url: str


@app.post("/analyze", status_code=202, response_model=JobCreated)
async def analyze(req: AnalyzeRequest):
    job_id = str(uuid.uuid4())
    _jobs[job_id] = Job(job_id=job_id)
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run, job_id, req.issue_url)
    return JobCreated(job_id=job_id, status_url=f"/jobs/{job_id}")


@app.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# 挂载前端静态文件
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
