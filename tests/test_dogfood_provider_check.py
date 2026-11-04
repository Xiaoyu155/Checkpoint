from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

import visual_agent.dogfood_provider_check as provider_check
from visual_agent.dogfood_provider_check import (
    PROVIDER_CHECK_MARKER,
    dogfood_provider_receipt_to_markdown,
    run_dogfood_provider_check,
    verify_dogfood_provider_receipt,
)


_SECRET = "provider-secret-that-must-not-appear"
_BASE_URL = "https://provider.example/v1"
_INPUTS = {
    "provider_id": "sub2api",
    "base_url": _BASE_URL,
    "model": "codex-test-model",
    "key_env": "SUB2API_KEY",
    "executable": "codex",
}


def _clock(*values: float):
    iterator = iter(values)
    return lambda: next(iterator)


def _passing_receipt() -> dict[str, object]:
    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, PROVIDER_CHECK_MARKER, "")

    return run_dogfood_provider_check(
        **_INPUTS,
        environ={"SUB2API_KEY": _SECRET},
        runner=runner,
        monotonic=_clock(1.0, 1.125),
    )


def test_provider_check_runs_codex_in_empty_read_only_directory_with_minimal_env() -> None:
    observed: dict[str, object] = {}

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = Path(str(kwargs["cwd"]))
        observed["argv"] = argv
        observed["cwd_empty"] = list(cwd.iterdir()) == []
        observed["cwd_mode"] = stat.S_IMODE(cwd.stat().st_mode)
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, PROVIDER_CHECK_MARKER + "\n", "ignored")

    result = run_dogfood_provider_check(
        **_INPUTS,
        environ={
            "SUB2API_KEY": _SECRET,
            "PATH": "tools",
            "UNRELATED_SECRET": "must-not-be-forwarded",
        },
        runner=runner,
        monotonic=_clock(1.0, 1.125),
    )

    assert verify_dogfood_provider_receipt(result) is True
    assert len(result["receipt_digest"]) == 64
    int(result["receipt_digest"], 16)
    assert {key: value for key, value in result.items() if key != "receipt_digest"} == {
        "schema_version": 1,
        "kind": "pacer_dogfood_provider_check",
        "status": "passed",
        "passed": True,
        "reason_codes": [],
        "provider_id": "sub2api",
        "model": "codex-test-model",
        "wire_api": "responses",
        "sandbox": "read-only",
        "exit_code": 0,
        "marker_matched": True,
        "duration_ms": 125,
    }
    argv = observed["argv"]
    assert isinstance(argv, list)
    assert "--ignore-user-config" in argv
    assert "model_providers.sub2api.wire_api='responses'" in argv
    assert "model_providers.sub2api.env_key='SUB2API_KEY'" in argv
    assert _SECRET not in repr(argv)
    assert observed["cwd_empty"] is True
    assert int(observed["cwd_mode"]) & stat.S_IWUSR == 0
    assert observed["env"] == {
        "PATH": "tools",
        "SUB2API_KEY": _SECRET,
        "NO_COLOR": "1",
    }
    assert _SECRET not in repr(result)
    assert _BASE_URL not in repr(result)
    assert "SUB2API_KEY" not in repr(result)


def test_receipt_digest_is_canonical_and_detects_required_field_tampering() -> None:
    receipt = _passing_receipt()
    reordered = dict(reversed(list(receipt.items())))

    assert verify_dogfood_provider_receipt(reordered) is True
    assert provider_check._receipt_digest(reordered) == receipt["receipt_digest"]

    changes = {
        "status": "failed",
        "provider_id": "other-provider",
        "model": "other-model",
        "exit_code": 9,
        "marker_matched": False,
        "duration_ms": 126,
    }
    for field, value in changes.items():
        tampered = {**receipt, field: value}
        assert provider_check._receipt_digest(tampered) != receipt["receipt_digest"]
        assert verify_dogfood_provider_receipt(tampered) is False


def test_receipt_verification_fails_closed_for_malformed_receipts() -> None:
    receipt = _passing_receipt()

    assert verify_dogfood_provider_receipt(None) is False
    assert verify_dogfood_provider_receipt({}) is False
    assert verify_dogfood_provider_receipt({**receipt, "schema_version": 2}) is False
    assert verify_dogfood_provider_receipt(
        {key: value for key, value in receipt.items() if key != "receipt_digest"}
    ) is False
    assert verify_dogfood_provider_receipt({**receipt, "receipt_digest": "invalid"}) is False
    assert verify_dogfood_provider_receipt({**receipt, "base_url": _BASE_URL}) is False


