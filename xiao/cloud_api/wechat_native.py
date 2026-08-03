from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


WECHAT_API_BASE = "https://api.mch.weixin.qq.com"
_PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")
_OUT_TRADE_NO = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
_CLOSE_RETRYABLE_CODES = frozenset({"ORDER_NOT_EXIST", "SYSTEM_ERROR", "FREQUENCY_LIMITED"})


class WechatNativeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        provider_code: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.provider_code = str(provider_code or "")[:80]


@dataclass(frozen=True)
class CreditPackage:
    id: str
    name: str
    description: str
    amount_fen: int
    credit_microusd: int

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "amount_fen": self.amount_fen,
            "credit_microusd": self.credit_microusd,
            "currency": "CNY",
        }


@dataclass(frozen=True)
class WechatNativeConfig:
    app_id: str
    mch_id: str
    cert_serial_no: str
    private_key_path: Path | None
    private_key_pem: str
    api_v3_key: str
    notify_url: str
    platform_public_key_id: str = ""
    platform_public_key_path: Path | None = None
    platform_cert_path: Path | None = None

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None
    ) -> WechatNativeConfig:
        values = os.environ if env is None else env

        def first(*names: str) -> str:
            return next(
                (str(values.get(name) or "").strip() for name in names if str(values.get(name) or "").strip()),
                "",
            )

        def optional_path(*names: str) -> Path | None:
            value = first(*names)
            return Path(value).expanduser().resolve() if value else None

        config = cls(
            app_id=first("PACER_WECHAT_APP_ID", "WX_APPID", "WECHAT_PAY_APP_ID"),
            mch_id=first("PACER_WECHAT_MCH_ID", "WXPAY_MCH_ID", "WECHAT_PAY_MCH_ID"),
            cert_serial_no=first(
                "PACER_WECHAT_CERT_SERIAL_NO",
                "WXPAY_CERT_SERIAL_NO",
                "WECHAT_PAY_CERT_SERIAL_NO",
            ),
            private_key_path=optional_path(
                "PACER_WECHAT_PRIVATE_KEY_PATH",
                "WXPAY_PRIVATE_KEY_PATH",
                "WECHAT_PAY_PRIVATE_KEY_PATH",
            ),
            private_key_pem=first("PACER_WECHAT_PRIVATE_KEY", "WXPAY_PRIVATE_KEY"),
            api_v3_key=first(
                "PACER_WECHAT_API_V3_KEY", "WXPAY_API_V3_KEY", "WECHAT_PAY_API_V3_KEY"
            ),
            notify_url=first(
                "PACER_WECHAT_NOTIFY_URL", "WXPAY_NOTIFY_URL", "WECHAT_PAY_NOTIFY_URL"
            ),
            platform_public_key_id=first(
                "PACER_WECHAT_PLATFORM_PUBLIC_KEY_ID", "WXPAY_PLATFORM_PUBLIC_KEY_ID"
            ),
            platform_public_key_path=optional_path(
                "PACER_WECHAT_PLATFORM_PUBLIC_KEY_PATH",
                "WXPAY_PLATFORM_PUBLIC_KEY_PATH",
            ),
            platform_cert_path=optional_path(
                "PACER_WECHAT_PLATFORM_CERT_PATH",
                "WXPAY_PLATFORM_CERT_PATH",
                "WECHAT_PAY_PLATFORM_CERT_PATH",
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("app_id", self.app_id),
                ("mch_id", self.mch_id),
                ("cert_serial_no", self.cert_serial_no),
                ("api_v3_key", self.api_v3_key),
                ("notify_url", self.notify_url),
            )
            if not value
        ]
        if not self.private_key_pem and self.private_key_path is None:
            missing.append("private_key")
        if missing:
            raise WechatNativeError(
                "wechat_not_configured",
                "WeChat Native payment is missing required server configuration.",
                status_code=503,
            )
        if not re.fullmatch(r"wx[0-9a-fA-F]{16}", self.app_id):
            raise WechatNativeError("invalid_wechat_config", "WeChat AppID is invalid.", status_code=503)
        if not self.mch_id.isdigit() or not (8 <= len(self.mch_id) <= 32):
            raise WechatNativeError("invalid_wechat_config", "WeChat merchant id is invalid.", status_code=503)
        if not re.fullmatch(r"[0-9A-Fa-f]{32,64}", self.cert_serial_no):
            raise WechatNativeError(
                "invalid_wechat_config", "WeChat merchant certificate serial is invalid.", status_code=503
            )
        if len(self.api_v3_key.encode("utf-8")) != 32:
            raise WechatNativeError(
                "invalid_wechat_config", "WeChat API v3 key must be exactly 32 bytes.", status_code=503
            )
        parsed = urllib.parse.urlsplit(self.notify_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise WechatNativeError(
                "invalid_wechat_config",
                "WeChat notify URL must be a public HTTPS URL without query or fragment.",
                status_code=503,
            )
        if self.private_key_path is not None and not self.private_key_path.is_file():
            raise WechatNativeError(
                "invalid_wechat_config", "WeChat merchant private-key file was not found.", status_code=503
            )

    def private_key_bytes(self) -> bytes:
        if self.private_key_pem:
            return self.private_key_pem.replace("\\n", "\n").encode("utf-8")
        if self.private_key_path is None:
            raise WechatNativeError("wechat_not_configured", "Merchant private key is missing.", status_code=503)
        return self.private_key_path.read_bytes()


@dataclass(frozen=True)
class WechatHttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]


