from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .codex_launcher import _native_codex_command


PROVIDER_CHECK_MARKER = "PACER_DOGFOOD_PROVIDER_OK_V1"
DEFAULT_PROVIDER_CHECK_TIMEOUT_SECONDS = 60.0
MAX_PROVIDER_CHECK_TIMEOUT_SECONDS = 300.0

_SAFE_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SHA256_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_RECEIPT_FIELDS = (
    "schema_version",
    "kind",
    "status",
    "passed",
    "reason_codes",
    "provider_id",
    "model",
    "wire_api",
    "sandbox",
    "exit_code",
    "marker_matched",
    "duration_ms",
)
_CHILD_ENV_ALLOWLIST = (
    "ALL_PROXY",
    "APPDATA",
    "CODEX_CA_CERTIFICATE",
    "COMSPEC",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LOCALAPPDATA",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)

ProviderCheckRunner = Callable[..., subprocess.CompletedProcess[str]]
MonotonicClock = Callable[[], float]


def run_dogfood_provider_check(
    *,
    provider_id: str,
    base_url: str,
    model: str,
    key_env: str,
    timeout_seconds: float = DEFAULT_PROVIDER_CHECK_TIMEOUT_SECONDS,
    runner: ProviderCheckRunner = subprocess.run,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    monotonic: MonotonicClock = time.monotonic,
) -> dict[str, Any]:
    """Verify a Responses provider through Codex without returning endpoint secrets."""

    started = monotonic()
    try:
        normalized = _normalize_inputs(
            provider_id=provider_id,
            base_url=base_url,
            model=model,
            key_env=key_env,
            timeout_seconds=timeout_seconds,
        )
    except (TypeError, ValueError):
        return _receipt(
            status="failed",
            reason_code="provider_check_invalid_input",
            provider_id="",
            model="",
            exit_code=None,
            marker_matched=False,
            started=started,
            monotonic=monotonic,
        )

    public = {
        "provider_id": normalized["provider_id"],
        "model": normalized["model"],
    }
    source_environment = os.environ if environ is None else environ
    credential = str(source_environment.get(normalized["key_env"], ""))
    if not credential.strip():
        return _receipt(
            status="failed",
            reason_code="provider_check_missing_credential",
            exit_code=None,
            marker_matched=False,
            started=started,
            monotonic=monotonic,
            **public,
        )

    resolved = executable or shutil.which("codex.cmd") or shutil.which("codex")
    if not resolved:
        return _receipt(
            status="failed",
            reason_code="provider_check_codex_not_installed",
            exit_code=None,
            marker_matched=False,
            started=started,
            monotonic=monotonic,
            **public,
        )

    argv = _provider_check_argv(Path(resolved), **normalized)
    child_environment = _provider_environment(
        source_environment,
        key_env=normalized["key_env"],
        credential=credential,
    )
    completed: subprocess.CompletedProcess[str]
    workspace_unchanged = False
    try:
        with tempfile.TemporaryDirectory(prefix="pacer-provider-check-") as temporary:
            cwd = Path(temporary)
            cwd.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                completed = runner(
                    argv,
                    cwd=str(cwd),
                    env=child_environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=normalized["timeout_seconds"],
                    check=False,
                )
                workspace_unchanged = not any(cwd.iterdir())
            finally:
                cwd.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    except subprocess.TimeoutExpired:
        return _receipt(
            status="failed",
            reason_code="provider_check_timeout",
            exit_code=None,
            marker_matched=False,
            started=started,
            monotonic=monotonic,
            **public,
        )
    except OSError:
        return _receipt(
            status="failed",
            reason_code="provider_check_launch_failed",
            exit_code=None,
            marker_matched=False,
            started=started,
            monotonic=monotonic,
            **public,
        )

    exit_code = int(completed.returncode)
    marker_matched = str(completed.stdout or "").strip() == PROVIDER_CHECK_MARKER
    if exit_code != 0:
        reason_code = "provider_check_nonzero_exit"
    elif not workspace_unchanged:
        reason_code = "provider_check_workspace_modified"
    elif not marker_matched:
        reason_code = "provider_check_marker_mismatch"
    else:
        reason_code = ""
    return _receipt(
        status="passed" if not reason_code else "failed",
        reason_code=reason_code,
        exit_code=exit_code,
        marker_matched=marker_matched,
        started=started,
        monotonic=monotonic,
        **public,
    )


def dogfood_provider_receipt_to_markdown(result: Mapping[str, Any]) -> str:
    reasons = [str(item) for item in result.get("reason_codes") or [] if str(item)]
    exit_code = result.get("exit_code")
    lines = [
        "## Pacer Dogfood Provider Check",
        "",
        f"Status: `{result.get('status', 'failed')}`",
        f"Provider: `{result.get('provider_id', '')}`",
        f"Model: `{result.get('model', '')}`",
        f"Wire API: `{result.get('wire_api', 'responses')}`",
        f"Sandbox: `{result.get('sandbox', 'read-only')}`",
        f"Exit code: `{exit_code if exit_code is not None else 'unavailable'}`",
        f"Marker matched: `{str(bool(result.get('marker_matched'))).lower()}`",
        f"Duration: `{int(result.get('duration_ms') or 0)} ms`",
        f"Receipt digest: `{result.get('receipt_digest', '')}`",
    ]
    if reasons:
        lines.extend(["", "Reasons:", *[f"- `{reason}`" for reason in reasons]])
    return "\n".join(lines)


def verify_dogfood_provider_receipt(receipt: object) -> bool:
    """Return whether a provider receipt is a valid, untampered public handoff."""

    if not isinstance(receipt, Mapping):
        return False
    try:
        if set(receipt) != {*_PUBLIC_RECEIPT_FIELDS, "receipt_digest"}:
            return False
        if not _valid_public_receipt_fields(receipt):
            return False
        supplied_digest = receipt["receipt_digest"]
        if not isinstance(supplied_digest, str) or not _SHA256_DIGEST.fullmatch(
            supplied_digest
        ):
            return False
        return hmac.compare_digest(supplied_digest, _receipt_digest(receipt))
    except (KeyError, TypeError, ValueError):
        return False


def _normalize_inputs(
    *,
    provider_id: str,
    base_url: str,
    model: str,
    key_env: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    provider = str(provider_id or "").strip()
    if not _SAFE_PROVIDER_ID.fullmatch(provider):
        raise ValueError("provider_id must be a simple provider id")

    endpoint = str(base_url or "").strip().rstrip("/")
    if len(endpoint) > 2048 or "'" in endpoint or any(ord(char) < 32 for char in endpoint):
        raise ValueError("base_url is not safe for Codex configuration")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain credentials, query parameters, or fragments")

    model_id = str(model or "").strip()
    if not model_id or len(model_id) > 160 or any(ord(char) < 32 for char in model_id):
        raise ValueError("model must be a bounded printable value")

    environment_name = str(key_env or "").strip()
    if not _SAFE_ENV_NAME.fullmatch(environment_name):
        raise ValueError("key_env must be an environment variable name")

    timeout = float(timeout_seconds)
    if not 0 < timeout <= MAX_PROVIDER_CHECK_TIMEOUT_SECONDS:
        raise ValueError("timeout_seconds is outside the allowed range")
    return {
        "provider_id": provider,
        "base_url": endpoint,
        "model": model_id,
        "key_env": environment_name,
        "timeout_seconds": timeout,
    }


def _provider_environment(
    source: Mapping[str, str],
    *,
    key_env: str,
    credential: str,
) -> dict[str, str]:
    child = {
        name: str(source[name])
        for name in _CHILD_ENV_ALLOWLIST
        if name in source and str(source[name])
    }
    child[key_env] = credential
    child["NO_COLOR"] = "1"
    return child


def _provider_check_argv(
    executable: Path,
    *,
    provider_id: str,
    base_url: str,
    model: str,
    key_env: str,
    timeout_seconds: float,
) -> list[str]:
    del timeout_seconds
    arguments = [
        "--ask-for-approval",
        "never",
        "--sandbox",
        "read-only",
        "exec",
        "--ignore-user-config",
        "--skip-git-repo-check",
        "--model",
        model,
        "-c",
        f"model_provider='{provider_id}'",
        "-c",
        f"model_providers.{provider_id}.name='Pacer Dogfood Provider Check'",
        "-c",
        f"model_providers.{provider_id}.base_url='{base_url}'",
        "-c",
        f"model_providers.{provider_id}.env_key='{key_env}'",
        "-c",
        f"model_providers.{provider_id}.wire_api='responses'",
        f"Reply with exactly {PROVIDER_CHECK_MARKER} and no other text.",
    ]
    return _native_codex_command(executable, arguments)


def _receipt(
    *,
    status: str,
    reason_code: str,
    provider_id: str,
    model: str,
    exit_code: int | None,
    marker_matched: bool,
    started: float,
    monotonic: MonotonicClock,
) -> dict[str, Any]:
    duration_ms = max(0, int(round((monotonic() - started) * 1000)))
    receipt = {
        "schema_version": 1,
        "kind": "pacer_dogfood_provider_check",
        "status": status,
        "passed": status == "passed",
        "reason_codes": [reason_code] if reason_code else [],
        "provider_id": provider_id,
        "model": model,
        "wire_api": "responses",
        "sandbox": "read-only",
        "exit_code": exit_code,
        "marker_matched": bool(marker_matched),
        "duration_ms": duration_ms,
    }
    receipt["receipt_digest"] = _receipt_digest(receipt)
    return receipt


def _receipt_digest(receipt: Mapping[str, Any]) -> str:
    public_receipt = {field: receipt[field] for field in _PUBLIC_RECEIPT_FIELDS}
    canonical = json.dumps(
        public_receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _valid_public_receipt_fields(receipt: Mapping[str, Any]) -> bool:
    schema_version = receipt.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        return False
    if receipt.get("kind") != "pacer_dogfood_provider_check":
        return False

    status = receipt.get("status")
    passed = receipt.get("passed")
    if status not in {"passed", "failed"} or not isinstance(passed, bool):
        return False
    if passed is not (status == "passed"):
        return False

    reasons = receipt.get("reason_codes")
    if not isinstance(reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in reasons
    ):
        return False
    if bool(reasons) is passed:
        return False

    provider_id = receipt.get("provider_id")
    model = receipt.get("model")
    if not isinstance(provider_id, str) or not isinstance(model, str):
        return False
    if provider_id and not _SAFE_PROVIDER_ID.fullmatch(provider_id):
        return False
    if len(model) > 160 or any(ord(char) < 32 for char in model):
        return False
    if receipt.get("wire_api") != "responses" or receipt.get("sandbox") != "read-only":
        return False

    exit_code = receipt.get("exit_code")
    if exit_code is not None and (
        not isinstance(exit_code, int) or isinstance(exit_code, bool)
    ):
        return False
    if not isinstance(receipt.get("marker_matched"), bool):
        return False
    duration_ms = receipt.get("duration_ms")
    if (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or duration_ms < 0
    ):
        return False
    return True
