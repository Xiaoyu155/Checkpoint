from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

from cloud_api.wechat_native import (
    WechatHttpResponse,
    WechatNativeClient,
    WechatNativeConfig,
    WechatNativeError,
    load_credit_packages,
    payment_readiness,
    qr_png_data_url,
)


def _private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _private_pem(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _public_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _certificate(key: rsa.RSAPrivateKey, serial: int = 0x1234ABCD) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "WeChat Pay Test")])
    now = datetime.now(timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )


def _config(
    merchant_key: rsa.RSAPrivateKey,
    *,
    public_key_id: str = "",
    public_key_path: Path | None = None,
) -> WechatNativeConfig:
    return WechatNativeConfig(
        app_id="wx0123456789abcdef",
        mch_id="1900000109",
        cert_serial_no="A" * 40,
        private_key_path=None,
        private_key_pem=_private_pem(merchant_key),
        api_v3_key="0123456789abcdef0123456789abcdef",
        notify_url="https://api.example.test/api/gateway/billing/wechat/notify",
        platform_public_key_id=public_key_id,
        platform_public_key_path=public_key_path,
    )


def _signed_headers(
    key: rsa.RSAPrivateKey,
    serial: str,
    body: bytes,
    *,
    timestamp: int | None = None,
) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    nonce = "native-test-nonce"
    message = f"{stamp}\n{nonce}\n".encode() + body + b"\n"
    signature = base64.b64encode(
        key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    return {
        "Wechatpay-Timestamp": stamp,
        "Wechatpay-Nonce": nonce,
        "Wechatpay-Serial": serial,
        "Wechatpay-Signature": signature,
    }


def test_credit_packages_are_server_defined_and_strict() -> None:
    env = {
        "PACER_WECHAT_CREDIT_PACKAGES_JSON": json.dumps(
            [
                {
                    "id": "starter",
                    "name": "Starter",
                    "description": "Pacer API credit",
                    "amount_fen": 100,
                    "credit_microusd": 2_000_000,
                }
            ]
        )
    }
    packages = load_credit_packages(env)
    assert packages[0].public() == {
        "id": "starter",
        "name": "Starter",
        "description": "Pacer API credit",
        "amount_fen": 100,
        "credit_microusd": 2_000_000,
        "currency": "CNY",
    }
    duplicated = json.loads(env["PACER_WECHAT_CREDIT_PACKAGES_JSON"]) * 2
    with pytest.raises(WechatNativeError) as exc_info:
        load_credit_packages(
            {"PACER_WECHAT_CREDIT_PACKAGES_JSON": json.dumps(duplicated)}
        )
    assert exc_info.value.code == "invalid_payment_packages"


def test_payment_readiness_requires_credentials_and_packages() -> None:
    readiness = payment_readiness({})
    assert readiness["ready"] is False
    assert set(readiness["reason_codes"]) == {
        "wechat_credentials_not_ready",
        "credit_packages_missing",
    }


def test_native_create_signs_request_and_verifies_response(tmp_path: Path) -> None:
    merchant_key = _private_key()
    platform_key = _private_key()
    platform_path = tmp_path / "wechatpay_public.pem"
    platform_path.write_bytes(_public_pem(platform_key))
    serial = "PUB_KEY_ID_0111111111"

    def transport(method, url, headers, body):
        assert method == "POST"
        assert url.endswith("/v3/pay/transactions/native")
        payload = json.loads(body)
        assert payload["amount"] == {"total": 100, "currency": "CNY"}
        auth = headers["Authorization"]
        fields = {
            part.split("=", 1)[0]: part.split("=", 1)[1].strip('"')
            for part in auth.split(" ", 1)[1].split(",")
        }
        request_message = (
            f"POST\n/v3/pay/transactions/native\n{fields['timestamp']}\n"
            f"{fields['nonce_str']}\n{body.decode()}\n"
        ).encode()
        merchant_key.public_key().verify(
            base64.b64decode(fields["signature"]),
            request_message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        response_body = b'{"code_url":"weixin://wxpay/bizpayurl?pr=test"}'
        return WechatHttpResponse(
            200, response_body, _signed_headers(platform_key, serial, response_body)
        )

    client = WechatNativeClient(
        _config(
            merchant_key,
            public_key_id=serial,
            public_key_path=platform_path,
        ),
        transport=transport,
    )
    result = client.create_order(
        out_trade_no="PCR260719000000ABCDEF12345678",
        description="Pacer credit",
        amount_fen=100,
        expires_at=time.time() + 900,
    )
    assert result["code_url"].startswith("weixin://")


def test_native_close_retries_transient_provider_visibility_error(monkeypatch) -> None:
    client = WechatNativeClient(_config(_private_key()))
    calls: list[str] = []

    def fake_request(method, path, payload=None):
        del payload
        calls.append(f"{method} {path}")
        if len(calls) == 1:
            raise WechatNativeError(
                "wechat_api_error",
                "order not visible yet",
                provider_code="ORDER_NOT_EXIST",
            )
        return WechatHttpResponse(204, b"", {})

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("cloud_api.wechat_native.time.sleep", lambda _seconds: None)

    client.close_order("PCR260719000000ABCDEF12345678")

    assert len(calls) == 2


def test_unknown_platform_key_refreshes_active_certificate(tmp_path: Path) -> None:
    merchant_key = _private_key()
    stale_key = _private_key()
    current_key = _private_key()
    stale_path = tmp_path / "stale.pem"
    stale_path.write_bytes(_public_pem(stale_key))
    certificate = _certificate(current_key)
    serial = format(certificate.serial_number, "X")
    api_key = b"0123456789abcdef0123456789abcdef"
    nonce = b"certificate"
    associated_data = b"certificate"
    ciphertext = AESGCM(api_key).encrypt(
        nonce,
        certificate.public_bytes(serialization.Encoding.PEM),
        associated_data,
    )
    certificate_body = json.dumps(
        {
            "data": [
                {
                    "serial_no": serial,
                    "encrypt_certificate": {
                        "algorithm": "AEAD_AES_256_GCM",
                        "nonce": nonce.decode(),
                        "associated_data": associated_data.decode(),
                        "ciphertext": base64.b64encode(ciphertext).decode(),
                    },
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    calls: list[str] = []

    def transport(method, url, _headers, _body):
        calls.append(url)
        assert method == "GET"
        assert url.endswith("/v3/certificates")
        return WechatHttpResponse(
            200,
            certificate_body,
            _signed_headers(current_key, serial, certificate_body),
        )

    client = WechatNativeClient(
        _config(
            merchant_key,
            public_key_id="PUB_KEY_ID_STALE",
            public_key_path=stale_path,
        ),
        transport=transport,
    )
    callback_body = b'{"event_type":"TRANSACTION.SUCCESS"}'
    client.verify_notification(
        _signed_headers(current_key, serial, callback_body), callback_body
    )
    assert calls == ["https://api.mch.weixin.qq.com/v3/certificates"]

    unknown_headers = _signed_headers(current_key, "UNKNOWN_SERIAL", callback_body)
    with pytest.raises(WechatNativeError) as unknown:
        client.verify_notification(unknown_headers, callback_body)
    assert unknown.value.code == "wechat_platform_key_unknown"
    assert calls == ["https://api.mch.weixin.qq.com/v3/certificates"]


def test_invalid_or_expired_callback_signature_is_rejected(tmp_path: Path) -> None:
    merchant_key = _private_key()
    platform_key = _private_key()
    path = tmp_path / "platform.pem"
    path.write_bytes(_public_pem(platform_key))
    serial = "PUB_KEY_ID_CURRENT"
    client = WechatNativeClient(
        _config(merchant_key, public_key_id=serial, public_key_path=path)
    )
    body = b"{}"
    headers = _signed_headers(platform_key, serial, body)
    headers["Wechatpay-Signature"] = base64.b64encode(b"not-a-signature").decode()
    with pytest.raises(WechatNativeError) as invalid:
        client.verify_notification(headers, body)
    assert invalid.value.code == "wechat_signature_invalid"

    expired_headers = _signed_headers(
        platform_key, serial, body, timestamp=int(time.time()) - 301
    )
    with pytest.raises(WechatNativeError) as expired:
        client.verify_notification(expired_headers, body)
    assert expired.value.code == "wechat_signature_invalid"


def test_qr_png_data_url_is_a_real_png() -> None:
    result = qr_png_data_url("weixin://wxpay/bizpayurl?pr=unit-test")
    raw = base64.b64decode(result.split(",", 1)[1])
    assert result.startswith("data:image/png;base64,")
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(raw) > 300
