from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cloud_api.auth import generate_api_key
from cloud_api.gateway_proxy import (
    GatewayProxy,
    StreamUsageTracker,
    apply_output_token_limit,
    build_upstream_url,
    estimate_input_tokens,
    extract_token_usage,
    validate_billable_request,
)
from cloud_api.gateway_store import (
    GatewayStore,
    GatewayStoreError,
    calculate_token_charge,
    validate_upstream_base_url,
)
from cloud_api.main import create_app
from cloud_api.setup_gateway import build_env_file, main as setup_gateway


def _admin_headers(monkeypatch) -> dict[str, str]:
    key = generate_api_key(salt="gateway-admin-salt")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", key.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", key.salt)
    return {"Authorization": f"Bearer {key.token}"}


def _app_client(
    tmp_path: Path, monkeypatch
) -> tuple[object, TestClient, dict[str, str]]:
    headers = _admin_headers(monkeypatch)
    app = create_app(
        workspace_root=tmp_path / "workspace", audit_log=tmp_path / "audit.jsonl"
    )
    return app, TestClient(app), headers


def _configured_gateway(
    tmp_path: Path,
    monkeypatch,
    *,
    models: list[str] | None = None,
    initial_credit_microusd: int = 2_000_000,
    rpm: int = 60,
    concurrency: int = 2,
) -> tuple[object, TestClient, dict[str, str], dict[str, object]]:
    models = models or ["gpt-test"]
    monkeypatch.setenv("PACER_TEST_UPSTREAM_KEY", "upstream-secret-value")
    app, client, admin = _app_client(tmp_path, monkeypatch)
    plan = client.post(
        "/api/gateway/admin/plans",
        headers=admin,
        json={"name": "Paid", "rpm": rpm, "concurrency": concurrency},
    ).json()["plan"]
    tenant = client.post(
        "/api/gateway/admin/tenants",
        headers=admin,
        json={
            "name": "Acme",
            "plan_id": plan["id"],
            "initial_credit_microusd": initial_credit_microusd,
        },
    ).json()["tenant"]
    for model in models:
        response = client.post(
            "/api/gateway/admin/prices",
            headers=admin,
            json={
                "model": model,
                "upstream_model": f"upstream-{model}",
                "input_price_microusd_per_million": 2_000_000,
                "cached_input_price_microusd_per_million": 500_000,
                "output_price_microusd_per_million": 10_000_000,
                "upstream_input_cost_microusd_per_million": 1_000_000,
                "upstream_output_cost_microusd_per_million": 5_000_000,
                "max_output_tokens": 4096,
            },
        )
        assert response.status_code == 200
    upstream = client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "primary",
            "base_url": "https://upstream.test/v1",
            "secret_env": "PACER_TEST_UPSTREAM_KEY",
            "models": models,
            "routing_contract": "test-openai-standard",
            "priority": 10,
            "max_concurrency": 5,
        },
    ).json()["upstream"]
    api_key = client.post(
        "/api/gateway/admin/api-keys",
        headers=admin,
        json={
            "tenant_id": tenant["id"],
            "name": "production",
            "allowed_models": models,
        },
    ).json()["api_key"]
    return (
        app,
        client,
        admin,
        {"plan": plan, "tenant": tenant, "upstream": upstream, "api_key": api_key},
    )


def test_token_charge_uses_cached_price_without_double_counting() -> None:
    amount = calculate_token_charge(
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=20,
        input_price_microusd_per_million=2_000_000,
        cached_input_price_microusd_per_million=500_000,
        output_price_microusd_per_million=10_000_000,
    )
    assert amount == 370


def test_gateway_setup_writes_only_admin_hash_and_prints_plaintext_once(
    tmp_path, capsys
) -> None:
    target = tmp_path / ".env.gateway"
    assert setup_gateway(["--env-file", str(target)]) == 0
    output = capsys.readouterr().out
    contents = target.read_text(encoding="utf-8")
    assert "Admin bearer token (shown once): va_cloud_admin_" in output
    token = output.split("Admin bearer token (shown once): ", 1)[1].splitlines()[0]
    assert token not in contents
    assert "VISUAL_AGENT_CLOUD_API_KEY_SHA256=" in contents
    assert "VISUAL_AGENT_CLOUD_API_KEY_SALT=" in contents
    assert "VISUAL_AGENT_CLOUD_ENABLE_TOKEN_ISSUER=0" in contents
    assert "PACER_GATEWAY_MAX_REQUEST_BYTES=10485760" in contents
    assert "PACER_UPSTREAM_PRIMARY_KEY=\n" in contents
    assert "PACER_UPSTREAM_BACKUP_KEY=\n" in contents
    assert "# PACER_ADMIN_LOGIN_EMAIL=" in contents
    assert "# PACER_UPSTREAM_PRIMARY_SIGNUP_EMAIL=" in contents
    assert "# PACER_ACME_EMAIL=" in contents
    assert "# CHECKPOINT_SMTP_PASSWORD=" in contents
    assert "# STRIPE_SECRET_KEY=" in contents
    assert "# PACER_WECHAT_API_V3_KEY=" in contents
    assert "# PACER_WECHAT_NOTIFY_URL=https://" in contents
    assert "# PACER_WECHAT_CREDIT_PACKAGES_JSON=[" in contents
    assert "# ALIPAY_APP_PRIVATE_KEY_PATH=" in contents
    assert token not in build_env_file(salt="salt", sha256="hash")


