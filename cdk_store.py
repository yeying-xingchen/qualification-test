from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("GCASH_CDK_DB", "data/cdks.sqlite3"))
DB_LOCK = threading.RLock()


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS cdks (
        code_hash TEXT PRIMARY KEY, quota INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
    )""")
    conn.commit()
    return conn


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def create_cdk(quota: int = 100, days: int = 30) -> str:
    quota = max(1, int(quota)); days = max(1, int(days))
    code = "GC-" + "-".join(secrets.token_hex(3).upper() for _ in range(4))
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with DB_LOCK:
        conn = _db()
        conn.execute("INSERT INTO cdks VALUES (?,?,?,?,?,?)", (_hash(code), quota, 0, expires, 1, datetime.now(timezone.utc).isoformat()))
        conn.commit(); conn.close()
    return code


def redeem_cdk(code: str) -> dict:
    code = str(code or "").strip().upper()
    with DB_LOCK:
        conn = _db(); row = conn.execute("SELECT * FROM cdks WHERE code_hash=?", (_hash(code),)).fetchone(); conn.close()
    if not row or not row["enabled"]:
        raise ValueError("CDK 无效或已禁用")
    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise ValueError("CDK 已过期")
    remaining = max(0, row["quota"] - row["used"])
    if not remaining:
        raise ValueError("CDK 额度已用尽")
    return {"code": code, "quota": row["quota"], "used": row["used"], "remaining": remaining, "expires_at": row["expires_at"]}


def consume(code: str, amount: int) -> dict:
    amount = max(1, int(amount)); code = str(code or "").strip().upper()
    with DB_LOCK:
        conn = _db(); row = conn.execute("SELECT * FROM cdks WHERE code_hash=?", (_hash(code),)).fetchone()
        if not row or not row["enabled"]: conn.close(); raise ValueError("CDK 无效或已禁用")
        if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc): conn.close(); raise ValueError("CDK 已过期")
        if row["used"] + amount > row["quota"]: conn.close(); raise ValueError("CDK 剩余额度不足")
        conn.execute("UPDATE cdks SET used=used+? WHERE code_hash=?", (amount, _hash(code))); conn.commit()
        result = {"quota": row["quota"], "used": row["used"] + amount, "remaining": row["quota"] - row["used"] - amount, "expires_at": row["expires_at"]}; conn.close(); return result
