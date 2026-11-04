from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "vps_gemini4_gateway"))
from binding_api import fingerprint, register_binding_api


class Account:
    def __init__(self, account_id, email, psid, psidts, psidcc):
        self.id, self.email, self.psid, self.psidts, self.psidcc = account_id, email, psid, psidts, psidcc
        self.tier, self.is_pro, self.plan, self.models, self.verified_model_name = "pro", True, "Pro", {}, ""

    def to_dict(self, sanitize=True):
        return {"id": self.id, "status": "active", "verified_model_name": self.verified_model_name}


@pytest.fixture
def binding(tmp_path):
    old = Account("original", "user@example.invalid", "old-session", "old-ts", "")
    pool = SimpleNamespace(accounts=[old], _lock=asyncio.Lock(), file_path=tmp_path / "accounts.json")
    Path(pool.file_path).write_text("[]")
    async def ping(account):
        account.verified_model_name = "4 Argon"
        return True, "actual model verified", 1.0
    @asynccontextmanager
    async def session():
        yield object()
    async def fetch(ses, account):
        return "new-ticket"
    def auth(value):
        if value != "Bearer test":
            raise HTTPException(401)
    app = FastAPI()
    ping_mock, fetch_mock = AsyncMock(side_effect=ping), AsyncMock(side_effect=fetch)
    register_binding_api(app, pool, Account, auth, session, fetch_mock, ping_mock)
    return TestClient(app), pool, ping_mock, fetch_mock


def post(client, payload, sync=False):
    return client.post("/admin/bindings" + ("/sync" if sync else ""), json=payload, headers={"Authorization":"Bearer test"})


def test_success_requires_real_gemini4_before_saving(binding):
    client, pool, ping, _ = binding
    result = post(client, {"psid":"new-session", "psidts":"ts"})
    assert result.status_code == 200
    assert result.json()["test_result"]["verified_model"] == "4 Argon"
    assert len(pool.accounts) == 2
    assert len(json.loads(Path(pool.file_path).read_text())) == 2
    assert ping.await_count == 1


def test_failed_login_preserves_existing_healthy_binding(binding):
    client, pool, ping, _ = binding
    original = pool.accounts[0]
    ping.side_effect = None
    ping.return_value = (False, "login expired", 0)
    response = post(client, {"id":"original", "psid":"old-session", "psidts":"bad-ts"})
    assert response.status_code == 422
    assert pool.accounts == [original]
    assert original.psidts == "old-ts"


def test_wrong_account_cannot_silently_overwrite_id(binding):
    client, pool, ping, _ = binding
    result = post(client, {"id":"original", "psid":"different-account", "psidts":"ts"})
    assert result.status_code == 409
    assert pool.accounts[0].psid == "old-session"
    assert ping.await_count == 0


def test_downgraded_model_is_rejected(binding):
    client, pool, ping, _ = binding
    async def downgraded(account):
        account.verified_model_name = "3.5 Flash-Lite"
        return True, "HTTP 200", 1
    ping.side_effect = downgraded
    result = post(client, {"psid":"new-session", "psidts":"ts"})
    assert result.status_code == 422
    assert len(pool.accounts) == 1


def test_sync_checks_login_without_spending_generation_quota(binding):
    client, pool, ping, fetch = binding
    result = post(client, {"id":"original", "psid":"old-session", "psidts":"new-ts", "expected_psid_hash":fingerprint("old-session")}, sync=True)
    assert result.status_code == 200
    assert pool.accounts[0].psidts == "new-ts"
    assert fetch.await_count == 1
    assert ping.await_count == 0


def test_dry_run_validates_without_mutating_pool(binding):
    client, pool, _, _ = binding
    result = post(client, {"psid":"new-session", "psidts":"ts", "validate_only":True})
    assert result.status_code == 200 and result.json()["saved"] is False
    assert len(pool.accounts) == 1