def test_upstream_url_validation_requires_tls_except_loopback() -> None:
    assert (
        validate_upstream_base_url("https://api.example.com/v1/")
        == "https://api.example.com/v1"
    )
    assert (
        validate_upstream_base_url("http://127.0.0.1:9000/v1")
        == "http://127.0.0.1:9000/v1"
    )
    with pytest.raises(GatewayStoreError, match="HTTPS"):
        validate_upstream_base_url("http://api.example.com/v1")
    with pytest.raises(GatewayStoreError, match="credentials"):
        validate_upstream_base_url("https://user:pass@api.example.com/v1")


def test_usage_helpers_cover_chat_and_responses_shapes() -> None:
    chat = extract_token_usage(
        {
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 5},
            }
        }
    )
    responses = extract_token_usage(
        {
            "response": {
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 7,
                    "input_tokens_details": {"cached_tokens": 4},
                }
            }
        }
    )
    assert chat is not None and (
        chat.input_tokens,
        chat.cached_input_tokens,
        chat.output_tokens,
    ) == (12, 5, 3)
    assert responses is not None and (
        responses.input_tokens,
        responses.cached_input_tokens,
        responses.output_tokens,
    ) == (20, 4, 7)
    assert (
        build_upstream_url("https://api.example/v1", "/v1/responses")
        == "https://api.example/v1/responses"
    )
    assert (
        estimate_input_tokens({"messages": [{"role": "user", "content": "12345678"}]})
        >= 8
    )
    tool_heavy = {
        "messages": [{"role": "user", "content": ""}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "large", "description": "x" * 100_000},
            }
        ],
    }
    assert estimate_input_tokens(tool_heavy) >= 100_000
    assert apply_output_token_limit(
        {"max_tokens": 999_999},
        endpoint="/v1/responses",
        max_output_tokens=4096,
    ) == {"max_output_tokens": 4096}


@pytest.mark.parametrize(
    ("endpoint", "payload", "code"),
    [
        ("/v1/responses", {"background": True}, "background_not_supported"),
        (
            "/v1/responses",
            {"service_tier": "priority"},
            "service_tier_not_supported",
        ),
        (
            "/v1/responses",
            {"tools": [{"type": "web_search_preview"}]},
            "metered_tool_not_supported",
        ),
        (
            "/v1/chat/completions",
            {"web_search_options": {"search_context_size": "low"}},
            "metered_tool_not_supported",
        ),
        (
            "/v1/chat/completions",
            {
                "messages": [
                    {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.test/a.png"},
                            }
                        ]
                    }
                ]
            },
            "modality_not_supported",
        ),
        (
            "/v1/chat/completions",
            {"modalities": ["text", "audio"], "audio": {"format": "wav"}},
            "modality_not_supported",
        ),
    ],
)
def test_gateway_rejects_usage_it_cannot_meter(
    endpoint: str, payload: dict[str, object], code: str
) -> None:
    with pytest.raises(GatewayStoreError) as rejected:
        validate_billable_request(payload, endpoint=endpoint)
    assert rejected.value.code == code


