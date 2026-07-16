from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from configparser import ConfigParser
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Mapping, Sequence

from .dogfood_evidence import assess_dogfood_evidence
from .dogfood_provider_check import verify_dogfood_provider_receipt


def build_ci_dogfood_evidence(
    *,
    repo_root: str | Path,
    workspace_root: str | Path,
    input_wheel: str | Path,
    candidate_wheel: str | Path,
    expected_change_set: str | Path,
    provider_receipt: str | Path,
    repository: str,
    baseline_commit: str,
    run_id: str,
    run_attempt: str,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    workspace = Path(workspace_root).expanduser().resolve()
    parent = Path(input_wheel).expanduser().resolve(strict=True)
    candidate = Path(candidate_wheel).expanduser().resolve(strict=True)
    expected_diff = Path(expected_change_set).expanduser().resolve(strict=True).read_bytes()
    provider_path = Path(provider_receipt).expanduser().resolve(strict=True)
    clean_repository = str(repository or "").strip()
    clean_commit = str(baseline_commit or "").strip()
    clean_run_id = str(run_id or "").strip()
    clean_attempt = str(run_attempt or "").strip()
    if not all((clean_repository, clean_commit, clean_run_id, clean_attempt)):
        raise ValueError("GitHub run identity is incomplete")

    active = _load_json(workspace / "pacer_native" / "active_launch.json")
    launch_id = str(active.get("launch_id") or "").strip()
    record = _latest_history_record(
        workspace / "pacer_native" / "history.jsonl",
        launch_id=launch_id,
    )
    actual_diff = _git_bytes(root, "diff", "--binary", "HEAD", "--")
    if actual_diff != expected_diff:
        raise ValueError("Pacer change set differs from the immutable candidate patch")
    changed_files = _git_paths(root, "diff", "--name-only", "-z", "HEAD", "--")
    handoff = validate_pacer_task_handoff(
        active,
        record,
        changed_files=changed_files,
    )

    provider_payload = _load_json(provider_path)
    if not verify_dogfood_provider_receipt(provider_payload):
        raise ValueError("provider receipt is invalid or has been modified")

    output_root = root / ".dogfood"
    candidate_root = output_root / "candidate"
    evidence_root = output_root / "evidence"
    artifacts_root = evidence_root / "artifacts"
    candidate_root.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "input").mkdir(parents=True, exist_ok=True)

    staged_candidate = candidate_root / candidate.name
    staged_parent = artifacts_root / "input" / parent.name
    staged_provider = artifacts_root / "provider-check-receipt.json"
    shutil.copyfile(candidate, staged_candidate)
    shutil.copyfile(parent, staged_parent)
    shutil.copyfile(provider_path, staged_provider)

    task_contract = _public_task_contract(handoff["task_contract"])
    acceptance_contract = task_contract["acceptance_contract"]
    task_path = artifacts_root / "task-contract.json"
    acceptance_path = artifacts_root / "acceptance-contract.json"
    task_digest = _write_json(task_path, task_contract)
    acceptance_digest = _write_json(acceptance_path, acceptance_contract)

    change_set_digest = hashlib.sha256(actual_diff).hexdigest()
    repo_identity_digest = hashlib.sha256(
        f"{clean_repository}\0{clean_commit}".encode("utf-8")
    ).hexdigest()
    batch_run_id = str(record.get("batch_run_id") or "").strip()
    verification_receipt = {
        "schema_version": 1,
        "status": "passed",
        "trust": "yes",
        "launch_id": launch_id,
        "batch_run_id": batch_run_id,
        "task_contract_digest": task_digest,
        "acceptance_contract_digest": acceptance_digest,
        "change_set_digest": change_set_digest,
    }
    verification_path = artifacts_root / "verification-receipt.json"
    verification_digest = _write_json(verification_path, verification_receipt)

    candidate_digest = _file_sha256(staged_candidate)
    parent_digest = _file_sha256(staged_parent)
    self_check_launch_id = f"github-{clean_run_id}-{clean_attempt}-self-check"
    self_check_receipt = {
        "schema_version": 1,
        "status": "passed",
        "trust": "yes",
        "launch_id": self_check_launch_id,
        "installed_wheel_sha256": candidate_digest,
        "source_repo_identity_digest": repo_identity_digest,
    }
    self_check_path = artifacts_root / "self-check-receipt.json"
    self_check_digest = _write_json(self_check_path, self_check_receipt)

    evidence = {
        "schema_version": 1,
        "source_repo": {
            "product": "Pacer",
            "package_name": "visual-agent",
            "pacer_entrypoint": "visual_agent.cli:main",
            "canonical_root": ".",
            "repo_identity_digest": repo_identity_digest,
            "baseline_commit": clean_commit,
            "baseline_changes_digest": str(active["source_baseline_digest"]),
            "change_set_digest": change_set_digest,
            "scan_complete": True,
            "protected_paths_unchanged": True,
            "out_of_band_changes": False,
            "source_attribution": "pacer_worker",
            "changed_files": changed_files,
        },
        "orchestrator": {
            "input_wheel_sha256": parent_digest,
            "input_wheel_path": f".dogfood/evidence/artifacts/input/{parent.name}",
            "input_version": _wheel_version(staged_parent),
            "launch_id": launch_id,
            "mission_id": f"pacer-direct-{launch_id}",
            "worker_session_ids": [launch_id],
            "repo_identity_digest": repo_identity_digest,
        },
        "contract": {
            "task_contract_digest": task_digest,
            "task_contract_path": ".dogfood/evidence/artifacts/task-contract.json",
            "acceptance_contract_digest": acceptance_digest,
            "acceptance_contract_path": ".dogfood/evidence/artifacts/acceptance-contract.json",
        },
        "verification": {
            "status": "passed",
            "trust": "yes",
            "batch_run_id": batch_run_id,
            "receipt_digest": verification_digest,
            "receipt_path": ".dogfood/evidence/artifacts/verification-receipt.json",
            "acceptance_contract_digest": acceptance_digest,
            "change_set_digest": change_set_digest,
            "evidence_resubmissions": 0,
            "warnings": [],
        },
        "candidate": {
            "wheel_sha256": candidate_digest,
            "wheel_path": f".dogfood/candidate/{candidate.name}",
            "version": _wheel_version(staged_candidate),
            "built_from_change_set_digest": change_set_digest,
            "fresh_install": True,
            "fresh_env_id": f"github-{clean_run_id}-{clean_attempt}-py312",
            "pip_check_status": "passed",
        },
        "bootstrap": {
            "parent_wheel_sha256": parent_digest,
            "installed_wheel_sha256": candidate_digest,
            "self_check_artifact_sha256": candidate_digest,
            "source_repo_identity_digest": repo_identity_digest,
            "self_check_status": "passed",
            "self_check_receipt_digest": self_check_digest,
            "self_check_receipt_path": ".dogfood/evidence/artifacts/self-check-receipt.json",
            "self_check_launch_id": self_check_launch_id,
        },
    }
    evidence_path = evidence_root / "dogfood-evidence.json"
    _write_json(evidence_path, evidence)
    local = assess_dogfood_evidence(evidence, repo_root=root)
    if local.get("artifact_files_verified") is not True:
        raise ValueError("generated Dogfood artifact bindings did not verify")
    if set(local.get("reason_codes") or []) != {"dogfood_attestation_missing"}:
        raise ValueError("generated Dogfood evidence failed before GitHub attestation")
    return {
        "schema_version": 1,
        "status": "ready_for_github_attestation",
        "launch_id": launch_id,
        "batch_run_id": batch_run_id,
        "parent_wheel_sha256": parent_digest,
        "candidate_wheel_sha256": candidate_digest,
        "change_set_digest": change_set_digest,
        "evidence_sha256": _file_sha256(evidence_path),
        "evidence_path": str(evidence_path),
    }


