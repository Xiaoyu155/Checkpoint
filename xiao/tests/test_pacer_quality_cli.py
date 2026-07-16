from __future__ import annotations

import json
from pathlib import Path

from visual_agent import cli_quality
from visual_agent.cli import main
from visual_agent.release_gate import release_manifest_digest


def test_release_manifest_cli_requires_and_accepts_locked_digest(capsys) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest_path = repo_root / ".pacer" / "release.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    code = main(
        [
            "pacer-release-manifest-check",
            "--manifest",
            str(manifest_path),
            "--expected-digest",
            release_manifest_digest(manifest),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["passed"] is True
    assert payload["digest_locked"] is True


def test_dogfood_policy_cli_requires_mature_three_lane_contract(capsys) -> None:
    repo_root = Path(__file__).resolve().parents[1]

    code = main(
        ["pacer-dogfood-policy-check", "--repo-root", str(repo_root), "--format", "json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["passed"] is True
    assert payload["target_score"] == 95
    assert payload["release_score"] == 100


def test_dogfood_cli_fails_closed_when_canonical_evidence_is_missing(
    tmp_path: Path,
    capsys,
) -> None:
    code = main(["pacer-dogfood-check", "--repo-root", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["passed"] is False
    assert payload["reason_codes"] == ["dogfood_evidence_load_failed"]


def test_dogfood_provider_cli_emits_only_the_bounded_receipt(
    monkeypatch,
    capsys,
) -> None:
    observed = {}

    def check(**kwargs):
        observed.update(kwargs)
        digest = "a" * 64
        return {
            "schema_version": 1,
            "kind": "pacer_dogfood_provider_check",
            "status": "passed",
            "passed": True,
            "reason_codes": [],
            "provider_id": "sub2api",
            "model": "gpt-test",
            "wire_api": "responses",
            "sandbox": "read-only",
            "exit_code": 0,
            "marker_matched": True,
            "duration_ms": 10,
            "receipt_digest": digest,
        }

    monkeypatch.setattr(cli_quality, "run_dogfood_provider_check", check)
    secret_endpoint = "https://private-relay.example/v1"
    code = main(
        [
            "pacer-dogfood-provider-check",
            "--provider-id",
            "sub2api",
            "--base-url",
            secret_endpoint,
            "--model",
            "gpt-test",
            "--key-env",
            "SUB2API_API_KEY",
            "--format",
            "json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 0
    assert payload["passed"] is True
    assert payload["marker_matched"] is True
    assert payload["receipt_digest"] == "a" * 64
    assert observed["base_url"] == secret_endpoint
    assert secret_endpoint not in output
    assert "SUB2API_API_KEY" not in output


def test_release_bundle_cli_fails_closed_without_attested_evidence(
    tmp_path: Path,
    capsys,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((repo_root / ".pacer" / "release.json").read_text(encoding="utf-8"))
    canonical = tmp_path / ".pacer" / "release.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text(json.dumps(manifest), encoding="utf-8")

    code = main(
        [
            "pacer-release-check",
            "--repo-root",
            str(tmp_path),
            "--expected-digest",
            release_manifest_digest(manifest),
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 1
    assert payload["passed"] is False
    assert "release_bundle_unavailable" in payload["reason_codes"]
    assert str(tmp_path) not in output


def test_release_bundle_cli_rejects_noncanonical_manifest_without_path_leak(
    tmp_path: Path,
    capsys,
) -> None:
    alternate = tmp_path / "release.json"
    alternate.write_text("{}", encoding="utf-8")

    code = main(
        [
            "pacer-release-check",
            "--repo-root",
            str(tmp_path),
            "--manifest",
            str(alternate),
            "--expected-digest",
            "0" * 64,
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert code == 1
    assert payload["reason_codes"] == ["release_evidence_load_failed"]
    assert str(tmp_path) not in output