def test_receipt_digest_never_hashes_private_provider_inputs(monkeypatch) -> None:
    original_sha256 = provider_check.hashlib.sha256
    hashed_payloads: list[bytes] = []

    def recording_sha256(value: bytes):
        hashed_payloads.append(bytes(value))
        return original_sha256(value)

    monkeypatch.setattr(provider_check.hashlib, "sha256", recording_sha256)
    result = _passing_receipt()

    hashed = b"".join(hashed_payloads)
    assert _BASE_URL.encode() not in hashed
    assert _SECRET.encode() not in hashed
    assert b"SUB2API_KEY" not in hashed
    assert _BASE_URL not in repr(result)
    assert _SECRET not in repr(result)
    assert "SUB2API_KEY" not in repr(result)


def test_provider_check_fails_closed_when_credential_is_missing() -> None:
    called = False

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("runner must not be called")

    result = run_dogfood_provider_check(**_INPUTS, environ={}, runner=runner)

    assert result["passed"] is False
    assert result["reason_codes"] == ["provider_check_missing_credential"]
    assert result["exit_code"] is None
    assert called is False


def test_provider_check_fails_closed_on_timeout() -> None:
    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    result = run_dogfood_provider_check(
        **_INPUTS,
        environ={"SUB2API_KEY": _SECRET},
        runner=runner,
    )

    assert result["reason_codes"] == ["provider_check_timeout"]
    assert result["marker_matched"] is False
    assert _SECRET not in repr(result)


def test_provider_check_fails_closed_on_nonzero_without_returning_output() -> None:
    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 17, _SECRET, _SECRET)

    result = run_dogfood_provider_check(
        **_INPUTS,
        environ={"SUB2API_KEY": _SECRET},
        runner=runner,
    )

    assert result["reason_codes"] == ["provider_check_nonzero_exit"]
    assert result["exit_code"] == 17
    assert _SECRET not in repr(result)


def test_provider_check_fails_closed_if_workspace_is_modified() -> None:
    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        cwd = Path(str(kwargs["cwd"]))
        cwd.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        (cwd / "unexpected.txt").write_text("changed", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, PROVIDER_CHECK_MARKER, "")

    result = run_dogfood_provider_check(
        **_INPUTS,
        environ={"SUB2API_KEY": _SECRET},
        runner=runner,
    )

    assert result["reason_codes"] == ["provider_check_workspace_modified"]


@pytest.mark.parametrize("stdout", ["", "almost", PROVIDER_CHECK_MARKER + " extra"])
def test_provider_check_fails_closed_on_marker_mismatch(stdout: str) -> None:
    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    result = run_dogfood_provider_check(
        **_INPUTS,
        environ={"SUB2API_KEY": _SECRET},
        runner=runner,
    )

    assert result["reason_codes"] == ["provider_check_marker_mismatch"]
    assert result["marker_matched"] is False


def test_provider_check_rejects_endpoint_credentials_without_echoing_input() -> None:
    result = run_dogfood_provider_check(
        **{**_INPUTS, "base_url": "https://user:password@provider.example/v1"},
        environ={"SUB2API_KEY": _SECRET},
    )

    assert result["reason_codes"] == ["provider_check_invalid_input"]
    assert "password" not in repr(result)
    assert "provider.example" not in repr(result)


def test_provider_check_markdown_contains_only_public_receipt_fields() -> None:
    digest = "a" * 64
    markdown = dogfood_provider_receipt_to_markdown(
        {
            "status": "passed",
            "provider_id": "sub2api",
            "model": "gpt-test",
            "wire_api": "responses",
            "sandbox": "read-only",
            "exit_code": 0,
            "marker_matched": True,
            "duration_ms": 12,
            "reason_codes": [],
            "receipt_digest": digest,
        }
    )

    assert "Status: `passed`" in markdown
    assert "Duration: `12 ms`" in markdown
    assert f"Receipt digest: `{digest}`" in markdown
    assert _BASE_URL not in markdown
    assert _SECRET not in markdown
