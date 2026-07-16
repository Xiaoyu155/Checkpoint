from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from .subprocess_window import hidden_subprocess_kwargs


MAX_GITHUB_ATTESTATION_OUTPUT_BYTES = 2 * 1024 * 1024
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WORKFLOW = re.compile(
    r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+@[^\s]+$"
)
AttestationRunner = Callable[..., subprocess.CompletedProcess[Any]]


def verify_github_artifact_attestations(
    subjects: Sequence[str | Path],
    *,
    repository: str,
    signer_workflow: str = "",
    run_id: str = "",
    run_attempt: str = "",
    runner: AttestationRunner = subprocess.run,
) -> dict[str, Any]:
    """Delegate DSSE/Sigstore verification to GitHub CLI's maintained verifier."""

    clean_repository = str(repository or "").strip()
    clean_workflow = str(signer_workflow or "").strip()
    if not _REPOSITORY.fullmatch(clean_repository):
        return _failed("github_attestation_repository_invalid")
    if clean_workflow and not _WORKFLOW.fullmatch(clean_workflow):
        return _failed("github_attestation_workflow_invalid")
    paths = [Path(item).expanduser().resolve() for item in subjects]
    if not paths or len({path for path in paths}) != len(paths):
        return _failed("github_attestation_subjects_invalid")

    verified: list[dict[str, str]] = []
    for path in paths:
        try:
            digest = _file_sha256(path)
        except OSError:
            return _failed("github_attestation_subject_unavailable")
        argv = [
            "gh",
            "attestation",
            "verify",
            str(path),
            "--repo",
            clean_repository,
            "--format",
            "json",
        ]
        if clean_workflow:
            argv.extend(["--signer-workflow", clean_workflow])
        try:
            completed = runner(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=60.0,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.SubprocessError):
            return _failed("github_attestation_verifier_unavailable")
        if completed.returncode != 0:
            return _failed("github_attestation_verification_failed")
        try:
            stdout = _bounded_output(completed.stdout)
            payload = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _failed("github_attestation_output_invalid")
        if not isinstance(payload, (dict, list)) or not payload:
            return _failed("github_attestation_output_invalid")
        verified.append({"sha256": digest})

    clean_run_id = str(run_id or os.environ.get("GITHUB_RUN_ID") or "").strip()
    clean_run_attempt = str(
        run_attempt or os.environ.get("GITHUB_RUN_ATTEMPT") or ""
    ).strip()
    identity_material = "\0".join(
        [
            clean_repository,
            clean_workflow,
            clean_run_id,
            clean_run_attempt,
        ]
    )
    identity_digest = (
        hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
        if clean_run_id and clean_run_attempt
        else ""
    )
    return {
        "schema_version": 1,
        "status": "verified",
        "verified": True,
        "provider": "github-artifact-attestation",
        "repository": clean_repository,
        "signer_workflow": clean_workflow,
        "key_id": f"github:{clean_repository}",
        "run_identity_digest": identity_digest,
        "subjects": verified,
        "reason_codes": [],
    }


def _failed(reason: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "failed",
        "verified": False,
        "provider": "github-artifact-attestation",
        "run_identity_digest": "",
        "subjects": [],
        "reason_codes": [reason],
    }


def _bounded_output(value: Any) -> str:
    raw = value if isinstance(value, bytes) else str(value or "").encode("utf-8")
    if len(raw) > MAX_GITHUB_ATTESTATION_OUTPUT_BYTES:
        raise UnicodeDecodeError("utf-8", raw, 0, len(raw), "attestation output too large")
    return raw.decode("utf-8", errors="strict")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
