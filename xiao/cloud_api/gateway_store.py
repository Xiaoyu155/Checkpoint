from __future__ import annotations

import json
import hashlib
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from .auth import hash_api_key


MICRO_USD = 1_000_000
_KEY_PATTERN = re.compile(r"^pacer_sk_([a-f0-9]{16})_[A-Za-z0-9_-]{20,}$")
_ENV_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class GatewayStoreError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True)
class GatewayPrincipal:
    key_id: str
    tenant_id: str
    tenant_name: str
    balance_microusd: int
    rpm: int
    concurrency: int
    tenant_rpm: int
    tenant_concurrency: int
    allowed_models: tuple[str, ...]


def calculate_token_charge(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    input_price_microusd_per_million: int,
    cached_input_price_microusd_per_million: int,
    output_price_microusd_per_million: int,
) -> int:
    cached = min(max(0, int(cached_input_tokens)), max(0, int(input_tokens)))
    regular = max(0, int(input_tokens)) - cached
    numerator = (
        regular * max(0, int(input_price_microusd_per_million))
        + cached * max(0, int(cached_input_price_microusd_per_million))
        + max(0, int(output_tokens)) * max(0, int(output_price_microusd_per_million))
    )
    return int(math.ceil(numerator / 1_000_000)) if numerator else 0


