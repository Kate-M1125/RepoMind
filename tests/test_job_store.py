"""SQLite Job 持久化不依赖 FastAPI 或外部服务。"""
from backend.job_store import JobStore


def test_job_store_round_trip(tmp_path):
    path = tmp_path / "jobs.sqlite"
    first = JobStore(path)
    first.save("job-1", {"job_id": "job-1", "status": "done"}, "now")

    # 新实例模拟服务重启，仍能读取之前任务。
    second = JobStore(path)
    assert second.load_all() == [{"job_id": "job-1", "status": "done"}]


def test_job_store_upsert_replaces_payload(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite")
    store.save("job-1", {"job_id": "job-1", "status": "running"}, "one")
    store.save("job-1", {"job_id": "job-1", "status": "done"}, "two")
    assert store.load_all()[0]["status"] == "done"
