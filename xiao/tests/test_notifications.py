from __future__ import annotations

from visual_agent.notifications import (
    NotificationConfig,
    build_event_notification,
    notification_config_template,
    send_email_notification,
)


def test_notification_template_contains_required_fields() -> None:
    template = notification_config_template()

    assert "smtp_host" in template
    assert "recipient" in template
    assert "quota_exhausted" in template["enabled_events"]


def test_build_event_notification_includes_mission_context() -> None:
    notification = build_event_notification(
        "mission_verified",
        {
            "project": "yuansi_app",
            "mission_id": "m1",
            "objective": "Fix checkout",
            "status": "verified",
            "report_path": "final_report.md",
        },
    )

    assert notification["subject"] == "[Pacer] Mission verified"
    assert "Fix checkout" in notification["body"]
    assert "final_report.md" in notification["body"]


def test_send_email_notification_dry_run_does_not_require_network() -> None:
    config = NotificationConfig(
        smtp_host="smtp.example.com",
        smtp_port=587,
        username="sender@example.com",
        password="secret",
        sender="sender@example.com",
        recipient="user@example.com",
    )
    notification = build_event_notification("quota_warning", {"quota": "5h 91%"})

    result = send_email_notification(notification, config=config, dry_run=True)

    assert result["status"] == "planned"
    assert result["to"] == "user@example.com"
    assert "Quota warning" in result["subject"]


def test_send_email_notification_blocks_without_config() -> None:
    result = send_email_notification(build_event_notification("mission_failed", {}), config=None, config_path="missing.json")

    assert result["status"] == "blocked"
    assert result["reason"] == "notification_config_missing"