def test_stream_usage_tracker_reads_final_sse_usage() -> None:
    tracker = StreamUsageTracker()
    tracker.feed(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
    tracker.feed(b'data: {"usage":{"prompt_tokens":11,"completion_tokens":2}}\n\n')
    tracker.feed(b"data: [DONE]\n\n")
    tracker.finish()
    usage = tracker.resolved_usage(50)
    assert usage.source == "upstream"
    assert (usage.input_tokens, usage.output_tokens) == (11, 2)


def test_stream_usage_tracker_merges_split_usage_and_requires_terminal() -> None:
    tracker = StreamUsageTracker()
    tracker.feed(b'data: {"usage":{"input_tokens":21}}\n\n')
    tracker.feed(b'data: {"usage":{"output_tokens":8}}\n\n')
    tracker.feed(b'data: {"type":"response.completed"}\n\n')
    tracker.finish()
    usage = tracker.resolved_usage(1)
    assert (usage.input_tokens, usage.output_tokens) == (21, 8)
    assert tracker.terminal_seen is True


def test_lease_heartbeat_retries_transient_sqlite_lock(monkeypatch) -> None:
    real_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    class FlakyStore:
        calls = 0

        def renew_lease(self, _request_id: str, *, lease_seconds: float) -> bool:
            assert lease_seconds == 60
            self.calls += 1
            if self.calls == 1:
                raise sqlite3.OperationalError("database is locked")
            return False

    store = FlakyStore()
    asyncio.run(GatewayProxy(store)._lease_heartbeat("req-test", lease_seconds=60))
    assert store.calls == 2


def test_admin_endpoints_fail_closed_without_admin_auth(tmp_path, monkeypatch) -> None:
    _app, client, _admin = _app_client(tmp_path, monkeypatch)
    assert client.get("/api/gateway/admin/summary").status_code == 401
    assert (
        client.post(
            "/api/gateway/admin/tenants", json={"name": "forbidden"}
        ).status_code
        == 401
    )


def test_gateway_static_and_admin_responses_send_security_headers(
    tmp_path, monkeypatch
) -> None:
    _app, client, admin = _app_client(tmp_path, monkeypatch)
    page = client.get("/gateway")
    summary = client.get("/api/gateway/admin/summary", headers=admin)
    customer_error = client.get("/v1/models")
    assert page.status_code == summary.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert summary.headers["cache-control"] == "no-store"
    assert customer_error.headers["cache-control"] == "no-store"


def test_customer_key_plaintext_is_returned_once_and_not_stored(
    tmp_path, monkeypatch
) -> None:
    _app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    token = str(configured["api_key"]["token"])
    issued = client.post(
        "/api/gateway/admin/api-keys",
        headers=admin,
        json={
            "tenant_id": configured["tenant"]["id"],
            "name": "no-store",
            "allowed_models": ["gpt-test"],
        },
    )
    assert issued.headers["cache-control"] == "no-store"
    listed = client.get("/api/gateway/admin/api-keys", headers=admin).json()["api_keys"]
    assert token not in json.dumps(listed)
    assert all(
        "key_sha256" not in item and "key_salt" not in item and "token" not in item
        for item in listed
    )
    database = tmp_path / "workspace" / "cloud_gateway.db"
    assert token.encode() not in database.read_bytes()


def test_manual_credit_is_idempotent(tmp_path, monkeypatch) -> None:
    _app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    tenant_id = str(configured["tenant"]["id"])
    path = f"/api/gateway/admin/tenants/{tenant_id}/balance"
    first = client.post(
        path,
        headers={**admin, "Idempotency-Key": "bank-42"},
        json={"amount_microusd": 500_000},
    )
    second = client.post(
        path,
        headers={**admin, "Idempotency-Key": "bank-42"},
        json={"amount_microusd": 500_000},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["tenant"]["balance_microusd"] == 2_500_000
    assert second.json()["tenant"]["balance_microusd"] == 2_500_000
    assert second.json()["ledger_entry"]["replayed"] is True


def test_plan_renewal_confirms_payment_and_grants_included_credit_once(
    tmp_path, monkeypatch
) -> None:
    _app, client, admin = _app_client(tmp_path, monkeypatch)
    plan = client.post(
        "/api/gateway/admin/plans",
        headers=admin,
        json={
            "name": "Monthly",
            "monthly_fee_microusd": 9_000_000,
            "included_credit_microusd": 12_000_000,
            "rpm": 100,
            "concurrency": 4,
        },
    ).json()["plan"]
    tenant = client.post(
        "/api/gateway/admin/tenants",
        headers=admin,
        json={"name": "Subscriber", "plan_id": plan["id"]},
    ).json()["tenant"]
    api_key = client.post(
        "/api/gateway/admin/api-keys",
        headers=admin,
        json={"tenant_id": tenant["id"], "allowed_models": ["gpt-test"]},
    ).json()["api_key"]
    store = GatewayStore(tmp_path / "workspace" / "cloud_gateway.db")
    with pytest.raises(GatewayStoreError) as missing_subscription:
        store.authenticate_api_key(str(api_key["token"]))
    assert missing_subscription.value.code == "subscription_required"
    path = f"/api/gateway/admin/tenants/{tenant['id']}/subscription"
    underpaid = client.post(
        path,
        headers={**admin, "Idempotency-Key": "invoice-underpaid"},
        json={"amount_paid_microusd": 8_000_000},
    )
    assert underpaid.status_code == 402
    invalid_period = client.post(
        path,
        headers={**admin, "Idempotency-Key": "invoice-year"},
        json={"amount_paid_microusd": 9_000_000, "period_days": 366},
    )
    assert invalid_period.status_code == 400
    first = client.post(
        path,
        headers={**admin, "Idempotency-Key": "invoice-1001"},
        json={"amount_paid_microusd": 9_000_000, "period_days": 30},
    )
    second = client.post(
        path,
        headers={**admin, "Idempotency-Key": "invoice-1001"},
        json={"amount_paid_microusd": 9_000_000, "period_days": 30},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["tenant"]["balance_microusd"] == 12_000_000
    assert second.json()["tenant"]["balance_microusd"] == 12_000_000
    assert second.json()["subscription"]["replayed"] is True
    assert store.authenticate_api_key(str(api_key["token"])).tenant_id == tenant["id"]
    events = client.get("/api/gateway/admin/subscriptions", headers=admin).json()[
        "items"
    ]
    assert len(events) == 1
    assert events[0]["amount_paid_microusd"] == 9_000_000
    summary = client.get("/api/gateway/admin/summary", headers=admin).json()["summary"]
    assert summary["confirmed_cash_microusd"] == 9_000_000
    assert summary["subscription_cash_microusd"] == 9_000_000
    with sqlite3.connect(tmp_path / "workspace" / "cloud_gateway.db") as conn:
        conn.execute(
            "UPDATE gateway_subscription_events SET period_end = 0 WHERE tenant_id = ?",
            (tenant["id"],),
        )
    with pytest.raises(GatewayStoreError) as expired_subscription:
        store.authenticate_api_key(str(api_key["token"]))
    assert expired_subscription.value.code == "subscription_required"


def test_external_payment_reference_is_global_across_tenants_and_flows(
    tmp_path, monkeypatch
) -> None:
    _app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    first_id = str(configured["tenant"]["id"])
    second = client.post(
        "/api/gateway/admin/tenants",
        headers=admin,
        json={"name": "Second", "plan_id": configured["plan"]["id"]},
    ).json()["tenant"]
    first_path = f"/api/gateway/admin/tenants/{first_id}/balance"
    first = client.post(
        first_path,
        headers={**admin, "Idempotency-Key": "bank-global-42"},
        json={"amount_microusd": 500_000},
    )
    replay = client.post(
        first_path,
        headers={**admin, "Idempotency-Key": "bank-global-42"},
        json={"amount_microusd": 500_000},
    )
    changed_amount = client.post(
        first_path,
        headers={**admin, "Idempotency-Key": "bank-global-42"},
        json={"amount_microusd": 600_000},
    )
    other_tenant = client.post(
        f"/api/gateway/admin/tenants/{second['id']}/balance",
        headers={**admin, "Idempotency-Key": "bank-global-42"},
        json={"amount_microusd": 500_000},
    )
    other_flow = client.post(
        f"/api/gateway/admin/tenants/{first_id}/subscription",
        headers={**admin, "Idempotency-Key": "bank-global-42"},
        json={"amount_paid_microusd": 0},
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json()["ledger_entry"]["replayed"] is True
    assert changed_amount.status_code == 409
    assert other_tenant.status_code == 409
    assert other_flow.status_code == 409
    summary = client.get("/api/gateway/admin/summary", headers=admin).json()["summary"]
    assert summary["confirmed_cash_microusd"] == 500_000
    assert summary["balance_cash_microusd"] == 500_000


def test_nonstream_proxy_settles_exact_usage_and_never_forwards_customer_key(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    customer_token = str(configured["api_key"]["token"])
    unique_prompt = "prompt-must-not-be-persisted-92831"
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer upstream-secret-value"
        assert customer_token not in request.headers["authorization"]
        assert "openai-organization" not in request.headers
        assert "anthropic-beta" not in request.headers
        payload = json.loads(request.content)
        assert payload["model"] == "upstream-gpt-test"
        assert payload["messages"][0]["content"] == unique_prompt
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 20},
                },
            },
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {customer_token}",
            "Idempotency-Key": "chat-1",
            "OpenAI-Organization": "customer-controlled-org",
            "Anthropic-Beta": "customer-controlled-beta",
        },
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": unique_prompt}],
            "max_tokens": 100,
        },
    )
    assert response.status_code == 200
    assert response.headers["x-pacer-cost-microusd"] == "370"
    assert response.headers["x-pacer-usage-source"] == "upstream"
    assert len(calls) == 1
    requests = client.get("/api/gateway/admin/requests", headers=admin).json()["items"]
    assert requests[0]["status"] == "settled"
    assert requests[0]["actual_microusd"] == 370
    assert requests[0]["upstream_cost_microusd"] == 200
    assert requests[0]["cached_input_tokens"] == 20
    tenant = client.get("/api/gateway/admin/tenants", headers=admin).json()["tenants"][
        0
    ]
    assert tenant["balance_microusd"] == 2_000_000 - 370
    assert (
        unique_prompt.encode()
        not in (tmp_path / "workspace" / "cloud_gateway.db").read_bytes()
    )


