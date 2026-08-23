from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request

from gcash_checker import GCashCheckerError, check_gcash, proxy_candidates

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_AS_ASCII"] = False
MAX_BATCH = max(1, int(os.getenv("GCASH_MAX_BATCH", "100")))
WORKERS = max(1, int(os.getenv("GCASH_WORKERS", "4")))
executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="gcash-check")
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_result(result: Any) -> dict[str, Any]:
    return result.as_dict() if hasattr(result, "as_dict") else {"qualified": False}


def _proxy_transport_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "connect tunnel failed", "curl: (7)", "proxy", "could not resolve proxy",
        "failed to connect", "connection refused",
    ))


def run_one(job_id: str, index: int, item: dict[str, str], with_promo: bool) -> None:
    raw_candidates = item.get("proxies") or [item.get("proxy") or ""]
    candidates = []
    for raw_proxy in raw_candidates:
        try:
            candidates.extend(proxy_candidates(raw_proxy))
        except GCashCheckerError:
            candidates.append(raw_proxy)
    last_error: BaseException | None = None
    for candidate in candidates:
        try:
            result = check_gcash(item["token"], candidate, with_promo=with_promo)
            row = {"index": index, "ok": True, **safe_result(result)}
            break
        except GCashCheckerError as exc:
            last_error = exc
            if not _proxy_transport_error(exc) or candidate == candidates[-1]:
                row = {"index": index, "ok": False, "qualified": False, "error": str(exc)}
                break
        except Exception as exc:
            last_error = exc
            if not _proxy_transport_error(exc) or candidate == candidates[-1]:
                app.logger.exception("batch item failed")
                row = {"index": index, "ok": False, "qualified": False, "error": f"{type(exc).__name__}: {exc}"}
                break
    else:
        row = {"index": index, "ok": False, "qualified": False, "error": str(last_error or "检测失败")}
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job["results"][index] = row
        job["completed"] += 1
        if job["completed"] >= job["total"]:
            job["status"] = "completed"
            job["finished_at"] = now()


def parse_items(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_items = payload.get("items")
    if isinstance(raw_items, list):
        items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            token = str(item.get("token") or item.get("access_token") or "").strip()
            proxy_values = item.get("proxies") or item.get("proxy") or ""
            if isinstance(proxy_values, list):
                proxy_list = [str(value).strip() for value in proxy_values if str(value).strip()]
            else:
                proxy_list = [str(proxy_values).strip()] if str(proxy_values).strip() else []
            items.append({"token": token, "proxy": proxy_list[0] if proxy_list else "", "proxies": proxy_list})
        return items
    tokens = [line.strip() for line in str(payload.get("tokens") or "").splitlines() if line.strip()]
    proxies = [line.strip() for line in str(payload.get("proxies") or "").splitlines() if line.strip()]
    if not proxies:
        return [{"token": token, "proxy": "", "proxies": []} for token in tokens]
    # Keep the whole pool on every item so a failed tunnel can be retried with
    # another proxy; the first proxy is still assigned round-robin.
    proxy_cycle = cycle(proxies)
    return [{"token": token, "proxy": next(proxy_cycle), "proxies": proxies} for token in tokens]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "gcash-qualification-gui", "workers": WORKERS})


@app.post("/api/gcash/batch")
def create_batch():
    payload = request.get_json(silent=True) or {}
    items = parse_items(payload)
    if not items:
        return jsonify({"ok": False, "error": "请至少输入一条 Token"}), 400
    if len(items) > MAX_BATCH:
        return jsonify({"ok": False, "error": f"单批最多 {MAX_BATCH} 条"}), 400
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id, "status": "running", "total": len(items), "completed": 0,
        "created_at": now(), "results": [None] * len(items),
    }
    with jobs_lock:
        jobs[job_id] = job
    with_promo = bool(payload.get("with_promo", False))
    for index, item in enumerate(items):
        executor.submit(run_one, job_id, index, item, with_promo)
    return jsonify({"ok": True, "job_id": job_id, "total": len(items)})


@app.get("/api/gcash/batch/<job_id>")
def batch_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在或已过期"}), 404
        snapshot = dict(job)
        snapshot["results"] = list(job["results"])
    return jsonify({"ok": True, **snapshot})


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "18097")), debug=False)
