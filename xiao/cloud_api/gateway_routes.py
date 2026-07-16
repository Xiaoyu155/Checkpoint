from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Header, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .auth import bearer_token
from .gateway_proxy import GatewayProxy
from .gateway_store import GatewayStore, GatewayStoreError

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

    @app.middleware("http")
    async def gateway_security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/gateway"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
            )
        if path.startswith("/api/gateway") or path in _CUSTOMER_API_PATHS:
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
    return {
        "ready": all(checks.values()),
        "checks": checks,
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