def test_duplicate_idempotency_key_does_not_call_upstream_or_charge_twice(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    customer_token = str(configured["api_key"]["token"])
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    headers = {
        "Authorization": f"Bearer {customer_token}",
        "Idempotency-Key": "same-operation",
    }
    payload = {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 10,
    }
    first = client.post("/v1/chat/completions", headers=headers, json=payload)
    second = client.post("/v1/chat/completions", headers=headers, json=payload)
    conflict = client.post(
        "/v1/chat/completions",
        headers=headers,
        json={**payload, "messages": [{"role": "user", "content": "different"}]},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_request"
    assert second.json()["error"]["request_id"] == first.headers["x-pacer-request-id"]
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert calls == 1
    assert client.get("/api/gateway/admin/requests", headers=admin).json()["total"] == 1


def test_billable_request_requires_stable_idempotency_key(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"usage": {"input_tokens": 1}})

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {configured['api_key']['token']}"},
        json={"model": "gpt-test", "input": "hello"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_required"
    assert calls == 0
    assert client.get("/api/gateway/admin/requests", headers=admin).json()["total"] == 0


def test_proxy_clamps_forwarded_output_limit_to_reserved_limit(
    tmp_path, monkeypatch
) -> None:
    app, client, _admin, configured = _configured_gateway(tmp_path, monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["max_output_tokens"] == 4096
        assert "max_tokens" not in payload
        return httpx.Response(
            200,
            json={"output": [], "usage": {"input_tokens": 10, "output_tokens": 1}},
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    token = str(configured["api_key"]["token"])
    response = client.post(
        "/v1/responses",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "clamped-output",
        },
        json={"model": "gpt-test", "input": "hello", "max_tokens": 999_999},
    )
    assert response.status_code == 200


def test_chat_multiple_choices_reserves_total_output_and_preserves_margin(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["n"] == 10
        assert payload["max_tokens"] == 10
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 100},
            },
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {configured['api_key']['token']}",
            "Idempotency-Key": "ten-choices",
        },
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "n": 10,
            "max_tokens": 10,
        },
    )
    assert response.status_code == 200
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["reserved_microusd"] >= item["actual_microusd"]
    assert item["actual_microusd"] >= item["upstream_cost_microusd"]


