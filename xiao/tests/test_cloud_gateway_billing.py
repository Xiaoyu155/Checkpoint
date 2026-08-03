from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from cloud_api.auth import generate_api_key
from cloud_api.gateway_store import GatewayStore
from cloud_api.main import create_app
from cloud_api.wechat_native import WechatNativeError


PACKAGES = json.dumps(
    [
        {
            "id": "starter",
            "name": "Starter credit",
            "description": "Pacer API credit",
            "amount_fen": 100,
            "credit_microusd": 2_000_000,
        }
    ]
)
ROOT = Path(__file__).resolve().parents[1]


class FakeWechatClient:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            app_id="wx0123456789abcdef", mch_id="1900000109"
        )
        self.orders: dict[str, dict[str, Any]] = {}
        self.state = "NOTPAY"
        self.amount_delta = 0
        self.callback_transaction: dict[str, Any] | None = None
        self.query_calls = 0
        self.close_calls = 0

    def create_order(
        self,
        *,
        out_trade_no: str,
        description: str,
        amount_fen: int,
        expires_at: float,
    ) -> dict[str, str]:
        self.orders[out_trade_no] = {
            "description": description,
            "amount_fen": amount_fen,
            "expires_at": expires_at,
        }
        return {"code_url": f"weixin://wxpay/bizpayurl?pr={out_trade_no}"}

    def transaction(self, out_trade_no: str) -> dict[str, Any]:
        order = self.orders[out_trade_no]
        result = {
            "appid": self.config.app_id,
            "mchid": self.config.mch_id,
            "out_trade_no": out_trade_no,
            "trade_type": "NATIVE",
            "trade_state": self.state,
            "amount": {
                "total": int(order["amount_fen"]) + self.amount_delta,
                "currency": "CNY",
            },
        }
        if self.state == "SUCCESS":
            result["transaction_id"] = f"420000000000{out_trade_no[-16:]}"
        return result

    def query_order(self, out_trade_no: str) -> dict[str, Any]:
        self.query_calls += 1
        return self.transaction(out_trade_no)

    def close_order(self, out_trade_no: str) -> None:
        self.close_calls += 1
        self.state = "CLOSED"

    def verify_notification(self, headers, _body) -> None:
        if headers.get("wechatpay-signature") != "valid-test-signature":
            raise WechatNativeError(
                "wechat_signature_invalid", "Invalid signature.", status_code=401
            )

    def decrypt_notification(self, _payload) -> dict[str, Any]:
        assert self.callback_transaction is not None
        return self.callback_transaction


def _billing_client(
    tmp_path: Path,
    monkeypatch,
    *,
    monthly_fee_microusd: int = 0,
) -> tuple[TestClient, GatewayStore, FakeWechatClient, dict[str, Any], dict[str, str]]:
    monkeypatch.setenv("PACER_WECHAT_CREDIT_PACKAGES_JSON", PACKAGES)
    app = create_app(
        workspace_root=tmp_path / "workspace",
        audit_log=tmp_path / "billing-audit.jsonl",
    )
    provider = FakeWechatClient()
    app.state.wechat_native_client = provider
    store: GatewayStore = app.state.gateway_store
    plan_id = "plan_starter"
    if monthly_fee_microusd:
        plan = store.create_plan(
            name="Paid",
            monthly_fee_microusd=monthly_fee_microusd,
            included_credit_microusd=0,
        )
        plan_id = str(plan["id"])
    tenant = store.create_tenant(name="Billing customer", plan_id=plan_id)
    api_key = store.create_api_key(tenant_id=str(tenant["id"]), name="Billing")
    headers = {"Authorization": f"Bearer {api_key['token']}"}
    return TestClient(app), store, provider, tenant, headers


def _create_order(client: TestClient, headers: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        "/api/gateway/billing/wechat/orders",
        headers=headers,
        json={"package_id": "starter", "amount_fen": 1},
    )
    assert response.status_code == 201
    return response.json()


def test_billing_page_and_api_send_strict_browser_headers(tmp_path, monkeypatch) -> None:
    client, _store, _provider, _tenant, headers = _billing_client(
        tmp_path, monkeypatch
    )
    page = client.get("/billing")
    me = client.get("/api/gateway/billing/me", headers=headers)
    assert page.status_code == me.status_code == 200
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert me.headers["cache-control"] == "no-store"
    assert "Pacer 额度中心" in page.text


