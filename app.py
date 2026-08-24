from __future__ import annotations

import os
import threading
import uuid
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from itertools import cycle
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request, session

from gcash_checker import GCashCheckerError, check_gcash, proxy_candidates
from cdk_store import consume, create_cdk, redeem_cdk

app = Flask(__name__, template_folder="templates", static_folder="static")
app.config["JSON_AS_ASCII"] = False
app.secret_key = os.getenv("GCASH_SESSION_SECRET", "change-this-session-secret")
ADMIN_PASSWORD = os.getenv("GCASH_ADMIN_PASSWORD", "admin")
mode_lock = threading.RLock()
MODE_FILE = Path(os.getenv("GCASH_MODE_FILE", "data/service_mode.json"))

def _load_mode() -> str:
    configured = os.getenv("GCASH_MODE", "").lower().strip()
    if configured in {"self", "visitor"}:
        return configured
    try:
        value = json.loads(MODE_FILE.read_text(encoding="utf-8")).get("mode", "self")
        return value if value in {"self", "visitor"} else "self"
    except Exception:
        return "self"

service_mode = {"mode": _load_mode()}
MAX_BATCH = max(1, int(os.getenv("GCASH_MAX_BATCH", "100")))
# GCASH_WORKERS is the default per-job concurrency. Keep a larger shared pool so
# a request can opt into higher concurrency without creating an executor per job.
WORKERS = max(1, int(os.getenv("GCASH_WORKERS", "4")))
MAX_WORKERS = max(WORKERS, int(os.getenv("GCASH_MAX_WORKERS", "32")))
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="gcash-check")
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.RLock()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_result(result: Any, visitor: bool = False) -> dict[str, Any]:
    data = result.as_dict() if hasattr(result, "as_dict") else {"qualified": False}
    if visitor:
        data.pop("access_token", None)
        data.pop("account_email", None)
        data.pop("submitted_row", None)
    return data


def _proxy_transport_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "connect tunnel failed", "curl: (7)", "proxy", "could not resolve proxy",
        "failed to connect", "connection refused",
    ))


def _channel_proxy_values(item: dict[str, Any], channel: str) -> list[str]:
    channel_proxies = item.get("channel_proxies")
    if isinstance(channel_proxies, dict):
        values = channel_proxies.get(channel) or channel_proxies.get(channel.lower())
        if isinstance(values, list):
            return [str(value).strip() for value in values if str(value).strip()]
        if values:
            return [str(values).strip()]
    return [str(value).strip() for value in (item.get("proxies") or [item.get("proxy") or ""]) if str(value).strip()]


def run_one(job_id: str, index: int, item: dict[str, Any], with_promo: bool, target_channel: str, preset: dict[str, str], visitor: bool = False, channels: list[str] | None = None) -> None:
    raw_candidates = _channel_proxy_values(item, target_channel)
    candidates = []
    for raw_proxy in raw_candidates:
        try:
            candidates.extend(proxy_candidates(raw_proxy))
        except GCashCheckerError:
            candidates.append(raw_proxy)
    last_error: BaseException | None = None
    for candidate in candidates:
        try:
            result = check_gcash(item["token"], candidate, with_promo=with_promo, target_channel=target_channel, preset=preset, target_channels=channels)
            row = {"index": index, "ok": True, **safe_result(result, visitor)}
            if not visitor:
                row["submitted_row"] = item["token"]
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


def run_one_limited(semaphore: threading.BoundedSemaphore, *args: Any) -> None:
    with semaphore:
        run_one(*args)


