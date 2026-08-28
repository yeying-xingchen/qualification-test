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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
import base64

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

STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_VERSION_FULL = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
KNOWN_PUBLISHABLE_KEYS = {
    "1Pj377KslHRdbaPg": "pk_live_51Pj377KslHRdbaPgTJYjThzH3f5dt1N1vK7LUp0qh0yNSarhfZ6nfbG7FFlh8KLxVkvdMWN5o6Mc4Vda6NHaSnaV00C2Sbl8Zs",
    "1HOrSwC6h1nxGoI3": "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
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
    available_channels: list[str] = field(default_factory=list)
    channel_details: list[dict[str, Any]] = field(default_factory=list)
    channel_availability: dict[str, bool] = field(default_factory=dict)
    account_email: str = ""
    access_token: str = ""
    country: str = "PH"

    def as_dict(self) -> dict[str, Any]:
        return {
            "qualified": self.qualified,
            "channel": self.target_channel,
            "target_channel": self.target_channel,
            "country": self.country,
            "currency": self.checkout_currency or "PHP",
            "checkout_session_id": self.checkout_session_id,
            "processor_entity": self.processor_entity,
            "payment_method_type": self.payment_method_type,
            "checkout_amount": self.checkout_amount,
            "proxy_configured": self.proxy_configured,
            "evidence": self.evidence,
            "available_channels": self.available_channels,
            "channel_details": self.channel_details,
            "channel_availability": self.channel_availability,
            "gcash_available": self.qualified,
            "account_email": self.account_email,
            "access_token": self.access_token,
        }


def extract_account_email(value: str) -> str:
    text = str(value or "").strip()
    parts = text.split("----")
    if parts and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", parts[0].strip()):
        return parts[0].strip()
    token = extract_access_token(text)
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        profile = data.get("https://api.openai.com/profile") or {}
        return str(profile.get("email") or data.get("email") or "")
    except Exception:
        return ""


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
        raise GCashCheckerError("必须提供目标国家出口代理")
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
    currency = str(preset.get("currency") or {"GB": "GBP", "NL": "EUR", "VN": "VND", "PH": "PHP", "IN": "INR", "PL": "PLN", "BR": "BRL"}.get(country, "USD")).upper()
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
    custom_match = re.search(r"oaics_[A-Za-z0-9]+", raw)
    stripe_match = re.search(r"cs_(?:live|test)_[A-Za-z0-9]+", raw)
    processor = str(data.get("processor_entity") or "openai_ie")
    if custom_match:
        return http, {
            "checkout_session_id": custom_match.group(0),
            "processor_entity": processor,
            "checkout_provider": str(data.get("checkout_provider") or "open_ai"),
            "is_custom_checkout": True,
            "publishable_key": str(data.get("publishable_key") or ""),
        }
    if stripe_match:
        return http, {
            "checkout_session_id": stripe_match.group(0),
            "processor_entity": processor,
            "checkout_provider": str(data.get("checkout_provider") or "stripe"),
            "is_custom_checkout": False,
            "publishable_key": str(data.get("publishable_key") or ""),
        }
    raise GCashCheckerError("Checkout 未返回可识别会话（oaics_ 或 cs_*）")


def _fetch_state(http: Any, token: str, sid: str, processor: str, device_id: str) -> dict[str, Any]:
    response = http.get(f"https://chatgpt.com/backend-api/payments/checkout/{processor}/{sid}", headers=_headers(token, device_id), timeout=45)
    if response.status_code != 200:
        raise RuntimeError(f"读取 Checkout 失败：HTTP {response.status_code} {(response.text or '')[:200]}")
    return response.json() or {}


def _stripe_headers() -> dict[str, str]:
    return {"User-Agent": CHROME_UA, "Accept": "application/json", "Origin": "https://js.stripe.com", "Referer": "https://js.stripe.com/"}


def _verify_stripe_pk(http: Any, session_id: str, preferred: str = "") -> str:
    keys = [preferred] if preferred else []
    keys.extend(pk for pk in KNOWN_PUBLISHABLE_KEYS.values() if pk and pk not in keys)
    last = ""
    for pk in keys:
        try:
            response = http.post(
                f"{STRIPE_API}/v1/payment_pages/{session_id}/init",
                data={"key": pk, "_stripe_version": STRIPE_VERSION_BASE, "browser_locale": "en-US"},
                headers=_stripe_headers(), timeout=20,
            )
            if response.status_code == 200:
                return pk
            last = f"{response.status_code}: {(response.text or '')[:160]}"
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"无法确认 Stripe publishable_key：{last}")


