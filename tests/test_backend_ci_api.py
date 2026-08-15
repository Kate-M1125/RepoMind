"""CI FastAPI 分阶段授权接口测试；工作流本身全部 Mock，不访问 GitHub/LLM。"""
import hashlib
import time
from unittest.mock import patch

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from backend.app import Job, Status, _ci_result, _jobs, app


client = TestClient(app)


def setup_function():
    _jobs.clear()


def test_rejects_invalid_ci_url():
    response = client.post("/api/ci/analyze", json={"ci_url": "not-a-url"})
    assert response.status_code == 422


def test_creates_ci_job_without_execution_permission():
    with patch("backend.app._run_ci") as run:
        response = client.post(
            "/api/ci/analyze",
            json={"ci_url": "https://github.com/acme/widget/actions/runs/42"},
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        # 线程池调度可能稍后调用 Mock，接口状态本身是本测试关注点。
        for _ in range(20):
            if run.called:
                break
            time.sleep(0.01)
        job = _jobs[job_id]
        assert job.task_type == "ci"
        assert job.permission_level == "analyze"


def test_verify_requires_exact_confirmation():
    _jobs["job"] = Job(job_id="job", task_type="ci", source_url="url", status=Status.done)
    response = client.post("/api/ci/tasks/job/verify", json={"confirmation": "yes"})
    assert response.status_code == 400


def test_running_job_cannot_be_restarted():
    _jobs["job"] = Job(job_id="job", task_type="ci", source_url="url", status=Status.running)
    response = client.post("/api/ci/tasks/job/verify", json={"confirmation": "RUN_PATCH"})
    assert response.status_code == 409


def test_verify_requires_integrity_checked_patch():
    """不能在缺少补丁摘要时把任务权限升级到代码执行。"""
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        result={"patch": "diff --git a/a b/a\n"},
    )
    response = client.post(
        "/api/ci/tasks/job/verify", json={"confirmation": "RUN_PATCH"},
    )
    assert response.status_code == 409


def test_verify_passes_locked_patch_to_worker():
    patch_text = "diff --git a/a b/a\n"
    digest = hashlib.sha256(patch_text.encode()).hexdigest()
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        result={"patch": patch_text, "patch_sha256": digest},
    )
    with patch("backend.app._run_ci") as run:
        response = client.post(
            "/api/ci/tasks/job/verify", json={"confirmation": "RUN_PATCH"},
        )
        assert response.status_code == 202
        for _ in range(20):
            if run.called:
                break
            time.sleep(0.01)
        assert run.call_args.kwargs["locked_result"]["patch_sha256"] == digest
        assert _jobs["job"].stage_history[-1]["stage"] == "verify"
        assert _jobs["job"].stage_history[-1]["patch_sha256"] == digest


def test_rejects_duplicate_verification():
    patch_text = "diff --git a/a b/a\n"
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        permission_level="verify",
        result={"patch": patch_text, "patch_sha256": hashlib.sha256(patch_text.encode()).hexdigest()},
    )
    response = client.post(
        "/api/ci/tasks/job/verify", json={"confirmation": "RUN_PATCH"},
    )
    assert response.status_code == 409


def test_rejects_duplicate_publish():
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        permission_level="publish",
        result={"verification_status": "passed", "patch_review_status": "approved"},
    )
    response = client.post(
        "/api/ci/tasks/job/publish", json={"confirmation": "CREATE_DRAFT_PR"},
    )
    assert response.status_code == 409


def test_configured_api_key_is_required(monkeypatch):
    monkeypatch.setenv("REPOMIND_API_KEYS", "alpha,beta")
    response = client.post(
        "/api/ci/analyze",
        json={"ci_url": "https://github.com/acme/widget/actions/runs/42"},
    )
    assert response.status_code == 401


def test_job_owner_isolated_between_api_keys(monkeypatch):
    monkeypatch.setenv("REPOMIND_API_KEYS", "alpha,beta")
    owner = hashlib.sha256(b"alpha").hexdigest()[:16]
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        owner_id=owner,
    )
    response = client.get("/jobs/job", headers={"Authorization": "Bearer beta"})
    assert response.status_code == 404


def test_active_job_limit(monkeypatch):
    monkeypatch.setenv("REPOMIND_MAX_ACTIVE_JOBS", "1")
    _jobs["busy"] = Job(job_id="busy", status=Status.running)
    response = client.post(
        "/api/ci/analyze",
        json={"ci_url": "https://github.com/acme/widget/actions/runs/42"},
    )
    assert response.status_code == 429


def test_publish_requires_successful_verification():
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        result={"verification_status": "failed", "patch_review_status": "approved"},
    )
    response = client.post(
        "/api/ci/tasks/job/publish", json={"confirmation": "CREATE_DRAFT_PR"},
    )
    assert response.status_code == 409


def test_publish_rejects_blocked_review():
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        result={"verification_status": "passed", "patch_review_status": "blocked"},
    )
    response = client.post(
        "/api/ci/tasks/job/publish", json={"confirmation": "CREATE_DRAFT_PR"},
    )
    assert response.status_code == 409


def test_publish_rejects_missing_review_verdict():
    _jobs["job"] = Job(
        job_id="job", task_type="ci", source_url="url", status=Status.done,
        permission_level="verify",
        result={"verification_status": "passed", "patch_review_status": ""},
    )
    response = client.post(
        "/api/ci/tasks/job/publish", json={"confirmation": "CREATE_DRAFT_PR"},
    )
    assert response.status_code == 409


def test_ci_result_uses_allowlist():
    result = _ci_result({"run_id": 42, "root_cause": "boom", "secret": "do-not-return"})
    assert result["run_id"] == 42
    assert result["root_cause"] == "boom"
    assert "secret" not in result
