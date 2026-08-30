from __future__ import annotations

"""SQLite persistence for batch qualification jobs.

The worker keeps the live job object in memory for fast progress updates, while
this module stores a copy after each state change so completed jobs remain
available after a page refresh or process restart.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DB_PATH = Path(os.getenv("GCASH_TASK_DB", "data/tasks.sqlite3"))
DB_LOCK = threading.RLock()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    completed INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    workers INTEGER NOT NULL,
    retries INTEGER NOT NULL,
    with_promo INTEGER NOT NULL DEFAULT 0,
    visitor INTEGER NOT NULL DEFAULT 0,
    target_channel TEXT NOT NULL,
    preset_json TEXT NOT NULL,
    regions_json TEXT,
    items_json TEXT NOT NULL,
    results_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with DB_LOCK:
        conn = _connect()
        try:
            conn.execute(_SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
            conn.commit()
        finally:
            conn.close()


def _decode(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    results = _decode(row["results_json"], [])
    items = _decode(row["items_json"], [])
    regions = _decode(row["regions_json"], None)
    return {
        "job_id": row["job_id"],
        "status": row["status"],
        "total": int(row["total"]),
        "completed": int(row["completed"]),
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "results": results if isinstance(results, list) else [],
        "retries": int(row["retries"]),
        "workers": int(row["workers"]),
        "items": items if isinstance(items, list) else [],
        "with_promo": bool(row["with_promo"]),
        "visitor": bool(row["visitor"]),
        "target_channel": row["target_channel"],
        "preset": _decode(row["preset_json"], {}),
        "regions": regions if isinstance(regions, list) else None,
    }


def save_task(job: dict[str, Any]) -> None:
    """Insert or replace a complete job snapshot.

    The original items are intentionally kept server-side because the retry
    endpoint needs the submitted token/proxy data. API serializers must never
    expose this field.
    """
    with DB_LOCK:
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO tasks (
                    job_id, status, total, completed, created_at, finished_at,
                    workers, retries, with_promo, visitor, target_channel,
                    preset_json, regions_json, items_json, results_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    total=excluded.total,
                    completed=excluded.completed,
                    created_at=excluded.created_at,
                    finished_at=excluded.finished_at,
                    workers=excluded.workers,
                    retries=excluded.retries,
                    with_promo=excluded.with_promo,
                    visitor=excluded.visitor,
                    target_channel=excluded.target_channel,
                    preset_json=excluded.preset_json,
                    regions_json=excluded.regions_json,
                    items_json=excluded.items_json,
                    results_json=excluded.results_json,
                    updated_at=excluded.updated_at
                """,
                (
                    str(job["job_id"]),
                    str(job.get("status") or "running"),
                    int(job.get("total") or 0),
                    int(job.get("completed") or 0),
                    str(job.get("created_at") or _now()),
                    job.get("finished_at"),
                    int(job.get("workers") or 1),
                    int(job.get("retries") or 1),
                    int(bool(job.get("with_promo"))),
                    int(bool(job.get("visitor"))),
                    str(job.get("target_channel") or "gcash"),
                    _json(job.get("preset") or {}),
                    _json(job.get("regions")) if job.get("regions") is not None else None,
                    _json(job.get("items") or []),
                    _json(job.get("results") or []),
                    _now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_task(job_id: str) -> dict[str, Any] | None:
    with DB_LOCK:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE job_id = ?", (job_id,)).fetchone()
            return _row_to_job(row) if row else None
        finally:
            conn.close()


def _is_qualified(row: Any) -> bool:
    if not isinstance(row, dict) or not row.get("ok"):
        return False
    if row.get("qualified"):
        return True
    availability = row.get("channel_availability")
    if isinstance(availability, dict) and any(bool(value) for value in availability.values()):
        return True
    details = row.get("channel_details")
    if isinstance(details, list):
        return any(
            isinstance(detail, dict)
            and isinstance(detail.get("selected"), dict)
            and any(bool(value) for value in detail["selected"].values())
            for detail in details
        )
    return False


def _summary(job: dict[str, Any]) -> dict[str, Any]:
    results = job.get("results") if isinstance(job.get("results"), list) else []
    rows = [row for row in results if isinstance(row, dict)]
    return {
        "job_id": job["job_id"],
        "status": job.get("status") or "running",
        "total": int(job.get("total") or 0),
        "completed": int(job.get("completed") or 0),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
        "workers": int(job.get("workers") or 1),
        "retries": int(job.get("retries") or 1),
        "with_promo": bool(job.get("with_promo")),
        "visitor": bool(job.get("visitor")),
        "target_channel": job.get("target_channel") or "gcash",
        "qualified_count": sum(1 for row in rows if _is_qualified(row)),
        "failed_count": sum(1 for row in rows if not row.get("ok")),
    }


def list_tasks(limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    with DB_LOCK:
        conn = _connect()
        try:
            total = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            tasks = [_summary(_row_to_job(row)) for row in rows]
            return tasks, total
        finally:
            conn.close()


def delete_task(job_id: str) -> bool:
    with DB_LOCK:
        conn = _connect()
        try:
            cursor = conn.execute("DELETE FROM tasks WHERE job_id = ?", (job_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()


def recover_running_tasks() -> int:
    """Finish jobs abandoned by a process restart with explicit error rows."""
    recovered = 0
    with DB_LOCK:
        conn = _connect()
        try:
            rows = conn.execute("SELECT * FROM tasks WHERE status = 'running'").fetchall()
            for row in rows:
                job = _row_to_job(row)
                results = job["results"]
                total = int(job.get("total") or len(results))
                if len(results) < total:
                    results.extend([None] * (total - len(results)))
                for index in range(total):
                    if results[index] is None:
                        results[index] = {
                            "index": index,
                            "ok": False,
                            "qualified": False,
                            "error": "服务重启，检测被中断",
                        }
                job["results"] = results[:total]
                job["completed"] = total
                job["status"] = "completed"
                job["finished_at"] = _now()
                conn.execute(
                    "UPDATE tasks SET status=?, completed=?, finished_at=?, results_json=?, updated_at=? WHERE job_id=?",
                    (job["status"], total, job["finished_at"], _json(job["results"]), _now(), job["job_id"]),
                )
                recovered += 1
            conn.commit()
            return recovered
        finally:
            conn.close()


init_db()
