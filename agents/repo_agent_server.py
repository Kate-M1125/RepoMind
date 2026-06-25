"""
RepoAgent HTTP 服务
- POST /analyze     提交分析任务，立即返回 job_id（非阻塞）
- GET  /jobs/{id}   查询任务状态和结果
- 自动 clone 目标仓库并建 RAG 索引（如果还没有）
"""
import sys
import uuid
import shutil
import asyncio
import threading
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

from workflow.graph import app as langgraph_app
from context.rag_index import index_repo, get_repo_collection
from config import settings

CLONE_BASE = settings.clone_base
CLONE_BASE.mkdir(parents=True, exist_ok=True)

# ── per-repo 锁，防止并发重复 clone/index ─────────────────────────
_repo_locks: dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()


def _get_repo_lock(repo_name: str) -> threading.Lock:
    with _locks_mutex:
        if repo_name not in _repo_locks:
            _repo_locks[repo_name] = threading.Lock()
        return _repo_locks[repo_name]


# ── Job 状态存储（内存，进程内共享）──────────────────────────────
class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class Job(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.pending
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


_jobs: dict[str, Job] = {}

# ── FastAPI App ───────────────────────────────────────────────────
fastapi_app = FastAPI(title="RepoAgent")


class AnalyzeRequest(BaseModel):
    issue_url: str
    repo_name: str   # e.g. "fastapi/fastapi"


class JobCreated(BaseModel):
    job_id: str
    status_url: str


# ── 同步工作函数（在线程池里执行，不阻塞事件循环）──────────────
def _ensure_indexed(repo_name: str) -> None:
    owner, repo = repo_name.split("/", 1)
    safe_name = repo_name.replace("/", "__")
    repo_path = CLONE_BASE / safe_name
    tmp_path = repo_path.with_suffix(".tmp")

    with _get_repo_lock(repo_name):
        if not repo_path.exists():
            clone_url = f"https://github.com/{repo_name}.git"
            print(f"[RepoAgent] cloning {clone_url} → {tmp_path}")
            if tmp_path.exists():
                shutil.rmtree(tmp_path)
            subprocess.run(
                ["git", "clone", "--depth=1", clone_url, str(tmp_path)],
                check=True, capture_output=True,
            )
            print(f"[RepoAgent] indexing {tmp_path}")
            index_repo(str(tmp_path), owner=owner, repo_name=repo)
            tmp_path.rename(repo_path)
            print(f"[RepoAgent] ready: {repo_path}")
        else:
            col = get_repo_collection(owner, repo)
            if col.count() == 0:
                print(f"[RepoAgent] re-indexing {repo_path}")
                index_repo(str(repo_path), owner=owner, repo_name=repo)


def _run_analysis(job_id: str, issue_url: str, repo_name: str) -> None:
    """同步任务函数，在 BackgroundTasks 的线程里运行。"""
    job = _jobs[job_id]
    job.status = JobStatus.running
    try:
        _ensure_indexed(repo_name)
        config = {"configurable": {"thread_id": job_id}}
        result = langgraph_app.invoke(
            {"issue_url": issue_url, "iteration": 0, "error": None},
            config=config,
        )
        job.result = {
            "repo_name": repo_name,
            "root_cause": result.get("root_cause", ""),
            "fix_suggestion": result.get("fix_suggestion", ""),
            "confidence": result.get("confidence", 0.0),
            "final_report": result.get("final_report", ""),
        }
        job.status = JobStatus.done
    except subprocess.CalledProcessError as e:
        job.status = JobStatus.failed
        job.error = f"clone failed: {e.stderr.decode()[:200]}"
    except Exception as e:
        job.status = JobStatus.failed
        job.error = str(e)


# ── 路由 ─────────────────────────────────────────────────────────
@fastapi_app.post("/analyze", status_code=202, response_model=JobCreated)
async def analyze(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    """提交分析任务，立即返回 job_id，不阻塞事件循环。"""
    job_id = str(uuid.uuid4())
    _jobs[job_id] = Job(job_id=job_id)
    # run_in_executor 把同步阻塞函数移到线程池，释放事件循环
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_analysis, job_id, req.issue_url, req.repo_name)
    return JobCreated(job_id=job_id, status_url=f"/jobs/{job_id}")


@fastapi_app.get("/jobs/{job_id}", response_model=Job)
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


if __name__ == "__main__":
    import os
    import uvicorn
    port = int(os.getenv("REPO_AGENT_PORT", "8001"))
    uvicorn.run(fastapi_app, host="0.0.0.0", port=port)