def test_embeddings_reserve_input_only(tmp_path, monkeypatch) -> None:
    app, client, admin, configured = _configured_gateway(
        tmp_path, monkeypatch, models=["embed-test"]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "max_tokens" not in payload
        assert "max_output_tokens" not in payload
        return httpx.Response(
            200,
            json={
                "data": [{"embedding": [0.1, 0.2], "index": 0}],
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            },
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/embeddings",
        headers={
            "Authorization": f"Bearer {configured['api_key']['token']}",
            "Idempotency-Key": "embedding-input-only",
        },
        json={"model": "embed-test", "input": "hello"},
    )
    assert response.status_code == 200
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["reserved_microusd"] < 10_000
    assert item["output_tokens"] == 0


def test_upstream_health_check_uses_secret_without_returning_it(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer upstream-secret-value"
        return httpx.Response(200, json={"object": "list", "data": []})

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        f"/api/gateway/admin/upstreams/{configured['upstream']['id']}/test",
        headers=admin,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "upstream-secret-value" not in response.text


def test_gateway_rejects_non_openai_compatible_upstream(tmp_path, monkeypatch) -> None:
    _app, client, admin = _app_client(tmp_path, monkeypatch)
    response = client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "wrong adapter",
            "provider": "anthropic",
            "base_url": "https://anthropic.test/v1",
            "secret_env": "PACER_ANTHROPIC_KEY",
            "models": ["claude"],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_upstream_provider"


def test_overlapping_upstreams_require_same_routing_contract(
    tmp_path, monkeypatch
) -> None:
    _app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("PACER_TEST_CONFLICT_KEY", "conflict-secret")
    conflict = client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "different-contract",
            "base_url": "https://conflict.test/v1",
            "secret_env": "PACER_TEST_CONFLICT_KEY",
            "models": ["gpt-test"],
            "routing_contract": "other-alias-or-cost-contract",
        },
    )
    assert conflict.status_code == 200
    store = GatewayStore(tmp_path / "workspace" / "cloud_gateway.db")
    assert store.eligible_upstreams("gpt-test") == []
    store.record_upstream_result(
        str(configured["upstream"]["id"]),
        success=False,
        http_status=500,
        latency_ms=1,
    )
    assert store.eligible_upstreams("gpt-test") == []
    summary = client.get("/api/gateway/admin/summary", headers=admin).json()
    assert summary["setup"]["ready"] is False
    response = client.post(
        "/v1/responses",
        headers={
            "Authorization": f"Bearer {configured['api_key']['token']}",
            "Idempotency-Key": "blocked-mixed-contract",
        },
        json={"model": "gpt-test", "input": "hello"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"


def test_proxy_fails_over_before_output_and_charges_once(tmp_path, monkeypatch) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("PACER_TEST_BACKUP_KEY", "backup-secret")
    client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "backup",
            "base_url": "https://backup.test/v1",
            "secret_env": "PACER_TEST_BACKUP_KEY",
            "models": ["gpt-test"],
            "routing_contract": "test-openai-standard",
            "priority": 20,
        },
    )
    hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "upstream.test":
            return httpx.Response(503, json={"error": "overloaded"})
        assert request.headers["authorization"] == "Bearer backup-secret"
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    token = str(configured["api_key"]["token"])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "failover-1"},
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
        },
    )
    assert response.status_code == 200
    assert hosts == ["upstream.test", "backup.test"]
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["attempt_count"] == 2
    assert item["status"] == "settled"
    attempts = client.get(
        f"/api/gateway/admin/requests/{item['id']}/attempts", headers=admin
    ).json()["items"]
    assert [attempt["status"] for attempt in attempts] == ["http_error", "success"]
    ledger = client.get("/api/gateway/admin/ledger", headers=admin).json()["items"]
    assert [entry["kind"] for entry in ledger].count("reserve") == 1
    assert [entry["kind"] for entry in ledger].count("settlement") == 1
    upstreams = client.get("/api/gateway/admin/upstreams", headers=admin).json()[
        "upstreams"
    ]
    primary = next(item for item in upstreams if item["name"] == "primary")
    assert primary["consecutive_failures"] == 1
    assert primary["circuit_open_until"] > 0


def test_capacity_race_on_primary_reselects_backup(tmp_path, monkeypatch) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("PACER_TEST_BACKUP_KEY", "backup-secret")
    backup = client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "capacity-backup",
            "base_url": "https://capacity-backup.test/v1",
            "secret_env": "PACER_TEST_BACKUP_KEY",
            "models": ["gpt-test"],
            "routing_contract": "test-openai-standard",
            "priority": 20,
        },
    ).json()["upstream"]
    original_begin = app.state.gateway_store.begin_request
    raced = False

    def racing_begin(**kwargs):
        nonlocal raced
        if kwargs["upstream_id"] == configured["upstream"]["id"] and not raced:
            raced = True
            raise GatewayStoreError(
                "upstream_busy", "capacity changed", status_code=503
            )
        return original_begin(**kwargs)

    monkeypatch.setattr(app.state.gateway_store, "begin_request", racing_begin)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "capacity-backup.test"
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            },
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    token = str(configured["api_key"]["token"])
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "capacity-race",
        },
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["upstream_id"] == backup["id"]


def test_nonstream_read_error_stops_failover_and_requires_reconciliation(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("PACER_TEST_BACKUP_KEY", "backup-secret")
    client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "read-backup",
            "base_url": "https://read-backup.test/v1",
            "secret_env": "PACER_TEST_BACKUP_KEY",
            "models": ["gpt-test"],
            "routing_contract": "test-openai-standard",
            "priority": 20,
        },
    )

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise httpx.ReadError("connection reset")
            yield b""  # pragma: no cover

    backup_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal backup_calls
        if request.url.host == "upstream.test":
            return httpx.Response(200, stream=BrokenStream())
        backup_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    token = str(configured["api_key"]["token"])
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "read-failover",
        },
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 10,
        },
    )
    assert response.status_code == 502
    assert backup_calls == 0
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["status"] == "indeterminate"
    assert item["attempt_count"] == 1
    assert item["error_code"] == "upstream_read_indeterminate"
    reconciled = client.post(
        f"/api/gateway/admin/requests/{item['id']}/reconcile",
        headers=admin,
        json={
            "action": "capture",
            "actual_microusd": 100,
            "upstream_cost_microusd": 50,
            "input_tokens": 8,
            "output_tokens": 1,
        },
    )
    assert reconciled.status_code == 200
    captured = reconciled.json()["request"]
    assert captured["status"] == "settled"
    assert captured["actual_microusd"] == 100
    assert captured["upstream_cost_microusd"] == 50
    attempt = client.get(
        f"/api/gateway/admin/requests/{item['id']}/attempts", headers=admin
    ).json()["items"][0]
    assert attempt["status"] == "indeterminate"
    assert attempt["upstream_cost_microusd"] == 50


