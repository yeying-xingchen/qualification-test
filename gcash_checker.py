"""Standalone GCash qualification checker.

This module creates a
PH/PHP custom checkout, reads its published payment methods, and stops before
confirm/start so it cannot initiate a payment.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from curl_cffi import requests

from sentinel_token import SentinelTokenProvider

OPENAI_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0"
MAX_PROXY_RETRIES = 2
# Some Checkout responses expose only an opaque custom-payment-method id.
# This known OpenAI GCash method must be treated as GCash even without a
# human-readable name in the method payload.
KNOWN_GCASH_METHOD_IDS = {
    "cpmt_1TOgstC6h1nxGoI3WUVEY2cJ",
}


class GCashCheckerError(RuntimeError):
    pass


@dataclass(frozen=True)
class QualificationResult:
    qualified: bool
    checkout_session_id: str
    target_channel: str
    processor_entity: str
    payment_method_type: str
    checkout_amount: Any
    checkout_currency: str
    proxy_configured: bool
    evidence: str
    available_channels: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "qualified": self.qualified,
            "channel": self.target_channel,
            "target_channel": self.target_channel,
            "country": "PH",
            "currency": self.checkout_currency or "PHP",
            "checkout_session_id": self.checkout_session_id,
            "processor_entity": self.processor_entity,
            "payment_method_type": self.payment_method_type,
            "checkout_amount": self.checkout_amount,
            "proxy_configured": self.proxy_configured,
            "evidence": self.evidence,
            "available_channels": self.available_channels,
            "gcash_available": self.qualified,
        }


def extract_access_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for part in reversed(text.split("----")):
        if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", part.strip()):
            return part.strip()
    match = re.search(r"ey[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text)
    return match.group(0) if match else text


def _proxy(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise GCashCheckerError("必须提供菲律宾出口代理")
    try:
        parts = shlex.split(value)
    except ValueError as exc:
        raise GCashCheckerError(f"代理命令格式无效：{exc}") from exc
    if parts and parts[0].lower() == "curl":
        proxy = next((parts[i + 1] for i, p in enumerate(parts[:-1]) if p in {"-x", "--proxy"}), "")
        auth = next((parts[i + 1] for i, p in enumerate(parts[:-1]) if p in {"-u", "-U", "--proxy-user"}), "")
        if not proxy:
            raise GCashCheckerError("curl 格式缺少 -x/--proxy")
        value = proxy
        if auth and "@" not in value:
            scheme = value.split("://", 1)[0] if "://" in value else "http"
            host = value.split("://", 1)[-1]
            value = f"{scheme}://{auth}@{host}"
    if "://" not in value:
        fields = value.split(":")
        if len(fields) == 4:
            host, port, user, password = fields
            value = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        else:
            value = "http://" + value
    if not re.match(r"^(https?|socks5h?)://[^\s]+$", value, re.I):
        raise GCashCheckerError("代理格式无效")
    return value


normalize_proxy = _proxy


def proxy_candidates(value: str) -> list[str]:
    normalized = _proxy(value)
    parsed = urlsplit(normalized)
    result = [normalized]
    if parsed.scheme.lower() == "http":
        result.append(urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)))
    return result


class _ProxySentinel(SentinelTokenProvider):
    def __init__(self, proxy: str, cookies: dict[str, str]):
        super().__init__(impersonate="firefox144", cookies=cookies)
        self.proxy = proxy

    async def _get_session(self):
        if not self._session:
            self._session = requests.AsyncSession(
                impersonate="firefox144", timeout=70,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
            )
        return self._session


async def _sentinel_headers(proxy: str, device_id: str, did: str) -> dict[str, str]:
    last = "empty token"
    for attempt in range(2):
        provider = _ProxySentinel(proxy, {"oai-did": did})
        try:
            token, so, diag = await provider.get_token_pair("chatgpt_checkout", device_id)
            if token and (not diag.get("turnstile_required") or diag.get("has_t")) and (not diag.get("so_required") or diag.get("has_so")):
                return {
                    "OpenAI-Sentinel-Token": json.dumps(token, separators=(",", ":")),
                    "OpenAI-Sentinel-SO-Token": json.dumps(so, separators=(",", ":")) if so else "",
                }
            last = str(diag.get("init_error") or "empty Sentinel response")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        finally:
            await provider.close()
        if attempt == 0:
            await asyncio.sleep(0.6)
    raise RuntimeError(f"Sentinel token generation failed after fresh-session retry: {last[:320]}")


def _headers(token: str, device_id: str, sentinel: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9", "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/", "User-Agent": CHROME_UA,
        "OAI-Language": "en-US", "OAI-Device-Id": device_id, **(sentinel or {}),
    }


def _session(proxy: str):
    session = requests.Session(impersonate="firefox144")
    session.trust_env = False
    session.proxies = {"http": proxy, "https": proxy}
    return session


def _create_checkout(token: str, proxy: str, device_id: str, did: str, preset: dict[str, str]) -> tuple[Any, dict[str, Any]]:
    http = _session(proxy)
    try:
        http.cookies.set("oai-did", did, domain="chatgpt.com")
    except Exception:
        pass
    try:
        http.get("https://chatgpt.com/api/auth/csrf", headers={"User-Agent": CHROME_UA}, timeout=20)
    except Exception:
        pass
    sentinel = asyncio.run(_sentinel_headers(proxy, device_id, did))
    country = str(preset.get("country") or "PH").upper()
    currency = str(preset.get("currency") or {"GB": "GBP", "NL": "EUR", "VN": "VND", "PH": "PHP"}.get(country, "USD")).upper()
    payload = {
        "entry_point": "all_plans_pricing_modal", "plan_name": str(preset.get("plan_name") or "chatgptplusplan"),
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/", "checkout_ui_mode": "custom", "check_card_proxy": True,
    }
    response = http.post(OPENAI_CHECKOUT_URL, json=payload, headers=_headers(token, device_id, sentinel), timeout=60)
    text = response.text or ""
    if response.status_code != 200:
        raise RuntimeError(f"OpenAI Checkout HTTP {response.status_code}: {text[:300]}")
    try:
        data = response.json() or {}
    except Exception as exc:
        raise RuntimeError(f"Checkout 返回非 JSON：{text[:200]}") from exc
    raw = " ".join(str(data.get(k) or "") for k in ("checkout_session_id", "url")) + " " + text
    match = re.search(r"oaics_[A-Za-z0-9]+", raw)
    if not match:
        raise GCashCheckerError("Checkout 未返回 OAICS 自定义会话")
    sid = match.group(0)
    processor = str(data.get("processor_entity") or "openai_ie")
    return http, {"checkout_session_id": sid, "processor_entity": processor}


def _fetch_state(http: Any, token: str, sid: str, processor: str, device_id: str) -> dict[str, Any]:
    response = http.get(f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{sid}", headers=_headers(token, device_id), timeout=45)
    if response.status_code != 200:
        raise RuntimeError(f"读取 Checkout 失败：HTTP {response.status_code} {(response.text or '')[:200]}")
    return response.json() or {}


def _amount(state: dict[str, Any]) -> Any:
    for obj in (state, state.get("checkout_session") if isinstance(state, dict) else {}):
        if not isinstance(obj, dict):
            continue
        for key in ("amount_due", "checkout_amount", "total", "due"):
            if obj.get(key) is not None:
                return obj[key]
    return None


def _currency(state: dict[str, Any]) -> str:
    for key in ("currency", "checkout_currency"):
        if state.get(key):
            return str(state[key]).upper()
    return "PHP"


def _channel_name(method: Any) -> str:
    if isinstance(method, str):
        return method.strip()
    if not isinstance(method, dict):
        return ""
    for key in ("type", "name", "display_name", "payment_method_type", "provider", "label", "id"):
        value = str(method.get(key) or "").strip()
        if value and not value.startswith("cpmt_"):
            return value
    return str(method.get("id") or "").strip()


def _available_channels(methods: Any) -> list[str]:
    if not isinstance(methods, list):
        return []
    channels = []
    for method in methods:
        name = _channel_name(method)
        if isinstance(method, dict) and str(method.get("id") or "") in KNOWN_GCASH_METHOD_IDS:
            name = "gcash"
        if name.lower() == "gcash" and not (
            isinstance(method, dict) and str(method.get("id") or "") in KNOWN_GCASH_METHOD_IDS
        ):
            name = str(method.get("id") or "unknown") if isinstance(method, dict) else name
        if name and name.lower() not in {item.lower() for item in channels}:
            channels.append(name)
    return channels


def _channel_available(methods: Any, target: str) -> bool:
    target = target.lower().strip()
    if target == "gcash":
        return bool(_gcash_method(methods))
    if target == "card":
        return any(
            isinstance(method, dict)
            and not str(method.get("id") or "").startswith("cpmt_")
            and "card" in json.dumps(method, ensure_ascii=False).lower()
            for method in (methods if isinstance(methods, list) else [])
        )
    return any(target in json.dumps(method, ensure_ascii=False).lower() for method in (methods if isinstance(methods, list) else []))


def _gcash_method(methods: Any) -> str:
    if not isinstance(methods, list):
        return ""
    for method in methods:
        if not isinstance(method, dict):
            continue
        method_id = str(method.get("id") or "")
        if not method_id.startswith("cpmt_"):
            continue
        if method_id in KNOWN_GCASH_METHOD_IDS:
            return method_id
        text = json.dumps(method, ensure_ascii=False).lower()
        if "gcash" in text and method_id in KNOWN_GCASH_METHOD_IDS:
            return method_id
    return ""


def check_gcash(access_token: str, proxy: str, *, plan: str = "plus", with_promo: bool = False, target_channel: str = "gcash", preset: dict[str, str] | None = None) -> QualificationResult:
    token = extract_access_token(access_token)
    if not token:
        raise GCashCheckerError("缺少 Access Token")
    preset = preset or {}
    target_channel = str(preset.get("channel") or target_channel or "gcash").lower().strip()
    if plan != str(preset.get("plan") or "plus").lower():
        raise GCashCheckerError("检测预设的计划参数不一致")
    normalized = _proxy(proxy)
    last_error: BaseException | None = None
    for candidate in proxy_candidates(normalized)[:MAX_PROXY_RETRIES]:
        try:
            device_id, did = str(uuid.uuid4()), str(uuid.uuid4())
            http, meta = _create_checkout(token, candidate, device_id, did, preset)
            state = _fetch_state(http, token, meta["checkout_session_id"], meta["processor_entity"], device_id)
            methods = state.get("custom_payment_methods")
            method_id = _gcash_method(methods) if target_channel == "gcash" else ""
            for _ in range(3):
                if method_id:
                    break
                await_seconds = 0.8
                import time
                time.sleep(await_seconds)
                state = _fetch_state(http, token, meta["checkout_session_id"], meta["processor_entity"], device_id)
                methods = state.get("custom_payment_methods")
                method_id = _gcash_method(methods) if target_channel == "gcash" else ""
            channels = _available_channels(state.get("custom_payment_methods"))
            available = bool(method_id) if target_channel == "gcash" else _channel_available(state.get("custom_payment_methods"), target_channel)
            country = str(preset.get("country") or "PH").upper()
            return QualificationResult(available, meta["checkout_session_id"], target_channel, target_channel if available else "", _amount(state), _currency(state), True, f"{target_channel} channel published" if available else f"{country} checkout 未发布 {target_channel}", channels)
        except Exception as exc:
            last_error = exc
            if "CONNECT tunnel failed" not in str(exc) and "curl: (7)" not in str(exc):
                break
    raise last_error if isinstance(last_error, Exception) else GCashCheckerError("检测失败")
