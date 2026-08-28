from __future__ import annotations

import asyncio
import os
import threading
import uuid
import json
from pathlib import Path
from itertools import cycle
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request, session

from gcash_checker import GCashCheckerError, MAX_PROXY_RETRIES, check_gcash, proxy_candidates, _is_informative_error
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
# GCASH_WORKERS is the default per-job concurrency. MAX_WORKERS caps the
# per-batch ``workers`` request parameter and the total number of concurrent
# checkout checks across all running jobs (global semaphore).
WORKERS = max(1, int(os.getenv("GCASH_WORKERS", "4")))
MAX_WORKERS = max(WORKERS, int(os.getenv("GCASH_MAX_WORKERS", "32")))
# Hard per-item timeout (seconds). A single hung item (e.g. a network call that
# never returns) must not hold its semaphore slot forever, otherwise the
# sliding window stalls and the batch appears to "stop detecting midway". After
# this long the item is cancelled, an error row is stored and the window moves
# on. This is the outer backstop; each individual check_gcash call is additionally
# bounded by CHECK_TIMEOUT in gcash_checker.run_one/_run_one_multi.
ITEM_TIMEOUT = max(30, int(os.getenv("GCASH_ITEM_TIMEOUT", "900")))
# Hard timeout for a single check_gcash call (one checkout attempt). A hung
# call must not occupy its semaphore slot across many retries / regions.
CHECK_TIMEOUT = max(30, int(os.getenv("GCASH_CHECK_TIMEOUT", "150")))
# A single shared asyncio loop runs every checkout task. Flask request threads
# only submit work through run_coroutine_threadsafe, so there is no
# thread-per-token overhead and no per-request event loop creation. gcash_checker
# keeps its per-proxy session pool / sentinel cache on this loop.
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, name="gcash-asyncio", daemon=True).start()
_global_semaphore: asyncio.Semaphore | None = None
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
        "connect tunnel failed", "curl: (7)", "curl: (35)", "tls connect error",
        "ssl connect error", "wrong_version_number", "proxy", "could not resolve proxy",
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


async def run_one(job_id: str, index: int, item: dict[str, Any], with_promo: bool, target_channel: str, preset: dict[str, str], visitor: bool = False, regions: list[dict[str, Any]] | None = None, retries: int | None = None) -> None:
    if regions:
        await _run_one_multi(job_id, index, item, with_promo, visitor, regions, retries)
        return
    max_attempts = max(1, int(retries if retries is not None else MAX_PROXY_RETRIES))
    raw_candidates = _channel_proxy_values(item, target_channel)
    candidates = []
    for raw_proxy in raw_candidates:
        try:
            candidates.extend(proxy_candidates(raw_proxy))
        except GCashCheckerError:
            candidates.append(raw_proxy)
    if not candidates:
        _store_row(job_id, index, {"index": index, "ok": False, "qualified": False, "error": "必须提供目标国家出口代理"})
        return
    last_error: BaseException | None = None
    row: dict[str, Any] | None = None
    for attempt in range(max_attempts):
        candidate = candidates[attempt % len(candidates)]
        try:
            result = await asyncio.wait_for(
                check_gcash(item["token"], candidate, with_promo=with_promo, target_channel=target_channel, preset=preset, retries=1),
                timeout=CHECK_TIMEOUT,
            )
            row = {"index": index, "ok": True, **safe_result(result, visitor)}
            if not visitor:
                row["submitted_row"] = item["token"]
            break
        except Exception as exc:
            # 优先保留含真实原因（如 403 forbidden ip=...）的错误，而不是被
            # TLS 握手噪声等覆盖。
            if last_error is None or not _is_informative_error(str(last_error)):
                last_error = exc
    if row is None:
        if isinstance(last_error, GCashCheckerError):
            row = {"index": index, "ok": False, "qualified": False, "error": str(last_error)}
        elif last_error is not None:
            app.logger.exception("batch item failed after retries")
            row = {"index": index, "ok": False, "qualified": False, "error": f"{type(last_error).__name__}: {last_error}"}
        else:
            row = {"index": index, "ok": False, "qualified": False, "error": "检测失败"}
    _store_row(job_id, index, row)


def _store_row(job_id: str, index: int, row: dict[str, Any]) -> None:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        # Idempotent: a row may be stored from both the run_one flow and the
        # task-guard fallback. Only the first write counts, so `completed` can
        # never exceed `total` and the job is guaranteed to terminate.
        if job["results"][index] is not None:
            return
        job["results"][index] = row
        job["completed"] += 1
        if job["completed"] >= job["total"]:
            job["status"] = "completed"
            job["finished_at"] = now()