def parse_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items")
    payload_channel_proxies = payload.get("channel_proxies") if isinstance(payload.get("channel_proxies"), dict) else {}
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
            item_channel_proxies = item.get("channel_proxies")
            normalized_channel_proxies = dict(payload_channel_proxies)
            if isinstance(item_channel_proxies, dict):
                normalized_channel_proxies.update(item_channel_proxies)
            normalized_channel_proxies = {str(channel).lower().strip(): ([str(value).strip() for value in values if str(value).strip()] if isinstance(values, list) else [line.strip() for line in str(values or "").splitlines() if line.strip()]) for channel, values in normalized_channel_proxies.items()}
            items.append({"token": token, "proxy": proxy_list[0] if proxy_list else "", "proxies": proxy_list, "channel_proxies": normalized_channel_proxies})
        return items
    tokens = [line.strip() for line in str(payload.get("tokens") or "").splitlines() if line.strip()]
    proxies = [line.strip() for line in str(payload.get("proxies") or "").splitlines() if line.strip()]
    channel_proxies = {}
    for channel, values in payload_channel_proxies.items():
        if isinstance(values, list):
            normalized = [str(value).strip() for value in values if str(value).strip()]
        else:
            normalized = [line.strip() for line in str(values or "").splitlines() if line.strip()]
        if normalized:
            channel_proxies[str(channel).lower().strip()] = normalized
    if not proxies and not channel_proxies:
        return [{"token": token, "proxy": "", "proxies": [], "channel_proxies": {}} for token in tokens]
    # Keep the whole pool on every item so a failed tunnel can be retried with
    # another proxy; the first proxy is still assigned round-robin.
    proxy_cycle = cycle(proxies)
    return [{"token": token, "proxy": next(proxy_cycle), "proxies": proxies, "channel_proxies": channel_proxies} for token in tokens]


@app.get("/")
def index():
    # Pass the resolved mode to the template so self-deployed pages do not
    # include visitor-only CDK UI at all.
    with mode_lock:
        mode = service_mode["mode"]
    return render_template("index.html", service_mode=mode)


@app.get("/admin")
def admin_page():
    return render_template("admin.html")


@app.post("/api/cdk/redeem")
def cdk_redeem():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, **redeem_cdk(payload.get("cdk") or payload.get("code"))})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.post("/api/cdk/create")
def cdk_create():
    expected = os.getenv("GCASH_ADMIN_KEY", "")
    if expected and request.headers.get("X-Admin-Key") != expected:
        return jsonify({"ok": False, "error": "管理员密钥错误"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify({"ok": True, "cdk": create_cdk(payload.get("quota", 100), payload.get("days", 30))})
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def admin_required() -> bool:
    return bool(session.get("admin"))


@app.post("/api/admin/login")
def admin_login():
    payload = request.get_json(silent=True) or {}
    if str(payload.get("password") or "") != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "管理员密码错误"}), 403
    session["admin"] = True
    return jsonify({"ok": True, "admin": True})