def test_connect_failure_can_fail_over_before_provider_acceptance(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    monkeypatch.setenv("PACER_TEST_CONNECT_BACKUP_KEY", "backup-secret")
    client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "connect-backup",
            "base_url": "https://connect-backup.test/v1",
            "secret_env": "PACER_TEST_CONNECT_BACKUP_KEY",
            "models": ["gpt-test"],
            "routing_contract": "test-openai-standard",
            "priority": 20,
        },
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upstream.test":
            raise httpx.ConnectError("dial failed", request=request)
        return httpx.Response(
            200,
            headers={"X-Request-Id": "provider-backup-1"},
            json={
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 1},
            },
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {configured['api_key']['token']}",
            "Idempotency-Key": "safe-connect-failover",
        },
        json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    attempts = client.get(
        f"/api/gateway/admin/requests/{item['id']}/attempts", headers=admin
    ).json()["items"]
    assert [attempt["status"] for attempt in attempts] == ["network_error", "success"]
    assert attempts[1]["upstream_request_id"] == "provider-backup-1"
    assert attempts[1]["upstream_cost_microusd"] > 0


def test_success_without_usage_is_indeterminate_instead_of_estimated(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Request-Id": "provider-no-usage"},
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {configured['api_key']['token']}",
            "Idempotency-Key": "no-usage-json",
        },
        json={"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 502
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["status"] == "indeterminate"
    assert item["actual_microusd"] == 0
    assert item["error_code"] == "upstream_usage_missing"


def test_all_upstreams_failing_releases_full_reservation(tmp_path, monkeypatch) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, headers={"Retry-After": "5"}, json={"error": "rate limited"}
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    token = str(configured["api_key"]["token"])
    response = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "rate-limited"},
        json={"model": "gpt-test", "input": "hello", "max_output_tokens": 100},
    )
    assert response.status_code == 429
    tenant = client.get("/api/gateway/admin/tenants", headers=admin).json()["tenants"][
        0
    ]
    assert tenant["balance_microusd"] == 2_000_000
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["status"] == "failed"
    assert item["actual_microusd"] == 0
    ledger = client.get("/api/gateway/admin/ledger", headers=admin).json()["items"]
    assert {entry["kind"] for entry in ledger} >= {"reserve", "release"}


def test_streaming_proxy_settles_usage_from_final_event(tmp_path, monkeypatch) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    sse = (
        'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        'data: {"usage":{"prompt_tokens":30,"completion_tokens":4,"prompt_tokens_details":{"cached_tokens":10}}}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=sse
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    token = str(configured["api_key"]["token"])
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "stream-1"},
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 50,
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert response.content == sse
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["status"] == "settled"
    assert item["usage_source"] == "upstream"
    assert (
        item["input_tokens"],
        item["cached_input_tokens"],
        item["output_tokens"],
    ) == (30, 10, 4)


def test_stream_tail_error_after_terminal_usage_still_settles(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)

    class TerminalThenReset(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\n'
            yield b"data: [DONE]\n\n"
            raise httpx.ReadError("tail reset after terminal")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=TerminalThenReset(),
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {configured['api_key']['token']}",
            "Idempotency-Key": "terminal-then-reset",
        },
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["status"] == "settled"
    assert (item["input_tokens"], item["output_tokens"]) == (12, 3)
    attempts = client.get(
        f"/api/gateway/admin/requests/{item['id']}/attempts", headers=admin
    ).json()["items"]
    assert attempts[0]["status"] == "success"


def test_stream_without_terminal_event_is_not_marked_as_upstream_success(
    tmp_path, monkeypatch
) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    token = str(configured["api_key"]["token"])
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "missing-terminal",
        },
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert "stream_terminal_missing" in response.text
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["status"] == "indeterminate"
    assert item["error_code"] == "stream_terminal_missing"
    attempt = client.get(
        f"/api/gateway/admin/requests/{item['id']}/attempts", headers=admin
    ).json()["items"][0]
    assert attempt["status"] == "indeterminate"


def test_stream_terminal_without_usage_is_indeterminate(tmp_path, monkeypatch) -> None:
    app, client, admin, configured = _configured_gateway(tmp_path, monkeypatch)
    sse = (
        'data: {"choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n'
    ).encode()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=sse
        )

    app.state.gateway_transport = httpx.MockTransport(handler)
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {configured['api_key']['token']}",
            "Idempotency-Key": "stream-no-usage",
        },
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        },
    )
    assert response.status_code == 200
    assert "upstream_usage_missing" in response.text
    item = client.get("/api/gateway/admin/requests", headers=admin).json()["items"][0]
    assert item["status"] == "indeterminate"
    assert item["actual_microusd"] == 0


def test_client_disconnect_still_drains_terminal_usage_and_settles(
    tmp_path, monkeypatch
) -> None:
    _app, _client, _admin, configured = _configured_gateway(tmp_path, monkeypatch)
    store = GatewayStore(tmp_path / "workspace" / "cloud_gateway.db")
    principal = store.authenticate_api_key(str(configured["api_key"]["token"]))
    request = store.begin_request(
        principal=principal,
        upstream_id=str(configured["upstream"]["id"]),
        endpoint="/v1/chat/completions",
        model="gpt-test",
        idempotency_key="disconnect-drain",
        streaming=True,
        estimated_input_tokens=10,
        max_output_tokens=20,
        lease_seconds=60,
    )
    attempt = store.start_attempt(
        str(request["id"]),
        str(configured["upstream"]["id"]),
        lease_seconds=60,
    )
    closed: list[str] = []

    class FakeResponse:
        status_code = 200

        async def aclose(self) -> None:
            closed.append("response")

    class FakeClient:
        async def aclose(self) -> None:
            closed.append("client")

    async def remainder():
        await asyncio.sleep(0.03)
        yield b'data: {"usage":{"prompt_tokens":42,"completion_tokens":9}}\n\n'
        yield b"data: [DONE]\n\n"

    async def scenario() -> None:
        stream = GatewayProxy(store)._stream_response(
            first_chunk=b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
            iterator=remainder(),
            response=FakeResponse(),
            client=FakeClient(),
            request_id=str(request["id"]),
            upstream_id=str(configured["upstream"]["id"]),
            attempt_id=str(attempt["id"]),
            input_estimate=10,
            started=0,
            lease_seconds=60,
        )
        assert await anext(stream)
        await stream.aclose()
        for _ in range(100):
            if store.get_request(str(request["id"]))[
                "status"
            ] == "settled" and closed == ["response", "client"]:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    settled = store.get_request(str(request["id"]))
    assert settled["status"] == "settled"
    assert (settled["input_tokens"], settled["output_tokens"]) == (42, 9)
    assert closed == ["response", "client"]