WechatTransport = Callable[[str, str, Mapping[str, str], bytes | None], WechatHttpResponse]


def load_credit_packages(env: Mapping[str, str] | None = None) -> tuple[CreditPackage, ...]:
    values = os.environ if env is None else env
    raw = str(values.get("PACER_WECHAT_CREDIT_PACKAGES_JSON") or "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WechatNativeError(
            "invalid_payment_packages", "Credit package configuration is not valid JSON.", status_code=503
        ) from exc
    if not isinstance(parsed, list) or not 1 <= len(parsed) <= 12:
        raise WechatNativeError(
            "invalid_payment_packages", "Credit packages must contain between 1 and 12 items.", status_code=503
        )
    result: list[CreditPackage] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            raise WechatNativeError(
                "invalid_payment_packages", "Each credit package must be an object.", status_code=503
            )
        package_id = str(item.get("id") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        description = str(item.get("description") or name).strip()
        try:
            amount_fen = int(item.get("amount_fen"))
            credit_microusd = int(item.get("credit_microusd"))
        except (TypeError, ValueError) as exc:
            raise WechatNativeError(
                "invalid_payment_packages", "Credit package amounts must be integers.", status_code=503
            ) from exc
        if (
            not _PACKAGE_ID.fullmatch(package_id)
            or package_id in seen
            or not name
            or len(name) > 80
            or not description
            or len(description) > 120
            or not 1 <= amount_fen <= 100_000_000
            or not 1 <= credit_microusd <= 10_000_000_000_000
        ):
            raise WechatNativeError(
                "invalid_payment_packages", "Credit package fields are invalid or duplicated.", status_code=503
            )
        seen.add(package_id)
        result.append(
            CreditPackage(
                id=package_id,
                name=name,
                description=description,
                amount_fen=amount_fen,
                credit_microusd=credit_microusd,
            )
        )
    return tuple(result)


def payment_readiness(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    reasons: list[str] = []
    try:
        config = WechatNativeConfig.from_env(env)
        serialization.load_pem_private_key(config.private_key_bytes(), password=None)
    except (WechatNativeError, OSError, ValueError, TypeError):
        reasons.append("wechat_credentials_not_ready")
    try:
        packages = load_credit_packages(env)
    except WechatNativeError:
        packages = ()
        reasons.append("credit_packages_invalid")
    if not packages and "credit_packages_invalid" not in reasons:
        reasons.append("credit_packages_missing")
    return {
        "ready": not reasons,
        "provider": "wechat_native",
        "reason_codes": reasons,
        "package_count": len(packages),
    }


def generate_out_trade_no() -> str:
    return f"PCR{datetime.now(timezone.utc):%y%m%d%H%M%S}{secrets.token_hex(7).upper()}"


def qr_png_data_url(value: str) -> str:
    if not str(value or "").startswith("weixin://"):
        raise WechatNativeError("invalid_code_url", "WeChat did not return a Native code URL.")
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError as exc:
        raise WechatNativeError(
            "qr_dependency_missing", "Install the cloud payment dependencies to render QR codes.", status_code=503
        ) from exc
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_M, box_size=7, border=3)
    qr.add_data(value)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#17201d", back_color="#ffffff")
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


class WechatNativeClient:
    def __init__(
        self,
        config: WechatNativeConfig,
        *,
        transport: WechatTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        loaded = serialization.load_pem_private_key(config.private_key_bytes(), password=None)
        if not isinstance(loaded, rsa.RSAPrivateKey) or loaded.key_size < 2048:
            raise WechatNativeError(
                "invalid_wechat_config", "Merchant private key must be RSA 2048 bits or stronger.", status_code=503
            )
        self._private_key = loaded
        self._transport = transport or _urllib_transport
        self._now = now
        self._platform_keys: dict[str, rsa.RSAPublicKey] = {}
        self._cert_lock = threading.Lock()
        self._last_certificate_refresh_at = 0.0
        self._load_configured_platform_keys()

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        transport: WechatTransport | None = None,
    ) -> WechatNativeClient:
        return cls(WechatNativeConfig.from_env(env), transport=transport)

    def create_order(
        self,
        *,
        out_trade_no: str,
        description: str,
        amount_fen: int,
        expires_at: float,
    ) -> dict[str, Any]:
        if not _OUT_TRADE_NO.fullmatch(out_trade_no):
            raise WechatNativeError("invalid_order", "WeChat order number is invalid.", status_code=400)
        expiry = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(timespec="seconds")
        payload = {
            "appid": self.config.app_id,
            "mchid": self.config.mch_id,
            "description": str(description or "Pacer API credit")[:127],
            "out_trade_no": out_trade_no,
            "time_expire": expiry,
            "notify_url": self.config.notify_url,
            "amount": {"total": int(amount_fen), "currency": "CNY"},
        }
        result = self._request_json("POST", "/v3/pay/transactions/native", payload)
        code_url = str(result.get("code_url") or "")
        if not code_url.startswith("weixin://"):
            raise WechatNativeError("invalid_wechat_response", "WeChat response did not include a Native code URL.")
        return {"code_url": code_url}

    def query_order(self, out_trade_no: str) -> dict[str, Any]:
        path = f"/v3/pay/transactions/out-trade-no/{urllib.parse.quote(out_trade_no)}"
        query = urllib.parse.urlencode({"mchid": self.config.mch_id})
        return self._request_json("GET", f"{path}?{query}")

    def close_order(self, out_trade_no: str) -> None:
        path = f"/v3/pay/transactions/out-trade-no/{urllib.parse.quote(out_trade_no)}/close"
        # Native orders can be briefly invisible to the close endpoint just
        # after creation. Retry only provider errors known to be transient; a
        # genuine invalid order still surfaces to the caller unchanged.
        for attempt in range(3):
            try:
                self._request("POST", path, {"mchid": self.config.mch_id})
                return
            except WechatNativeError as exc:
                if exc.provider_code not in _CLOSE_RETRYABLE_CODES or attempt >= 2:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def verify_notification(self, headers: Mapping[str, str], body: bytes) -> None:
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        self._verify_signature_headers(normalized, body)

    def decrypt_notification(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        resource = payload.get("resource") if isinstance(payload, Mapping) else None
        if not isinstance(resource, Mapping) or resource.get("algorithm") != "AEAD_AES_256_GCM":
            raise WechatNativeError("invalid_notification", "WeChat notification resource is invalid.", status_code=400)
        try:
            plaintext = AESGCM(self.config.api_v3_key.encode("utf-8")).decrypt(
                str(resource.get("nonce") or "").encode("utf-8"),
                base64.b64decode(str(resource.get("ciphertext") or ""), validate=True),
                str(resource.get("associated_data") or "").encode("utf-8"),
            )
            result = json.loads(plaintext.decode("utf-8"))
        except Exception as exc:
            raise WechatNativeError(
                "notification_decryption_failed", "WeChat notification could not be decrypted.", status_code=400
            ) from exc
        if not isinstance(result, dict):
            raise WechatNativeError("invalid_notification", "WeChat transaction payload is invalid.", status_code=400)
        return result

    def _request_json(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        response = self._request(method, path, payload)
        if not response.body:
            return {}
        try:
            parsed = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WechatNativeError("invalid_wechat_response", "WeChat returned invalid JSON.") from exc
        if not isinstance(parsed, dict):
            raise WechatNativeError("invalid_wechat_response", "WeChat returned an invalid response object.")
        return parsed

    def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> WechatHttpResponse:
        body_text = "" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        body = body_text.encode("utf-8") if payload is not None else None
        headers = self._authorization_headers(method, path, body_text)
        response = self._transport(method, WECHAT_API_BASE + path, headers, body)
        normalized = WechatHttpResponse(
            status_code=int(response.status_code),
            body=bytes(response.body or b""),
            headers={str(key).lower(): str(value) for key, value in response.headers.items()},
        )
        if 200 <= normalized.status_code < 300:
            self._verify_signature_headers(normalized.headers, normalized.body)
            return normalized
        try:
            error = json.loads(normalized.body.decode("utf-8")) if normalized.body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            error = {}
        provider_code = str(error.get("code") or "") if isinstance(error, dict) else ""
        provider_message = str(error.get("message") or "") if isinstance(error, dict) else ""
        raise WechatNativeError(
            "wechat_api_error",
            provider_message[:180] or f"WeChat API returned HTTP {normalized.status_code}.",
            status_code=502,
            provider_code=provider_code,
        )

    def _authorization_headers(self, method: str, path: str, body: str) -> dict[str, str]:
        timestamp = str(int(self._now()))
        nonce = secrets.token_hex(16)
        message = f"{method}\n{path}\n{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
        signature = base64.b64encode(
            self._private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        ).decode("ascii")
        authorization = (
            "WECHATPAY2-SHA256-RSA2048 "
            f'mchid="{self.config.mch_id}",nonce_str="{nonce}",timestamp="{timestamp}",'
            f'serial_no="{self.config.cert_serial_no}",signature="{signature}"'
        )
        return {
            "Authorization": authorization,
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "Pacer-Gateway-WeChat-Native/1.0",
        }

    def _verify_signature_headers(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        extra_keys: Mapping[str, rsa.RSAPublicKey] | None = None,
        refresh_unknown: bool = True,
    ) -> None:
        timestamp = str(headers.get("wechatpay-timestamp") or "")
        nonce = str(headers.get("wechatpay-nonce") or "")
        signature = str(headers.get("wechatpay-signature") or "")
        serial = str(headers.get("wechatpay-serial") or "")
        try:
            timestamp_int = int(timestamp)
        except ValueError as exc:
            raise WechatNativeError(
                "wechat_signature_invalid", "WeChat signature headers are missing or invalid.", status_code=401
            ) from exc
        if not nonce or not signature or not serial or abs(self._now() - timestamp_int) > 300:
            raise WechatNativeError(
                "wechat_signature_invalid", "WeChat signature headers are missing, expired, or invalid.", status_code=401
            )
        key = (extra_keys or {}).get(serial) or self._platform_keys.get(serial)
        if key is None and refresh_unknown:
            self._refresh_platform_certificates()
            key = self._platform_keys.get(serial)
        if key is None:
            raise WechatNativeError(
                "wechat_platform_key_unknown", "WeChat response used an unknown platform key.", status_code=401
            )
        message = f"{timestamp}\n{nonce}\n".encode("utf-8") + body + b"\n"
        try:
            key.verify(base64.b64decode(signature, validate=True), message, padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError) as exc:
            raise WechatNativeError(
                "wechat_signature_invalid", "WeChat response signature verification failed.", status_code=401
            ) from exc

    def _refresh_platform_certificates(self) -> None:
        with self._cert_lock:
            if self._last_certificate_refresh_at and (
                self._now() - self._last_certificate_refresh_at < 30
            ):
                return
            path = "/v3/certificates"
            headers = self._authorization_headers("GET", path, "")
            response = self._transport("GET", WECHAT_API_BASE + path, headers, None)
            if int(response.status_code) != 200:
                raise WechatNativeError(
                    "wechat_certificate_refresh_failed", "Unable to refresh WeChat platform certificates."
                )
            try:
                payload = json.loads(bytes(response.body).decode("utf-8"))
                loaded: dict[str, rsa.RSAPublicKey] = {}
                for item in payload.get("data") or []:
                    encrypted = item["encrypt_certificate"]
                    plaintext = AESGCM(self.config.api_v3_key.encode("utf-8")).decrypt(
                        str(encrypted["nonce"]).encode("utf-8"),
                        base64.b64decode(str(encrypted["ciphertext"]), validate=True),
                        str(encrypted.get("associated_data") or "").encode("utf-8"),
                    )
                    certificate = x509.load_pem_x509_certificate(plaintext)
                    public_key = certificate.public_key()
                    if not isinstance(public_key, rsa.RSAPublicKey):
                        continue
                    now = datetime.fromtimestamp(self._now(), tz=timezone.utc)
                    if certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
                        loaded[str(item["serial_no"])] = public_key
                if not loaded:
                    raise ValueError("no active platform certificate")
            except Exception as exc:
                raise WechatNativeError(
                    "wechat_certificate_refresh_failed", "WeChat platform certificates could not be decrypted."
                ) from exc
            normalized_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
            self._verify_signature_headers(
                normalized_headers,
                bytes(response.body),
                extra_keys=loaded,
                refresh_unknown=False,
            )
            self._platform_keys.update(loaded)
            self._last_certificate_refresh_at = self._now()

    def _load_configured_platform_keys(self) -> None:
        if self.config.platform_public_key_id and self.config.platform_public_key_path:
            try:
                key = serialization.load_pem_public_key(
                    self.config.platform_public_key_path.read_bytes()
                )
            except (OSError, ValueError, TypeError):
                key = None
            if isinstance(key, rsa.RSAPublicKey):
                self._platform_keys[self.config.platform_public_key_id] = key
        if self.config.platform_cert_path:
            try:
                certificate = x509.load_pem_x509_certificate(
                    self.config.platform_cert_path.read_bytes()
                )
                key = certificate.public_key()
            except (OSError, ValueError):
                return
            if isinstance(key, rsa.RSAPublicKey):
                self._platform_keys[format(certificate.serial_number, "X")] = key


def _urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> WechatHttpResponse:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return WechatHttpResponse(response.status, response.read(), dict(response.headers.items()))
    except urllib.error.HTTPError as exc:
        return WechatHttpResponse(exc.code, exc.read(), dict(exc.headers.items()))
    except (OSError, urllib.error.URLError) as exc:
        raise WechatNativeError("wechat_network_error", "Unable to reach WeChat Pay.") from exc


def payment_expiry(*, now: float | None = None, minutes: int = 15) -> float:
    base = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    return (base + timedelta(minutes=max(5, min(minutes, 120)))).timestamp()
