"""Quota trades between Pacer users, with source identity and phishing checks.

Listings carry a fingerprint of a credential, never the credential. Invocation
authorization carries a hash of the prompt, never the prompt. Seller-supplied
trust labels and extra hosts are rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

_OWNER = re.compile(r"[A-Za-z0-9_.@-]{1,64}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SECRET_FIELDS = frozenset(
    {
        "key",
        "api_key",
        "raw_key",
        "token",
        "access_token",
        "cookie",
        "password",
        "psid",
        "psidts",
        "prompt",
        "messages",
        "authorization",
        "trust_tier",
        "secret",
    }
)


class MarketError(ValueError):
    """A listing, confirmation, or invocation violates the market rules."""


class SourceKind(str, Enum):
    OFFICIAL_API = "official_api"
    FIRST_PARTY_GATEWAY = "first_party_gateway"
    DESKTOP_SESSION = "desktop_session"


class PrivacyClass(str, Enum):
    CONFIDENTIAL = "confidential"
    PUBLIC_QUOTA = "public_quota"


@dataclass(frozen=True)
class _HostRule:
    provider: str
    kinds: frozenset[SourceKind]
    prefixes: tuple[str, ...]
    port: int
    observed_markers: tuple[str, ...] = ()


_HOSTS: dict[str, _HostRule] = {
    "generativelanguage.googleapis.com": _HostRule(
        "google", frozenset({SourceKind.OFFICIAL_API}), ("gemini-",), 443
    ),
    "api.x.ai": _HostRule("xai", frozenset({SourceKind.OFFICIAL_API}), ("grok-",), 443),
    "direct.aiboxz.com": _HostRule(
        "google",
        frozenset({SourceKind.FIRST_PARTY_GATEWAY}),
        ("gemini-4",),
        443,
        ("argon", "gemini 4"),
    ),
    "127.0.0.1": _HostRule(
        "workbuddy",
        frozenset({SourceKind.DESKTOP_SESSION}),
        ("workbuddy", "deepseek", "kimi", "glm"),
        0,
    ),
}


def credential_fingerprint(secret: str) -> str:
    """Return the only credential form the market stores."""
    if not secret or any(ch.isspace() for ch in secret):
        raise MarketError("credential is empty")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:16]


def _reject_secret_fields(payload: Mapping[str, object]) -> None:
    found = sorted(set(payload) & _SECRET_FIELDS)
    if found:
        raise MarketError("listing contains a secret or prompt field: " + ", ".join(found))


def _owner(value: str) -> str:
    if not isinstance(value, str) or not _OWNER.fullmatch(value):
        raise MarketError("owner id is invalid")
    return value


def _model_name(value: str) -> str:
    name = str(value or "").strip()
    if not name or len(name) > 80 or any(ch in name for ch in "\r\n\t") or not name.isprintable():
        raise MarketError("model name is invalid")
    return name


def _family_matches(advertised: str, observed: str, rule: _HostRule) -> None:
    left = advertised.lower()
    right = observed.lower()
    if not any(left.startswith(prefix) for prefix in rule.prefixes):
        raise MarketError("advertised model is not sold at this host")
    if rule.observed_markers:
        if not any(marker in right for marker in rule.observed_markers):
            raise MarketError("observed model does not match the host")
        return
    if right != left and not right.startswith(left):
        raise MarketError("observed model does not match the advertised model")


@dataclass(frozen=True)
class TokenProvenance:
    """Identity of one quota source and the model that source actually ran."""

    owner_id: str
    source_kind: SourceKind
    provider: str
    endpoint_host: str
    endpoint_port: int
    advertised_model: str
    observed_model: str
    credential_fingerprint: str

    def __post_init__(self) -> None:
        owner = _owner(self.owner_id)
        host = str(self.endpoint_host or "").strip().lower().rstrip(".")
        if "://" in host or "/" in host or "@" in host or not host.isascii():
            raise MarketError("endpoint must be a bare allowlisted host")
        rule = _HOSTS.get(host)
        if rule is None:
            raise MarketError("endpoint host is not allowlisted")
        kind = self.source_kind if isinstance(self.source_kind, SourceKind) else SourceKind(self.source_kind)
        if kind not in rule.kinds:
            raise MarketError("source kind is not valid for this host")
        port = int(self.endpoint_port)
        expected_port = rule.port if rule.port else port
        if rule.port and port != rule.port:
            raise MarketError("endpoint port is not valid for this host")
        if not 1 <= port <= 65535:
            raise MarketError("endpoint port is invalid")
        advertised = _model_name(self.advertised_model)
        observed = _model_name(self.observed_model)
        _family_matches(advertised, observed, rule)
        fingerprint = str(self.credential_fingerprint or "")
        if not _FINGERPRINT.fullmatch(fingerprint):
            raise MarketError("credential fingerprint must be 16 hex characters")
        if self.provider != rule.provider:
            raise MarketError("provider does not match the host")
        object.__setattr__(self, "owner_id", owner)
        object.__setattr__(self, "source_kind", kind)
        object.__setattr__(self, "endpoint_host", host)
        object.__setattr__(self, "endpoint_port", expected_port)
        object.__setattr__(self, "advertised_model", advertised)
        object.__setattr__(self, "observed_model", observed)
        object.__setattr__(self, "credential_fingerprint", fingerprint)

    def public_card(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id,
            "source_kind": self.source_kind.value,
            "provider": self.provider,
            "endpoint_host": self.endpoint_host,
            "endpoint_port": self.endpoint_port,
            "advertised_model": self.advertised_model,
            "observed_model": self.observed_model,
            "credential_fingerprint": self.credential_fingerprint,
        }

    def canonical(self) -> bytes:
        return json.dumps(self.public_card(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def warning_for(provenance: TokenProvenance, privacy: PrivacyClass) -> str:
    if privacy is PrivacyClass.CONFIDENTIAL:
        return (
            f"这次只使用你自己的官方接口 {provenance.endpoint_host}，"
            f"实际模型是 {provenance.observed_model}。请求不会发给其他用户。"
        )
    return (
        f"额度属于 {provenance.owner_id}。请求将发给 {provenance.endpoint_host}:{provenance.endpoint_port}，"
        f"实际模型是 {provenance.observed_model}。对方节点和模型厂商能看到请求内容。Pacer 不保存提问原文。"
    )


@dataclass(frozen=True)
class Listing:
    listing_id: str
    provenance: TokenProvenance
    units: int
    ask_mills: int
    floor_mills: int
    opened_at: float
    duration_s: float

    def price_mills(self, now: float) -> int:
        elapsed = max(0.0, float(now) - self.opened_at)
        if elapsed >= self.duration_s:
            return self.floor_mills
        span = self.ask_mills - self.floor_mills
        remain = self.duration_s - elapsed
        return self.floor_mills + int(span * remain // self.duration_s)


@dataclass(frozen=True)
class Fill:
    fill_id: str
    buyer_id: str
    listing_id: str
    units: int
    price_mills: int
    provenance: TokenProvenance


@dataclass(frozen=True)
class InvocationReceipt:
    caller_id: str
    privacy: PrivacyClass
    prompt_sha256: str
    provenance: TokenProvenance
    fill_id: str
    warning: str

    def public_dict(self) -> dict[str, object]:
        return {
            "caller_id": self.caller_id,
            "privacy": self.privacy.value,
            "prompt_sha256": self.prompt_sha256,
            "fill_id": self.fill_id,
            "warning": self.warning,
            "provenance": self.provenance.public_card(),
        }


class QuotaMarket:
    """In-process Dutch auction. It has no payment or prompt endpoint."""

    def __init__(self, confirmation_key: bytes):
        if not isinstance(confirmation_key, (bytes, bytearray)) or len(confirmation_key) < 32:
            raise MarketError("confirmation key must be at least 32 bytes")
        self._key = bytes(confirmation_key)
        self._listings: dict[str, Listing] = {}
        self._fills: dict[str, Fill] = {}
        self._holds: dict[str, tuple[str, int]] = {}
        self._available: dict[str, int] = {}
        self._locked: dict[str, int] = {}

    def credit(self, user_id: str, mills: int) -> None:
        """Test and operator hook. Not exposed as a network payment API."""
        user = _owner(user_id)
        amount = int(mills)
        if amount <= 0:
            raise MarketError("credit must be positive")
        self._available[user] = self._available.get(user, 0) + amount

    def offer(
        self,
        provenance: TokenProvenance,
        *,
        seller_id: str,
        units: int,
        ask_mills: int,
        floor_mills: int,
        opened_at: float,
        duration_s: float,
    ) -> Listing:
        if _owner(seller_id) != provenance.owner_id:
            raise MarketError("only the credential owner can list this quota")
        if int(units) <= 0 or int(ask_mills) <= 0 or int(floor_mills) < 0:
            raise MarketError("price and units must be positive")
        if int(floor_mills) > int(ask_mills) or float(duration_s) <= 0:
            raise MarketError("auction window is invalid")
        listing_id = hashlib.sha256(provenance.canonical() + f"|{opened_at}|{units}".encode("utf-8")).hexdigest()[:24]
        listing = Listing(
            listing_id=listing_id,
            provenance=provenance,
            units=int(units),
            ask_mills=int(ask_mills),
            floor_mills=int(floor_mills),
            opened_at=float(opened_at),
            duration_s=float(duration_s),
        )
        self._listings[listing_id] = listing
        return listing

    def offer_from_mapping(self, payload: Mapping[str, object], *, seller_id: str, opened_at: float) -> Listing:
        _reject_secret_fields(payload)
        provenance = TokenProvenance(
            owner_id=str(payload["owner_id"]),
            source_kind=SourceKind(str(payload["source_kind"])),
            provider=str(payload["provider"]),
            endpoint_host=str(payload["endpoint_host"]),
            endpoint_port=int(payload["endpoint_port"]),
            advertised_model=str(payload["advertised_model"]),
            observed_model=str(payload["observed_model"]),
            credential_fingerprint=str(payload["credential_fingerprint"]),
        )
        return self.offer(
            provenance,
            seller_id=seller_id,
            units=int(payload["units"]),
            ask_mills=int(payload["ask_mills"]),
            floor_mills=int(payload["floor_mills"]),
            opened_at=opened_at,
            duration_s=float(payload["duration_s"]),
        )

    def quote(self, listing_id: str, now: float) -> dict[str, object]:
        listing = self._require_listing(listing_id)
        price = listing.price_mills(now)
        card = listing.provenance.public_card()
        card.update(
            {
                "listing_id": listing.listing_id,
                "units_left": listing.units,
                "price_mills": price,
                "privacy": PrivacyClass.PUBLIC_QUOTA.value,
                "warning": warning_for(listing.provenance, PrivacyClass.PUBLIC_QUOTA),
            }
        )
        return card

    def confirm(
        self,
        *,
        buyer_id: str,
        listing_id: str,
        units: int,
        limit_mills: int,
        now: float,
        ttl_s: float = 60.0,
    ) -> str:
        listing = self._require_listing(listing_id)
        if int(units) <= 0 or int(units) > listing.units:
            raise MarketError("units are not available")
        quoted = listing.price_mills(now)
        if int(limit_mills) < quoted:
            raise MarketError("limit is below the current auction price")
        payload = {
            "buyer_id": _owner(buyer_id),
            "listing_id": listing.listing_id,
            "units": int(units),
            "limit_mills": int(limit_mills),
            "quoted_mills": quoted,
            "provenance": listing.provenance.public_card(),
            "expires_at": float(now) + float(ttl_s),
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        mac = hmac.new(self._key, body, hashlib.sha256).hexdigest()
        token = mac + "." + body.hex()
        self._lock(payload["buyer_id"], int(limit_mills) * int(units))
        self._holds[token] = (payload["buyer_id"], int(limit_mills) * int(units))
        return token

    def buy(self, confirmation: str, now: float) -> Fill:
        payload = self._open_confirmation(confirmation, now)
        listing = self._require_listing(payload["listing_id"])
        if listing.provenance.public_card() != payload["provenance"]:
            self._release(confirmation)
            raise MarketError("listing changed after confirmation")
        if listing.price_mills(now) != int(payload["quoted_mills"]):
            self._release(confirmation)
            raise MarketError("auction price changed; confirm again")
        if confirmation not in self._holds:
            raise MarketError("confirmation was already used")
        units = int(payload["units"])
        if units > listing.units:
            self._release(confirmation)
            raise MarketError("units are no longer available")
        price = int(payload["quoted_mills"])
        buyer, held = self._holds.pop(confirmation)
        charge = price * units
        refund = held - charge
        self._locked[buyer] = self._locked.get(buyer, 0) - held
        self._available[buyer] = self._available.get(buyer, 0) + refund
        if self._available[buyer] < 0 or self._locked[buyer] < 0 or refund < 0:
            raise MarketError("balance lock is inconsistent")
        updated = Listing(
            listing_id=listing.listing_id,
            provenance=listing.provenance,
            units=listing.units - units,
            ask_mills=listing.ask_mills,
            floor_mills=listing.floor_mills,
            opened_at=listing.opened_at,
            duration_s=listing.duration_s,
        )
        self._listings[listing.listing_id] = updated
        fill_id = hashlib.sha256(confirmation.encode("utf-8")).hexdigest()[:24]
        fill = Fill(fill_id, buyer, listing.listing_id, units, price, listing.provenance)
        self._fills[fill_id] = fill
        return fill

    def authorize_invocation(
        self,
        *,
        caller_id: str,
        provenance: TokenProvenance,
        privacy: PrivacyClass,
        prompt_sha256: str,
        fill_id: str = "",
    ) -> InvocationReceipt:
        caller = _owner(caller_id)
        digest = str(prompt_sha256 or "")
        if not _SHA256.fullmatch(digest):
            raise MarketError("prompt must be a sha256 hex digest")
        level = privacy if isinstance(privacy, PrivacyClass) else PrivacyClass(privacy)
        if level is PrivacyClass.CONFIDENTIAL:
            if caller != provenance.owner_id:
                raise MarketError("confidential work can only use the caller's own credential")
            if provenance.source_kind is not SourceKind.OFFICIAL_API:
                raise MarketError("confidential work can only use an official API host")
            used_fill = ""
        else:
            fill = self._fills.get(fill_id)
            if fill is None or fill.buyer_id != caller or fill.provenance != provenance:
                raise MarketError("public quota call needs a fill for this exact source")
            used_fill = fill.fill_id
        return InvocationReceipt(
            caller_id=caller,
            privacy=level,
            prompt_sha256=digest,
            provenance=provenance,
            fill_id=used_fill,
            warning=warning_for(provenance, level),
        )

    def _require_listing(self, listing_id: str) -> Listing:
        listing = self._listings.get(listing_id)
        if listing is None or listing.units <= 0:
            raise MarketError("listing is not available")
        return listing

    def _lock(self, buyer_id: str, amount: int) -> None:
        available = self._available.get(buyer_id, 0)
        if available < amount:
            raise MarketError("insufficient available balance")
        self._available[buyer_id] = available - amount
        self._locked[buyer_id] = self._locked.get(buyer_id, 0) + amount

    def _release(self, confirmation: str) -> None:
        hold = self._holds.pop(confirmation, None)
        if hold is None:
            return
        buyer, amount = hold
        self._locked[buyer] = self._locked.get(buyer, 0) - amount
        self._available[buyer] = self._available.get(buyer, 0) + amount

    def _open_confirmation(self, confirmation: str, now: float) -> dict[str, object]:
        try:
            mac, encoded = confirmation.split(".", 1)
            body = bytes.fromhex(encoded)
        except ValueError as exc:
            raise MarketError("confirmation is malformed") from exc
        expected = hmac.new(self._key, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise MarketError("confirmation signature is invalid")
        payload = json.loads(body.decode("utf-8"))
        if float(now) > float(payload["expires_at"]):
            raise MarketError("confirmation expired")
        return payload
