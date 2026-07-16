from __future__ import annotations

from pathlib import Path


def test_reusable_dogfood_workflow_uses_pinned_oidc_attestation_chain() -> None:
    repo = Path(__file__).resolve().parents[1]
    workflow = (repo / ".github" / "workflows" / "pacer-dogfood.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_call:" in workflow
    assert "permissions: {}" in workflow
    assert "attestations: write" in workflow
    assert "id-token: write" in workflow
    assert "actions/attest-build-provenance@977bb373ede98d70efdf65b84cb5f73e068dcc2a" in workflow
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in workflow
    assert "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f" in workflow
    assert "--require-github-provenance" in workflow
    assert "--minimum-score 95" in workflow
    assert workflow.count("subject-path:") == 3
    assert "pull_request_target:" not in workflow


def test_repository_root_exposes_reusable_dogfood_workflow() -> None:
    repository = Path(__file__).resolve().parents[2]
    workflow = (repository / ".github" / "workflows" / "pacer-dogfood.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_call:" in workflow
    assert "permissions: {}" in workflow
    assert "working-directory: xiao" in workflow
    assert "--minimum-score 95" in workflow
    assert "--require-github-provenance" in workflow
    assert workflow.count("subject-path:") == 3
    assert "pull_request_target:" not in workflow


def test_dogfood_producer_keeps_provider_secrets_out_of_reusable_verifier() -> None:
    repository = Path(__file__).resolve().parents[2]
    workflow = (
        repository / ".github" / "workflows" / "pacer-dogfood-run.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "PACER_DOGFOOD_PROVIDER_API_KEY" in workflow
    assert "PACER_DOGFOOD_PROVIDER_BASE_URL" in workflow
    assert "scripts/pacer_dogfood_candidate.patch" in workflow
    assert "Configure ephemeral Codex MCP layer" in workflow
    assert "[mcp_servers.pacer]" in workflow
    assert "required = true" in workflow
    assert "--sandbox danger-full-access" in workflow
    assert "permissions:\n      contents: read" in workflow
    assert "uses: ./.github/workflows/pacer-dogfood.yml" in workflow
    assert "secrets: inherit" not in workflow
    assert "pull_request_target:" not in workflow
