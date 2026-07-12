from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any


DEFAULT_EVENTS = {
    "mission_verified",
    "mission_failed",
    "mission_stopped",
    "quota_warning",
    "quota_exhausted",
    "worker_error",
    "needs_user_input",
}


@dataclass(frozen=True)
class NotificationConfig:
    smtp_host: str
    smtp_port: int
    username: str
    password: str
    sender: str
    recipient: str
    use_tls: bool = True
    enabled_events: tuple[str, ...] = tuple(sorted(DEFAULT_EVENTS))


def load_notification_config(path: str | Path | None = None) -> NotificationConfig | None:
    cfg_path = Path(path).expanduser() if path else _default_config_path()
    payload: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            payload = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        except ValueError:
            payload = {}
    smtp_host = str(os.environ.get("CHECKPOINT_SMTP_HOST") or payload.get("smtp_host") or "").strip()
    recipient = str(os.environ.get("CHECKPOINT_NOTIFY_TO") or payload.get("recipient") or "").strip()
    sender = str(os.environ.get("CHECKPOINT_NOTIFY_FROM") or payload.get("sender") or payload.get("username") or "").strip()
    username = str(os.environ.get("CHECKPOINT_SMTP_USERNAME") or payload.get("username") or sender or "").strip()
    password = str(os.environ.get("CHECKPOINT_SMTP_PASSWORD") or payload.get("password") or "").strip()
    if not smtp_host or not recipient or not sender:
        return None
    events = payload.get("enabled_events") if isinstance(payload.get("enabled_events"), list) else sorted(DEFAULT_EVENTS)
    return NotificationConfig(
        smtp_host=smtp_host,
        smtp_port=int(os.environ.get("CHECKPOINT_SMTP_PORT") or payload.get("smtp_port") or 587),
        username=username,
        password=password,
        sender=sender,
        recipient=recipient,
        use_tls=bool(payload.get("use_tls", True)),
        enabled_events=tuple(str(item) for item in events if str(item).strip()),
    )


def notification_config_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "use_tls": True,
        "username": "user@example.com",
        "password": "app-password",
        "sender": "user@example.com",
        "recipient": "you@example.com",
        "enabled_events": sorted(DEFAULT_EVENTS),
    }


def build_event_notification(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    event_name = str(event or "").strip()
    subject_map = {
        "mission_verified": "Mission verified",
        "mission_failed": "Mission failed",
        "mission_stopped": "Mission stopped",
        "quota_warning": "Quota warning",
        "quota_exhausted": "Quota exhausted",
        "worker_error": "Worker error",
        "needs_user_input": "Needs user input",
    }
    subject = subject_map.get(event_name, event_name or "Dev task update")
    body_lines = [subject, ""]
    for key in ("project", "mission_id", "objective", "status", "stop_reason", "agent", "model", "quota"):
        value = payload.get(key)
        if value not in (None, ""):
            body_lines.append(f"{key}: {value}")
    if payload.get("message"):
        body_lines.extend(["", str(payload["message"])])
    if payload.get("report_path"):
        body_lines.append(f"report: {payload['report_path']}")
    return {
        "schema_version": 1,
        "event": event_name,
        "subject": f"[Pacer] {subject}",
        "body": "\n".join(body_lines).rstrip() + "\n",
        "payload": payload,
    }


def send_email_notification(
    notification: dict[str, Any],
    *,
    config: NotificationConfig | None = None,
    config_path: str | Path | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    cfg = config or load_notification_config(config_path)
    if cfg is None:
        return {"status": "blocked", "reason": "notification_config_missing", "notification": notification}
    event = str(notification.get("event") or "")
    if event and event not in cfg.enabled_events:
        return {"status": "skipped", "reason": "event_disabled", "event": event}
    message = EmailMessage()
    message["From"] = cfg.sender
    message["To"] = cfg.recipient
    message["Subject"] = str(notification.get("subject") or "Pacer notification")
    message.set_content(str(notification.get("body") or ""))
    if dry_run:
        return {
            "status": "planned",
            "to": cfg.recipient,
            "subject": message["Subject"],
            "body": str(notification.get("body") or ""),
        }
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20) as smtp:
        if cfg.use_tls:
            smtp.starttls()
        if cfg.username or cfg.password:
            smtp.login(cfg.username, cfg.password)
        smtp.send_message(message)
    return {"status": "sent", "to": cfg.recipient, "subject": message["Subject"]}


def _default_config_path() -> Path:
    override = os.environ.get("CHECKPOINT_NOTIFY_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    pacer_path = Path.home() / ".pacer" / "notifications.json"
    if pacer_path.exists():
        return pacer_path
    legacy = Path.home() / ".checkpoint" / "notifications.json"
    if legacy.exists():
        return legacy
    return pacer_path