def validate_upstream_base_url(value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise GatewayStoreError(
            "invalid_upstream_url",
            "Upstream URL must be an HTTP(S) origin without credentials.",
        )
    if parsed.query or parsed.fragment:
        raise GatewayStoreError(
            "invalid_upstream_url", "Upstream URL cannot contain a query or fragment."
        )
    if parsed.scheme == "http" and parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise GatewayStoreError(
            "insecure_upstream_url", "Non-loopback upstreams must use HTTPS."
        )
    return candidate


class GatewayStore:
    """Persistent commercial gateway state with short SQLite transactions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS gateway_plans (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        monthly_fee_microusd INTEGER NOT NULL DEFAULT 0,
                        included_credit_microusd INTEGER NOT NULL DEFAULT 0,
                        rpm INTEGER NOT NULL DEFAULT 60,
                        concurrency INTEGER NOT NULL DEFAULT 2,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_tenants (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        plan_id TEXT REFERENCES gateway_plans(id) ON DELETE SET NULL,
                        balance_microusd INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_api_keys (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL REFERENCES gateway_tenants(id) ON DELETE CASCADE,
                        name TEXT NOT NULL,
                        key_prefix TEXT NOT NULL UNIQUE,
                        key_salt TEXT NOT NULL,
                        key_sha256 TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        rpm_override INTEGER NOT NULL DEFAULT 0,
                        concurrency_override INTEGER NOT NULL DEFAULT 0,
                        allowed_models_json TEXT NOT NULL DEFAULT '[]',
                        expires_at REAL,
                        last_used_at REAL,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_upstreams (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL UNIQUE,
                        provider TEXT NOT NULL DEFAULT 'openai-compatible',
                        routing_contract TEXT NOT NULL DEFAULT '',
                        base_url TEXT NOT NULL,
                        secret_env TEXT NOT NULL,
                        models_json TEXT NOT NULL DEFAULT '[]',
                        priority INTEGER NOT NULL DEFAULT 100,
                        weight INTEGER NOT NULL DEFAULT 1,
                        max_concurrency INTEGER NOT NULL DEFAULT 20,
                        timeout_seconds REAL NOT NULL DEFAULT 120,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        circuit_open_until REAL NOT NULL DEFAULT 0,
                        last_error_code TEXT NOT NULL DEFAULT '',
                        last_latency_ms REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_prices (
                        model TEXT PRIMARY KEY,
                        upstream_model TEXT NOT NULL,
                        input_price_microusd_per_million INTEGER NOT NULL,
                        cached_input_price_microusd_per_million INTEGER NOT NULL DEFAULT 0,
                        output_price_microusd_per_million INTEGER NOT NULL,
                        upstream_input_cost_microusd_per_million INTEGER NOT NULL DEFAULT 0,
                        upstream_output_cost_microusd_per_million INTEGER NOT NULL DEFAULT 0,
                        max_output_tokens INTEGER NOT NULL DEFAULT 4096,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        version INTEGER NOT NULL DEFAULT 1,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_requests (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL REFERENCES gateway_tenants(id),
                        key_id TEXT NOT NULL REFERENCES gateway_api_keys(id),
                        upstream_id TEXT NOT NULL REFERENCES gateway_upstreams(id),
                        model TEXT NOT NULL,
                        upstream_model TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        http_status INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT NOT NULL DEFAULT '',
                        streaming INTEGER NOT NULL DEFAULT 0,
                        reserved_microusd INTEGER NOT NULL DEFAULT 0,
                        actual_microusd INTEGER NOT NULL DEFAULT 0,
                        upstream_cost_microusd INTEGER NOT NULL DEFAULT 0,
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        usage_source TEXT NOT NULL DEFAULT '',
                        price_snapshot_json TEXT NOT NULL,
                        latency_ms REAL NOT NULL DEFAULT 0,
                        attempt_count INTEGER NOT NULL DEFAULT 1,
                        provider_started_at REAL,
                        heartbeat_at REAL,
                        created_at REAL NOT NULL,
                        settled_at REAL,
                        UNIQUE(key_id, idempotency_key)
                    );
                    CREATE TABLE IF NOT EXISTS gateway_leases (
                        request_id TEXT PRIMARY KEY REFERENCES gateway_requests(id) ON DELETE CASCADE,
                        key_id TEXT NOT NULL,
                        upstream_id TEXT NOT NULL,
                        expires_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_rate_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key_id TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_ledger (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL REFERENCES gateway_tenants(id),
                        request_id TEXT,
                        kind TEXT NOT NULL,
                        amount_microusd INTEGER NOT NULL,
                        balance_after_microusd INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        note TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        UNIQUE(tenant_id, idempotency_key)
                    );
                    CREATE TABLE IF NOT EXISTS gateway_subscription_events (
                        id TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL REFERENCES gateway_tenants(id),
                        plan_id TEXT NOT NULL REFERENCES gateway_plans(id),
                        amount_paid_microusd INTEGER NOT NULL,
                        credit_granted_microusd INTEGER NOT NULL,
                        period_start REAL NOT NULL,
                        period_end REAL NOT NULL,
                        external_reference TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        UNIQUE(tenant_id, external_reference)
                    );
                    CREATE TABLE IF NOT EXISTS gateway_payment_references (
                        external_reference TEXT PRIMARY KEY,
                        tenant_id TEXT NOT NULL REFERENCES gateway_tenants(id),
                        operation TEXT NOT NULL,
                        amount_microusd INTEGER NOT NULL,
                        result_id TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS gateway_wechat_orders (
                        id TEXT PRIMARY KEY,
                        out_trade_no TEXT NOT NULL UNIQUE,
                        tenant_id TEXT NOT NULL REFERENCES gateway_tenants(id),
                        package_id TEXT NOT NULL,
                        package_name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        amount_fen INTEGER NOT NULL,
                        credit_microusd INTEGER NOT NULL,
                        currency TEXT NOT NULL DEFAULT 'CNY',
                        status TEXT NOT NULL,
                        code_url TEXT NOT NULL DEFAULT '',
                        transaction_id TEXT UNIQUE,
                        provider_payload_json TEXT NOT NULL DEFAULT '{}',
                        error_code TEXT NOT NULL DEFAULT '',
                        expires_at REAL NOT NULL,
                        last_provider_check_at REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        paid_at REAL,
                        closed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS pacer_accounts (
                        id TEXT PRIMARY KEY,
                        email TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL DEFAULT '',
                        password_salt TEXT NOT NULL,
                        password_hash TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        email_verified_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS pacer_email_verification_codes (
                        id TEXT PRIMARY KEY,
                        email TEXT NOT NULL,
                        purpose TEXT NOT NULL,
                        code_hash TEXT NOT NULL,
                        expires_at REAL NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        consumed_at REAL,
                        created_at REAL NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_pacer_email_codes_lookup
                        ON pacer_email_verification_codes(email, purpose, created_at DESC);
                    CREATE TABLE IF NOT EXISTS pacer_login_sessions (
                        id TEXT PRIMARY KEY,
                        account_id TEXT NOT NULL REFERENCES pacer_accounts(id) ON DELETE CASCADE,
                        token_hash TEXT NOT NULL UNIQUE,
                        expires_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        revoked_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_pacer_sessions_token
                        ON pacer_login_sessions(token_hash, expires_at);
                    CREATE TABLE IF NOT EXISTS pacer_account_tenants (
                        account_id TEXT NOT NULL REFERENCES pacer_accounts(id) ON DELETE CASCADE,
                        tenant_id TEXT NOT NULL REFERENCES gateway_tenants(id) ON DELETE CASCADE,
                        created_at REAL NOT NULL,
                        PRIMARY KEY(account_id, tenant_id)
                    );
                    CREATE TABLE IF NOT EXISTS gateway_attempts (
                        id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL REFERENCES gateway_requests(id) ON DELETE CASCADE,
                        upstream_id TEXT NOT NULL REFERENCES gateway_upstreams(id),
                        attempt_no INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        http_status INTEGER NOT NULL DEFAULT 0,
                        error_code TEXT NOT NULL DEFAULT '',
                        response_started INTEGER NOT NULL DEFAULT 0,
                        upstream_request_id TEXT NOT NULL DEFAULT '',
                        input_tokens INTEGER NOT NULL DEFAULT 0,
                        output_tokens INTEGER NOT NULL DEFAULT 0,
                        upstream_cost_microusd INTEGER NOT NULL DEFAULT 0,
                        latency_ms REAL NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        finished_at REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_gateway_requests_tenant_created
                        ON gateway_requests(tenant_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_gateway_requests_upstream_status
                        ON gateway_requests(upstream_id, status);
                    CREATE INDEX IF NOT EXISTS idx_gateway_rate_key_created
                        ON gateway_rate_events(key_id, created_at);
                    CREATE INDEX IF NOT EXISTS idx_gateway_ledger_tenant_created
                        ON gateway_ledger(tenant_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_gateway_subscription_tenant_period
                        ON gateway_subscription_events(tenant_id, period_end DESC);
                    CREATE INDEX IF NOT EXISTS idx_gateway_wechat_orders_tenant_created
                        ON gateway_wechat_orders(tenant_id, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_gateway_wechat_orders_status_expiry
                        ON gateway_wechat_orders(status, expires_at);
                    CREATE INDEX IF NOT EXISTS idx_gateway_attempts_request
                        ON gateway_attempts(request_id, attempt_no);
                    """
                )
                self._ensure_column(
                    conn,
                    "gateway_requests",
                    "request_fingerprint",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    conn, "gateway_requests", "provider_started_at", "REAL"
                )
                self._ensure_column(conn, "gateway_requests", "heartbeat_at", "REAL")
                self._ensure_column(
                    conn,
                    "gateway_attempts",
                    "upstream_request_id",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    conn,
                    "gateway_attempts",
                    "input_tokens",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                self._ensure_column(
                    conn,
                    "gateway_upstreams",
                    "routing_contract",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    conn,
                    "gateway_attempts",
                    "output_tokens",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                self._ensure_column(
                    conn,
                    "gateway_attempts",
                    "upstream_cost_microusd",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO gateway_payment_references
                    (external_reference, tenant_id, operation, amount_microusd, result_id, created_at)
                    SELECT external_reference, tenant_id, 'subscription', amount_paid_microusd, id, created_at
                    FROM gateway_subscription_events
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO gateway_payment_references
                    (external_reference, tenant_id, operation, amount_microusd, result_id, created_at)
                    SELECT substr(idempotency_key, 7), tenant_id, 'balance', amount_microusd, id, created_at
                    FROM gateway_ledger
                    WHERE idempotency_key LIKE 'admin:%'
                    """
                )
                now = time.time()
                conn.execute(
                    """
                    INSERT OR IGNORE INTO gateway_plans
                    (id, name, monthly_fee_microusd, included_credit_microusd, rpm, concurrency, enabled, created_at, updated_at)
                    VALUES ('plan_starter', 'Starter', 0, 0, 60, 2, 1, ?, ?)
                    """,
                    (now, now),
                )
            self._schema_ready = True

    # Account credentials are intentionally separate from gateway API keys.
    @staticmethod
    def normalize_account_email(email: str) -> str:
        value = str(email or "").strip().lower()
        if len(value) > 254 or not _EMAIL_PATTERN.fullmatch(value):
            raise GatewayStoreError("invalid_email", "A valid email address is required.")
        return value

    @staticmethod
    def hash_password(password: str, salt: str) -> str:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), 240_000
        ).hex()

    @staticmethod
    def hash_session_token(token: str) -> str:
        return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

    def create_email_code(self, *, email: str, purpose: str, code: str, ttl_seconds: int = 600) -> dict[str, Any]:
        normalized = self.normalize_account_email(email)
        clean_purpose = str(purpose or "register").strip().lower()
        if clean_purpose not in {"register", "password_reset"} or not re.fullmatch(r"\d{6}", str(code or "")):
            raise GatewayStoreError("invalid_verification_code", "Verification code is invalid.")
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE pacer_email_verification_codes SET consumed_at = ? WHERE email = ? AND purpose = ? AND consumed_at IS NULL",
                (now, normalized, clean_purpose),
            )
            row = {
                "id": f"email_code_{uuid.uuid4().hex}",
                "email": normalized,
                "purpose": clean_purpose,
                "code_hash": self.hash_session_token(str(code)),
                "expires_at": now + max(60, min(int(ttl_seconds), 1800)),
                "created_at": now,
            }
            conn.execute(
                """INSERT INTO pacer_email_verification_codes
                (id, email, purpose, code_hash, expires_at, created_at)
                VALUES (:id, :email, :purpose, :code_hash, :expires_at, :created_at)""",
                row,
            )
        return {"id": row["id"], "email": normalized, "purpose": clean_purpose, "expires_at": row["expires_at"]}

    def consume_email_code(self, *, email: str, purpose: str, code: str) -> bool:
        normalized = self.normalize_account_email(email)
        clean_purpose = str(purpose or "register").strip().lower()
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT * FROM pacer_email_verification_codes
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1""",
                (normalized, clean_purpose),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now or int(row["attempts"]) >= 5:
                return False
            expected = str(row["code_hash"])
            actual = self.hash_session_token(str(code or ""))
            if not secrets.compare_digest(expected, actual):
                conn.execute("UPDATE pacer_email_verification_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
                return False
            conn.execute("UPDATE pacer_email_verification_codes SET consumed_at = ? WHERE id = ?", (now, row["id"]))
            return True

    def register_account(
        self, *, email: str, password: str, display_name: str = ""
    ) -> dict[str, Any]:
        normalized = self.normalize_account_email(email)
        if len(str(password or "")) < 8 or len(str(password or "")) > 256:
            raise GatewayStoreError("invalid_password", "Password must be 8 to 256 characters.")
        self.ensure_schema()
        now = time.time()
        account_id = f"acct_{uuid.uuid4().hex[:20]}"
        salt = secrets.token_hex(16)
        name = str(display_name or "").strip()[:100] or normalized.split("@", 1)[0]
        tenant_name = f"Pacer - {name}"[:120]
        try:
            with self._transaction() as conn:
                existing = conn.execute("SELECT id FROM pacer_accounts WHERE email = ?", (normalized,)).fetchone()
                if existing is not None:
                    raise GatewayStoreError("account_exists", "An account with this email already exists.", status_code=409)
                plan = conn.execute("SELECT id FROM gateway_plans WHERE id = 'plan_starter' AND enabled = 1").fetchone()
                if plan is None:
                    raise GatewayStoreError("plan_unavailable", "Starter plan is not available.", status_code=503)
                tenant_id = f"tenant_{uuid.uuid4().hex[:20]}"
                conn.execute(
                    """INSERT INTO pacer_accounts
                    (id, email, display_name, password_salt, password_hash, email_verified_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (account_id, normalized, name, salt, self.hash_password(password, salt), now, now, now),
                )
                conn.execute(
                    """INSERT INTO gateway_tenants
                    (id, name, status, plan_id, balance_microusd, created_at, updated_at)
                    VALUES (?, ?, 'active', 'plan_starter', 0, ?, ?)""",
                    (tenant_id, tenant_name, now, now),
                )
                conn.execute(
                    "INSERT INTO pacer_account_tenants (account_id, tenant_id, created_at) VALUES (?, ?, ?)",
                    (account_id, tenant_id, now),
                )
                key_id = f"key_{uuid.uuid4().hex[:16]}"
                key_token = f"pacer_sk_{uuid.uuid4().hex[:16]}_{secrets.token_urlsafe(24)}"
                key_salt = secrets.token_hex(16)
                conn.execute(
                    """INSERT INTO gateway_api_keys
                    (id, tenant_id, name, key_prefix, key_salt, key_sha256, status,
                     rpm_override, concurrency_override, allowed_models_json, created_at)
                    VALUES (?, ?, '默认 API Key', ?, ?, ?, 'active', 0, 0, '[]', ?)""",
                    (key_id, tenant_id, key_token[:24], key_salt, hash_api_key(key_token, key_salt), now),
                )
        except sqlite3.IntegrityError as exc:
            raise GatewayStoreError("account_exists", "An account with this email already exists.", status_code=409) from exc
        return {"id": account_id, "email": normalized, "display_name": name, "tenant_id": tenant_id, "api_key": key_token}

    def authenticate_account(self, *, email: str, password: str) -> dict[str, Any]:
        normalized = self.normalize_account_email(email)
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM pacer_accounts WHERE email = ?", (normalized,)).fetchone()
            if row is None or row["status"] != "active" or not secrets.compare_digest(
                self.hash_password(str(password or ""), str(row["password_salt"])), str(row["password_hash"])
            ):
                raise GatewayStoreError("invalid_credentials", "Email or password is incorrect.", status_code=401)
            tenant = conn.execute(
                "SELECT tenant_id FROM pacer_account_tenants WHERE account_id = ? ORDER BY created_at LIMIT 1",
                (row["id"],),
            ).fetchone()
        if tenant is None:
            raise GatewayStoreError("tenant_not_found", "Account tenant is not configured.", status_code=503)
        return {"id": str(row["id"]), "email": str(row["email"]), "display_name": str(row["display_name"]), "tenant_id": str(tenant["tenant_id"])}

    def reset_account_password(self, *, email: str, password: str) -> None:
        normalized = self.normalize_account_email(email)
        if len(str(password or "")) < 8 or len(str(password or "")) > 256:
            raise GatewayStoreError("invalid_password", "Password must be 8 to 256 characters.")
        salt = secrets.token_hex(16)
        now = time.time()
        self.ensure_schema()
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE pacer_accounts SET password_salt = ?, password_hash = ?, updated_at = ? WHERE email = ? AND status = 'active'",
                (salt, self.hash_password(password, salt), now, normalized),
            )
            if cursor.rowcount != 1:
                raise GatewayStoreError("account_not_found", "Account was not found.", status_code=404)
            conn.execute(
                "UPDATE pacer_login_sessions SET revoked_at = ? WHERE account_id = (SELECT id FROM pacer_accounts WHERE email = ?)",
                (now, normalized),
            )

    def create_login_session(self, account_id: str, *, ttl_seconds: int = 2_592_000) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO pacer_login_sessions
                (id, account_id, token_hash, expires_at, last_seen_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (f"sess_{uuid.uuid4().hex}", account_id, self.hash_session_token(token), now + max(300, min(int(ttl_seconds), 31_536_000)), now, now),
            )
        return token

    def account_from_session(self, token: str) -> dict[str, Any]:
        token_hash = self.hash_session_token(token)
        now = time.time()
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT a.id, a.email, a.display_name, a.status, l.expires_at,
                at.tenant_id FROM pacer_login_sessions l
                JOIN pacer_accounts a ON a.id = l.account_id
                JOIN pacer_account_tenants at ON at.account_id = a.id
                WHERE l.token_hash = ? AND l.revoked_at IS NULL AND l.expires_at > ?
                ORDER BY at.created_at LIMIT 1""",
                (token_hash, now),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise GatewayStoreError("invalid_session", "Login session is invalid or expired.", status_code=401)
            conn.execute("UPDATE pacer_login_sessions SET last_seen_at = ? WHERE token_hash = ?", (now, token_hash))
        return {"id": str(row["id"]), "email": str(row["email"]), "display_name": str(row["display_name"]), "tenant_id": str(row["tenant_id"]), "expires_at": float(row["expires_at"])}

    def revoke_login_session(self, token: str) -> None:
        self.ensure_schema()
        with self._connect() as conn:
            conn.execute("UPDATE pacer_login_sessions SET revoked_at = ? WHERE token_hash = ?", (time.time(), self.hash_session_token(token)))

    def account_principal(self, token: str) -> GatewayPrincipal:
        account = self.account_from_session(token)
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT k.*, t.name AS tenant_name, t.balance_microusd,
                p.rpm AS plan_rpm, p.concurrency AS plan_concurrency
                FROM gateway_api_keys k JOIN gateway_tenants t ON t.id = k.tenant_id
                LEFT JOIN gateway_plans p ON p.id = t.plan_id
                WHERE k.tenant_id = ? AND k.status = 'active' ORDER BY k.created_at LIMIT 1""",
                (account["tenant_id"],),
            ).fetchone()
        if row is None:
            raise GatewayStoreError("api_key_not_configured", "Account API key is not configured.", status_code=503)
        return GatewayPrincipal(
            key_id=str(row["id"]), tenant_id=str(row["tenant_id"]), tenant_name=str(row["tenant_name"]),
            balance_microusd=int(row["balance_microusd"]),
            rpm=min(int(row["rpm_override"] or row["plan_rpm"] or 60), int(row["plan_rpm"] or 60)),
            concurrency=min(int(row["concurrency_override"] or row["plan_concurrency"] or 2), int(row["plan_concurrency"] or 2)),
            tenant_rpm=int(row["plan_rpm"] or 60), tenant_concurrency=int(row["plan_concurrency"] or 2),
            allowed_models=tuple(_json_list(row["allowed_models_json"])),
        )

    def create_plan(
        self,
        *,
        name: str,
        monthly_fee_microusd: int = 0,
        included_credit_microusd: int = 0,
        rpm: int = 60,
        concurrency: int = 2,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name:
            raise GatewayStoreError("invalid_plan", "Plan name is required.")
        if (
            min(monthly_fee_microusd, included_credit_microusd) < 0
            or rpm < 1
            or concurrency < 1
        ):
            raise GatewayStoreError(
                "invalid_plan",
                "Plan prices must be non-negative and limits must be positive.",
            )
        plan_id = f"plan_{uuid.uuid4().hex[:16]}"
        now = time.time()
        self.ensure_schema()
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO gateway_plans
                    (id, name, monthly_fee_microusd, included_credit_microusd, rpm, concurrency, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        plan_id,
                        clean_name,
                        monthly_fee_microusd,
                        included_credit_microusd,
                        rpm,
                        concurrency,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GatewayStoreError(
                "plan_exists", "A plan with that name already exists.", status_code=409
            ) from exc
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            raise GatewayStoreError(
                "plan_not_found", "Plan was not found.", status_code=404
            )
        return dict(row)

    def list_plans(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM gateway_plans ORDER BY created_at, id"
                )
            ]

    def create_tenant(
        self,
        *,
        name: str,
        plan_id: str = "plan_starter",
        initial_credit_microusd: int = 0,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        if not clean_name or initial_credit_microusd < 0:
            raise GatewayStoreError(
                "invalid_tenant",
                "Tenant name is required and initial credit cannot be negative.",
            )
        tenant_id = f"tenant_{uuid.uuid4().hex[:16]}"
        now = time.time()
        self.ensure_schema()
        with self._transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM gateway_plans WHERE id = ? AND enabled = 1",
                    (plan_id,),
                ).fetchone()
                is None
            ):
                raise GatewayStoreError(
                    "plan_not_found", "Enabled plan was not found.", status_code=404
                )
            conn.execute(
                """INSERT INTO gateway_tenants
                (id, name, status, plan_id, balance_microusd, created_at, updated_at)
                VALUES (?, ?, 'active', ?, ?, ?, ?)""",
                (tenant_id, clean_name, plan_id, initial_credit_microusd, now, now),
            )
            if initial_credit_microusd:
                self._insert_ledger(
                    conn,
                    tenant_id=tenant_id,
                    request_id="",
                    kind="credit",
                    amount=initial_credit_microusd,
                    balance_after=initial_credit_microusd,
                    idempotency_key=f"tenant-create:{tenant_id}",
                    note="Initial credit",
                    now=now,
                )
        return self.get_tenant(tenant_id)

    def get_tenant(self, tenant_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT t.*, p.name AS plan_name, p.rpm AS plan_rpm, p.concurrency AS plan_concurrency,
                p.monthly_fee_microusd AS plan_monthly_fee_microusd,
                p.included_credit_microusd AS plan_included_credit_microusd,
                p.enabled AS plan_enabled
                , (SELECT MAX(s.period_end) FROM gateway_subscription_events s WHERE s.tenant_id = t.id)
                    AS subscription_expires_at
                FROM gateway_tenants t LEFT JOIN gateway_plans p ON p.id = t.plan_id WHERE t.id = ?""",
                (tenant_id,),
            ).fetchone()
        if row is None:
            raise GatewayStoreError(
                "tenant_not_found", "Tenant was not found.", status_code=404
            )
        return dict(row)

    def list_tenants(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT t.*, p.name AS plan_name, p.rpm AS plan_rpm, p.concurrency AS plan_concurrency,
                p.monthly_fee_microusd AS plan_monthly_fee_microusd,
                p.included_credit_microusd AS plan_included_credit_microusd,
                p.enabled AS plan_enabled,
                (SELECT COUNT(*) FROM gateway_api_keys k WHERE k.tenant_id = t.id AND k.status = 'active') AS active_keys,
                (SELECT MAX(s.period_end) FROM gateway_subscription_events s WHERE s.tenant_id = t.id)
                    AS subscription_expires_at
                FROM gateway_tenants t LEFT JOIN gateway_plans p ON p.id = t.plan_id
                ORDER BY t.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def adjust_balance(
        self,
        *,
        tenant_id: str,
        amount_microusd: int,
        idempotency_key: str,
        payment_reference: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        if not amount_microusd or not str(idempotency_key or "").strip():
            raise GatewayStoreError(
                "invalid_adjustment",
                "A non-zero amount and idempotency key are required.",
            )
        self.ensure_schema()
        now = time.time()
        reference = str(payment_reference or "").strip()[:200]
        with self._transaction() as conn:
            receipt = None
            if reference:
                receipt = conn.execute(
                    "SELECT * FROM gateway_payment_references WHERE external_reference = ?",
                    (reference,),
                ).fetchone()
            if receipt is not None:
                if (
                    receipt["tenant_id"] != tenant_id
                    or receipt["operation"] != "balance"
                    or int(receipt["amount_microusd"]) != int(amount_microusd)
                ):
                    raise GatewayStoreError(
                        "payment_reference_conflict",
                        "External payment reference has already been used.",
                        status_code=409,
                    )
                prior_receipt = conn.execute(
                    "SELECT * FROM gateway_ledger WHERE id = ?",
                    (receipt["result_id"],),
                ).fetchone()
                if prior_receipt is not None:
                    return {**dict(prior_receipt), "replayed": True}
            prior = conn.execute(
                "SELECT * FROM gateway_ledger WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if prior is not None:
                if int(prior["amount_microusd"]) != int(amount_microusd):
                    raise GatewayStoreError(
                        "idempotency_conflict",
                        "Idempotency key already belongs to a different adjustment.",
                        status_code=409,
                    )
                if reference:
                    conn.execute(
                        """INSERT INTO gateway_payment_references
                        (external_reference, tenant_id, operation, amount_microusd, result_id, created_at)
                        VALUES (?, ?, 'balance', ?, ?, ?)""",
                        (
                            reference,
                            tenant_id,
                            int(amount_microusd),
                            prior["id"],
                            now,
                        ),
                    )
                return {**dict(prior), "replayed": True}
            tenant = conn.execute(
                "SELECT balance_microusd FROM gateway_tenants WHERE id = ?",
                (tenant_id,),
            ).fetchone()
            if tenant is None:
                raise GatewayStoreError(
                    "tenant_not_found", "Tenant was not found.", status_code=404
                )
            balance = int(tenant["balance_microusd"]) + int(amount_microusd)
            if balance < 0:
                raise GatewayStoreError(
                    "insufficient_balance",
                    "Adjustment would make the balance negative.",
                    status_code=402,
                )
            conn.execute(
                "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                (balance, now, tenant_id),
            )
            entry = self._insert_ledger(
                conn,
                tenant_id=tenant_id,
                request_id="",
                kind="credit" if amount_microusd > 0 else "debit",
                amount=amount_microusd,
                balance_after=balance,
                idempotency_key=idempotency_key,
                note=str(note or "")[:500],
                now=now,
            )
            if reference:
                conn.execute(
                    """INSERT INTO gateway_payment_references
                    (external_reference, tenant_id, operation, amount_microusd, result_id, created_at)
                    VALUES (?, ?, 'balance', ?, ?, ?)""",
                    (
                        reference,
                        tenant_id,
                        int(amount_microusd),
                        entry["id"],
                        now,
                    ),
                )
        return {**entry, "replayed": False}

    def create_wechat_order(
        self,
        *,
        tenant_id: str,
        out_trade_no: str,
        package_id: str,
        package_name: str,
        description: str,
        amount_fen: int,
        credit_microusd: int,
        expires_at: float,
    ) -> dict[str, Any]:
        if (
            not str(out_trade_no or "").strip()
            or not str(package_id or "").strip()
            or not str(package_name or "").strip()
            or int(amount_fen) <= 0
            or int(credit_microusd) <= 0
            or float(expires_at) <= time.time()
        ):
            raise GatewayStoreError("invalid_payment_order", "WeChat payment order fields are invalid.")
        order_id = f"wxorder_{uuid.uuid4().hex}"
        now = time.time()
        self.ensure_schema()
        with self._transaction() as conn:
            tenant = conn.execute(
                "SELECT 1 FROM gateway_tenants WHERE id = ? AND status = 'active'",
                (tenant_id,),
            ).fetchone()
            if tenant is None:
                raise GatewayStoreError(
                    "tenant_not_found", "Active tenant was not found.", status_code=404
                )
            existing = conn.execute(
                """SELECT id FROM gateway_wechat_orders
                WHERE tenant_id = ? AND expires_at > ?
                  AND (status = 'pending' OR (status = 'creating' AND created_at >= ?))
                ORDER BY created_at DESC LIMIT 1""",
                (tenant_id, now, now - 60),
            ).fetchone()
            if existing is not None:
                raise GatewayStoreError(
                    "payment_order_pending",
                    "A WeChat payment order is already in progress.",
                    status_code=409,
                    details={"order_id": existing["id"]},
                )
            conn.execute(
                """INSERT INTO gateway_wechat_orders
                (id, out_trade_no, tenant_id, package_id, package_name, description,
                 amount_fen, credit_microusd, currency, status, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CNY', 'creating', ?, ?, ?)""",
                (
                    order_id,
                    str(out_trade_no).strip(),
                    tenant_id,
                    str(package_id).strip(),
                    str(package_name).strip()[:80],
                    str(description).strip()[:127],
                    int(amount_fen),
                    int(credit_microusd),
                    float(expires_at),
                    now,
                    now,
                ),
            )
        return self.get_wechat_order(order_id, tenant_id=tenant_id, include_code_url=True)

    def activate_wechat_order(self, order_id: str, *, code_url: str) -> dict[str, Any]:
        if not str(code_url or "").startswith("weixin://"):
            raise GatewayStoreError("invalid_code_url", "WeChat Native code URL is invalid.")
        self.ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE gateway_wechat_orders SET status = 'pending', code_url = ?,
                error_code = '', updated_at = ? WHERE id = ? AND status = 'creating'""",
                (str(code_url), time.time(), order_id),
            )
        if cursor.rowcount != 1:
            raise GatewayStoreError(
                "payment_order_state_conflict", "WeChat payment order is not awaiting activation.", status_code=409
            )
        return self.get_wechat_order(order_id, include_code_url=True)

    def fail_wechat_order(self, order_id: str, *, error_code: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE gateway_wechat_orders SET status = 'failed', error_code = ?, updated_at = ?
                WHERE id = ? AND status NOT IN ('paid', 'closed')""",
                (str(error_code or "wechat_order_failed")[:80], time.time(), order_id),
            )
        if cursor.rowcount != 1:
            return self.get_wechat_order(order_id)
        return self.get_wechat_order(order_id)

    def close_wechat_order(
        self, order_id: str, *, status: str = "closed", error_code: str = ""
    ) -> dict[str, Any]:
        normalized = str(status or "closed").strip().lower()
        if normalized not in {"closed", "expired", "failed"}:
            raise GatewayStoreError("invalid_payment_status", "Payment close status is invalid.")
        now = time.time()
        self.ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE gateway_wechat_orders SET status = ?, error_code = ?, closed_at = ?, updated_at = ?
                WHERE id = ? AND status NOT IN ('paid', 'closed', 'expired')""",
                (normalized, str(error_code or "")[:80], now, now, order_id),
            )
        if cursor.rowcount != 1:
            return self.get_wechat_order(order_id)
        return self.get_wechat_order(order_id)

    def claim_wechat_order_refresh(self, order_id: str, *, interval_seconds: float = 5) -> bool:
        self.ensure_schema()
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE gateway_wechat_orders SET last_provider_check_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending' AND last_provider_check_at <= ?""",
                (now, now, order_id, now - max(1.0, float(interval_seconds))),
            )
        return cursor.rowcount == 1

    def complete_wechat_order(
        self,
        *,
        out_trade_no: str,
        transaction_id: str,
        amount_fen: int,
        currency: str,
        provider_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reference = str(transaction_id or "").strip()
        if not reference:
            raise GatewayStoreError("invalid_payment", "WeChat transaction id is required.")
        now = time.time()
        self.ensure_schema()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_wechat_orders WHERE out_trade_no = ?",
                (str(out_trade_no or "").strip(),),
            ).fetchone()
            if row is None:
                raise GatewayStoreError(
                    "payment_order_not_found", "WeChat payment order was not found.", status_code=404
                )
            order = dict(row)
            if int(order["amount_fen"]) != int(amount_fen) or str(order["currency"]) != str(currency):
                raise GatewayStoreError(
                    "payment_amount_mismatch",
                    "WeChat payment amount or currency does not match the order.",
                    status_code=409,
                )
            if order["status"] == "paid":
                if str(order.get("transaction_id") or "") != reference:
                    raise GatewayStoreError(
                        "payment_transaction_conflict",
                        "Payment order is already linked to another transaction.",
                        status_code=409,
                    )
                return {
                    "order": self._public_wechat_order(order),
                    "tenant": self.get_tenant(str(order["tenant_id"])),
                    "replayed": True,
                }
            prior_reference = conn.execute(
                "SELECT * FROM gateway_payment_references WHERE external_reference = ?",
                (reference,),
            ).fetchone()
            if prior_reference is not None:
                raise GatewayStoreError(
                    "payment_transaction_conflict",
                    "WeChat transaction has already been used.",
                    status_code=409,
                )
            tenant = conn.execute(
                "SELECT balance_microusd FROM gateway_tenants WHERE id = ? AND status = 'active'",
                (order["tenant_id"],),
            ).fetchone()
            if tenant is None:
                raise GatewayStoreError(
                    "tenant_not_found", "Active tenant was not found.", status_code=404
                )
            balance = int(tenant["balance_microusd"]) + int(order["credit_microusd"])
            conn.execute(
                "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                (balance, now, order["tenant_id"]),
            )
            ledger = self._insert_ledger(
                conn,
                tenant_id=str(order["tenant_id"]),
                request_id=str(order["id"]),
                kind="credit",
                amount=int(order["credit_microusd"]),
                balance_after=balance,
                idempotency_key=f"wechat:{reference}",
                note=f"WeChat Native credit: {order['package_name']}",
                now=now,
            )
            conn.execute(
                """INSERT INTO gateway_payment_references
                (external_reference, tenant_id, operation, amount_microusd, result_id, created_at)
                VALUES (?, ?, 'wechat_native', ?, ?, ?)""",
                (
                    reference,
                    order["tenant_id"],
                    int(order["credit_microusd"]),
                    ledger["id"],
                    now,
                ),
            )
            safe_payload = dict(provider_payload or {})
            safe_payload.pop("payer", None)
            conn.execute(
                """UPDATE gateway_wechat_orders SET status = 'paid', transaction_id = ?,
                provider_payload_json = ?, error_code = '', paid_at = ?, updated_at = ? WHERE id = ?""",
                (
                    reference,
                    json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                    now,
                    order["id"],
                ),
            )
            updated = conn.execute(
                "SELECT * FROM gateway_wechat_orders WHERE id = ?", (order["id"],)
            ).fetchone()
        return {
            "order": self._public_wechat_order(dict(updated)),
            "ledger_entry": ledger,
            "tenant": self.get_tenant(str(order["tenant_id"])),
            "replayed": False,
        }

    def get_wechat_order(
        self,
        order_id: str,
        *,
        tenant_id: str = "",
        include_code_url: bool = False,
    ) -> dict[str, Any]:
        self.ensure_schema()
        sql = "SELECT * FROM gateway_wechat_orders WHERE id = ?"
        params: tuple[Any, ...] = (order_id,)
        if tenant_id:
            sql += " AND tenant_id = ?"
            params = (order_id, tenant_id)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            raise GatewayStoreError(
                "payment_order_not_found", "WeChat payment order was not found.", status_code=404
            )
        return self._public_wechat_order(dict(row), include_code_url=include_code_url)

    def get_wechat_order_by_trade_no(self, out_trade_no: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_wechat_orders WHERE out_trade_no = ?", (out_trade_no,)
            ).fetchone()
        if row is None:
            raise GatewayStoreError(
                "payment_order_not_found", "WeChat payment order was not found.", status_code=404
            )
        return self._public_wechat_order(dict(row), include_code_url=True)

    def list_wechat_orders(
        self, *, tenant_id: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        sql = "SELECT * FROM gateway_wechat_orders"
        params: list[Any] = []
        if tenant_id:
            sql += " WHERE tenant_id = ?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._public_wechat_order(dict(row)) for row in rows]

    def renew_subscription(
        self,
        *,
        tenant_id: str,
        amount_paid_microusd: int,
        external_reference: str,
        period_days: int = 30,
    ) -> dict[str, Any]:
        reference = str(external_reference or "").strip()[:200]
        if not reference or amount_paid_microusd < 0 or period_days != 30:
            raise GatewayStoreError(
                "invalid_subscription_payment",
                "External reference, non-negative payment, and a 30-day period are required.",
            )
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            receipt = conn.execute(
                "SELECT * FROM gateway_payment_references WHERE external_reference = ?",
                (reference,),
            ).fetchone()
            if receipt is not None:
                prior_receipt = conn.execute(
                    "SELECT * FROM gateway_subscription_events WHERE id = ?",
                    (receipt["result_id"],),
                ).fetchone()
                same_period = (
                    prior_receipt is not None
                    and round(
                        float(prior_receipt["period_end"])
                        - float(prior_receipt["period_start"])
                    )
                    == period_days * 86400
                )
                if (
                    receipt["tenant_id"] != tenant_id
                    or receipt["operation"] != "subscription"
                    or int(receipt["amount_microusd"]) != int(amount_paid_microusd)
                    or not same_period
                ):
                    raise GatewayStoreError(
                        "payment_reference_conflict",
                        "External payment reference has already been used.",
                        status_code=409,
                    )
                return {**dict(prior_receipt), "replayed": True}
            prior = conn.execute(
                """SELECT * FROM gateway_subscription_events
                WHERE tenant_id = ? AND external_reference = ?""",
                (tenant_id, reference),
            ).fetchone()
            if prior is not None:
                if (
                    int(prior["amount_paid_microusd"]) != int(amount_paid_microusd)
                    or round(float(prior["period_end"]) - float(prior["period_start"]))
                    != period_days * 86400
                ):
                    raise GatewayStoreError(
                        "idempotency_conflict",
                        "Payment reference already belongs to a different renewal.",
                        status_code=409,
                    )
                conn.execute(
                    """INSERT INTO gateway_payment_references
                    (external_reference, tenant_id, operation, amount_microusd, result_id, created_at)
                    VALUES (?, ?, 'subscription', ?, ?, ?)""",
                    (
                        reference,
                        tenant_id,
                        int(amount_paid_microusd),
                        prior["id"],
                        now,
                    ),
                )
                return {**dict(prior), "replayed": True}
            tenant = conn.execute(
                """SELECT t.balance_microusd, t.plan_id, p.monthly_fee_microusd,
                p.included_credit_microusd, p.enabled
                FROM gateway_tenants t JOIN gateway_plans p ON p.id = t.plan_id
                WHERE t.id = ? AND t.status = 'active'""",
                (tenant_id,),
            ).fetchone()
            if tenant is None or not tenant["enabled"]:
                raise GatewayStoreError(
                    "tenant_or_plan_unavailable",
                    "Active tenant and plan were not found.",
                    status_code=404,
                )
            fee = int(tenant["monthly_fee_microusd"])
            if amount_paid_microusd < fee:
                raise GatewayStoreError(
                    "subscription_underpaid",
                    "Confirmed payment is below the plan monthly fee.",
                    status_code=402,
                )
            prior_end = conn.execute(
                "SELECT MAX(period_end) FROM gateway_subscription_events WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()[0]
            period_start = max(now, float(prior_end or 0))
            period_end = period_start + period_days * 86400
            credit = int(tenant["included_credit_microusd"])
            balance = int(tenant["balance_microusd"]) + credit
            event = {
                "id": f"subscription_{uuid.uuid4().hex}",
                "tenant_id": tenant_id,
                "plan_id": str(tenant["plan_id"]),
                "amount_paid_microusd": int(amount_paid_microusd),
                "credit_granted_microusd": credit,
                "period_start": period_start,
                "period_end": period_end,
                "external_reference": reference,
                "created_at": now,
            }
            conn.execute(
                """INSERT INTO gateway_subscription_events
                (id, tenant_id, plan_id, amount_paid_microusd, credit_granted_microusd,
                 period_start, period_end, external_reference, created_at)
                VALUES (:id, :tenant_id, :plan_id, :amount_paid_microusd,
                        :credit_granted_microusd, :period_start, :period_end,
                        :external_reference, :created_at)""",
                event,
            )
            conn.execute(
                """INSERT INTO gateway_payment_references
                (external_reference, tenant_id, operation, amount_microusd, result_id, created_at)
                VALUES (?, ?, 'subscription', ?, ?, ?)""",
                (
                    reference,
                    tenant_id,
                    int(amount_paid_microusd),
                    event["id"],
                    now,
                ),
            )
            conn.execute(
                "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                (balance, now, tenant_id),
            )
            self._insert_ledger(
                conn,
                tenant_id=tenant_id,
                request_id="",
                kind="subscription_credit",
                amount=credit,
                balance_after=balance,
                idempotency_key=f"subscription:{reference}",
                note=f"Plan renewal through {period_end:.0f}",
                now=now,
            )
        return {**event, "replayed": False}

    def list_subscription_events(
        self, *, tenant_id: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        self.ensure_schema()
        sql = "SELECT * FROM gateway_subscription_events"
        params: list[Any] = []
        if tenant_id:
            sql += " WHERE tenant_id = ?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def create_api_key(
        self,
        *,
        tenant_id: str,
        name: str,
        rpm_override: int = 0,
        concurrency_override: int = 0,
        allowed_models: list[str] | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        if rpm_override < 0 or concurrency_override < 0:
            raise GatewayStoreError(
                "invalid_limits", "API key limit overrides cannot be negative."
            )
        public_id = uuid.uuid4().hex[:16]
        key_id = f"key_{public_id}"
        token = f"pacer_sk_{public_id}_{secrets.token_urlsafe(32)}"
        salt = secrets.token_hex(16)
        key_prefix = f"pacer_sk_{public_id}"
        models = sorted(
            {str(item).strip() for item in (allowed_models or []) if str(item).strip()}
        )
        self.ensure_schema()
        with self._transaction() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM gateway_tenants WHERE id = ? AND status = 'active'",
                    (tenant_id,),
                ).fetchone()
                is None
            ):
                raise GatewayStoreError(
                    "tenant_not_found", "Active tenant was not found.", status_code=404
                )
            conn.execute(
                """INSERT INTO gateway_api_keys
                (id, tenant_id, name, key_prefix, key_salt, key_sha256, status, rpm_override,
                 concurrency_override, allowed_models_json, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)""",
                (
                    key_id,
                    tenant_id,
                    str(name or "Default key").strip() or "Default key",
                    key_prefix,
                    salt,
                    hash_api_key(token, salt),
                    rpm_override,
                    concurrency_override,
                    json.dumps(models),
                    expires_at,
                    time.time(),
                ),
            )
        return {**self.get_api_key(key_id), "token": token}

    def get_api_key(self, key_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, tenant_id, name, key_prefix, status, rpm_override, concurrency_override,
                allowed_models_json, expires_at, last_used_at, created_at FROM gateway_api_keys WHERE id = ?""",
                (key_id,),
            ).fetchone()
        if row is None:
            raise GatewayStoreError(
                "key_not_found", "API key was not found.", status_code=404
            )
        result = dict(row)
        result["allowed_models"] = _json_list(result.pop("allowed_models_json", "[]"))
        return result

    def list_api_keys(self, *, tenant_id: str = "") -> list[dict[str, Any]]:
        self.ensure_schema()
        sql = """SELECT id, tenant_id, name, key_prefix, status, rpm_override, concurrency_override,
            allowed_models_json, expires_at, last_used_at, created_at FROM gateway_api_keys"""
        params: tuple[Any, ...] = ()
        if tenant_id:
            sql += " WHERE tenant_id = ?"
            params = (tenant_id,)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["allowed_models"] = _json_list(item.pop("allowed_models_json", "[]"))
            result.append(item)
        return result

    def revoke_api_key(self, key_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE gateway_api_keys SET status = 'revoked' WHERE id = ?", (key_id,)
            )
        if cursor.rowcount != 1:
            raise GatewayStoreError(
                "key_not_found", "API key was not found.", status_code=404
            )
        return self.get_api_key(key_id)

    def authenticate_api_key(self, token: str) -> GatewayPrincipal:
        return self._authenticate_api_key(token, require_subscription=True)

    def authenticate_billing_api_key(self, token: str) -> GatewayPrincipal:
        return self._authenticate_api_key(token, require_subscription=False)

    def _authenticate_api_key(
        self, token: str, *, require_subscription: bool
    ) -> GatewayPrincipal:
        match = _KEY_PATTERN.fullmatch(str(token or "").strip())
        if match is None:
            raise GatewayStoreError(
                "invalid_api_key", "Missing or invalid API key.", status_code=401
            )
        key_id = f"key_{match.group(1)}"
        self.ensure_schema()
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """SELECT k.*, t.name AS tenant_name, t.status AS tenant_status, t.balance_microusd,
                p.rpm AS plan_rpm, p.concurrency AS plan_concurrency,
                p.monthly_fee_microusd AS plan_monthly_fee_microusd,
                p.enabled AS plan_enabled,
                (SELECT MAX(s.period_end) FROM gateway_subscription_events s
                 WHERE s.tenant_id = t.id) AS subscription_expires_at
                FROM gateway_api_keys k
                JOIN gateway_tenants t ON t.id = k.tenant_id
                LEFT JOIN gateway_plans p ON p.id = t.plan_id
                WHERE k.id = ?""",
                (key_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] != "active"
                or row["tenant_status"] != "active"
            ):
                raise GatewayStoreError(
                    "invalid_api_key", "Missing or invalid API key.", status_code=401
                )
            if row["expires_at"] is not None and float(row["expires_at"]) <= now:
                raise GatewayStoreError(
                    "expired_api_key", "API key has expired.", status_code=401
                )
            expected = hash_api_key(token, str(row["key_salt"]))
            if not secrets.compare_digest(expected, str(row["key_sha256"])):
                raise GatewayStoreError(
                    "invalid_api_key", "Missing or invalid API key.", status_code=401
                )
            if require_subscription and not row["plan_enabled"]:
                raise GatewayStoreError(
                    "plan_unavailable",
                    "Tenant plan is not active.",
                    status_code=403,
                )
            if require_subscription and int(row["plan_monthly_fee_microusd"] or 0) > 0 and (
                row["subscription_expires_at"] is None
                or float(row["subscription_expires_at"]) <= now
            ):
                raise GatewayStoreError(
                    "subscription_required",
                    "Paid plan subscription is missing or expired.",
                    status_code=402,
                )
            conn.execute(
                "UPDATE gateway_api_keys SET last_used_at = ? WHERE id = ?",
                (now, key_id),
            )
        tenant_rpm = int(row["plan_rpm"] or 60)
        tenant_concurrency = int(row["plan_concurrency"] or 2)
        key_rpm = int(row["rpm_override"] or tenant_rpm)
        key_concurrency = int(row["concurrency_override"] or tenant_concurrency)
        return GatewayPrincipal(
            key_id=key_id,
            tenant_id=str(row["tenant_id"]),
            tenant_name=str(row["tenant_name"]),
            balance_microusd=int(row["balance_microusd"]),
            rpm=min(key_rpm, tenant_rpm),
            concurrency=min(key_concurrency, tenant_concurrency),
            tenant_rpm=tenant_rpm,
            tenant_concurrency=tenant_concurrency,
            allowed_models=tuple(_json_list(row["allowed_models_json"])),
        )

    def upsert_price(
        self,
        *,
        model: str,
        upstream_model: str = "",
        input_price_microusd_per_million: int,
        output_price_microusd_per_million: int,
        cached_input_price_microusd_per_million: int = 0,
        upstream_input_cost_microusd_per_million: int = 0,
        upstream_output_cost_microusd_per_million: int = 0,
        max_output_tokens: int = 4096,
        enabled: bool = True,
    ) -> dict[str, Any]:
        clean_model = str(model or "").strip()
        if (
            not clean_model
            or min(
                input_price_microusd_per_million,
                output_price_microusd_per_million,
                cached_input_price_microusd_per_million,
                upstream_input_cost_microusd_per_million,
                upstream_output_cost_microusd_per_million,
            )
            < 0
            or max_output_tokens < 1
        ):
            raise GatewayStoreError(
                "invalid_price",
                "Model, non-negative prices, and max output tokens are required.",
            )
        self.ensure_schema()
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO gateway_prices
                (model, upstream_model, input_price_microusd_per_million,
                 cached_input_price_microusd_per_million, output_price_microusd_per_million,
                 upstream_input_cost_microusd_per_million, upstream_output_cost_microusd_per_million,
                 max_output_tokens, enabled, version, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(model) DO UPDATE SET
                    upstream_model = excluded.upstream_model,
                    input_price_microusd_per_million = excluded.input_price_microusd_per_million,
                    cached_input_price_microusd_per_million = excluded.cached_input_price_microusd_per_million,
                    output_price_microusd_per_million = excluded.output_price_microusd_per_million,
                    upstream_input_cost_microusd_per_million = excluded.upstream_input_cost_microusd_per_million,
                    upstream_output_cost_microusd_per_million = excluded.upstream_output_cost_microusd_per_million,
                    max_output_tokens = excluded.max_output_tokens,
                    enabled = excluded.enabled,
                    version = gateway_prices.version + 1,
                    updated_at = excluded.updated_at""",
                (
                    clean_model,
                    str(upstream_model or clean_model).strip() or clean_model,
                    input_price_microusd_per_million,
                    cached_input_price_microusd_per_million,
                    output_price_microusd_per_million,
                    upstream_input_cost_microusd_per_million,
                    upstream_output_cost_microusd_per_million,
                    max_output_tokens,
                    int(enabled),
                    now,
                ),
            )
        return self.get_price(clean_model)

    def get_price(self, model: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_prices WHERE model = ?", (model,)
            ).fetchone()
        if row is None:
            raise GatewayStoreError(
                "model_not_configured",
                "Model price is not configured.",
                status_code=404,
            )
        return dict(row)

    def list_prices(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        self.ensure_schema()
        sql = "SELECT * FROM gateway_prices"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY model"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql)]

    def create_upstream(
        self,
        *,
        name: str,
        base_url: str,
        secret_env: str,
        models: list[str],
        provider: str = "openai-compatible",
        routing_contract: str = "",
        priority: int = 100,
        weight: int = 1,
        max_concurrency: int = 20,
        timeout_seconds: float = 120,
    ) -> dict[str, Any]:
        clean_name = str(name or "").strip()
        clean_env = str(secret_env or "").strip()
        clean_provider = str(provider or "openai-compatible").strip().lower()
        clean_contract = str(routing_contract or "").strip()[:120]
        clean_models = sorted(
            {str(item).strip() for item in models if str(item).strip()}
        )
        if not clean_name or not _ENV_PATTERN.fullmatch(clean_env) or not clean_models:
            raise GatewayStoreError(
                "invalid_upstream",
                "Name, uppercase secret environment name, and models are required.",
            )
        if weight < 1 or max_concurrency < 1 or not 1 <= float(timeout_seconds) <= 900:
            raise GatewayStoreError(
                "invalid_upstream",
                "Weight, concurrency, or timeout is outside the supported range.",
            )
        if clean_provider != "openai-compatible":
            raise GatewayStoreError(
                "unsupported_upstream_provider",
                "This gateway release only accepts OpenAI-compatible upstreams.",
            )
        upstream_id = f"upstream_{uuid.uuid4().hex[:16]}"
        now = time.time()
        self.ensure_schema()
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO gateway_upstreams
                    (id, name, provider, routing_contract, base_url, secret_env, models_json, priority, weight,
                     max_concurrency, timeout_seconds, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (
                        upstream_id,
                        clean_name,
                        clean_provider,
                        clean_contract,
                        validate_upstream_base_url(base_url),
                        clean_env,
                        json.dumps(clean_models),
                        int(priority),
                        int(weight),
                        int(max_concurrency),
                        float(timeout_seconds),
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GatewayStoreError(
                "upstream_exists",
                "An upstream with that name already exists.",
                status_code=409,
            ) from exc
        return self.get_upstream(upstream_id)

    def get_upstream(self, upstream_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_upstreams WHERE id = ?", (upstream_id,)
            ).fetchone()
        if row is None:
            raise GatewayStoreError(
                "upstream_not_found", "Upstream was not found.", status_code=404
            )
        return self._public_upstream(dict(row))

    def list_upstreams(self) -> list[dict[str, Any]]:
        self.ensure_schema()
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT u.*,
                (SELECT COUNT(*) FROM gateway_leases l WHERE l.upstream_id = u.id AND l.expires_at > ?) AS active_requests
                FROM gateway_upstreams u ORDER BY u.priority, u.name""",
                (now,),
            ).fetchall()
        return [self._public_upstream(dict(row)) for row in rows]

    def set_upstream_enabled(self, upstream_id: str, enabled: bool) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE gateway_upstreams SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), time.time(), upstream_id),
            )
        if cursor.rowcount != 1:
            raise GatewayStoreError(
                "upstream_not_found", "Upstream was not found.", status_code=404
            )
        return self.get_upstream(upstream_id)

    def eligible_upstreams(
        self, model: str, *, exclude: set[str] | None = None
    ) -> list[dict[str, Any]]:
        excluded = exclude or set()
        now = time.time()
        pool = []
        for item in self.list_upstreams():
            if not item["enabled"] or not item["secret_configured"]:
                continue
            if model not in item["models"]:
                continue
            pool.append(item)
        if len(pool) > 1:
            contracts = {
                str(item.get("routing_contract") or "").strip() for item in pool
            }
            if "" in contracts or len(contracts) != 1:
                return []
        candidates = [
            item
            for item in pool
            if item["id"] not in excluded
            and float(item["circuit_open_until"]) <= now
            and int(item.get("active_requests") or 0) < int(item["max_concurrency"])
        ]
        candidates.sort(
            key=lambda item: (
                int(item["priority"]),
                (int(item.get("active_requests") or 0) + 1)
                / max(1, int(item["weight"])),
                str(item["id"]),
            )
        )
        return candidates

    def begin_request(
        self,
        *,
        principal: GatewayPrincipal,
        upstream_id: str,
        endpoint: str,
        model: str,
        idempotency_key: str,
        streaming: bool,
        estimated_input_tokens: int,
        max_output_tokens: int,
        lease_seconds: float,
        request_fingerprint: str = "",
    ) -> dict[str, Any]:
        if principal.allowed_models and model not in principal.allowed_models:
            raise GatewayStoreError(
                "model_forbidden",
                "API key is not allowed to use this model.",
                status_code=403,
            )
        clean_idempotency = (
            str(idempotency_key or "").strip()[:200] or f"auto:{uuid.uuid4().hex}"
        )
        self.ensure_schema()
        self.recover_expired_reservations()
        request_id = f"req_{uuid.uuid4().hex}"
        now = time.time()
        with self._transaction() as conn:
            self._cleanup_limits(conn, now)
            duplicate = conn.execute(
                """SELECT id, status, request_fingerprint FROM gateway_requests
                WHERE key_id = ? AND idempotency_key = ?""",
                (principal.key_id, clean_idempotency),
            ).fetchone()
            if duplicate is not None:
                same_payload = bool(request_fingerprint) and secrets.compare_digest(
                    str(duplicate["request_fingerprint"] or ""), request_fingerprint
                )
                raise GatewayStoreError(
                    "duplicate_request" if same_payload else "idempotency_conflict",
                    f"Idempotency key already belongs to request {duplicate['id']} ({duplicate['status']}).",
                    status_code=409,
                    details={
                        "request_id": str(duplicate["id"]),
                        "request_status": str(duplicate["status"]),
                    },
                )
            tenant = conn.execute(
                """SELECT t.balance_microusd, t.status, p.enabled AS plan_enabled,
                p.monthly_fee_microusd AS plan_monthly_fee_microusd,
                (SELECT MAX(s.period_end) FROM gateway_subscription_events s
                 WHERE s.tenant_id = t.id) AS subscription_expires_at
                FROM gateway_tenants t LEFT JOIN gateway_plans p ON p.id = t.plan_id
                WHERE t.id = ?""",
                (principal.tenant_id,),
            ).fetchone()
            if tenant is None or tenant["status"] != "active":
                raise GatewayStoreError(
                    "tenant_disabled", "Tenant is not active.", status_code=403
                )
            if not tenant["plan_enabled"]:
                raise GatewayStoreError(
                    "plan_unavailable",
                    "Tenant plan is not active.",
                    status_code=403,
                )
            if int(tenant["plan_monthly_fee_microusd"] or 0) > 0 and (
                tenant["subscription_expires_at"] is None
                or float(tenant["subscription_expires_at"]) <= now
            ):
                raise GatewayStoreError(
                    "subscription_required",
                    "Paid plan subscription is missing or expired.",
                    status_code=402,
                )
            price_row = conn.execute(
                "SELECT * FROM gateway_prices WHERE model = ? AND enabled = 1",
                (model,),
            ).fetchone()
            if price_row is None:
                raise GatewayStoreError(
                    "model_not_configured",
                    "Model price is not configured.",
                    status_code=404,
                )
            upstream = conn.execute(
                "SELECT * FROM gateway_upstreams WHERE id = ? AND enabled = 1",
                (upstream_id,),
            ).fetchone()
            if upstream is None or model not in _json_list(upstream["models_json"]):
                raise GatewayStoreError(
                    "upstream_unavailable",
                    "No eligible upstream is available.",
                    status_code=503,
                )
            rpm_used = int(
                conn.execute(
                    "SELECT COUNT(*) FROM gateway_rate_events WHERE key_id = ? AND created_at > ?",
                    (principal.key_id, now - 60),
                ).fetchone()[0]
            )
            tenant_rpm_used = int(
                conn.execute(
                    """SELECT COUNT(*) FROM gateway_rate_events e
                    JOIN gateway_api_keys k ON k.id = e.key_id
                    WHERE k.tenant_id = ? AND e.created_at > ?""",
                    (principal.tenant_id, now - 60),
                ).fetchone()[0]
            )
            active_key = int(
                conn.execute(
                    "SELECT COUNT(*) FROM gateway_leases WHERE key_id = ?",
                    (principal.key_id,),
                ).fetchone()[0]
            )
            active_tenant = int(
                conn.execute(
                    """SELECT COUNT(*) FROM gateway_leases l
                    JOIN gateway_requests r ON r.id = l.request_id
                    WHERE r.tenant_id = ?""",
                    (principal.tenant_id,),
                ).fetchone()[0]
            )
            active_upstream = int(
                conn.execute(
                    "SELECT COUNT(*) FROM gateway_leases WHERE upstream_id = ?",
                    (upstream_id,),
                ).fetchone()[0]
            )
            if rpm_used >= principal.rpm or tenant_rpm_used >= principal.tenant_rpm:
                raise GatewayStoreError(
                    "rate_limit_exceeded",
                    "Tenant or API key requests-per-minute limit exceeded.",
                    status_code=429,
                )
            if (
                active_key >= principal.concurrency
                or active_tenant >= principal.tenant_concurrency
            ):
                raise GatewayStoreError(
                    "concurrency_limit_exceeded",
                    "Tenant or API key concurrency limit exceeded.",
                    status_code=429,
                )
            if active_upstream >= int(upstream["max_concurrency"]):
                raise GatewayStoreError(
                    "upstream_busy",
                    "Selected upstream is at its concurrency limit.",
                    status_code=503,
                )
            price = dict(price_row)
            effective_max_output = (
                0
                if endpoint.endswith("/embeddings")
                else min(
                    max(1, int(max_output_tokens or price["max_output_tokens"])),
                    int(price["max_output_tokens"]),
                )
            )
            reserve = calculate_token_charge(
                input_tokens=max(
                    1, int(math.ceil(max(1, estimated_input_tokens) * 1.25))
                ),
                cached_input_tokens=0,
                output_tokens=effective_max_output,
                input_price_microusd_per_million=price[
                    "input_price_microusd_per_million"
                ],
                cached_input_price_microusd_per_million=price[
                    "cached_input_price_microusd_per_million"
                ],
                output_price_microusd_per_million=price[
                    "output_price_microusd_per_million"
                ],
            )
            balance = int(tenant["balance_microusd"])
            if reserve <= 0:
                raise GatewayStoreError(
                    "invalid_price",
                    "Configured model price cannot produce a reservation.",
                    status_code=503,
                )
            if balance < reserve:
                raise GatewayStoreError(
                    "insufficient_balance",
                    "Insufficient balance for this request reservation.",
                    status_code=402,
                )
            balance_after = balance - reserve
            snapshot = {
                key: price[key]
                for key in (
                    "model",
                    "upstream_model",
                    "input_price_microusd_per_million",
                    "cached_input_price_microusd_per_million",
                    "output_price_microusd_per_million",
                    "upstream_input_cost_microusd_per_million",
                    "upstream_output_cost_microusd_per_million",
                    "max_output_tokens",
                    "version",
                )
            }
            conn.execute(
                "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                (balance_after, now, principal.tenant_id),
            )
            conn.execute(
                """INSERT INTO gateway_requests
                (id, tenant_id, key_id, upstream_id, model, upstream_model, endpoint, idempotency_key,
                 request_fingerprint, status, streaming, reserved_microusd, price_snapshot_json,
                 heartbeat_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)""",
                (
                    request_id,
                    principal.tenant_id,
                    principal.key_id,
                    upstream_id,
                    model,
                    price["upstream_model"],
                    endpoint,
                    clean_idempotency,
                    str(request_fingerprint or "")[:64],
                    int(streaming),
                    reserve,
                    json.dumps(snapshot, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT INTO gateway_leases (request_id, key_id, upstream_id, expires_at) VALUES (?, ?, ?, ?)",
                (
                    request_id,
                    principal.key_id,
                    upstream_id,
                    now + max(5, min(float(lease_seconds), 1800)),
                ),
            )
            conn.execute(
                "INSERT INTO gateway_rate_events (key_id, created_at) VALUES (?, ?)",
                (principal.key_id, now),
            )
            self._insert_ledger(
                conn,
                tenant_id=principal.tenant_id,
                request_id=request_id,
                kind="reserve",
                amount=-reserve,
                balance_after=balance_after,
                idempotency_key=f"reserve:{request_id}",
                note=f"Reserve for {model}",
                now=now,
            )
        return {
            "id": request_id,
            "tenant_id": principal.tenant_id,
            "key_id": principal.key_id,
            "upstream_id": upstream_id,
            "model": model,
            "upstream_model": price["upstream_model"],
            "reserved_microusd": reserve,
            "price_snapshot": snapshot,
            "estimated_input_tokens": max(1, int(estimated_input_tokens)),
            "max_output_tokens": effective_max_output,
        }

    def switch_upstream(self, request_id: str, upstream_id: str) -> None:
        self.ensure_schema()
        with self._transaction() as conn:
            request = conn.execute(
                "SELECT status, model FROM gateway_requests WHERE id = ?", (request_id,)
            ).fetchone()
            upstream = conn.execute(
                "SELECT * FROM gateway_upstreams WHERE id = ? AND enabled = 1",
                (upstream_id,),
            ).fetchone()
            if request is None or request["status"] != "reserved" or upstream is None:
                raise GatewayStoreError(
                    "upstream_unavailable",
                    "Cannot switch this request to the selected upstream.",
                    status_code=503,
                )
            if request["model"] not in _json_list(upstream["models_json"]):
                raise GatewayStoreError(
                    "upstream_unavailable",
                    "Selected upstream does not serve the requested model.",
                    status_code=503,
                )
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM gateway_leases WHERE upstream_id = ?",
                    (upstream_id,),
                ).fetchone()[0]
            )
            if active >= int(upstream["max_concurrency"]):
                raise GatewayStoreError(
                    "upstream_busy",
                    "Selected upstream is at its concurrency limit.",
                    status_code=503,
                )
            conn.execute(
                """UPDATE gateway_requests SET upstream_id = ?, attempt_count = attempt_count + 1,
                heartbeat_at = ? WHERE id = ?""",
                (upstream_id, time.time(), request_id),
            )
            conn.execute(
                "UPDATE gateway_leases SET upstream_id = ? WHERE request_id = ?",
                (upstream_id, request_id),
            )

    def start_attempt(
        self, request_id: str, upstream_id: str, *, lease_seconds: float
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = time.time()
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        with self._transaction() as conn:
            request = conn.execute(
                "SELECT status, attempt_count FROM gateway_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if request is None or request["status"] != "reserved":
                raise GatewayStoreError(
                    "request_not_active",
                    "Gateway request is no longer active.",
                    status_code=409,
                )
            conn.execute(
                """UPDATE gateway_requests SET upstream_id = ?, provider_started_at = ?,
                heartbeat_at = ? WHERE id = ?""",
                (upstream_id, now, now, request_id),
            )
            conn.execute(
                "UPDATE gateway_leases SET upstream_id = ?, expires_at = ? WHERE request_id = ?",
                (
                    upstream_id,
                    now + max(5, min(float(lease_seconds), 1800)),
                    request_id,
                ),
            )
            attempt = {
                "id": attempt_id,
                "request_id": request_id,
                "upstream_id": upstream_id,
                "attempt_no": int(request["attempt_count"]),
                "status": "started",
                "created_at": now,
            }
            conn.execute(
                """INSERT INTO gateway_attempts
                (id, request_id, upstream_id, attempt_no, status, created_at)
                VALUES (:id, :request_id, :upstream_id, :attempt_no, :status, :created_at)""",
                attempt,
            )
        return attempt

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        http_status: int,
        error_code: str = "",
        response_started: bool = False,
        upstream_request_id: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        upstream_cost_microusd: int = 0,
        latency_ms: float = 0,
    ) -> None:
        self.ensure_schema()
        normalized_status = str(status or "unknown")[:32]
        with self._transaction() as conn:
            conn.execute(
                """UPDATE gateway_attempts SET status = ?, http_status = ?, error_code = ?,
                response_started = ?, upstream_request_id = ?, input_tokens = ?, output_tokens = ?,
                upstream_cost_microusd = ?, latency_ms = ?, finished_at = ? WHERE id = ?""",
                (
                    normalized_status,
                    int(http_status),
                    str(error_code or "")[:80],
                    int(response_started),
                    str(upstream_request_id or "")[:200],
                    max(0, int(input_tokens)),
                    max(0, int(output_tokens)),
                    max(0, int(upstream_cost_microusd)),
                    max(0, float(latency_ms)),
                    time.time(),
                    attempt_id,
                ),
            )
            if normalized_status == "network_error" and not response_started:
                attempt = conn.execute(
                    "SELECT request_id FROM gateway_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
                if attempt is not None:
                    ambiguous = int(
                        conn.execute(
                            """SELECT COUNT(*) FROM gateway_attempts
                            WHERE request_id = ? AND (response_started = 1 OR status = 'indeterminate')""",
                            (attempt["request_id"],),
                        ).fetchone()[0]
                    )
                    if not ambiguous:
                        conn.execute(
                            "UPDATE gateway_requests SET provider_started_at = NULL WHERE id = ?",
                            (attempt["request_id"],),
                        )

    def list_attempts(self, request_id: str) -> list[dict[str, Any]]:
        self.ensure_schema()
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM gateway_attempts WHERE request_id = ?
                    ORDER BY attempt_no, created_at""",
                    (request_id,),
                )
            ]

    def renew_lease(self, request_id: str, *, lease_seconds: float) -> bool:
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            cursor = conn.execute(
                """UPDATE gateway_leases SET expires_at = ?
                WHERE request_id = ? AND EXISTS (
                    SELECT 1 FROM gateway_requests r
                    WHERE r.id = gateway_leases.request_id AND r.status = 'reserved'
                )""",
                (
                    now + max(5, min(float(lease_seconds), 1800)),
                    request_id,
                ),
            )
            if cursor.rowcount:
                conn.execute(
                    "UPDATE gateway_requests SET heartbeat_at = ? WHERE id = ? AND status = 'reserved'",
                    (now, request_id),
                )
        return cursor.rowcount == 1

    def mark_request_indeterminate(
        self,
        request_id: str,
        *,
        error_code: str,
        http_status: int,
        latency_ms: float,
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise GatewayStoreError(
                    "request_not_found",
                    "Gateway request was not found.",
                    status_code=404,
                )
            if row["status"] != "reserved":
                return dict(row)
            conn.execute(
                """UPDATE gateway_requests SET status = 'indeterminate', error_code = ?,
                http_status = ?, latency_ms = ?, settled_at = ? WHERE id = ?""",
                (
                    str(error_code or "upstream_result_indeterminate")[:80],
                    int(http_status),
                    max(0, float(latency_ms)),
                    now,
                    request_id,
                ),
            )
            conn.execute(
                "DELETE FROM gateway_leases WHERE request_id = ?", (request_id,)
            )
        return self.get_request(request_id)

    def settle_request(
        self,
        request_id: str,
        *,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        usage_source: str,
        error_code: str = "",
        http_status: int,
        latency_ms: float,
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise GatewayStoreError(
                    "request_not_found",
                    "Gateway request was not found.",
                    status_code=404,
                )
            if row["status"] != "reserved":
                return dict(row)
            price = json.loads(row["price_snapshot_json"])
            calculated = calculate_token_charge(
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                input_price_microusd_per_million=price[
                    "input_price_microusd_per_million"
                ],
                cached_input_price_microusd_per_million=price[
                    "cached_input_price_microusd_per_million"
                ],
                output_price_microusd_per_million=price[
                    "output_price_microusd_per_million"
                ],
            )
            upstream_cost = calculate_token_charge(
                input_tokens=input_tokens,
                cached_input_tokens=0,
                output_tokens=output_tokens,
                input_price_microusd_per_million=price[
                    "upstream_input_cost_microusd_per_million"
                ],
                cached_input_price_microusd_per_million=price[
                    "upstream_input_cost_microusd_per_million"
                ],
                output_price_microusd_per_million=price[
                    "upstream_output_cost_microusd_per_million"
                ],
            )
            tenant = conn.execute(
                "SELECT balance_microusd FROM gateway_tenants WHERE id = ?",
                (row["tenant_id"],),
            ).fetchone()
            reserved = int(row["reserved_microusd"])
            actual = min(calculated, reserved)
            charge_capped = calculated > reserved
            delta = reserved - actual
            balance = int(tenant["balance_microusd"]) + delta
            conn.execute(
                "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                (balance, now, row["tenant_id"]),
            )
            conn.execute(
                """UPDATE gateway_requests SET status = 'settled', http_status = ?, actual_microusd = ?,
                upstream_cost_microusd = ?, input_tokens = ?, cached_input_tokens = ?, output_tokens = ?,
                usage_source = ?, error_code = ?, latency_ms = ?, settled_at = ? WHERE id = ?""",
                (
                    int(http_status),
                    actual,
                    upstream_cost,
                    max(0, int(input_tokens)),
                    max(0, int(cached_input_tokens)),
                    max(0, int(output_tokens)),
                    str(usage_source or "estimated")[:32],
                    "charge_capped_at_reservation"
                    if charge_capped
                    else str(error_code or "")[:80],
                    max(0, float(latency_ms)),
                    now,
                    request_id,
                ),
            )
            conn.execute(
                "DELETE FROM gateway_leases WHERE request_id = ?", (request_id,)
            )
            self._insert_ledger(
                conn,
                tenant_id=str(row["tenant_id"]),
                request_id=request_id,
                kind="settlement",
                amount=delta,
                balance_after=balance,
                idempotency_key=f"settle:{request_id}",
                note=(
                    f"Final charge {actual} micro-USD"
                    if not charge_capped
                    else f"Charge capped at reservation {actual}; calculated {calculated} micro-USD"
                ),
                now=now,
            )
        return self.get_request(request_id)

    def fail_request(
        self,
        request_id: str,
        *,
        error_code: str,
        http_status: int,
        latency_ms: float,
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise GatewayStoreError(
                    "request_not_found",
                    "Gateway request was not found.",
                    status_code=404,
                )
            if row["status"] != "reserved":
                return dict(row)
            tenant = conn.execute(
                "SELECT balance_microusd FROM gateway_tenants WHERE id = ?",
                (row["tenant_id"],),
            ).fetchone()
            balance = int(tenant["balance_microusd"]) + int(row["reserved_microusd"])
            conn.execute(
                "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                (balance, now, row["tenant_id"]),
            )
            conn.execute(
                """UPDATE gateway_requests SET status = 'failed', error_code = ?, http_status = ?,
                latency_ms = ?, settled_at = ? WHERE id = ?""",
                (
                    str(error_code or "upstream_error")[:80],
                    int(http_status),
                    max(0, latency_ms),
                    now,
                    request_id,
                ),
            )
            conn.execute(
                "DELETE FROM gateway_leases WHERE request_id = ?", (request_id,)
            )
            self._insert_ledger(
                conn,
                tenant_id=str(row["tenant_id"]),
                request_id=request_id,
                kind="release",
                amount=int(row["reserved_microusd"]),
                balance_after=balance,
                idempotency_key=f"release:{request_id}",
                note=f"Released after {error_code}",
                now=now,
            )
        return self.get_request(request_id)

    def record_upstream_result(
        self,
        upstream_id: str,
        *,
        success: bool,
        http_status: int,
        latency_ms: float,
        retry_after_seconds: float = 0,
    ) -> None:
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT consecutive_failures FROM gateway_upstreams WHERE id = ?",
                (upstream_id,),
            ).fetchone()
            if row is None:
                return
            if success:
                conn.execute(
                    """UPDATE gateway_upstreams SET consecutive_failures = 0, circuit_open_until = 0,
                    last_error_code = '', last_latency_ms = ?, updated_at = ? WHERE id = ?""",
                    (max(0, latency_ms), now, upstream_id),
                )
                return
            failures = int(row["consecutive_failures"]) + 1
            if http_status in {401, 403}:
                cooldown = 300.0
            elif http_status == 429:
                cooldown = max(1.0, min(float(retry_after_seconds or 15), 300.0))
            elif http_status >= 500 or http_status == 0:
                cooldown = min(60.0, float(2 ** min(failures, 6)))
            else:
                cooldown = 0.0
            conn.execute(
                """UPDATE gateway_upstreams SET consecutive_failures = ?, circuit_open_until = ?,
                last_error_code = ?, last_latency_ms = ?, updated_at = ? WHERE id = ?""",
                (
                    failures,
                    now + cooldown if cooldown else 0,
                    str(http_status or "network_error"),
                    max(0, latency_ms),
                    now,
                    upstream_id,
                ),
            )

    def get_request(self, request_id: str) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_requests WHERE id = ?", (request_id,)
            ).fetchone()
        if row is None:
            raise GatewayStoreError(
                "request_not_found", "Gateway request was not found.", status_code=404
            )
        return self._public_request(dict(row))

    def list_requests(
        self, *, tenant_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        self.ensure_schema()
        where = " WHERE tenant_id = ?" if tenant_id else ""
        params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM gateway_requests{where}", params
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT * FROM gateway_requests{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
            ).fetchall()
        return {
            "total": total,
            "items": [self._public_request(dict(row)) for row in rows],
        }

    def list_ledger(
        self, *, tenant_id: str = "", limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        self.ensure_schema()
        where = " WHERE tenant_id = ?" if tenant_id else ""
        count_params: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        sql = f"SELECT * FROM gateway_ledger{where}"
        params: list[Any] = []
        if tenant_id:
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.append(max(1, min(int(limit), 500)))
        params.append(max(0, int(offset)))
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM gateway_ledger{where}", count_params
                ).fetchone()[0]
            )
            items = [dict(row) for row in conn.execute(sql, params)]
        return {"total": total, "items": items}

    def summary(self) -> dict[str, Any]:
        self.recover_expired_reservations()
        self.ensure_schema()
        today = time.time() - 86400
        with self._connect() as conn:
            counts = conn.execute(
                """SELECT COUNT(*) AS requests,
                COALESCE(SUM(CASE WHEN status = 'settled' THEN actual_microusd ELSE 0 END), 0) AS revenue,
                COALESCE(SUM(CASE WHEN status = 'settled' THEN upstream_cost_microusd ELSE 0 END), 0) AS upstream_cost,
                COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) AS failures,
                COALESCE(SUM(CASE WHEN status = 'indeterminate' THEN 1 ELSE 0 END), 0) AS indeterminate,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens
                FROM gateway_requests WHERE created_at >= ?""",
                (today,),
            ).fetchone()
            tenant_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM gateway_tenants WHERE status = 'active'"
                ).fetchone()[0]
            )
            upstream_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM gateway_upstreams WHERE enabled = 1"
                ).fetchone()[0]
            )
            active = int(
                conn.execute(
                    "SELECT COUNT(*) FROM gateway_leases WHERE expires_at > ?",
                    (time.time(),),
                ).fetchone()[0]
            )
            payments = conn.execute(
                """SELECT
                COALESCE(SUM(CASE WHEN operation = 'subscription' THEN amount_microusd ELSE 0 END), 0)
                    AS subscription_cash,
                COALESCE(SUM(CASE WHEN operation = 'balance' AND amount_microusd > 0
                                  THEN amount_microusd ELSE 0 END), 0) AS balance_cash
                FROM gateway_payment_references WHERE created_at >= ?""",
                (today,),
            ).fetchone()
            wechat = conn.execute(
                """SELECT COUNT(*) AS paid_orders, COALESCE(SUM(amount_fen), 0) AS paid_fen
                FROM gateway_wechat_orders WHERE status = 'paid' AND paid_at >= ?""",
                (today,),
            ).fetchone()
        revenue = int(counts["revenue"])
        upstream_cost = int(counts["upstream_cost"])
        subscription_cash = int(payments["subscription_cash"])
        balance_cash = int(payments["balance_cash"])
        return {
            "window": "24h",
            "requests": int(counts["requests"]),
            "failed_requests": int(counts["failures"]),
            "indeterminate_requests": int(counts["indeterminate"]),
            "tokens": int(counts["tokens"]),
            "revenue_microusd": revenue,
            "upstream_cost_microusd": upstream_cost,
            "gross_margin_microusd": revenue - upstream_cost,
            "confirmed_cash_microusd": subscription_cash + balance_cash,
            "subscription_cash_microusd": subscription_cash,
            "balance_cash_microusd": balance_cash,
            "wechat_native_paid_orders": int(wechat["paid_orders"]),
            "wechat_native_cash_fen": int(wechat["paid_fen"]),
            "active_tenants": tenant_count,
            "enabled_upstreams": upstream_count,
            "active_requests": active,
        }

    def recover_expired_reservations(self) -> int:
        self.ensure_schema()
        with self._transaction() as conn:
            return self._cleanup_limits(conn, time.time())

    def reconcile_indeterminate_request(
        self,
        request_id: str,
        *,
        action: str,
        actual_microusd: int | None = None,
        upstream_cost_microusd: int | None = None,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> dict[str, Any]:
        normalized = str(action or "").strip().lower()
        if normalized not in {"capture", "release"}:
            raise GatewayStoreError(
                "invalid_reconciliation",
                "Reconciliation action must be capture or release.",
            )
        numeric_values = (
            upstream_cost_microusd,
            input_tokens,
            cached_input_tokens,
            output_tokens,
        )
        if actual_microusd is not None and int(actual_microusd) < 0:
            raise GatewayStoreError(
                "invalid_reconciliation", "Captured amount cannot be negative."
            )
        if any(value is not None and int(value) < 0 for value in numeric_values):
            raise GatewayStoreError(
                "invalid_reconciliation", "Reconciliation values cannot be negative."
            )
        self.ensure_schema()
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_requests WHERE id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise GatewayStoreError(
                    "request_not_found",
                    "Gateway request was not found.",
                    status_code=404,
                )
            if row["status"] != "indeterminate":
                return {**dict(row), "replayed": True}
            tenant = conn.execute(
                "SELECT balance_microusd FROM gateway_tenants WHERE id = ?",
                (row["tenant_id"],),
            ).fetchone()
            balance = int(tenant["balance_microusd"])
            if normalized == "release":
                amount = int(row["reserved_microusd"])
                balance += amount
                conn.execute(
                    "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                    (balance, now, row["tenant_id"]),
                )
                conn.execute(
                    """UPDATE gateway_requests SET status = 'failed', error_code = 'manual_release',
                    actual_microusd = 0, usage_source = 'manual_reconcile', settled_at = ?
                    WHERE id = ?""",
                    (now, request_id),
                )
                kind = "release"
            else:
                reserved = int(row["reserved_microusd"])
                captured = reserved if actual_microusd is None else int(actual_microusd)
                if captured > reserved:
                    raise GatewayStoreError(
                        "invalid_reconciliation",
                        "Captured amount cannot exceed the reservation.",
                    )
                amount = reserved - captured
                balance += amount
                if amount:
                    conn.execute(
                        "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                        (balance, now, row["tenant_id"]),
                    )
                conn.execute(
                    """UPDATE gateway_requests SET status = 'settled', error_code = 'manual_capture',
                    actual_microusd = ?, upstream_cost_microusd = ?, input_tokens = ?,
                    cached_input_tokens = ?, output_tokens = ?, usage_source = 'manual_reconcile',
                    settled_at = ? WHERE id = ?""",
                    (
                        captured,
                        max(0, int(upstream_cost_microusd or 0)),
                        max(0, int(input_tokens)),
                        max(0, int(cached_input_tokens)),
                        max(0, int(output_tokens)),
                        now,
                        request_id,
                    ),
                )
                conn.execute(
                    """UPDATE gateway_attempts SET input_tokens = ?, output_tokens = ?,
                    upstream_cost_microusd = ? WHERE id = (
                        SELECT id FROM gateway_attempts WHERE request_id = ?
                        ORDER BY attempt_no DESC, created_at DESC LIMIT 1
                    )""",
                    (
                        max(0, int(input_tokens)),
                        max(0, int(output_tokens)),
                        max(0, int(upstream_cost_microusd or 0)),
                        request_id,
                    ),
                )
                kind = "settlement"
            self._insert_ledger(
                conn,
                tenant_id=str(row["tenant_id"]),
                request_id=request_id,
                kind=kind,
                amount=amount,
                balance_after=balance,
                idempotency_key=f"reconcile:{normalized}:{request_id}",
                note=(
                    f"Manual capture of {int(row['reserved_microusd']) - amount} micro-USD"
                    if normalized == "capture"
                    else "Manual release of indeterminate request"
                ),
                now=now,
            )
        return {**self.get_request(request_id), "replayed": False}

    def tenant_usage(self, principal: GatewayPrincipal) -> dict[str, Any]:
        self.ensure_schema()
        with self._connect() as conn:
            tenant = conn.execute(
                """SELECT t.*, p.name AS plan_name
                FROM gateway_tenants t LEFT JOIN gateway_plans p ON p.id = t.plan_id
                WHERE t.id = ?""",
                (principal.tenant_id,),
            ).fetchone()
            subscription_expires_at = conn.execute(
                "SELECT MAX(period_end) FROM gateway_subscription_events WHERE tenant_id = ?",
                (principal.tenant_id,),
            ).fetchone()[0]
            totals = conn.execute(
                """SELECT COUNT(*) AS requests, COALESCE(SUM(actual_microusd), 0) AS spent,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM gateway_requests WHERE tenant_id = ? AND status = 'settled'""",
                (principal.tenant_id,),
            ).fetchone()
        return {
            "tenant_id": principal.tenant_id,
            "tenant_name": principal.tenant_name,
            "plan_id": tenant["plan_id"],
            "plan_name": tenant["plan_name"],
            "balance_microusd": int(tenant["balance_microusd"]),
            "limits": {"rpm": principal.rpm, "concurrency": principal.concurrency},
            "subscription_expires_at": subscription_expires_at,
            "usage": dict(totals),
        }

    def _public_upstream(self, item: dict[str, Any]) -> dict[str, Any]:
        item["models"] = _json_list(item.pop("models_json", "[]"))
        secret_env = str(item.pop("secret_env", ""))
        item["secret_env"] = secret_env
        item["secret_configured"] = bool(os.environ.get(secret_env))
        item["enabled"] = bool(item.get("enabled"))
        return item

    @staticmethod
    def _public_request(item: dict[str, Any]) -> dict[str, Any]:
        item["streaming"] = bool(item.get("streaming"))
        try:
            item["price_snapshot"] = json.loads(item.pop("price_snapshot_json", "{}"))
        except json.JSONDecodeError:
            item["price_snapshot"] = {}
        return item

    @staticmethod
    def _public_wechat_order(
        item: dict[str, Any], *, include_code_url: bool = False
    ) -> dict[str, Any]:
        item.pop("provider_payload_json", None)
        if not include_code_url:
            item.pop("code_url", None)
        item["amount_yuan"] = round(int(item.get("amount_fen") or 0) / 100, 2)
        return item

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path), timeout=15, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _cleanup_limits(conn: sqlite3.Connection, now: float) -> int:
        expired = conn.execute(
            """SELECT r.id, r.tenant_id, r.reserved_microusd, r.provider_started_at,
            EXISTS(
                SELECT 1 FROM gateway_attempts a
                WHERE a.request_id = r.id
                  AND (a.response_started = 1 OR a.status = 'indeterminate')
            ) AS attempt_ambiguous
            FROM gateway_leases l JOIN gateway_requests r ON r.id = l.request_id
            WHERE l.expires_at <= ? AND r.status = 'reserved'""",
            (now,),
        ).fetchall()
        for row in expired:
            if row["provider_started_at"] is not None or row["attempt_ambiguous"]:
                conn.execute(
                    """UPDATE gateway_requests SET status = 'indeterminate',
                    error_code = 'lease_expired_after_upstream_start', http_status = 504,
                    settled_at = ? WHERE id = ? AND status = 'reserved'""",
                    (now, row["id"]),
                )
                continue
            tenant = conn.execute(
                "SELECT balance_microusd FROM gateway_tenants WHERE id = ?",
                (row["tenant_id"],),
            ).fetchone()
            balance = int(tenant["balance_microusd"]) + int(row["reserved_microusd"])
            conn.execute(
                "UPDATE gateway_tenants SET balance_microusd = ?, updated_at = ? WHERE id = ?",
                (balance, now, row["tenant_id"]),
            )
            conn.execute(
                """UPDATE gateway_requests SET status = 'failed', error_code = 'lease_expired',
                http_status = 504, settled_at = ? WHERE id = ? AND status = 'reserved'""",
                (now, row["id"]),
            )
            GatewayStore._insert_ledger(
                conn,
                tenant_id=str(row["tenant_id"]),
                request_id=str(row["id"]),
                kind="release",
                amount=int(row["reserved_microusd"]),
                balance_after=balance,
                idempotency_key=f"lease-expired:{row['id']}",
                note="Released expired request reservation",
                now=now,
            )
        conn.execute("DELETE FROM gateway_leases WHERE expires_at <= ?", (now,))
        conn.execute(
            "DELETE FROM gateway_rate_events WHERE created_at <= ?", (now - 120,)
        )
        return len(expired)

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        existing = {
            str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _insert_ledger(
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        request_id: str,
        kind: str,
        amount: int,
        balance_after: int,
        idempotency_key: str,
        note: str,
        now: float,
    ) -> dict[str, Any]:
        entry = {
            "id": f"ledger_{uuid.uuid4().hex}",
            "tenant_id": tenant_id,
            "request_id": request_id or None,
            "kind": kind,
            "amount_microusd": int(amount),
            "balance_after_microusd": int(balance_after),
            "idempotency_key": idempotency_key,
            "note": note,
            "created_at": now,
        }
        conn.execute(
            """INSERT INTO gateway_ledger
            (id, tenant_id, request_id, kind, amount_microusd, balance_after_microusd, idempotency_key, note, created_at)
            VALUES (:id, :tenant_id, :request_id, :kind, :amount_microusd, :balance_after_microusd,
                    :idempotency_key, :note, :created_at)""",
            entry,
        )
        return entry


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []
