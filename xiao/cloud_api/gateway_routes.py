from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Header, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .auth import bearer_token
from .gateway_proxy import GatewayProxy
from .gateway_store import GatewayStore, GatewayStoreError
from .wechat_native import (
    CreditPackage,
    WechatNativeClient,
    WechatNativeError,
    generate_out_trade_no,
    load_credit_packages,
    payment_expiry,
    payment_readiness,
    qr_png_data_url,
)

_CUSTOMER_API_PATHS = {
    "/v1/models",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/embeddings",
}


def install_gateway_routes(
    app: Any,
    *,
    workspace_root: str | Path,
    require_admin: Callable[[str], None],
    audit_event: Callable[[dict[str, Any]], None],
) -> GatewayStore:
    root = Path(workspace_root).resolve()
    configured_db = str(os.environ.get("PACER_GATEWAY_DB") or "").strip()
    db_path = Path(configured_db) if configured_db else root / "cloud_gateway.db"
    if not db_path.is_absolute():
        db_path = root / db_path
    store = GatewayStore(db_path)
    store.recover_expired_reservations()
    app.state.gateway_store = store
    proxy = GatewayProxy(
        store, transport_resolver=lambda: getattr(app.state, "gateway_transport", None)
    )
    static_dir = Path(__file__).with_name("gateway_static")
    wechat_client_cache: list[WechatNativeClient] = []

    def resolve_wechat_client() -> WechatNativeClient:
        override = getattr(app.state, "wechat_native_client", None)
        if override is not None:
            return override
        if not wechat_client_cache:
            wechat_client_cache.append(WechatNativeClient.from_env())
        return wechat_client_cache[0]

    def configured_packages() -> tuple[CreditPackage, ...]:
        return load_credit_packages()

    def account_session_token(request: Request) -> str:
        return str(request.cookies.get("pacer_session") or "").strip()

    def billing_principal(request: Request, authorization: str) -> Any:
        token = bearer_token(authorization)
        if token:
            return store.authenticate_billing_api_key(token)
        session = account_session_token(request)
        if session:
            return store.account_principal(session)
        raise GatewayStoreError("authentication_required", "API Key or account login is required.", status_code=401)

    def set_session_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            "pacer_session", token, max_age=2_592_000, httponly=True,
            secure=str(os.environ.get("PACER_ACCOUNT_COOKIE_SECURE") or "").lower() in {"1", "true", "yes"},
            samesite="lax", path="/",
        )

    @app.middleware("http")
    async def gateway_security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/gateway") or path.startswith("/billing"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
            )
        if path.startswith("/api/gateway") or path.startswith("/billing") or path in _CUSTOMER_API_PATHS:
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(GatewayStoreError)
    async def gateway_store_error_handler(_request: Request, exc: GatewayStoreError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": str(exc),
                    "type": "gateway_error",
                    "code": exc.code,
                    **exc.details,
                }
            },
        )

    @app.exception_handler(WechatNativeError)
    async def wechat_native_error_handler(_request: Request, exc: WechatNativeError):
        details = {"provider_code": exc.provider_code} if exc.provider_code else {}
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "message": str(exc),
                    "type": "wechat_native_error",
                    "code": exc.code,
                    **details,
                }
            },
        )

    @app.get("/gateway", include_in_schema=False)
    def gateway_dashboard():
        return FileResponse(static_dir / "index.html", media_type="text/html")

    @app.get("/gateway.css", include_in_schema=False)
    def gateway_dashboard_css():
        return FileResponse(static_dir / "gateway.css", media_type="text/css")

    @app.get("/gateway.js", include_in_schema=False)
    def gateway_dashboard_js():
        return FileResponse(
            static_dir / "gateway.js", media_type="application/javascript"
        )

    @app.get("/billing", include_in_schema=False)
    def billing_page():
        return FileResponse(static_dir / "billing.html", media_type="text/html")

    @app.get("/billing.css", include_in_schema=False)
    def billing_css():
        return FileResponse(static_dir / "billing.css", media_type="text/css")

    @app.get("/billing.js", include_in_schema=False)
    def billing_js():
        return FileResponse(static_dir / "billing.js", media_type="application/javascript")

    @app.post("/api/account/verification-codes")
    def account_request_code(payload: dict[str, Any]) -> dict[str, Any]:
        email = str(payload.get("email") or "")
        purpose = str(payload.get("purpose") or "register").strip().lower()
        if purpose not in {"register", "password_reset"}:
            raise GatewayStoreError("invalid_code_purpose", "Unsupported verification code purpose.")
        normalized = store.normalize_account_email(email)
        code = f"{secrets.randbelow(1_000_000):06d}"
        record = store.create_email_code(email=normalized, purpose=purpose, code=code)
        # A real SMTP adapter can consume the same event without exposing codes to clients.
        dev_mode = str(os.environ.get("PACER_ACCOUNT_DEV_CODES") or "").lower() in {"1", "true", "yes", "on"}
        outbox = str(os.environ.get("PACER_ACCOUNT_EMAIL_OUTBOX") or "").strip()
        if outbox:
            path = Path(outbox).resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"email": normalized, "purpose": purpose, "code": code, "expires_at": record["expires_at"]}, ensure_ascii=False) + "\n")
        response: dict[str, Any] = {"status": "accepted", "expires_at": record["expires_at"]}
        if dev_mode:
            response["dev_code"] = code
        audit_event({"event": "account_verification_requested", "email": normalized, "purpose": purpose})
        return response

    @app.post("/api/account/register")
    def account_register(payload: dict[str, Any], response: Response) -> dict[str, Any]:
        email = store.normalize_account_email(str(payload.get("email") or ""))
        if not store.consume_email_code(email=email, purpose="register", code=str(payload.get("verification_code") or payload.get("code") or "")):
            raise GatewayStoreError("invalid_verification_code", "Verification code is invalid or expired.", status_code=400)
        account = store.register_account(email=email, password=str(payload.get("password") or ""), display_name=str(payload.get("display_name") or payload.get("name") or ""))
        session = store.create_login_session(str(account["id"]))
        set_session_cookie(response, session)
        api_key = str(account.pop("api_key") or "")
        return {"account": account, "session": {"expires_in": 2_592_000}, "api_key": api_key, "api_key_notice": "Store this API Key securely. It will not be shown again."}

    @app.post("/api/account/login")
    def account_login(payload: dict[str, Any], response: Response) -> dict[str, Any]:
        account = store.authenticate_account(email=str(payload.get("email") or ""), password=str(payload.get("password") or ""))
        session = store.create_login_session(str(account["id"]))
        set_session_cookie(response, session)
        return {"account": account, "session": {"expires_in": 2_592_000}}

    @app.get("/api/account/me")
    def account_me(request: Request) -> dict[str, Any]:
        token = account_session_token(request)
        if not token:
            raise GatewayStoreError("authentication_required", "Account login is required.", status_code=401)
        account = store.account_from_session(token)
        return {"account": account, "tenant": store.get_tenant(str(account["tenant_id"]))}

    @app.post("/api/account/logout")
    def account_logout(request: Request, response: Response) -> dict[str, str]:
        token = account_session_token(request)
        if token:
            store.revoke_login_session(token)
        response.delete_cookie("pacer_session", path="/")
        return {"status": "signed_out"}

    @app.post("/api/account/password-reset")
    def account_password_reset(payload: dict[str, Any]) -> dict[str, Any]:
        email = store.normalize_account_email(str(payload.get("email") or ""))
        code = str(payload.get("verification_code") or payload.get("code") or "")
        if not store.consume_email_code(email=email, purpose="password_reset", code=code):
            raise GatewayStoreError("invalid_verification_code", "Verification code is invalid or expired.", status_code=400)
        store.reset_account_password(email=email, password=str(payload.get("password") or ""))
        return {"status": "password_reset"}

    def current_account(request: Request) -> dict[str, Any]:
        token = account_session_token(request)
        if not token:
            raise GatewayStoreError("authentication_required", "Account login is required.", status_code=401)
        return store.account_from_session(token)

    @app.get("/api/account/api-keys")
    def account_api_keys(request: Request) -> dict[str, Any]:
        account = current_account(request)
        return {"api_keys": store.list_api_keys(tenant_id=str(account["tenant_id"]))}

    @app.post("/api/account/api-keys")
    def account_create_api_key(payload: dict[str, Any], request: Request) -> dict[str, Any]:
        account = current_account(request)
        result = store.create_api_key(
            tenant_id=str(account["tenant_id"]),
            name=str(payload.get("name") or "个人 API Key"),
            rpm_override=int(payload.get("rpm_override") or 0),
            concurrency_override=int(payload.get("concurrency_override") or 0),
            allowed_models=payload.get("allowed_models") if isinstance(payload.get("allowed_models"), list) else [],
        )
        return {"api_key": result, "notice": "Store the token securely. It will not be shown again."}

    @app.post("/api/account/api-keys/{key_id}/revoke")
    def account_revoke_api_key(key_id: str, request: Request) -> dict[str, Any]:
        account = current_account(request)
        key = store.get_api_key(key_id)
        if str(key.get("tenant_id")) != str(account["tenant_id"]):
            raise GatewayStoreError("key_not_found", "API key was not found.", status_code=404)
        return {"api_key": store.revoke_api_key(key_id)}

    @app.get("/api/gateway/admin/summary")
    def admin_summary(authorization: str = Header(default="")) -> dict[str, Any]:
        require_admin(authorization)
        return {
            "schema_version": 1,
            "database": str(db_path),
            "summary": store.summary(),
            "setup": _setup_status(store),
        }

    @app.get("/api/gateway/admin/plans")
    def admin_list_plans(authorization: str = Header(default="")) -> dict[str, Any]:
        require_admin(authorization)
        return {"plans": store.list_plans()}

    @app.post("/api/gateway/admin/plans")
    def admin_create_plan(
        payload: dict[str, Any], authorization: str = Header(default="")
    ) -> dict[str, Any]:
        require_admin(authorization)
        result = store.create_plan(
            name=str(payload.get("name") or ""),
            monthly_fee_microusd=_integer(payload, "monthly_fee_microusd"),
            included_credit_microusd=_integer(payload, "included_credit_microusd"),
            rpm=_integer(payload, "rpm", 60),
            concurrency=_integer(payload, "concurrency", 2),
        )
        _admin_audit(audit_event, "plan_create", result["id"])
        return {"plan": result}

    @app.get("/api/gateway/admin/tenants")
    def admin_list_tenants(authorization: str = Header(default="")) -> dict[str, Any]:
        require_admin(authorization)
        return {"tenants": store.list_tenants()}

    @app.post("/api/gateway/admin/tenants")
    def admin_create_tenant(
        payload: dict[str, Any], authorization: str = Header(default="")
    ) -> dict[str, Any]:
        require_admin(authorization)
        result = store.create_tenant(
            name=str(payload.get("name") or ""),
            plan_id=str(payload.get("plan_id") or "plan_starter"),
            initial_credit_microusd=_integer(payload, "initial_credit_microusd"),
        )
        _admin_audit(audit_event, "tenant_create", result["id"])
        return {"tenant": result}

    @app.post("/api/gateway/admin/tenants/{tenant_id}/balance")
    def admin_adjust_balance(
        tenant_id: str,
        payload: dict[str, Any],
        authorization: str = Header(default=""),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        require_admin(authorization)
        reference = str(
            idempotency_key or payload.get("external_reference") or ""
        ).strip()
        if not reference:
            raise GatewayStoreError(
                "idempotency_required",
                "Recharge or adjustment requires an idempotency key.",
            )
        result = store.adjust_balance(
            tenant_id=tenant_id,
            amount_microusd=_integer(payload, "amount_microusd"),
            idempotency_key=f"admin:{reference}",
            payment_reference=reference,
            note=str(payload.get("note") or "Manual balance adjustment"),
        )
        _admin_audit(audit_event, "balance_adjust", tenant_id)
        return {"ledger_entry": result, "tenant": store.get_tenant(tenant_id)}

    @app.post("/api/gateway/admin/tenants/{tenant_id}/subscription")
    def admin_renew_subscription(
        tenant_id: str,
        payload: dict[str, Any],
        authorization: str = Header(default=""),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        require_admin(authorization)
        reference = str(
            idempotency_key or payload.get("external_reference") or ""
        ).strip()
        if not reference:
            raise GatewayStoreError(
                "idempotency_required",
                "Subscription confirmation requires an external payment reference.",
            )
        event = store.renew_subscription(
            tenant_id=tenant_id,
            amount_paid_microusd=_integer(payload, "amount_paid_microusd"),
            external_reference=reference,
            period_days=_integer(payload, "period_days", 30),
        )
        _admin_audit(audit_event, "subscription_renew", tenant_id)
        return {"subscription": event, "tenant": store.get_tenant(tenant_id)}

    @app.get("/api/gateway/admin/subscriptions")
    def admin_list_subscriptions(
        tenant_id: str = "",
        limit: int = 100,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        return {
            "items": store.list_subscription_events(tenant_id=tenant_id, limit=limit)
        }

    @app.get("/api/gateway/admin/api-keys")
    def admin_list_api_keys(
        tenant_id: str = "",
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        return {"api_keys": store.list_api_keys(tenant_id=tenant_id)}

    @app.post("/api/gateway/admin/api-keys")
    def admin_create_api_key(
        payload: dict[str, Any],
        response: Response,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        models = payload.get("allowed_models")
        allowed_models = (
            [str(item).strip() for item in models if str(item).strip()]
            if isinstance(models, list)
            else []
        )
        allow_all_models = bool(payload.get("allow_all_models"))
        if not allowed_models and not allow_all_models:
            raise GatewayStoreError(
                "model_scope_required",
                "API key requires allowed_models or explicit allow_all_models=true.",
            )
        expires_at = _optional_float(payload.get("expires_at"))
        if expires_at is None and not bool(payload.get("never_expires")):
            expires_at = time.time() + 90 * 86400
        result = store.create_api_key(
            tenant_id=str(payload.get("tenant_id") or ""),
            name=str(payload.get("name") or "Default key"),
            rpm_override=_integer(payload, "rpm_override"),
            concurrency_override=_integer(payload, "concurrency_override"),
            allowed_models=allowed_models,
            expires_at=expires_at,
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        _admin_audit(audit_event, "api_key_create", result["id"])
        return {
            "api_key": result,
            "warning": "The plaintext token is returned once.",
            "allow_all_models": allow_all_models,
        }

    @app.post("/api/gateway/admin/api-keys/{key_id}/revoke")
    def admin_revoke_api_key(
        key_id: str, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        require_admin(authorization)
        result = store.revoke_api_key(key_id)
        _admin_audit(audit_event, "api_key_revoke", key_id)
        return {"api_key": result}

    @app.get("/api/gateway/admin/upstreams")
    def admin_list_upstreams(authorization: str = Header(default="")) -> dict[str, Any]:
        require_admin(authorization)
        return {"upstreams": store.list_upstreams()}

    @app.post("/api/gateway/admin/upstreams")
    def admin_create_upstream(
        payload: dict[str, Any], authorization: str = Header(default="")
    ) -> dict[str, Any]:
        require_admin(authorization)
        models = payload.get("models")
        result = store.create_upstream(
            name=str(payload.get("name") or ""),
            base_url=str(payload.get("base_url") or ""),
            secret_env=str(payload.get("secret_env") or ""),
            models=[str(item) for item in models] if isinstance(models, list) else [],
            provider=str(payload.get("provider") or "openai-compatible"),
            routing_contract=str(payload.get("routing_contract") or ""),
            priority=_integer(payload, "priority", 100),
            weight=_integer(payload, "weight", 1),
            max_concurrency=_integer(payload, "max_concurrency", 20),
            timeout_seconds=_number(payload, "timeout_seconds", 120),
        )
        _admin_audit(audit_event, "upstream_create", result["id"])
        return {"upstream": result}

    @app.post("/api/gateway/admin/upstreams/{upstream_id}/enabled")
    def admin_set_upstream_enabled(
        upstream_id: str,
        payload: dict[str, Any],
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        result = store.set_upstream_enabled(upstream_id, bool(payload.get("enabled")))
        _admin_audit(audit_event, "upstream_toggle", upstream_id)
        return {"upstream": result}

    @app.post("/api/gateway/admin/upstreams/{upstream_id}/test")
    async def admin_test_upstream(
        upstream_id: str, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        require_admin(authorization)
        import httpx

        from .gateway_proxy import build_upstream_url

        upstream = store.get_upstream(upstream_id)
        token = str(os.environ.get(str(upstream["secret_env"])) or "")
        if not token:
            raise GatewayStoreError(
                "upstream_secret_missing",
                "Configured upstream secret environment variable is empty.",
                status_code=503,
            )
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=min(15.0, float(upstream["timeout_seconds"])),
                follow_redirects=False,
                trust_env=False,
                transport=getattr(app.state, "gateway_transport", None),
            ) as client:
                result = await client.get(
                    build_upstream_url(str(upstream["base_url"]), "/v1/models"),
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            latency = (time.perf_counter() - started) * 1000
            store.record_upstream_result(
                upstream_id, success=False, http_status=0, latency_ms=latency
            )
            return {
                "ok": False,
                "status": 0,
                "latency_ms": latency,
                "error": type(exc).__name__,
            }
        latency = (time.perf_counter() - started) * 1000
        ok = 200 <= result.status_code < 300
        store.record_upstream_result(
            upstream_id,
            success=ok,
            http_status=result.status_code,
            latency_ms=latency,
        )
        return {"ok": ok, "status": result.status_code, "latency_ms": latency}

    @app.get("/api/gateway/admin/prices")
    def admin_list_prices(authorization: str = Header(default="")) -> dict[str, Any]:
        require_admin(authorization)
        return {"prices": store.list_prices()}

    @app.post("/api/gateway/admin/prices")
    def admin_upsert_price(
        payload: dict[str, Any], authorization: str = Header(default="")
    ) -> dict[str, Any]:
        require_admin(authorization)
        result = store.upsert_price(
            model=str(payload.get("model") or ""),
            upstream_model=str(payload.get("upstream_model") or ""),
            input_price_microusd_per_million=_integer(
                payload, "input_price_microusd_per_million"
            ),
            cached_input_price_microusd_per_million=_integer(
                payload, "cached_input_price_microusd_per_million"
            ),
            output_price_microusd_per_million=_integer(
                payload, "output_price_microusd_per_million"
            ),
            upstream_input_cost_microusd_per_million=_integer(
                payload, "upstream_input_cost_microusd_per_million"
            ),
            upstream_output_cost_microusd_per_million=_integer(
                payload, "upstream_output_cost_microusd_per_million"
            ),
            max_output_tokens=_integer(payload, "max_output_tokens", 4096),
            enabled=bool(payload.get("enabled", True)),
        )
        _admin_audit(audit_event, "price_upsert", result["model"])
        return {"price": result}

    @app.get("/api/gateway/admin/requests")
    def admin_list_requests(
        tenant_id: str = "",
        limit: int = 100,
        offset: int = 0,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        return store.list_requests(tenant_id=tenant_id, limit=limit, offset=offset)

    @app.get("/api/gateway/admin/requests/{request_id}/attempts")
    def admin_list_request_attempts(
        request_id: str, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        require_admin(authorization)
        store.get_request(request_id)
        return {"items": store.list_attempts(request_id)}

    @app.post("/api/gateway/admin/requests/{request_id}/reconcile")
    def admin_reconcile_request(
        request_id: str,
        payload: dict[str, Any],
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        actual = payload.get("actual_microusd")
        result = store.reconcile_indeterminate_request(
            request_id,
            action=str(payload.get("action") or ""),
            actual_microusd=(
                None if actual is None else _integer(payload, "actual_microusd")
            ),
            upstream_cost_microusd=_integer(payload, "upstream_cost_microusd"),
            input_tokens=_integer(payload, "input_tokens"),
            cached_input_tokens=_integer(payload, "cached_input_tokens"),
            output_tokens=_integer(payload, "output_tokens"),
        )
        _admin_audit(audit_event, "request_reconcile", request_id)
        return {"request": result}

    @app.get("/api/gateway/admin/ledger")
    def admin_list_ledger(
        tenant_id: str = "",
        limit: int = 100,
        offset: int = 0,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        return store.list_ledger(tenant_id=tenant_id, limit=limit, offset=offset)

    @app.get("/api/gateway/admin/wechat-orders")
    def admin_list_wechat_orders(
        tenant_id: str = "",
        limit: int = 100,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        require_admin(authorization)
        return {"items": store.list_wechat_orders(tenant_id=tenant_id, limit=limit)}

    @app.post("/api/gateway/admin/wechat-orders/{order_id}/reconcile")
    def admin_reconcile_wechat_order(
        order_id: str, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        """Recover a callback-delayed Native order from the provider of record.

        This endpoint never credits from operator-supplied amounts. It queries
        WeChat, then sends the provider transaction through the same strict
        merchant/type/amount/idempotency checks used by callbacks.
        """
        require_admin(authorization)
        order = store.get_wechat_order(order_id, include_code_url=False)
        if order["status"] in {"paid", "closed", "expired", "failed"}:
            return {"status": "terminal", "reconciled": False, "order": order}
        try:
            transaction = resolve_wechat_client().query_order(str(order["out_trade_no"]))
        except WechatNativeError as exc:
            if exc.provider_code not in {"ORDER_NOT_EXIST", "SYSTEM_ERROR", "FREQUENCY_LIMITED"}:
                raise
            current = store.get_wechat_order(order_id, include_code_url=False)
            audit_event(
                {
                    "event": "wechat_native_reconcile_deferred",
                    "order_id": order_id,
                    "provider_code": exc.provider_code,
                }
            )
            return {
                "status": "deferred",
                "reconciled": False,
                "provider_code": exc.provider_code,
                "order": current,
            }
        result = _reconcile_wechat_transaction(
            store=store,
            client=resolve_wechat_client(),
            transaction=transaction,
            audit_event=audit_event,
            source="admin_reconcile",
        )
        _admin_audit(audit_event, "wechat_order_reconcile", order_id)
        return {"status": "reconciled", "reconciled": True, **result}

    @app.get("/api/gateway/billing/packages")
    def billing_packages() -> dict[str, Any]:
        packages = configured_packages()
        status = payment_readiness()
        if getattr(app.state, "wechat_native_client", None) is not None:
            status = {
                "ready": bool(packages),
                "provider": "wechat_native",
                "reason_codes": [] if packages else ["credit_packages_missing"],
                "package_count": len(packages),
            }
        return {"payment": status, "packages": [item.public() for item in packages]}

    @app.get("/api/gateway/billing/me")
    def billing_me(request: Request, authorization: str = Header(default="")) -> dict[str, Any]:
        principal = billing_principal(request, authorization)
        return store.tenant_usage(principal)

    @app.get("/api/gateway/billing/wechat/orders")
    def billing_list_orders(
        request: Request, limit: int = 20, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        principal = billing_principal(request, authorization)
        return {
            "items": store.list_wechat_orders(
                tenant_id=principal.tenant_id, limit=max(1, min(int(limit), 100))
            )
        }

    @app.post("/api/gateway/billing/wechat/orders")
    def billing_create_order(
        payload: dict[str, Any], request: Request, authorization: str = Header(default="")
    ):
        principal = billing_principal(request, authorization)
        package_id = str(payload.get("package_id") or "").strip().lower()
        package = next((item for item in configured_packages() if item.id == package_id), None)
        if package is None:
            raise GatewayStoreError(
                "payment_package_not_found", "Payment package was not found.", status_code=404
            )
        client = resolve_wechat_client()
        expires_at = payment_expiry(minutes=15)
        out_trade_no = generate_out_trade_no()
        order = store.create_wechat_order(
            tenant_id=principal.tenant_id,
            out_trade_no=out_trade_no,
            package_id=package.id,
            package_name=package.name,
            description=package.description,
            amount_fen=package.amount_fen,
            credit_microusd=package.credit_microusd,
            expires_at=expires_at,
        )
        try:
            created = client.create_order(
                out_trade_no=out_trade_no,
                description=package.description,
                amount_fen=package.amount_fen,
                expires_at=expires_at,
            )
            order = store.activate_wechat_order(
                str(order["id"]), code_url=str(created.get("code_url") or "")
            )
        except Exception as exc:
            error_code = exc.code if isinstance(exc, WechatNativeError) else "wechat_order_failed"
            store.fail_wechat_order(str(order["id"]), error_code=error_code)
            raise
        audit_event(
            {
                "event": "wechat_native_order_created",
                "tenant_id": principal.tenant_id,
                "order_id": order["id"],
                "package_id": package.id,
                "amount_fen": package.amount_fen,
            }
        )
        return JSONResponse(
            status_code=201,
            content={
                "order": order,
                "qr_png_data_url": qr_png_data_url(str(order["code_url"])),
            },
        )

    @app.get("/api/gateway/billing/wechat/orders/{order_id}")
    def billing_get_order(
        order_id: str, request: Request, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        principal = billing_principal(request, authorization)
        order = store.get_wechat_order(order_id, tenant_id=principal.tenant_id)
        if order["status"] == "pending" and float(order["expires_at"]) <= time.time():
            try:
                resolve_wechat_client().close_order(str(order["out_trade_no"]))
            except WechatNativeError:
                pass
            order = store.close_wechat_order(order_id, status="expired", error_code="payment_expired")
        elif order["status"] == "pending" and store.claim_wechat_order_refresh(order_id):
            try:
                transaction = resolve_wechat_client().query_order(str(order["out_trade_no"]))
                order = _reconcile_wechat_transaction(
                    store=store,
                    client=resolve_wechat_client(),
                    transaction=transaction,
                    audit_event=audit_event,
                    source="query",
                )["order"]
            except WechatNativeError as exc:
                if exc.provider_code not in {"ORDER_NOT_EXIST", "SYSTEM_ERROR", "FREQUENCY_LIMITED"}:
                    raise
                order = store.get_wechat_order(order_id, tenant_id=principal.tenant_id)
        qr_data_url = ""
        if order["status"] == "pending":
            private_order = store.get_wechat_order(
                order_id,
                tenant_id=principal.tenant_id,
                include_code_url=True,
            )
            qr_data_url = qr_png_data_url(str(private_order["code_url"]))
        return {
            "order": order,
            "tenant": store.get_tenant(principal.tenant_id),
            "qr_png_data_url": qr_data_url,
        }

    @app.post("/api/gateway/billing/wechat/orders/{order_id}/close")
    def billing_close_order(
        order_id: str, request: Request, authorization: str = Header(default="")
    ) -> dict[str, Any]:
        principal = billing_principal(request, authorization)
        order = store.get_wechat_order(order_id, tenant_id=principal.tenant_id)
        if order["status"] == "pending":
            resolve_wechat_client().close_order(str(order["out_trade_no"]))
            order = store.close_wechat_order(order_id)
        return {"order": order}

    @app.post("/api/gateway/billing/wechat/notify")
    async def billing_wechat_notify(request: Request):
        body = await request.body()
        if not body or len(body) > 1_048_576:
            raise WechatNativeError(
                "invalid_notification", "WeChat notification body is invalid.", status_code=400
            )
        client = resolve_wechat_client()
        client.verify_notification(dict(request.headers.items()), body)
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatNativeError(
                "invalid_notification", "WeChat notification JSON is invalid.", status_code=400
            ) from exc
        if not isinstance(payload, dict):
            raise WechatNativeError(
                "invalid_notification", "WeChat notification object is invalid.", status_code=400
            )
        if str(payload.get("event_type") or "") != "TRANSACTION.SUCCESS":
            return Response(status_code=204)
        transaction = client.decrypt_notification(payload)
        _reconcile_wechat_transaction(
            store=store,
            client=client,
            transaction=transaction,
            audit_event=audit_event,
            source="callback",
        )
        return Response(status_code=204)

    @app.get("/api/gateway/me")
    def gateway_me(authorization: str = Header(default="")) -> dict[str, Any]:
        principal = store.authenticate_api_key(bearer_token(authorization))
        return store.tenant_usage(principal)

    @app.get("/v1/models")
    def gateway_models(authorization: str = Header(default="")) -> dict[str, Any]:
        principal = store.authenticate_api_key(bearer_token(authorization))
        configured = {item["model"] for item in store.list_prices(enabled_only=True)}
        available = {
            model
            for upstream in store.list_upstreams()
            if upstream["enabled"] and upstream["secret_configured"]
            for model in upstream["models"]
        }
        models = configured & available
        if principal.allowed_models:
            models &= set(principal.allowed_models)
        return {
            "object": "list",
            "data": [
                {"id": model, "object": "model", "owned_by": "pacer-gateway"}
                for model in sorted(models)
            ],
        }

    @app.post("/v1/chat/completions")
    async def gateway_chat_completions(
        request: Request,
        authorization: str = Header(default=""),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        x_request_id: str = Header(default="", alias="X-Request-Id"),
    ):
        return await proxy.forward(
            request,
            endpoint="/v1/chat/completions",
            authorization=authorization,
            idempotency_key=idempotency_key or x_request_id,
        )

    @app.post("/v1/responses")
    async def gateway_responses(
        request: Request,
        authorization: str = Header(default=""),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        x_request_id: str = Header(default="", alias="X-Request-Id"),
    ):
        return await proxy.forward(
            request,
            endpoint="/v1/responses",
            authorization=authorization,
            idempotency_key=idempotency_key or x_request_id,
        )

    @app.post("/v1/embeddings")
    async def gateway_embeddings(
        request: Request,
        authorization: str = Header(default=""),
        idempotency_key: str = Header(default="", alias="Idempotency-Key"),
        x_request_id: str = Header(default="", alias="X-Request-Id"),
    ):
        return await proxy.forward(
            request,
            endpoint="/v1/embeddings",
            authorization=authorization,
            idempotency_key=idempotency_key or x_request_id,
        )

    return store


def _reconcile_wechat_transaction(
    *,
    store: GatewayStore,
    client: WechatNativeClient,
    transaction: dict[str, Any],
    audit_event: Callable[[dict[str, Any]], None],
    source: str,
) -> dict[str, Any]:
    if not isinstance(transaction, dict):
        raise WechatNativeError(
            "invalid_wechat_transaction",
            "WeChat returned an invalid transaction object.",
        )
    out_trade_no = str(transaction.get("out_trade_no") or "").strip()
    if not out_trade_no:
        raise WechatNativeError(
            "invalid_wechat_transaction",
            "WeChat transaction is missing the merchant order number.",
        )
    order = store.get_wechat_order_by_trade_no(out_trade_no)
    if (
        str(transaction.get("appid") or "") != client.config.app_id
        or str(transaction.get("mchid") or "") != client.config.mch_id
    ):
        raise WechatNativeError(
            "wechat_merchant_mismatch",
            "WeChat transaction does not belong to this application and merchant.",
            status_code=409,
        )
    if str(transaction.get("trade_type") or "").upper() != "NATIVE":
        raise WechatNativeError(
            "wechat_trade_type_mismatch",
            "WeChat transaction is not a Native payment.",
            status_code=409,
        )

    trade_state = str(transaction.get("trade_state") or "").upper()
    if trade_state == "SUCCESS":
        amount = transaction.get("amount")
        if not isinstance(amount, dict):
            raise WechatNativeError(
                "invalid_wechat_transaction",
                "WeChat transaction amount is missing.",
            )
        total = amount.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            raise WechatNativeError(
                "invalid_wechat_transaction",
                "WeChat transaction amount is invalid.",
            )
        currency = str(amount.get("currency") or "").upper()
        transaction_id = str(transaction.get("transaction_id") or "").strip()
        if not transaction_id:
            raise WechatNativeError(
                "invalid_wechat_transaction",
                "WeChat transaction id is missing.",
            )
        result = store.complete_wechat_order(
            out_trade_no=out_trade_no,
            transaction_id=transaction_id,
            amount_fen=total,
            currency=currency,
            provider_payload=transaction,
        )
        audit_event(
            {
                "event": "wechat_native_payment_confirmed",
                "source": str(source or "unknown")[:24],
                "tenant_id": result["order"]["tenant_id"],
                "order_id": result["order"]["id"],
                "amount_fen": result["order"]["amount_fen"],
                "replayed": bool(result["replayed"]),
            }
        )
        return result

    if trade_state in {"NOTPAY", "USERPAYING"}:
        return {
            "order": order,
            "tenant": store.get_tenant(str(order["tenant_id"])),
            "replayed": False,
        }

    terminal_states = {
        "CLOSED": ("closed", "wechat_closed"),
        "REVOKED": ("closed", "wechat_revoked"),
        "PAYERROR": ("failed", "wechat_payerror"),
        "REFUND": ("failed", "wechat_refunded"),
    }
    terminal = terminal_states.get(trade_state)
    if terminal is None:
        raise WechatNativeError(
            "invalid_wechat_transaction",
            "WeChat returned an unsupported transaction state.",
            provider_code=trade_state,
        )
    status, error_code = terminal
    order = store.close_wechat_order(
        str(order["id"]), status=status, error_code=error_code
    )
    audit_event(
        {
            "event": "wechat_native_payment_terminal",
            "source": str(source or "unknown")[:24],
            "tenant_id": order["tenant_id"],
            "order_id": order["id"],
            "provider_state": trade_state,
            "status": order["status"],
        }
    )
    return {
        "order": order,
        "tenant": store.get_tenant(str(order["tenant_id"])),
        "replayed": False,
    }


def _setup_status(store: GatewayStore) -> dict[str, Any]:
    upstreams = store.list_upstreams()
    prices = store.list_prices(enabled_only=True)
    tenants = store.list_tenants()
    now = time.time()
    active_tenant_ids = {
        str(item["id"])
        for item in tenants
        if item.get("status") == "active"
        and item.get("plan_enabled")
        and int(item.get("balance_microusd") or 0) > 0
        and (
            int(item.get("plan_monthly_fee_microusd") or 0) == 0
            or (
                item.get("subscription_expires_at") is not None
                and float(item["subscription_expires_at"]) > now
            )
        )
    }
    priced_models = {str(item["model"]) for item in prices}
    pools: dict[str, list[dict[str, Any]]] = {}
    for upstream in upstreams:
        if not upstream["enabled"] or not upstream["secret_configured"]:
            continue
        for model in upstream["models"]:
            pools.setdefault(str(model), []).append(upstream)
    routed_models = set()
    for model, pool in pools.items():
        contracts = {str(item.get("routing_contract") or "").strip() for item in pool}
        contract_valid = len(pool) == 1 or ("" not in contracts and len(contracts) == 1)
        if contract_valid and any(
            float(item["circuit_open_until"]) <= now for item in pool
        ):
            routed_models.add(model)
    serviceable_models = priced_models & routed_models
    usable_key = any(
        item.get("status") == "active"
        and str(item.get("tenant_id") or "") in active_tenant_ids
        and (item.get("expires_at") is None or float(item["expires_at"]) > now)
        and (
            not item.get("allowed_models")
            or bool(set(item["allowed_models"]) & serviceable_models)
        )
        for item in store.list_api_keys()
    )
    checks = {
        "tenant": bool(tenants),
        "price": bool(prices),
        "upstream": bool(serviceable_models),
        "customer_key": usable_key,
    }
    payment = payment_readiness()
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "payment": payment,
        "serviceable_models": sorted(serviceable_models),
    }


def _admin_audit(
    audit_event: Callable[[dict[str, Any]], None], action: str, resource_id: str
) -> None:
    audit_event(
        {
            "method": "ADMIN",
            "path": "/api/gateway/admin",
            "status": "success",
            "http_status": 200,
            "action": action,
            "resource_id": resource_id,
        }
    )


def _integer(payload: dict[str, Any], key: str, default: int = 0) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool):
        raise GatewayStoreError("invalid_number", f"{key} must be an integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise GatewayStoreError("invalid_number", f"{key} must be an integer.") from exc


def _number(payload: dict[str, Any], key: str, default: float = 0) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool):
        raise GatewayStoreError("invalid_number", f"{key} must be numeric.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise GatewayStoreError("invalid_number", f"{key} must be numeric.") from exc


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise GatewayStoreError(
            "invalid_number", "expires_at must be a Unix timestamp."
        ) from exc