@app.post("/api/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return jsonify({"ok": True})


@app.get("/api/admin/status")
def admin_status():
    with mode_lock:
        mode = service_mode["mode"]
    return jsonify({"ok": True, "admin": admin_required(), "mode": mode})


@app.post("/api/admin/mode")
def admin_mode():
    if not admin_required():
        return jsonify({"ok": False, "error": "需要管理员登录"}), 401
    payload = request.get_json(silent=True) or {}
    mode = str(payload.get("mode") or "").lower()
    if mode not in {"self", "visitor"}:
        return jsonify({"ok": False, "error": "mode 必须是 self 或 visitor"}), 400
    with mode_lock:
        service_mode["mode"] = mode
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = MODE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps({"mode": mode}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(MODE_FILE)
    return jsonify({"ok": True, "mode": mode})


@app.get("/api/health")
def health():
    with mode_lock:
        mode = service_mode["mode"]
    return jsonify({"ok": True, "service": "gcash-qualification-gui", "workers": WORKERS, "mode": mode})


BUILTIN_PRESETS = {
    "gcash": {"channel": "gcash", "country": "PH", "currency": "PHP", "plan": "plus"},
    "card": {"channel": "card", "country": "PH", "currency": "PHP", "plan": "plus"},
    "paypal_uk": {"channel": "paypal", "country": "GB", "currency": "GBP", "plan": "plus"},
    "ideal_nl": {"channel": "ideal", "country": "NL", "currency": "EUR", "plan": "plus"},
    "momo_vn": {"channel": "momo", "country": "VN", "currency": "VND", "plan": "plus"},
    "gopay_id": {"channel": "gopay", "country": "ID", "currency": "IDR", "plan": "plus"},
}


def resolve_preset(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    custom = payload.get("presets") if isinstance(payload.get("presets"), dict) else {}
    name = str(payload.get("preset") or "gcash").strip()
    preset = dict(BUILTIN_PRESETS.get(name, {}))
    if isinstance(custom.get(name), dict):
        preset.update(custom[name])
    if not preset:
        preset = {"channel": str(payload.get("target_channel") or "gcash"), "country": "PH", "currency": "PHP", "plan": "plus"}
    return name, preset


@app.get("/api/presets")
def list_presets():
    return jsonify({"ok": True, "presets": BUILTIN_PRESETS})


@app.post("/api/gcash/check")
def check_one():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token") or payload.get("access_token") or ""
    name, preset = resolve_preset(payload)
    target_channel = str(payload.get("target_channel") or preset.get("channel") or "gcash").lower()
    channel_map = payload.get("channel_proxies") if isinstance(payload.get("channel_proxies"), dict) else {}
    proxy = payload.get("proxy") or channel_map.get(target_channel) or ""
    if isinstance(proxy, list):
        proxy = proxy[0] if proxy else ""
    if not token or not proxy:
        return jsonify({"ok": False, "error": "token 和该渠道 proxy 均为必填"}), 400
    try:
        result = check_gcash(token, str(proxy), target_channel=target_channel, preset=preset)
    except GCashCheckerError as exc:
        return jsonify({"ok": False, "preset": name, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("single channel check failed")
        return jsonify({"ok": False, "preset": name, "error": f"{type(exc).__name__}: {exc}"}), 502
    return jsonify({"ok": True, "preset": name, **result.as_dict()})


@app.post("/api/gcash/batch")
def create_batch():
    payload = request.get_json(silent=True) or {}
    items = parse_items(payload)
    if not items:
        return jsonify({"ok": False, "error": "请至少输入一条 Token"}), 400
    if len(items) > MAX_BATCH:
        return jsonify({"ok": False, "error": f"单批最多 {MAX_BATCH} 条"}), 400
    try:
        requested_workers = int(payload.get("workers") or WORKERS)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "workers 必须是整数"}), 400
    if requested_workers < 1 or requested_workers > MAX_WORKERS:
        return jsonify({"ok": False, "error": f"workers 范围为 1-{MAX_WORKERS}"}), 400
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id, "status": "running", "total": len(items), "completed": 0,
        "created_at": now(), "results": [None] * len(items),
    }
    with jobs_lock:
        jobs[job_id] = job
    with_promo = bool(payload.get("with_promo", False))
    with mode_lock:
        configured_mode = service_mode["mode"]
    visitor = configured_mode == "visitor"
    cdk = str(payload.get("cdk") or "").strip()
    if visitor:
        if not cdk:
            return jsonify({"ok": False, "error": "访客模式需要 CDK"}), 400
        try:
            consume(cdk, len(items))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 403
    preset_name, preset = resolve_preset(payload)
    target_channel = str(payload.get("target_channel") or preset.get("channel") or "gcash").lower()
    # Submit one task per item. The process-wide executor bounds total load;
    # the per-job semaphore enforces the requested concurrency.
    job["workers"] = min(requested_workers, len(items))
    semaphore = threading.BoundedSemaphore(job["workers"])
    channels = [str(channel).lower() for channel in (payload.get("channels") or [target_channel]) if str(channel).strip()]
    for index, item in enumerate(items):
        executor.submit(run_one_limited, semaphore, job_id, index, item, with_promo, target_channel, preset, visitor, channels)
    return jsonify({"ok": True, "job_id": job_id, "total": len(items), "workers": job["workers"]})


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