def _finalize_job(job_id: str) -> None:
    """Force a running job into a terminal state.

    Called once when the job's dispatch coroutine finishes (or is cancelled).
    Marks every row that still never produced a result as interrupted so the
    job always leaves the "running" state and the frontend stops polling.
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job["status"] == "completed":
            return
        for index, row in enumerate(job["results"]):
            if row is None:
                job["results"][index] = {"index": index, "ok": False, "qualified": False, "error": "检测被中断"}
                job["completed"] += 1
        job["status"] = "completed"
        job["finished_at"] = now()


async def _run_one_multi(job_id: str, index: int, item: dict[str, Any], with_promo: bool, visitor: bool, regions: list[dict[str, Any]], retries: int | None = None) -> None:
    """Check one token against several region presets; each region creates its
    own country/currency checkout and uses that channel's proxy pool. Every
    region/account is retried up to ``retries`` times before being marked failed."""
    max_attempts = max(1, int(retries if retries is not None else MAX_PROXY_RETRIES))
    region_rows = []
    for region in regions:
        region_channel = str(region.get("channel") or "gcash").lower()
        preset = {
            "channel": region_channel,
            "country": str(region.get("country") or "PH").upper(),
            "currency": str(region.get("currency") or "").upper(),
            "plan": str(region.get("plan") or "plus").lower(),
        }
        raw_candidates = _channel_proxy_values(item, region_channel)
        candidates = []
        for raw_proxy in raw_candidates:
            try:
                candidates.extend(proxy_candidates(raw_proxy))
            except GCashCheckerError:
                candidates.append(raw_proxy)
        last_error: BaseException | None = None
        result = None
        if not candidates:
            region_rows.append(_region_error(region, GCashCheckerError("必须提供目标国家出口代理")))
            continue
        for attempt in range(max_attempts):
            candidate = candidates[attempt % len(candidates)]
            try:
                result = await asyncio.wait_for(
                    check_gcash(
                        item["token"], candidate, with_promo=with_promo,
                        target_channel=region_channel, preset=preset, retries=1,
                    ),
                    timeout=CHECK_TIMEOUT,
                )
                break
            except Exception as exc:
                # 优先保留含真实原因（如 403 forbidden ip=...）的错误，而不是
                # 被 TLS 握手噪声等覆盖。
                if last_error is None or not _is_informative_error(str(last_error)):
                    last_error = exc
        if result is not None:
            region_rows.append(_region_result(region, result, visitor))
        else:
            region_rows.append(_region_error(region, last_error))
    _store_row(job_id, index, _aggregate_row(index, region_rows, visitor, item))


def _region_result(region: dict[str, Any], result: Any, visitor: bool) -> dict[str, Any]:
    data = safe_result(result, visitor)
    out = {
        "name": str(region.get("name") or region.get("preset") or result.target_channel),
        "channel": result.target_channel,
        "country": data.get("country") or region.get("country") or "PH",
        "currency": data.get("currency") or region.get("currency") or "",
        "qualified": result.qualified,
        "checkout_session_id": data.get("checkout_session_id", ""),
        "processor_entity": data.get("processor_entity", ""),
        "payment_method_type": data.get("payment_method_type", ""),
        "checkout_amount": data.get("checkout_amount"),
        "proxy_configured": data.get("proxy_configured", True),
        "evidence": data.get("evidence", ""),
        "available_channels": data.get("available_channels") or [],
        "channel_details": data.get("channel_details") or [],
        "channel_availability": data.get("channel_availability") or {},
        "error": "",
    }
    if not visitor:
        out["account_email"] = data.get("account_email", "")
        out["access_token"] = data.get("access_token", "")
    return out


def _region_error(region: dict[str, Any], error: BaseException | None) -> dict[str, Any]:
    return {
        "name": str(region.get("name") or region.get("preset") or "地区"),
        "channel": str(region.get("channel") or "gcash").lower(),
        "country": str(region.get("country") or "PH").upper(),
        "currency": str(region.get("currency") or "").upper(),
        "qualified": False,
        "error": str(error or "检测失败"),
        "available_channels": [],
        "channel_details": [],
        "channel_availability": {},
    }


def _aggregate_row(index: int, region_rows: list[dict[str, Any]], visitor: bool, item: dict[str, Any]) -> dict[str, Any]:
    """Merge per-region results into one table row: any-region qualification,
    union of published channels, per-region breakdown for the frontend."""
    succeeded = [r for r in region_rows if not r.get("error")]
    failed = [r for r in region_rows if r.get("error")]
    if not succeeded:
        return {
            "index": index, "ok": False, "qualified": False,
            "error": "；".join(r["error"] for r in failed) or "检测失败",
            "regions": region_rows,
        }
    selected: dict[str, bool] = {}
    for r in succeeded:
        for channel, value in (r.get("channel_availability") or {}).items():
            selected[str(channel)] = bool(selected.get(str(channel)) or value)
    available: list[str] = []
    details: list[Any] = []
    for r in succeeded:
        tag = str(r.get("name") or r.get("channel") or "")
        for channel in r.get("available_channels") or []:
            if str(channel).lower() not in {str(c).lower() for c in available}:
                available.append(channel)
        for detail in r.get("channel_details") or []:
            if isinstance(detail, dict) and isinstance(detail.get("selected"), dict):
                continue
            entry = dict(detail) if isinstance(detail, dict) else detail
            if isinstance(entry, dict):
                entry.setdefault("region", tag)
            details.append(entry)
    details.append({"selected": selected, "checkout_provider": "multi-region"})
    primary = succeeded[0]
    return {
        "index": index, "ok": True, "qualified": any(r.get("qualified") for r in succeeded),
        "channel": primary.get("channel") or "",
        "target_channel": primary.get("channel") or "",
        "country": primary.get("country") or "PH",
        "currency": primary.get("currency") or "",
        "checkout_session_id": next((r.get("checkout_session_id") or "" for r in succeeded if r.get("checkout_session_id")), ""),
        "processor_entity": next((r.get("processor_entity") or "" for r in succeeded if r.get("processor_entity")), ""),
        "payment_method_type": next((r.get("payment_method_type") or "" for r in succeeded if r.get("payment_method_type")), ""),
        "checkout_amount": next((r.get("checkout_amount") for r in succeeded if r.get("checkout_amount") is not None), None),
        "proxy_configured": True,
        "evidence": " | ".join(f"{r.get('name')}: {r.get('evidence')}" for r in succeeded if r.get("evidence")) or "检测完成",
        "available_channels": available,
        "channel_details": details,
        "channel_availability": selected,
        "regions": region_rows,
        "account_email": primary.get("account_email") or "",
        "access_token": primary.get("access_token") or "",
        "submitted_row": "" if visitor else item["token"],
    }


async def _dispatch_job(
    job_id: str,
    items: list[dict[str, Any]],
    requested_workers: int,
    with_promo: bool,
    target_channel: str,
    preset: dict[str, str],
    visitor: bool,
    regions: list[dict[str, Any]] | None,
    requested_retries: int,
) -> None:
    """Run one batch job on the shared asyncio loop.

    Every item is turned into a task immediately so all of them are pending in
    the very first event-loop pass ("一瞬间全部创建").  A per-job semaphore caps
    how many run concurrently and is released the moment one returns, so the
    next pending item starts right away ("一个请求返回补上一个").  The global
    semaphore (MAX_WORKERS) additionally bounds total in-flight checks across
    all running jobs, matching the old ThreadPoolExecutor ceiling.

    Every item also runs under a hard timeout (ITEM_TIMEOUT) and is fully
    guarded, so no hung request or unexpected exception can hold a semaphore
    slot forever and stall the batch mid-way.  The finally block finalizes the
    job so it always leaves the "running" state.
    """
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(MAX_WORKERS)
    job_semaphore = asyncio.Semaphore(requested_workers)

    async def _guarded(index: int, item: dict[str, Any]) -> None:
        try:
            async with _global_semaphore, job_semaphore:
                await asyncio.wait_for(
                    run_one(job_id, index, item, with_promo, target_channel, preset, visitor, regions, requested_retries),
                    timeout=ITEM_TIMEOUT,
                )
        except asyncio.TimeoutError:
            _store_row(job_id, index, {"index": index, "ok": False, "qualified": False, "error": "检测超时，已跳过"})
        except asyncio.CancelledError:
            # Preserve cancellation semantics for the whole job, but still give
            # the item a row so the finalizer can complete the job cleanly.
            _store_row(job_id, index, {"index": index, "ok": False, "qualified": False, "error": "检测被中断"})
            raise
        except Exception as exc:
            app.logger.exception("batch item unexpected failure")
            _store_row(job_id, index, {"index": index, "ok": False, "qualified": False, "error": f"{type(exc).__name__}: {exc}"})

    try:
        await asyncio.gather(*[asyncio.create_task(_guarded(index, item)) for index, item in enumerate(items)])
    except asyncio.CancelledError:
        raise
    except Exception:
        app.logger.exception("batch dispatch failed")
    finally:
        _finalize_job(job_id)


async def _retry_job_item(
    job_id: str,
    index: int,
    item: dict[str, Any],
    with_promo: bool,
    target_channel: str,
    preset: dict[str, str],
    visitor: bool,
    regions: list[dict[str, Any]] | None,
    retries: int,
) -> None:
    """Re-run a single failed account and write its row back into the job.

    Runs on the shared event loop with the same hard-timeout / guard rules as a
    normal batch item, so a retry can never stall the loop or a semaphore slot.
    The original job row was reset to ``None`` (and ``status`` back to running)
    by the retry endpoint before this coroutine is submitted.
    """
    global _global_semaphore
    if _global_semaphore is None:
        _global_semaphore = asyncio.Semaphore(MAX_WORKERS)
    try:
        async with _global_semaphore, asyncio.Semaphore(1):
            await asyncio.wait_for(
                run_one(job_id, index, item, with_promo, target_channel, preset, visitor, regions, retries),
                timeout=ITEM_TIMEOUT,
            )
    except asyncio.TimeoutError:
        _store_row(job_id, index, {"index": index, "ok": False, "qualified": False, "error": "重试超时，已跳过"})
    except asyncio.CancelledError:
        _store_row(job_id, index, {"index": index, "ok": False, "qualified": False, "error": "重试被中断"})
        raise
    except Exception as exc:
        app.logger.exception("batch item retry failed")
        _store_row(job_id, index, {"index": index, "ok": False, "qualified": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        _finalize_job(job_id)


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
    "paypal_nl": {"channel": "paypal", "country": "NL", "currency": "EUR", "plan": "plus"},
    "ideal_nl": {"channel": "ideal", "country": "NL", "currency": "EUR", "plan": "plus"},
    "momo_vn": {"channel": "momo", "country": "VN", "currency": "VND", "plan": "plus"},
    "gopay_id": {"channel": "gopay", "country": "ID", "currency": "IDR", "plan": "plus"},
    "upi_in": {"channel": "upi", "country": "IN", "currency": "INR", "plan": "plus"},
    "blik_pl": {"channel": "blik", "country": "PL", "currency": "PLN", "plan": "plus"},
    "pix_br": {"channel": "pix", "country": "BR", "currency": "BRL", "plan": "plus"},
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


def resolve_regions(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Resolve the optional multi-region request body. Accepts a list of preset
    names/objects or a {name: overrides} dict. Returns None when the payload
    carries no ``regions`` key so the legacy single-preset flow still works."""
    raw = payload.get("regions")
    if raw is None:
        return None
    custom = payload.get("presets") if isinstance(payload.get("presets"), dict) else {}
    if isinstance(raw, dict):
        entries: list[Any] = []
        for name, value in raw.items():
            entry: dict[str, Any] = {"preset": str(name)}
            if isinstance(value, dict):
                entry.update({str(key): item for key, item in value.items()})
            entries.append(entry)
    elif isinstance(raw, list):
        entries = [entry if isinstance(entry, dict) else {"preset": entry} for entry in raw]
    else:
        entries = []
    regions = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        preset_name = str(entry.get("preset") or entry.get("name") or "gcash").strip()
        if not preset_name:
            continue
        preset = dict(BUILTIN_PRESETS.get(preset_name, {}))
        if isinstance(custom.get(preset_name), dict):
            preset.update(custom[preset_name])
        if not preset:
            preset = {"channel": "gcash", "country": "PH", "currency": "PHP", "plan": "plus"}
        channel = str(entry.get("channel") or preset.get("channel") or "gcash").lower().strip()
        country = str(entry.get("country") or preset.get("country") or "PH").upper().strip()
        currency = str(entry.get("currency") or preset.get("currency") or "").upper().strip()
        regions.append({
            "name": str(entry.get("name") or preset_name).strip() or preset_name,
            "preset": preset_name,
            "channel": channel,
            "country": country,
            "currency": currency,
            "plan": str(entry.get("plan") or preset.get("plan") or "plus").lower().strip(),
        })
    return regions or None


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
        requested_retries = int(payload.get("retries") or MAX_PROXY_RETRIES)
    except (TypeError, ValueError):
        requested_retries = MAX_PROXY_RETRIES
    try:
        future = asyncio.run_coroutine_threadsafe(
            check_gcash(token, str(proxy), target_channel=target_channel, preset=preset, retries=requested_retries),
            _loop,
        )
        result = future.result(timeout=180)
    except GCashCheckerError as exc:
        return jsonify({"ok": False, "preset": name, "error": str(exc)}), 400
    except TimeoutError:
        return jsonify({"ok": False, "preset": name, "error": "检测超时（>180s）"}), 504
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
    try:
        requested_workers = int(payload.get("workers") or WORKERS)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "workers 必须是整数"}), 400
    if requested_workers < 1 or requested_workers > MAX_WORKERS:
        return jsonify({"ok": False, "error": f"workers 范围为 1-{MAX_WORKERS}"}), 400
    try:
        requested_retries = int(payload.get("retries") or MAX_PROXY_RETRIES)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "retries 必须是整数"}), 400
    if requested_retries < 1 or requested_retries > 20:
        return jsonify({"ok": False, "error": "retries 范围为 1-20"}), 400
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
    regions = resolve_regions(payload)
    if regions is not None:
        preset_name = regions[0]["preset"]
        preset = {"channel": regions[0]["channel"], "country": regions[0]["country"], "currency": regions[0]["currency"], "plan": regions[0]["plan"]}
        target_channel = regions[0]["channel"]
    else:
        preset_name, preset = resolve_preset(payload)
        target_channel = str(payload.get("target_channel") or preset.get("channel") or "gcash").lower()
    job_id = uuid.uuid4().hex
    workers = min(requested_workers, len(items))
    job = {
        "job_id": job_id, "status": "running", "total": len(items), "completed": 0,
        "created_at": now(), "results": [None] * len(items), "retries": requested_retries,
        "workers": workers,
        # Keep the original submission so a single row can be retried later
        # without resubmitting the whole batch.
        "items": items, "with_promo": with_promo, "visitor": visitor,
        "target_channel": target_channel, "preset": preset, "regions": regions,
    }
    with jobs_lock:
        jobs[job_id] = job
    # Submit one task per item immediately. All tasks are created in the same
    # event-loop pass, so the first ``workers`` checks go out at once; as each
    # one returns, the next pending task is released instantly (sliding window).
    asyncio.run_coroutine_threadsafe(
        _dispatch_job(job_id, items, workers, with_promo, target_channel, preset, visitor, regions, requested_retries),
        _loop,
    )
    return jsonify({"ok": True, "job_id": job_id, "total": len(items), "workers": workers, "retries": requested_retries})


