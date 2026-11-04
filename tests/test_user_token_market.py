import hashlib

import pytest

from visual_agent.user_token_market import (
    MarketError,
    PrivacyClass,
    QuotaMarket,
    SourceKind,
    TokenProvenance,
    credential_fingerprint,
)


KEY = b"p" * 32


def provenance(**overrides) -> TokenProvenance:
    payload = {
        "owner_id": "yxq190606_pro",
        "source_kind": SourceKind.FIRST_PARTY_GATEWAY,
        "provider": "google",
        "endpoint_host": "direct.aiboxz.com",
        "endpoint_port": 443,
        "advertised_model": "gemini-4",
        "observed_model": "4 Argon",
        "credential_fingerprint": credential_fingerprint("session-secret"),
    }
    payload.update(overrides)
    return TokenProvenance(**payload)


def market() -> QuotaMarket:
    item = QuotaMarket(KEY)
    item.credit("buyer_one", 10_000)
    return item


def test_fingerprint_hides_the_credential() -> None:
    secret = "sk-live-example"
    assert credential_fingerprint(secret) == hashlib.sha256(secret.encode()).hexdigest()[:16]
    assert secret not in credential_fingerprint(secret)


def test_reject_unlisted_host_and_mismatched_model() -> None:
    with pytest.raises(MarketError, match="allowlisted"):
        provenance(endpoint_host="100.64.0.1")
    with pytest.raises(MarketError, match="observed"):
        provenance(observed_model="3.5-flash")
    with pytest.raises(MarketError, match="fingerprint"):
        provenance(credential_fingerprint="sk-live-example")


def test_desktop_port_is_part_of_the_source_identity() -> None:
    card = provenance(
        owner_id="wb_acc2",
        source_kind=SourceKind.DESKTOP_SESSION,
        provider="workbuddy",
        endpoint_host="127.0.0.1",
        endpoint_port=57756,
        advertised_model="deepseek-v4",
        observed_model="deepseek-v4",
    ).public_card()
    assert card["endpoint_port"] == 57756
    with pytest.raises(MarketError, match="port"):
        provenance(endpoint_port=8443)


def test_listing_rejects_secrets_and_other_owners() -> None:
    item = market()
    with pytest.raises(MarketError, match="secret"):
        item.offer_from_mapping(
            {
                "owner_id": "yxq190606_pro",
                "source_kind": "first_party_gateway",
                "provider": "google",
                "endpoint_host": "direct.aiboxz.com",
                "endpoint_port": 443,
                "advertised_model": "gemini-4",
                "observed_model": "4 Argon",
                "credential_fingerprint": credential_fingerprint("session-secret"),
                "units": 2,
                "ask_mills": 1000,
                "floor_mills": 100,
                "duration_s": 100,
                "raw_key": "must-not-pass",
                "trust_tier": "official",
            },
            seller_id="yxq190606_pro",
            opened_at=0,
        )
    with pytest.raises(MarketError, match="owner"):
        item.offer(provenance(), seller_id="someone_else", units=1, ask_mills=100, floor_mills=10, opened_at=0, duration_s=10)


def test_dutch_auction_charges_the_confirmed_price_and_refunds_the_rest() -> None:
    item = market()
    listing = item.offer(
        provenance(),
        seller_id="yxq190606_pro",
        units=4,
        ask_mills=1000,
        floor_mills=200,
        opened_at=0,
        duration_s=100,
    )
    assert listing.price_mills(0) == 1000
    assert listing.price_mills(50) == 600
    confirmation = item.confirm(buyer_id="buyer_one", listing_id=listing.listing_id, units=2, limit_mills=900, now=50)
    fill = item.buy(confirmation, now=50)
    assert fill.price_mills == 600
    assert fill.units == 2
    assert item._available["buyer_one"] == 10_000 - 1_200
    assert item._locked["buyer_one"] == 0
    with pytest.raises(MarketError, match="price changed"):
        item.buy(confirmation, now=80)


def test_changed_source_invalidates_confirmation() -> None:
    item = market()
    listing = item.offer(
        provenance(),
        seller_id="yxq190606_pro",
        units=1,
        ask_mills=100,
        floor_mills=100,
        opened_at=0,
        duration_s=10,
    )
    confirmation = item.confirm(buyer_id="buyer_one", listing_id=listing.listing_id, units=1, limit_mills=100, now=0)
    swapped = provenance(owner_id="other_pro", observed_model="Gemini 4")
    item._listings[listing.listing_id] = type(listing)(
        listing_id=listing.listing_id,
        provenance=swapped,
        units=listing.units,
        ask_mills=listing.ask_mills,
        floor_mills=listing.floor_mills,
        opened_at=listing.opened_at,
        duration_s=listing.duration_s,
    )
    with pytest.raises(MarketError, match="changed"):
        item.buy(confirmation, now=0)


def test_confidential_call_stays_on_the_caller_official_api() -> None:
    item = market()
    own = TokenProvenance(
        owner_id="buyer_one",
        source_kind=SourceKind.OFFICIAL_API,
        provider="google",
        endpoint_host="generativelanguage.googleapis.com",
        endpoint_port=443,
        advertised_model="gemini-4-argon",
        observed_model="gemini-4-argon",
        credential_fingerprint=credential_fingerprint("AIza-own"),
    )
    digest = hashlib.sha256(b"private task").hexdigest()
    receipt = item.authorize_invocation(
        caller_id="buyer_one",
        provenance=own,
        privacy=PrivacyClass.CONFIDENTIAL,
        prompt_sha256=digest,
    )
    assert "不会发给其他用户" in receipt.warning
    assert "private task" not in str(receipt.public_dict())
    with pytest.raises(MarketError, match="caller's own"):
        item.authorize_invocation(
            caller_id="buyer_one",
            provenance=provenance(),
            privacy=PrivacyClass.CONFIDENTIAL,
            prompt_sha256=digest,
        )


def test_public_call_requires_the_purchased_source() -> None:
    item = market()
    listing = item.offer(
        provenance(),
        seller_id="yxq190606_pro",
        units=1,
        ask_mills=100,
        floor_mills=100,
        opened_at=0,
        duration_s=10,
    )
    confirmation = item.confirm(buyer_id="buyer_one", listing_id=listing.listing_id, units=1, limit_mills=100, now=0)
    fill = item.buy(confirmation, now=0)
    digest = hashlib.sha256(b"hello").hexdigest()
    receipt = item.authorize_invocation(
        caller_id="buyer_one",
        provenance=listing.provenance,
        privacy=PrivacyClass.PUBLIC_QUOTA,
        prompt_sha256=digest,
        fill_id=fill.fill_id,
    )
    assert receipt.provenance.observed_model == "4 Argon"
    assert "对方节点和模型厂商能看到请求内容" in receipt.warning
    with pytest.raises(MarketError, match="exact source"):
        item.authorize_invocation(
            caller_id="buyer_one",
            provenance=listing.provenance,
            privacy=PrivacyClass.PUBLIC_QUOTA,
            prompt_sha256=digest,
            fill_id="missing",
        )
    with pytest.raises(MarketError, match="sha256"):
        item.authorize_invocation(
            caller_id="buyer_one",
            provenance=listing.provenance,
            privacy=PrivacyClass.PUBLIC_QUOTA,
            prompt_sha256="hello",
            fill_id=fill.fill_id,
        )