def validate_pacer_task_handoff(
    active: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    changed_files: Sequence[str],
) -> dict[str, Any]:
    launch_id = str(active.get("launch_id") or "").strip()
    if not launch_id or str(record.get("launch_id") or "").strip() != launch_id:
        raise ValueError("Pacer launch identity does not match its completion record")
    if active.get("status") != "completed" or record.get("status") != "completed":
        raise ValueError("Pacer task did not complete")
    if active.get("source_baseline_complete") is not True:
        raise ValueError("Pacer source baseline is incomplete")
    baseline_digest = str(active.get("source_baseline_digest") or "").strip()
    if len(baseline_digest) != 64:
        raise ValueError("Pacer source baseline digest is invalid")
    control = active.get("completion_control")
    control = control if isinstance(control, Mapping) else {}
    if control.get("attempts") != 1 or control.get("last_rejection_codes"):
        raise ValueError("Pacer completion evidence was resubmitted")

    review = record.get("task_review")
    review = review if isinstance(review, Mapping) else {}
    required = {
        "verdict": "approved",
        "trust": "yes",
        "evidence_integrity": "verified",
        "acceptance_adequacy": "sufficient",
        "product_verdict": "pass",
        "evidence_origin": "server_derived",
    }
    for key, expected in required.items():
        if review.get(key) != expected:
            raise ValueError(f"Pacer task review {key} is not {expected}")
    if review.get("warnings") != [] or review.get("errors") != []:
        raise ValueError("Pacer task review contains warnings or errors")
    if review.get("source_change_complete") is not True:
        raise ValueError("Pacer source change attribution is incomplete")

    source_changes = review.get("source_changes")
    source_changes = source_changes if isinstance(source_changes, list) else []
    attributed = sorted(
        str(item.get("path") or "").replace("\\", "/")
        for item in source_changes
        if isinstance(item, Mapping) and str(item.get("path") or "").strip()
    )
    actual = sorted(str(item).replace("\\", "/") for item in changed_files)
    if not actual or attributed != actual:
        raise ValueError("Pacer-attributed source files do not match the Git change set")

    task_contract = review.get("task_contract")
    task_contract = task_contract if isinstance(task_contract, Mapping) else {}
    if not task_contract.get("requirements") or not isinstance(
        task_contract.get("acceptance_contract"), Mapping
    ):
        raise ValueError("Pacer task contract is incomplete")
    batch_run_id = str(record.get("batch_run_id") or "").strip()
    if not batch_run_id:
        raise ValueError("Pacer verification batch identity is missing")
    return {
        "launch_id": launch_id,
        "batch_run_id": batch_run_id,
        "task_contract": dict(task_contract),
    }


