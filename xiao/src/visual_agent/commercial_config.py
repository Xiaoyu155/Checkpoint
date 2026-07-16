from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REDACTED = "****"


@dataclass(frozen=True)
class CommercialConfig:
    auth_provider: str = "supabase"
    login_provider: str = "google"
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""
    google_oauth_configured: bool = False
    google_client_id: str = ""
    google_client_secret: str = ""
    billing_provider: str = "stripe"
    billing_mode: str = "subscriptions_with_portal"
    stripe_publishable_key: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_id: str = ""
    stripe_customer_portal_url: str = ""
    stripe_usage_meter_event: str = "pacer_managed_minutes"

    @property
    def auth_configured(self) -> bool:
        return bool(
            self.auth_provider == "supabase"
            and self.login_provider == "google"
            and self.supabase_url.strip()
            and self.supabase_anon_key.strip()
            and self.google_oauth_configured
        )

    @property
    def billing_configured(self) -> bool:
        return bool(
            self.billing_provider == "stripe"
            and self.stripe_publishable_key.strip()
            and self.stripe_secret_key.strip()
            and self.stripe_webhook_secret.strip()
            and self.stripe_price_id.strip()
        )

    @property
    def portal_configured(self) -> bool:
        return bool(self.billing_configured and self.stripe_customer_portal_url.strip())

    @property
    def usage_meter_configured(self) -> bool:
        return bool(self.stripe_usage_meter_event.strip())

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(
            {
                "schema_version": 1,
                "auth_configured": self.auth_configured,
                "billing_configured": self.billing_configured,
                "portal_configured": self.portal_configured,
                "usage_meter_configured": self.usage_meter_configured,
            }
        )
        if redact:
            for key in (
                "supabase_service_role_key",
                "google_client_secret",
                "stripe_secret_key",
                "stripe_webhook_secret",
            ):
                if payload.get(key):
                    payload[key] = REDACTED
        return payload


def commercial_config_path() -> Path:
    override = os.environ.get("PACER_COMMERCIAL_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pacer" / "commercial.json"


def load_commercial_config(path: str | Path | None = None) -> CommercialConfig:
    source = Path(path).expanduser() if path else commercial_config_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return CommercialConfig(
        auth_provider=str(payload.get("auth_provider") or "supabase").strip() or "supabase",
        login_provider=str(payload.get("login_provider") or "google").strip() or "google",
        supabase_url=str(payload.get("supabase_url") or "").strip(),
        supabase_anon_key=str(payload.get("supabase_anon_key") or "").strip(),
        supabase_service_role_key=str(payload.get("supabase_service_role_key") or "").strip(),
        google_oauth_configured=bool(payload.get("google_oauth_configured")),
        google_client_id=str(payload.get("google_client_id") or "").strip(),
        google_client_secret=str(payload.get("google_client_secret") or "").strip(),
        billing_provider=str(payload.get("billing_provider") or "stripe").strip() or "stripe",
        billing_mode=str(payload.get("billing_mode") or "subscriptions_with_portal").strip() or "subscriptions_with_portal",
        stripe_publishable_key=str(payload.get("stripe_publishable_key") or "").strip(),
        stripe_secret_key=str(payload.get("stripe_secret_key") or "").strip(),
        stripe_webhook_secret=str(payload.get("stripe_webhook_secret") or "").strip(),
        stripe_price_id=str(payload.get("stripe_price_id") or "").strip(),
        stripe_customer_portal_url=str(payload.get("stripe_customer_portal_url") or "").strip(),
        stripe_usage_meter_event=str(payload.get("stripe_usage_meter_event") or "pacer_managed_minutes").strip() or "pacer_managed_minutes",
    )


def save_commercial_config(
    config: CommercialConfig,
    path: str | Path | None = None,
    *,
    existing: CommercialConfig | None = None,
) -> Path:
    target = Path(path).expanduser() if path else commercial_config_path()
    prior = existing or load_commercial_config(target)
    payload = _merge_redacted(config, prior).to_dict(redact=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def commercial_config_from_payload(payload: dict[str, Any], *, existing: CommercialConfig | None = None) -> CommercialConfig:
    prior = existing or CommercialConfig()

    def text(key: str, default: str = "") -> str:
        return str(payload.get(key) if payload.get(key) is not None else default).strip()

    return CommercialConfig(
        auth_provider=text("auth_provider", "supabase") or "supabase",
        login_provider=text("login_provider", "google") or "google",
        supabase_url=text("supabase_url"),
        supabase_anon_key=text("supabase_anon_key"),
        supabase_service_role_key=_secret_payload(payload, "supabase_service_role_key", prior.supabase_service_role_key),
        google_oauth_configured=bool(payload.get("google_oauth_configured")),
        google_client_id=text("google_client_id"),
        google_client_secret=_secret_payload(payload, "google_client_secret", prior.google_client_secret),
        billing_provider=text("billing_provider", "stripe") or "stripe",
        billing_mode=text("billing_mode", "subscriptions_with_portal") or "subscriptions_with_portal",
        stripe_publishable_key=text("stripe_publishable_key"),
        stripe_secret_key=_secret_payload(payload, "stripe_secret_key", prior.stripe_secret_key),
        stripe_webhook_secret=_secret_payload(payload, "stripe_webhook_secret", prior.stripe_webhook_secret),
        stripe_price_id=text("stripe_price_id"),
        stripe_customer_portal_url=text("stripe_customer_portal_url"),
        stripe_usage_meter_event=text("stripe_usage_meter_event", "pacer_managed_minutes") or "pacer_managed_minutes",
    )


def _secret_payload(payload: dict[str, Any], key: str, existing: str) -> str:
    value = str(payload.get(key) or "").strip()
    return existing if value == REDACTED else value


def _merge_redacted(config: CommercialConfig, prior: CommercialConfig) -> CommercialConfig:
    payload = asdict(config)
    for key in (
        "supabase_service_role_key",
        "google_client_secret",
        "stripe_secret_key",
        "stripe_webhook_secret",
    ):
        if payload.get(key) == REDACTED:
            payload[key] = getattr(prior, key)
    return CommercialConfig(
        **payload
    )
