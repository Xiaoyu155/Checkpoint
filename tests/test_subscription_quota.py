from __future__ import annotations

import json

from visual_agent.subscription_quota import (
    format_statusline,
    load_quota_snapshot,
    quota_status,
    quota_store_path,
    quota_to_markdown,
    record_statusline_snapshot,
)

STATUSLINE_PAYLOAD = {
    "model": {"id": "claude-opus-4-8", "display_name": "Opus"},
    "version": "2.1.187",
    "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": 1900000000},
        "seven_day": {"used_percentage": 41.2, "resets_at": 1900400000},
    },
}


def test_record_and_load_snapshot(tmp_path):
    store = tmp_path / "quota.json"
    snapshot = record_statusline_snapshot(STATUSLINE_PAYLOAD, store_path=store)
    assert snapshot is not None
    assert snapshot["rate_limits"]["five_hour"]["used_percentage"] == 23.5

    loaded = load_quota_snapshot(store_path=store)
    assert loaded is not None
    assert loaded["model"] == "Opus"
    assert loaded["age_minutes"] is not None and loaded["age_minutes"] < 5


def test_record_ignores_payload_without_rate_limits(tmp_path):
    store = tmp_path / "quota.json"
    assert record_statusline_snapshot({"model": {"id": "x"}}, store_path=store) is None
    assert not store.exists()


def test_quota_status_warns_when_window_nearly_used(tmp_path):
    store = tmp_path / "quota.json"
    payload = json.loads(json.dumps(STATUSLINE_PAYLOAD))
    payload["rate_limits"]["five_hour"]["used_percentage"] = 91.0
    record_statusline_snapshot(payload, store_path=store)
    status = quota_status(load_quota_snapshot(store_path=store))
    assert status["level"] == "warn"
    assert any("5h" in message and "91" in message for message in status["messages"])


def test_quota_status_unknown_without_snapshot():
    status = quota_status(None)
    assert status["level"] == "unknown"
    assert any("statusLine" in message for message in status["messages"])


def test_format_statusline_shows_windows(tmp_path):
    store = tmp_path / "quota.json"
    snapshot = record_statusline_snapshot(STATUSLINE_PAYLOAD, store_path=store)
    line = format_statusline(STATUSLINE_PAYLOAD, snapshot)
    assert "Opus" in line
    assert "5h 24%" in line or "5h 23%" in line
    assert "7d 41%" in line


def test_quota_markdown_separates_subscription_from_spend(tmp_path):
    store = tmp_path / "quota.json"
    record_statusline_snapshot(STATUSLINE_PAYLOAD, store_path=store)
    text = quota_to_markdown(load_quota_snapshot(store_path=store))
    assert "5h window: 23.5% used" in text
    assert "not API spend" in text


def test_quota_store_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CHECKPOINT_QUOTA_PATH", str(tmp_path / "custom.json"))
    assert quota_store_path() == tmp_path / "custom.json"