def _stripe_payment_method_types(init_data: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").lower().strip()
        if text and text not in found:
            found.append(text)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key in ("type", "name", "display_name", "payment_method_type", "provider", "id"):
                add(value.get(key))
            for nested_key in ("payment_method_types", "payment_method_specs", "ordered_payment_method_types", "external_payment_method_specs"):
                nested = value.get(nested_key)
                if nested is not value:
                    walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            add(value)

    for section_name in ("payment_method_types", "payment_method_types_preference", "payment_method_specs", "ordered_payment_method_types", "external_payment_method_specs"):
        walk(init_data.get(section_name))
    return found


def _stripe_channel_available(methods: list[str], target: str) -> bool:
    target = target.lower().strip()
    # Stripe publishes BLIK as the exact `blik` payment method type. Accept
    # the occasional `blik_bank` alias, but avoid matching incidental labels.
    if target == "blik":
        return any(str(method).lower().strip() in {"blik", "blik_bank"} for method in methods)
    aliases = {
        "card": ("card", "link"),
        "paypal": ("paypal",),
        "ideal": ("ideal", "ideal_bank"),
        "momo": ("momo",),
        "gopay": ("gopay", "go_pay"),
        "gcash": ("gcash",),
        "twint": ("twint",),
        "pix": ("pix",),
        "upi": ("upi",),
        "blik": ("blik",),
        "kakao": ("kakao", "kakaopay", "kakao_pay"),
    }.get(target, (target,))
    return any(any(alias in method for alias in aliases) for method in methods)


def _fetch_stripe_checkout_state(http: Any, session_id: str, publishable_key: str, country: str) -> dict[str, Any]:
    pk = _verify_stripe_pk(http, session_id, publishable_key)
    country = country.upper()
    profile_locale = {"GB": "en-GB", "NL": "nl-NL", "DE": "de-DE", "FR": "fr-FR", "US": "en-US", "PH": "en-PH", "VN": "vi-VN", "ID": "id-ID", "IN": "en-IN", "PL": "pl-PL", "BR": "pt-BR"}.get(country, "en-US")
    timezone = {"GB": "Europe/London", "NL": "Europe/Amsterdam", "DE": "Europe/Berlin", "FR": "Europe/Paris", "PH": "Asia/Manila", "VN": "Asia/Ho_Chi_Minh", "ID": "Asia/Jakarta", "IN": "Asia/Kolkata", "PL": "Europe/Warsaw", "BR": "America/Sao_Paulo"}.get(country, "America/New_York")
    last_error = ""
    for version in (STRIPE_VERSION_BASE, STRIPE_VERSION_FULL):
        data = {
            "browser_locale": profile_locale,
            "browser_timezone": timezone,
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": profile_locale,
            "elements_session_client[is_aggregation_expected]": "false",
            "key": pk,
            "_stripe_version": version,
        }
        if version == STRIPE_VERSION_FULL:
            data["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
            data["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"
        response = http.post(f"{STRIPE_API}/v1/payment_pages/{session_id}/init", data=data, headers=_stripe_headers(), timeout=30)
        if response.status_code == 200:
            payload = response.json() or {}
            total = payload.get("total_summary") or {}
            return {
                "checkout_session_id": session_id,
                "currency": str(payload.get("currency") or "").upper(),
                "checkout_amount": total.get("due") if total.get("due") is not None else (payload.get("invoice") or {}).get("amount_due"),
                "payment_method_types": _stripe_payment_method_types(payload),
                "stripe_init": payload,
            }
        last_error = f"{response.status_code}: {(response.text or '')[:180]}"
        if response.status_code == 400 and "beta" in (response.text or "").lower():
            continue
        break
    raise RuntimeError(f"读取 Stripe Checkout 失败：{last_error}")


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
    init_currency = ((state.get("stripe_init") or {}) if isinstance(state.get("stripe_init"), dict) else {}).get("currency")
    if init_currency:
        return str(init_currency).upper()
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


def _channel_details(methods: Any) -> list[dict[str, Any]]:
    if not isinstance(methods, list):
        return []
    details = []
    for method in methods:
        if isinstance(method, dict):
            method_id = str(method.get("id") or "")
            name = "gcash" if method_id in KNOWN_GCASH_METHOD_IDS else _channel_name(method)
            if name:
                details.append({"name": name, "id": method_id, "raw_type": method.get("type") or ""})
        elif isinstance(method, str) and method.strip():
            details.append({"name": method.strip(), "id": method.strip(), "raw_type": ""})
    return details


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


def _blik_method(methods: Any) -> str:
    """Return a textual BLIK method identifier without guessing cpmt IDs."""
    if not isinstance(methods, list):
        return ""
    blik_values = {"blik", "blik_bank"}
    fields = ("type", "payment_method_type", "provider", "name", "display_name", "label")
    for method in methods:
        if isinstance(method, str) and method.lower().strip() in blik_values:
            return method.strip()
        if not isinstance(method, dict):
            continue
        for field_name in fields:
            value = str(method.get(field_name) or "").lower().strip()
            if value in blik_values:
                return str(method.get("id") or method.get(field_name) or "blik")
    return ""


def _channel_available(methods: Any, target: str) -> bool:
    target = target.lower().strip()
    if target == "gcash":
        return bool(_gcash_method(methods))
    if target == "blik":
        return bool(_blik_method(methods))
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


def check_gcash(access_token: str, proxy: str, *, plan: str = "plus", with_promo: bool = False, target_channel: str = "gcash", preset: dict[str, str] | None = None, target_channels: list[str] | None = None) -> QualificationResult:
    raw_account = str(access_token or "").strip()
    token = extract_access_token(raw_account)
    account_email = extract_account_email(raw_account)
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
            country = str(preset.get("country") or "PH").upper()
            selected = [str(channel).lower() for channel in (target_channels or [target_channel]) if str(channel).strip()]
            if str(meta.get("checkout_session_id") or "").startswith("cs_"):
                state = _fetch_stripe_checkout_state(
                    http, meta["checkout_session_id"], str(meta.get("publishable_key") or ""), country
                )
                channels = [str(method).lower() for method in (state.get("payment_method_types") or []) if str(method).strip()]
                details = [{"name": method, "id": method, "raw_type": "stripe"} for method in channels]
                availability = {channel: _stripe_channel_available(channels, channel) for channel in selected}
                available = availability.get(target_channel, False)
                details.append({"selected": availability, "checkout_provider": "stripe"})
                return QualificationResult(
                    available, meta["checkout_session_id"], target_channel,
                    meta["processor_entity"] if available else "",
                    target_channel if available else "", _amount(state), _currency(state), True,
                    f"{target_channel} channel published (Stripe Checkout)" if available else f"{country} Stripe checkout 未发布 {target_channel}",
                    channels, details, availability, account_email, token, country,
                )

            state = _fetch_state(http, token, meta["checkout_session_id"], meta["processor_entity"], device_id)
            methods = state.get("custom_payment_methods")
            method_id = _gcash_method(methods) if target_channel == "gcash" else ""
            for _ in range(3):
                if method_id or (target_channel != "gcash" and _channel_available(methods, target_channel)):
                    break
                await_seconds = 0.8
                import time
                time.sleep(await_seconds)
                state = _fetch_state(http, token, meta["checkout_session_id"], meta["processor_entity"], device_id)
                methods = state.get("custom_payment_methods")
                method_id = _gcash_method(methods) if target_channel == "gcash" else ""
            channels = _available_channels(state.get("custom_payment_methods"))
            details = _channel_details(state.get("custom_payment_methods"))
            availability = {channel: (_channel_available(state.get("custom_payment_methods"), channel) if channel != "gcash" else bool(_gcash_method(state.get("custom_payment_methods")))) for channel in selected}
            available = availability.get(target_channel, False)
            details.append({"selected": availability, "checkout_provider": "open_ai"})
            return QualificationResult(available, meta["checkout_session_id"], target_channel, target_channel if available else "", _amount(state), _currency(state), True, f"{target_channel} channel published" if available else f"{country} checkout 未发布 {target_channel}", channels, details, availability, account_email, token, country)
        except Exception as exc:
            last_error = exc
            if "CONNECT tunnel failed" not in str(exc) and "curl: (7)" not in str(exc):
                break
    raise last_error if isinstance(last_error, Exception) else GCashCheckerError("检测失败")