def test_admin_readiness_reports_wechat_payment_separately(
    tmp_path, monkeypatch
) -> None:
    admin = generate_api_key(salt="billing-readiness-test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", admin.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", admin.salt)
    client, _store, _provider, _tenant, _headers = _billing_client(tmp_path, monkeypatch)

    response = client.get(
        "/api/gateway/admin/summary",
        headers={"Authorization": f"Bearer {admin.token}"},
    )

    assert response.status_code == 200
    setup = response.json()["setup"]
    assert setup["ready"] is False
    assert setup["checks"]["customer_key"] is False
    assert setup["payment"]["ready"] is False
    assert "wechat_credentials_not_ready" in setup["payment"]["reason_codes"]


def test_gateway_secrets_are_runtime_mounted_and_excluded_from_build() -> None:
    compose = (ROOT / "docker-compose.gateway.yml").read_text(encoding="utf-8")
    dockerignore = (ROOT.parent / ".dockerignore").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "./cloud_api/secrets:/run/secrets:ro" in compose
    assert "**/cloud_api/secrets/**" in dockerignore
    assert "cloud_api/secrets/*" in gitignore


def test_zero_balance_and_expired_subscription_can_still_recharge(
    tmp_path, monkeypatch
) -> None:
    client, _store, _provider, tenant, headers = _billing_client(
        tmp_path, monkeypatch, monthly_fee_microusd=5_000_000
    )
    assert tenant["balance_microusd"] == 0
    assert client.get("/api/gateway/me", headers=headers).status_code == 402
    billing = client.get("/api/gateway/billing/me", headers=headers)
    packages = client.get("/api/gateway/billing/packages")
    assert billing.status_code == 200
    assert billing.json()["tenant_id"] == tenant["id"]
    assert billing.json()["plan_name"] == "Paid"
    assert packages.json()["payment"]["ready"] is True


def test_create_resume_close_and_tenant_isolation(tmp_path, monkeypatch) -> None:
    client, store, provider, tenant, headers = _billing_client(tmp_path, monkeypatch)
    created = _create_order(client, headers)
    order = created["order"]
    assert order["amount_fen"] == 100
    assert created["qr_png_data_url"].startswith("data:image/png;base64,")

    duplicate = client.post(
        "/api/gateway/billing/wechat/orders",
        headers=headers,
        json={"package_id": "starter"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["order_id"] == order["id"]

    resumed = client.get(
        f"/api/gateway/billing/wechat/orders/{order['id']}", headers=headers
    )
    assert resumed.status_code == 200
    assert resumed.json()["qr_png_data_url"].startswith("data:image/png;base64,")

    other = store.create_tenant(name="Other customer")
    other_key = store.create_api_key(tenant_id=str(other["id"]), name="Other")
    other_headers = {"Authorization": f"Bearer {other_key['token']}"}
    hidden = client.get(
        f"/api/gateway/billing/wechat/orders/{order['id']}", headers=other_headers
    )
    assert hidden.status_code == 404

    closed = client.post(
        f"/api/gateway/billing/wechat/orders/{order['id']}/close", headers=headers
    )
    assert closed.status_code == 200
    assert closed.json()["order"]["status"] == "closed"
    assert provider.close_calls == 1
    assert store.get_tenant(str(tenant["id"]))["balance_microusd"] == 0


def test_success_query_credits_once_and_callback_replay_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    client, store, provider, tenant, headers = _billing_client(tmp_path, monkeypatch)
    created = _create_order(client, headers)
    order = created["order"]
    provider.state = "SUCCESS"

    queried = client.get(
        f"/api/gateway/billing/wechat/orders/{order['id']}", headers=headers
    )
    assert queried.status_code == 200
    assert queried.json()["order"]["status"] == "paid"
    assert queried.json()["tenant"]["balance_microusd"] == 2_000_000

    provider.callback_transaction = provider.transaction(order["out_trade_no"])
    callback = client.post(
        "/api/gateway/billing/wechat/notify",
        headers={"Wechatpay-Signature": "valid-test-signature"},
        json={"event_type": "TRANSACTION.SUCCESS", "resource": {}},
    )
    assert callback.status_code == 204
    assert store.get_tenant(str(tenant["id"]))["balance_microusd"] == 2_000_000
    ledger = store.list_ledger(tenant_id=str(tenant["id"]))["items"]
    credit_entries = [item for item in ledger if item["kind"] == "credit"]
    assert len(credit_entries) == 1


def test_admin_reconcile_recovers_callback_delayed_native_order(
    tmp_path, monkeypatch
) -> None:
    admin = generate_api_key(salt="billing-admin-test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", admin.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", admin.salt)
    client, store, provider, tenant, headers = _billing_client(tmp_path, monkeypatch)
    created = _create_order(client, headers)
    order = created["order"]
    provider.state = "SUCCESS"

    admin_headers = {"Authorization": f"Bearer {admin.token}"}

    reconciled = client.post(
        f"/api/gateway/admin/wechat-orders/{order['id']}/reconcile",
        headers=admin_headers,
    )

    assert reconciled.status_code == 200
    payload = reconciled.json()
    assert payload["status"] == "reconciled"
    assert payload["reconciled"] is True
    assert payload["order"]["status"] == "paid"
    assert store.get_tenant(str(tenant["id"]))["balance_microusd"] == 2_000_000


def test_admin_reconcile_is_idempotent_for_paid_order(tmp_path, monkeypatch) -> None:
    admin = generate_api_key(salt="billing-admin-idempotent")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SHA256", admin.sha256)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY_SALT", admin.salt)
    client, store, provider, tenant, headers = _billing_client(tmp_path, monkeypatch)
    created = _create_order(client, headers)
    order = created["order"]
    provider.state = "SUCCESS"
    first = client.post(
        f"/api/gateway/admin/wechat-orders/{order['id']}/reconcile",
        headers={"Authorization": f"Bearer {admin.token}"},
    )
    second = client.post(
        f"/api/gateway/admin/wechat-orders/{order['id']}/reconcile",
        headers={"Authorization": f"Bearer {admin.token}"},
    )

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == "reconciled"
    assert second.json()["status"] == "terminal"
    assert store.get_tenant(str(tenant["id"]))["balance_microusd"] == 2_000_000


def test_amount_mismatch_and_invalid_callback_signature_never_credit(
    tmp_path, monkeypatch
) -> None:
    client, store, provider, tenant, headers = _billing_client(tmp_path, monkeypatch)
    created = _create_order(client, headers)
    order = created["order"]
    provider.state = "SUCCESS"
    provider.amount_delta = 1

    mismatch = client.get(
        f"/api/gateway/billing/wechat/orders/{order['id']}", headers=headers
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "payment_amount_mismatch"
    assert store.get_tenant(str(tenant["id"]))["balance_microusd"] == 0

    provider.callback_transaction = provider.transaction(order["out_trade_no"])
    invalid = client.post(
        "/api/gateway/billing/wechat/notify",
        headers={"Wechatpay-Signature": "invalid"},
        json={"event_type": "TRANSACTION.SUCCESS", "resource": {}},
    )
    assert invalid.status_code == 401
    assert store.get_tenant(str(tenant["id"]))["balance_microusd"] == 0


def test_callback_rejects_wrong_merchant_and_trade_type(tmp_path, monkeypatch) -> None:
    client, store, provider, tenant, headers = _billing_client(tmp_path, monkeypatch)
    created = _create_order(client, headers)
    order = created["order"]
    provider.state = "SUCCESS"
    transaction = provider.transaction(order["out_trade_no"])
    transaction["appid"] = "wxffffffffffffffff"
    provider.callback_transaction = transaction
    wrong_merchant = client.post(
        "/api/gateway/billing/wechat/notify",
        headers={"Wechatpay-Signature": "valid-test-signature"},
        json={"event_type": "TRANSACTION.SUCCESS", "resource": {}},
    )
    assert wrong_merchant.status_code == 409

    transaction["appid"] = provider.config.app_id
    transaction["trade_type"] = "JSAPI"
    wrong_type = client.post(
        "/api/gateway/billing/wechat/notify",
        headers={"Wechatpay-Signature": "valid-test-signature"},
        json={"event_type": "TRANSACTION.SUCCESS", "resource": {}},
    )
    assert wrong_type.status_code == 409
    assert store.get_tenant(str(tenant["id"]))["balance_microusd"] == 0
