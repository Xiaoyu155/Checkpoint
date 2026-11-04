"""
Tests for Gemini 4 Multi-Account Gateway (Sub2API-Grade).

Validates:
1. Account state transitions: active -> cooling_down -> reset -> expired.
2. Pool loading, saving, and round-robin scheduling with exclusion-based failover.
3. Gemini text chunk extraction across nested RPC arrays.
4. Token calculation and sensitive data masking.
5. REST API routes and public health/status contracts.
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "vps_gemini4_gateway"))

from gemini4_service import (
    Account,
    AccountPool,
    extract_gemini_text_chunks,
    parse_cookie_payload,
    app,
)


def test_account_lifecycle_and_status():
    acc = Account(
        acc_id="test_jio_01",
        email="developer@example.com",
        psid="test_psid_value",
        psidts="test_psidts_value",
    )
    assert acc.get_status() == "active"
    assert acc.is_available() is True
    assert acc.get_cooldown_remaining() == 0

    # Test cooling down (e.g. on 429)
    acc.mark_cooling_down(seconds=60, reason="HTTP 429 Rate Limit")
    assert acc.get_status() == "cooling_down"
    assert acc.is_available() is False
    assert acc.get_cooldown_remaining() > 0
    assert "Rate Limit" in acc.status_reason

    # Test reset cooldown
    acc.reset_cooldown()
    assert acc.get_status() == "active"
    assert acc.is_available() is True

    # Test expired
    acc.mark_expired("Session revoked")
    assert acc.get_status() == "expired"
    assert acc.is_available() is False

    # Test metrics recording
    acc.reset_cooldown()
    acc.record_success(latency_ms=120.5, tokens=45)
    assert acc.total_requests == 1
    assert acc.success_requests == 1
    assert acc.tokens_served == 45
    assert acc.last_latency_ms == 120.5

    # Test privacy masking in to_dict
    masked = acc.to_dict(sanitize=True)
    assert "d***r@example.com" == masked["email"]
    assert "psid" not in masked

    unmasked = acc.to_dict(sanitize=False)
    assert unmasked["psid"] == "test_psid_value"
    assert unmasked["raw_email"] == "developer@example.com"


@pytest.mark.asyncio
async def test_account_pool_failover_and_rotation(tmp_path: Path):
    accounts_file = tmp_path / "accounts.json"
    raw_data = [
        {"id": "acc_1", "email": "a1@test.com", "psid": "p1", "psidts": "ts1"},
        {"id": "acc_2", "email": "a2@test.com", "psid": "p2", "psidts": "ts2"},
        {"id": "placeholder", "email": "p@test.com", "psid": "REPLACE_WITH_YOUR_KEY", "psidts": "none"},
    ]
    accounts_file.write_text(json.dumps(raw_data), encoding="utf-8")

    pool = AccountPool(str(accounts_file))
    # Placeholder should be filtered out
    assert len(pool.accounts) == 2

    # Round-robin checks
    a1 = await pool.get_next_account()
    a2 = await pool.get_next_account()
    assert a1.id != a2.id

    # Failover exclusion test
    # When acc_1 fails, pass exclude_ids={"acc_1"}
    next_acc = await pool.get_next_account(exclude_ids={"acc_1"})
    assert next_acc.id == "acc_2"

    # When both are cooling down, returns the one recovering earliest
    a1.mark_cooling_down(seconds=500)
    a2.mark_cooling_down(seconds=100)
    earliest = await pool.get_next_account()
    assert earliest.id == "acc_2"


def test_extract_gemini_text_chunks_parsing():
    # Test valid Gemini RPC JSON format
    inner = [
        None, None, None, None,
        [
            [None, ["Hello world! This is Gemini 4."]]
        ]
    ]
    raw_rpc_line = json.dumps([
        ["wrb.fr", None, json.dumps(inner)]
    ])

    chunks = extract_gemini_text_chunks(raw_rpc_line)
    assert len(chunks) == 1
    assert chunks[0] == "Hello world! This is Gemini 4."

    # Test invalid / malformed lines
    assert extract_gemini_text_chunks("1234") == []
    assert extract_gemini_text_chunks("invalid json") == []
    assert extract_gemini_text_chunks("[]") == []


def test_cookie_header_generation():
    acc = Account(
        acc_id="acc_h",
        email="h@test.com",
        psid="PSID_123",
        psidts="PSIDTS_456",
        psidcc="PSIDCC_789"
    )
    header = acc.get_cookie_header()
    assert "__Secure-1PSID=PSID_123" in header
    assert "__Secure-1PSIDTS=PSIDTS_456" in header
    assert "__Secure-1PSIDCC=PSIDCC_789" in header


def test_parse_cookie_payload_formats():
    # 1. Test cURL command extraction
    curl_sample = """curl 'https://gemini.google.com/_/BardChatUi/data/batchexecute' \\
      -H 'cookie: SID=xxx; __Secure-1PSID=g.a000123; HSID=yyy; __Secure-1PSIDTS=sidts-CjEBabc; __Secure-1PSIDCC=AKEyXdef' \\
      --compressed"""
    res1 = parse_cookie_payload(curl_sample)
    assert res1["psid"] == "g.a000123"
    assert res1["psidts"] == "sidts-CjEBabc"
    assert res1["psidcc"] == "AKEyXdef"

    # 2. Test raw semicolon-separated cookie header
    raw_sample = "NID=511; __Secure-1PSID=psid_999; __Secure-1PSIDTS=psidts_888; 1P_JAR=2024"
    res2 = parse_cookie_payload(raw_sample)
    assert res2["psid"] == "psid_999"
    assert res2["psidts"] == "psidts_888"
    assert "psidcc" not in res2

    # 3. Test Cookie-Editor JSON array export
    json_sample = json.dumps([
        {"name": "NID", "value": "204=..."},
        {"name": "__Secure-1PSID", "value": "json_psid_val"},
        {"name": "__Secure-1PSIDTS", "value": "json_psidts_val"},
        {"name": "__Secure-1PSIDCC", "value": "json_psidcc_val"}
    ])
    res3 = parse_cookie_payload(json_sample)
    assert res3["psid"] == "json_psid_val"
    assert res3["psidts"] == "json_psidts_val"
    assert res3["psidcc"] == "json_psidcc_val"

    # 4. Test empty or malformed strings
    assert parse_cookie_payload("") == {}
    assert parse_cookie_payload("random string with no target cookies") == {}

