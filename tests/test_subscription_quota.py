from __future__ import annotations

import json

from visual_agent.subscription_quota import (
    format_statusline,
    load_quota_snapshot,
    parse_codex_usage_text,
    quota_status,
    quota_store_path,
    quota_summary,
    quota_to_markdown,
    record_codex_usage_snapshot,
    record_statusline_snapshot,
    refresh_codex_quota_snapshot,
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


def test_parse_codex_usage_text_extracts_five_hour_and_weekly_windows():
    windows = parse_codex_usage_text(
        "Codex usage\n5-hour window: 61% used, resets soon\nWeekly limit: 34% used"
    )
    assert windows["five_hour"]["used_percentage"] == 61.0
    assert windows["seven_day"]["used_percentage"] == 34.0


def test_parse_codex_usage_text_handles_status_like_short_and_chinese_labels():
    windows = parse_codex_usage_text("Codex /status\n五小时额度 12% 已用\n周额度 67% 已用")
    assert windows["five_hour"]["used_percentage"] == 12.0
    assert windows["seven_day"]["used_percentage"] == 67.0


def test_record_codex_usage_snapshot_does_not_invent_claude_provider(tmp_path):
    store = tmp_path / "quota.json"
    snapshot = record_codex_usage_snapshot(
        "Codex /usage: 5h 10% used; weekly 20% used",
        store_path=store,
    )
    loaded = load_quota_snapshot(store_path=store)

    assert loaded is not None
    assert set(loaded["providers"]) == {"codex"}
    assert "rate_limits" not in loaded
    assert snapshot["providers"]["codex"]["rate_limits"]["five_hour"]["used_percentage"] == 10.0


def test_legacy_claude_snapshot_still_normalizes_to_claude_provider(tmp_path):
    store = tmp_path / "quota.json"
    store.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "claude-code-statusline",
                "recorded_at": "2026-07-08T00:00:00+00:00",
                "model": "Opus",
                "rate_limits": {"five_hour": {"used_percentage": 44.0}},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_quota_snapshot(store_path=store)

    assert loaded is not None
    assert loaded["providers"]["claude-code"]["rate_limits"]["five_hour"]["used_percentage"] == 44.0


def test_record_codex_usage_snapshot_merges_with_claude_snapshot(tmp_path):
    store = tmp_path / "quota.json"
    record_statusline_snapshot(STATUSLINE_PAYLOAD, store_path=store)
    snapshot = record_codex_usage_snapshot(
        "Codex /usage\n5h remaining 42%\nweekly 79% used",
        store_path=store,
    )
    loaded = load_quota_snapshot(store_path=store)
    assert loaded is not None
    assert loaded["rate_limits"]["five_hour"]["used_percentage"] == 23.5
    assert loaded["providers"]["claude-code"]["rate_limits"]["seven_day"]["used_percentage"] == 41.2
    assert snapshot["providers"]["codex"]["rate_limits"]["five_hour"]["used_percentage"] == 58.0
    assert loaded["providers"]["codex"]["rate_limits"]["seven_day"]["used_percentage"] == 79.0
    summary = quota_summary(loaded)
    assert summary["max_used_percentage"] == 79.0
    assert any(item["provider"] == "codex" for item in summary["windows"])


def test_refresh_codex_quota_requires_explicit_command(tmp_path, monkeypatch):
    monkeypatch.delenv("PACER_CODEX_STATUS_COMMAND", raising=False)
    monkeypatch.delenv("CHECKPOINT_CODEX_STATUS_COMMAND", raising=False)
    result = refresh_codex_quota_snapshot(store_path=tmp_path / "quota.json")
    assert result["ok"] is False
    assert result["reason"] == "codex_status_command_missing"
    snapshot = result["snapshot"]
    assert snapshot["providers"]["codex"]["status"] == "unconfigured"
