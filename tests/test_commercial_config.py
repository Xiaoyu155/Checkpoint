from __future__ import annotations

from visual_agent.commercial_config import (
    REDACTED,
    CommercialConfig,
    commercial_config_from_payload,
    load_commercial_config,
    save_commercial_config,
)


def test_commercial_config_round_trips_and_redacts(tmp_path) -> None:
    path = tmp_path / "commercial.json"
    config = CommercialConfig(
        supabase_url="https://pacer.supabase.co",
        supabase_anon_key="anon-key",
        supabase_service_role_key="service-role",
        google_oauth_configured=True,
        google_client_id="google-client",
        google_client_secret="google-secret",
        stripe_publishable_key="pk_test_123",
        stripe_secret_key="sk_test_123",
        stripe_webhook_secret="whsec_123",
        stripe_price_id="price_123",
        stripe_customer_portal_url="https://billing.stripe.com/p/session",
    )

    save_commercial_config(config, path)
    loaded = load_commercial_config(path)
    public = loaded.to_dict(redact=True)

    assert loaded.auth_configured is True
    assert loaded.billing_configured is True
    assert loaded.portal_configured is True
    assert loaded.usage_meter_configured is True
    assert public["supabase_service_role_key"] == REDACTED
    assert public["google_client_secret"] == REDACTED
    assert public["stripe_secret_key"] == REDACTED
    assert public["stripe_webhook_secret"] == REDACTED


def test_commercial_config_preserves_redacted_secrets_on_save(tmp_path) -> None:
    path = tmp_path / "commercial.json"
    existing = CommercialConfig(
        supabase_service_role_key="service-role",
        google_client_secret="google-secret",
        stripe_secret_key="sk_test_123",
        stripe_webhook_secret="whsec_123",
    )
    save_commercial_config(existing, path)

    updated = commercial_config_from_payload(
        {
            "supabase_service_role_key": REDACTED,
            "google_client_secret": REDACTED,
            "stripe_secret_key": REDACTED,
            "stripe_webhook_secret": REDACTED,
            "stripe_usage_meter_event": "pacer_managed_minutes",
        },
        existing=load_commercial_config(path),
    )
    save_commercial_config(updated, path, existing=load_commercial_config(path))
    loaded = load_commercial_config(path)

    assert loaded.supabase_service_role_key == "service-role"
    assert loaded.google_client_secret == "google-secret"
    assert loaded.stripe_secret_key == "sk_test_123"
    assert loaded.stripe_webhook_secret == "whsec_123"


def test_usage_meter_event_is_reserved_without_live_stripe_keys() -> None:
    config = CommercialConfig(stripe_usage_meter_event="pacer_managed_minutes")

    assert config.billing_configured is False
    assert config.usage_meter_configured is True