def test_models_and_me_are_scoped_to_customer_key(tmp_path, monkeypatch) -> None:
    _app, client, admin, configured = _configured_gateway(
        tmp_path, monkeypatch, models=["gpt-a", "gpt-b"]
    )
    restricted = client.post(
        "/api/gateway/admin/api-keys",
        headers=admin,
        json={
            "tenant_id": configured["tenant"]["id"],
            "name": "restricted",
            "allowed_models": ["gpt-b"],
        },
    )
    assert restricted.status_code == 200
    token = restricted.json()["api_key"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    models = client.get("/v1/models", headers=headers)
    me = client.get("/api/gateway/me", headers=headers)
    assert models.status_code == me.status_code == 200
    assert [item["id"] for item in models.json()["data"]] == ["gpt-b"]
    assert me.json()["tenant_id"] == configured["tenant"]["id"]


def test_readiness_requires_model_route_intersection_and_usable_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("PACER_TEST_UPSTREAM_KEY", "configured")
    _app, client, admin = _app_client(tmp_path, monkeypatch)
    tenant = client.post(
        "/api/gateway/admin/tenants",
        headers=admin,
        json={"name": "Readiness"},
    ).json()["tenant"]
    client.post(
        "/api/gateway/admin/prices",
        headers=admin,
        json={
            "model": "priced-only",
            "input_price_microusd_per_million": 1_000_000,
            "output_price_microusd_per_million": 1_000_000,
        },
    )
    client.post(
        "/api/gateway/admin/upstreams",
        headers=admin,
        json={
            "name": "other-model",
            "base_url": "https://other.test/v1",
            "secret_env": "PACER_TEST_UPSTREAM_KEY",
            "models": ["routed-only"],
        },
    )
    key = client.post(
        "/api/gateway/admin/api-keys",
        headers=admin,
        json={"tenant_id": tenant["id"], "allowed_models": ["priced-only"]},
    ).json()["api_key"]
    assert key["token"]
    assert (
        client.get("/api/gateway/admin/summary", headers=admin).json()["setup"]["ready"]
        is False
    )
    client.post(
        "/api/gateway/admin/prices",
        headers=admin,
        json={
            "model": "routed-only",
            "input_price_microusd_per_million": 1_000_000,
            "output_price_microusd_per_million": 1_000_000,
        },
    )
    assert (
        client.get("/api/gateway/admin/summary", headers=admin).json()["setup"]["ready"]
        is False
    )
    client.post(f"/api/gateway/admin/api-keys/{key['id']}/revoke", headers=admin)
    assert (
        client.get("/api/gateway/admin/summary", headers=admin).json()["setup"]["ready"]
        is False
    )


def test_store_enforces_concurrency_and_insufficient_balance(
    tmp_path, monkeypatch
) -> None:
    _app, _client, _admin, configured = _configured_gateway(
        tmp_path,
        monkeypatch,
        initial_credit_microusd=100,
        concurrency=1,
    )
    store = GatewayStore(tmp_path / "workspace" / "cloud_gateway.db")
    principal = store.authenticate_api_key(str(configured["api_key"]["token"]))
    with pytest.raises(GatewayStoreError) as insufficient:
        store.begin_request(
            principal=principal,
            upstream_id=str(configured["upstream"]["id"]),
            endpoint="/v1/responses",
            model="gpt-test",
            idempotency_key="too-expensive",
            streaming=False,
            estimated_input_tokens=100,
            max_output_tokens=100,
            lease_seconds=60,
        )
    assert insufficient.value.code == "insufficient_balance"

    store.adjust_balance(
        tenant_id=principal.tenant_id, amount_microusd=10_000, idempotency_key="topup"
    )
    store.begin_request(
        principal=principal,
        upstream_id=str(configured["upstream"]["id"]),
        endpoint="/v1/responses",
        model="gpt-test",
        idempotency_key="held",
        streaming=False,
        estimated_input_tokens=1,
        max_output_tokens=10,
        lease_seconds=60,
    )
    with pytest.raises(GatewayStoreError) as concurrent:
        store.begin_request(
            principal=principal,
            upstream_id=str(configured["upstream"]["id"]),
            endpoint="/v1/responses",
            model="gpt-test",
            idempotency_key="second",
            streaming=False,
            estimated_input_tokens=1,
            max_output_tokens=10,
            lease_seconds=60,
        )
    assert concurrent.value.code == "concurrency_limit_exceeded"
    second_key = store.create_api_key(
        tenant_id=principal.tenant_id,
        name="second key",
        allowed_models=["gpt-test"],
    )
    second_principal = store.authenticate_api_key(str(second_key["token"]))
    with pytest.raises(GatewayStoreError) as tenant_concurrent:
        store.begin_request(
            principal=second_principal,
            upstream_id=str(configured["upstream"]["id"]),
            endpoint="/v1/responses",
            model="gpt-test",
            idempotency_key="second-key",
            streaming=False,
            estimated_input_tokens=1,
            max_output_tokens=10,
            lease_seconds=60,
        )
    assert tenant_concurrent.value.code == "concurrency_limit_exceeded"


def test_expired_lease_is_failed_and_reservation_is_released(
    tmp_path, monkeypatch
) -> None:
    _app, _client, _admin, configured = _configured_gateway(tmp_path, monkeypatch)
    database = tmp_path / "workspace" / "cloud_gateway.db"
    store = GatewayStore(database)
    token = str(configured["api_key"]["token"])
    principal = store.authenticate_api_key(token)
    before = store.get_tenant(principal.tenant_id)["balance_microusd"]
    request = store.begin_request(
        principal=principal,
        upstream_id=str(configured["upstream"]["id"]),
        endpoint="/v1/responses",
        model="gpt-test",
        idempotency_key="expires",
        streaming=False,
        estimated_input_tokens=10,
        max_output_tokens=10,
        lease_seconds=60,
    )
    assert store.get_tenant(principal.tenant_id)["balance_microusd"] < before
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE gateway_leases SET expires_at = 0 WHERE request_id = ?",
            (request["id"],),
        )
    assert store.recover_expired_reservations() == 1
    recovered = store.get_request(str(request["id"]))
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "lease_expired"
    assert store.get_tenant(principal.tenant_id)["balance_microusd"] == before
    assert store.recover_expired_reservations() == 0