@app.get("/api/gcash/batch/<job_id>")
def batch_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在或已过期"}), 404
        snapshot = dict(job)
        snapshot["results"] = list(job["results"])
    return jsonify({"ok": True, **snapshot})


@app.post("/api/gcash/batch/<job_id>/retry")
def retry_batch_item(job_id: str):
    """Retry a single failed account of a finished batch, in place.

    Resets ``results[index]`` to pending (and the job status back to running),
    then re-runs that one item with its original token / proxies / regions on
    the shared event loop.  The same row is updated in place when the retry
    finishes, so the frontend can keep polling this same job id.
    """
    payload = request.get_json(silent=True) or {}
    try:
        index = int(payload.get("index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "index 必须是整数"}), 400
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在或已过期"}), 404
        if not (0 <= index < job["total"]):
            return jsonify({"ok": False, "error": f"index 超出范围（0-{job['total'] - 1}）"}), 400
        items = job.get("items")
        if not isinstance(items, list) or not (0 <= index < len(items)):
            return jsonify({"ok": False, "error": "任务缺少可重试的原始数据"}), 400
        if job["results"][index] is None:
            return jsonify({"ok": False, "error": "该行正在检测中，请稍候"}), 409
        item = items[index]
        # Reset the row so the frontend shows "检测中…" again; decrement the
        # completed counter so _store_row's idempotent accounting stays valid.
        job["results"][index] = None
        job["completed"] = max(0, job["completed"] - 1)
        job["status"] = "running"
    with_promo = bool(job.get("with_promo", False))
    visitor = bool(job.get("visitor", False))
    target_channel = str(job.get("target_channel") or item.get("channel") or "gcash").lower()
    preset = job.get("preset") or {}
    regions = job.get("regions")
    retries = max(1, int(job.get("retries") or MAX_PROXY_RETRIES))
    asyncio.run_coroutine_threadsafe(
        _retry_job_item(job_id, index, item, with_promo, target_channel, preset, visitor, regions, retries),
        _loop,
    )
    return jsonify({"ok": True, "job_id": job_id, "index": index, "status": "retrying"})


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "18097")), debug=False)