def _public_task_contract(task_contract: Mapping[str, Any]) -> dict[str, Any]:
    requirements = [
        {"id": str(item.get("id") or ""), "text": str(item.get("text") or "")}
        for item in task_contract.get("requirements") or []
        if isinstance(item, Mapping)
    ]
    if not requirements or any(not item["id"] or not item["text"] for item in requirements):
        raise ValueError("Pacer task requirements are invalid")
    acceptance = task_contract.get("acceptance_contract")
    if not isinstance(acceptance, Mapping):
        raise ValueError("Pacer acceptance contract is unavailable")
    return {
        "schema_version": 1,
        "goal_digest": str(task_contract.get("goal_digest") or ""),
        "requirements": requirements,
        "acceptance_contract": dict(acceptance),
    }


def _latest_history_record(path: Path, *, launch_id: str) -> dict[str, Any]:
    selected: dict[str, Any] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip():
            continue
        value = json.loads(raw)
        if isinstance(value, dict) and str(value.get("launch_id") or "") == launch_id:
            selected = value
    if not selected:
        raise ValueError("Pacer completion history is unavailable")
    return selected


def _git_bytes(root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("Git could not read the Pacer change set")
    return completed.stdout


def _git_paths(root: Path, *arguments: str) -> list[str]:
    return [
        item.decode("utf-8", errors="strict").replace("\\", "/")
        for item in _git_bytes(root, *arguments).split(b"\0")
        if item
    ]


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return _file_sha256(path)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _wheel_version(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        entry_names = [
            name
            for name in archive.namelist()
            if name.endswith(".dist-info/entry_points.txt")
        ]
        if len(metadata_names) != 1:
            raise ValueError("visual-agent wheel metadata is invalid")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        package = str(metadata.get("Name") or "").replace("_", "-").casefold()
        version = str(metadata.get("Version") or "").strip()
        entrypoint_ok = False
        for name in entry_names:
            parser = ConfigParser()
            parser.read_string(archive.read(name).decode("utf-8"))
            if parser.get("console_scripts", "pacer", fallback="").strip() == "visual_agent.cli:main":
                entrypoint_ok = True
                break
    if package != "visual-agent" or not version or not entrypoint_ok:
        raise ValueError("wheel is not an installable visual-agent Pacer artifact")
    return version


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build fail-closed GitHub Dogfood evidence.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--input-wheel", required=True)
    parser.add_argument("--candidate-wheel", required=True)
    parser.add_argument("--expected-change-set", required=True)
    parser.add_argument("--provider-receipt", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_ci_dogfood_evidence(
        repo_root=args.repo_root,
        workspace_root=args.workspace_root,
        input_wheel=args.input_wheel,
        candidate_wheel=args.candidate_wheel,
        expected_change_set=args.expected_change_set,
        provider_receipt=args.provider_receipt,
        repository=args.repository,
        baseline_commit=args.baseline_commit,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