def test_begin_request_commits_expired_refund_before_new_reservation(
    tmp_path, monkeypatch
) -> None:
    _app, _client, _admin, configured = _configured_gateway(
        tmp_path, monkeypatch, initial_credit_microusd=150
    )
    database = tmp_path / "workspace" / "cloud_gateway.db"
    store = GatewayStore(database)
    principal = store.authenticate_api_key(str(configured["api_key"]["token"]))
    first = store.begin_request(
        principal=principal,
        upstream_id=str(configured["upstream"]["id"]),
        endpoint="/v1/responses",
        model="gpt-test",
        idempotency_key="expired-before-second",
        streaming=False,
        estimated_input_tokens=1,
        max_output_tokens=10,
        lease_seconds=60,
    )
    assert first["reserved_microusd"] == 104
    assert store.get_tenant(principal.tenant_id)["balance_microusd"] == 46
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE gateway_leases SET expires_at = 0 WHERE request_id = ?",
            (first["id"],),
        )
    second = store.begin_request(
        principal=principal,
        upstream_id=str(configured["upstream"]["id"]),
        endpoint="/v1/responses",
        model="gpt-test",
        idempotency_key="second-after-refund",
        streaming=False,
        estimated_input_tokens=1,
        max_output_tokens=10,
        lease_seconds=60,
    )
    assert second["reserved_microusd"] == 104
    assert store.get_request(str(first["id"]))["status"] == "failed"
    assert store.get_tenant(principal.tenant_id)["balance_microusd"] == 46


def test_expired_request_started_upstream_requires_manual_reconciliation(
    tmp_path, monkeypatch
) -> None:
    _app, _client, _admin, configured = _configured_gateway(tmp_path, monkeypatch)
    database = tmp_path / "workspace" / "cloud_gateway.db"
    store = GatewayStore(database)
    principal = store.authenticate_api_key(str(configured["api_key"]["token"]))
    before = store.get_tenant(principal.tenant_id)["balance_microusd"]
    request = store.begin_request(
        principal=principal,
        upstream_id=str(configured["upstream"]["id"]),
        endpoint="/v1/responses",
        model="gpt-test",
        idempotency_key="indeterminate",
        streaming=True,
        estimated_input_tokens=10,
        max_output_tokens=10,
        lease_seconds=60,
    )
    store.start_attempt(
        str(request["id"]),
        str(configured["upstream"]["id"]),
        lease_seconds=60,
    )
    held_balance = store.get_tenant(principal.tenant_id)["balance_microusd"]
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE gateway_leases SET expires_at = 0 WHERE request_id = ?",
            (request["id"],),
        )
    store.recover_expired_reservations()
    assert store.get_request(str(request["id"]))["status"] == "indeterminate"
    assert store.get_tenant(principal.tenant_id)["balance_microusd"] == held_balance
    reconciled = store.reconcile_indeterminate_request(
        str(request["id"]), action="release"
    )
    assert reconciled["status"] == "failed"
    assert reconciled["error_code"] == "manual_release"
    assert store.get_tenant(principal.tenant_id)["balance_microusd"] == before


def test_settlement_never_charges_above_reservation_or_makes_balance_negative(
    tmp_path, monkeypatch
) -> None:
    _app, _client, _admin, configured = _configured_gateway(
        tmp_path, monkeypatch, initial_credit_microusd=1_000
    )
    store = GatewayStore(tmp_path / "workspace" / "cloud_gateway.db")
    principal = store.authenticate_api_key(str(configured["api_key"]["token"]))
    request = store.begin_request(
        principal=principal,
        upstream_id=str(configured["upstream"]["id"]),
        endpoint="/v1/responses",
        model="gpt-test",
        idempotency_key="provider-overreports",
        streaming=False,
        estimated_input_tokens=1,
        max_output_tokens=1,
        lease_seconds=60,
    )
    settled = store.settle_request(
        str(request["id"]),
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=100_000,
        usage_source="upstream",
        http_status=200,
        latency_ms=1,
    )
    assert settled["actual_microusd"] == request["reserved_microusd"]
    assert settled["error_code"] == "charge_capped_at_reservation"
    assert store.get_tenant(principal.tenant_id)["balance_microusd"] >= 0
