from __future__ import annotations

from pathlib import Path


def test_core_pillar_audit_grants_isolated_codex_freedom_without_release_gate_bypass() -> None:
    repository = Path(__file__).resolve().parents[2]
    workflow = (
        repository / ".github" / "workflows" / "pacer-core-pillars-audit.yml"
    ).read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "routing" in workflow
    assert "memory" in workflow
    assert "managed" in workflow
    assert "acceptance" in workflow
    assert "--ask-for-approval never" in workflow
    assert "--sandbox danger-full-access" in workflow
    assert "required = true" in workflow
    assert "Call complete_pacer_task exactly once" in workflow
    assert "Verify audit file scope and source integrity" in workflow
    assert "git diff --exit-code HEAD -- ." in workflow
    assert "test \"$(git rev-parse HEAD)\" = \"$PACER_NESTED_BASELINE\"" in workflow
    assert "git status --porcelain --untracked-files=all" in workflow
    assert "^\\.agent-workspace\\/" in workflow
    assert "pacer-core-pillars-audit-${{ matrix.pillar }}" in workflow
    assert "uses: actions/attest-build-provenance" not in workflow
