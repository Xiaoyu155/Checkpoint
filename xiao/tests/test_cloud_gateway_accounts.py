from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from cloud_api.main import create_app


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("PACER_ACCOUNT_DEV_CODES", "1")
    return TestClient(create_app(workspace_root=tmp_path))


def _code(client: TestClient, email: str, purpose: str = "register") -> str:
    response = client.post(
        "/api/account/verification-codes", json={"email": email, "purpose": purpose}
    )
    assert response.status_code == 200
    return str(response.json()["dev_code"])


def test_register_session_and_billing_access(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    email = "owner@example.com"
    response = client.post(
        "/api/account/register",
        json={
            "email": email,
            "password": "correct horse battery",
            "verification_code": _code(client, email),
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["account"]["tenant_id"].startswith("tenant_")
    assert payload["api_key"].startswith("pacer_sk_")
    assert client.get("/api/account/me").status_code == 200
    billing = client.get("/api/gateway/billing/me")
    assert billing.status_code == 200
    assert billing.json()["tenant_id"] == payload["account"]["tenant_id"]
    keys = client.get("/api/account/api-keys")
    assert keys.status_code == 200
    assert len(keys.json()["api_keys"]) == 1
    created_key = client.post("/api/account/api-keys", json={"name": "Laptop"})
    assert created_key.status_code == 200
    assert created_key.json()["api_key"]["token"].startswith("pacer_sk_")
    key_id = created_key.json()["api_key"]["id"]
    assert client.post(f"/api/account/api-keys/{key_id}/revoke").status_code == 200

    assert client.post("/api/account/logout").status_code == 200
    assert client.get("/api/account/me").status_code == 401
    assert client.get("/api/gateway/billing/me").status_code == 401


def test_login_password_reset_and_single_use_code(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    email = "reset@example.com"
    client.post(
        "/api/account/register",
        json={
            "email": email,
            "password": "old password",
            "verification_code": _code(client, email),
        },
    )
    duplicate = client.post(
        "/api/account/register",
        json={
            "email": email,
            "password": "another password",
            "verification_code": _code(client, email),
        },
    )
    assert duplicate.status_code == 409

    reset_code = _code(client, email, "password_reset")
    reset = client.post(
        "/api/account/password-reset",
        json={"email": email, "password": "new password", "verification_code": reset_code},
    )
    assert reset.status_code == 200
    assert client.post("/api/account/password-reset", json={"email": email, "password": "bad again", "verification_code": reset_code}).status_code == 400
    login = client.post("/api/account/login", json={"email": email, "password": "new password"})
    assert login.status_code == 200


def test_account_code_is_not_reusable(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    email = "once@example.com"
    code = _code(client, email)
    first = client.post(
        "/api/account/register",
        json={"email": email, "password": "long enough password", "verification_code": code},
    )
    assert first.status_code == 200
    second = client.post(
        "/api/account/register",
        json={"email": "other@example.com", "password": "long enough password", "verification_code": code},
    )
    assert second.status_code == 400
