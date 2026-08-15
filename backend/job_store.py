"""API Job 的轻量 SQLite 持久化层。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any


class JobStore:
    """按 job_id 保存完整 JSON；单进程内用锁串行化 SQLite 写入。"""

    def __init__(self, path: Path | None = None):
        configured = os.getenv("REPOMIND_JOB_DB", "").strip()
        self.path = path or Path(configured or "repo_knowledge_base/api_jobs.sqlite")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                "job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def load_all(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT payload FROM jobs").fetchall()
        results = []
        for (payload,) in rows:
            try:
                results.append(json.loads(payload))
            except (TypeError, json.JSONDecodeError):
                # 单条损坏记录不能阻止整个 API 启动。
                continue
        return results

    def save(self, job_id: str, payload: dict[str, Any], updated_at: str) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs(job_id,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (job_id, encoded, updated_at),
            )
