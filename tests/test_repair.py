from __future__ import annotations

import json
from pathlib import Path

from visual_agent.cli import main
from visual_agent.mcp_server import run_workflow_payload
from visual_agent.repair import (
    _parse_repair_json,
    auto_repair_failure,
    build_failure_evidence_pack,
    normalize_model_repair,
    repair_to_markdown,
    suggest_workflow_repair,
)
from visual_agent.repair_history import build_repair_health, list_repair_history, repair_history_path, rollback_repair_history_entry
from visual_agent.workspace import init_workspace


ROOT = Path(__file__).resolve().parent.parent


def write_failure_workflow(workspace) -> None:
    (workspace.workflows_dir / "failure.yaml").write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )


def run_failed_workflow(tmp_path: Path):
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_failure_workflow(workspace)
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})
    return workspace, run


def run_typo_failure_workflow(tmp_path: Path):
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "typo_failure.yaml").write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})
    return workspace, run


def run_selector_drift_workflow(tmp_path: Path):
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "selector_drift.yaml").write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: selector_drift\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: click_login\n"
        "    action: click\n"
        "    target:\n"
        "      selector: \"#logn\"\n"
        "      text: 登录\n"
        "      role: button\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "selector_drift"})
    return workspace, run


def run_partial_typo_failure_workflow(tmp_path: Path):
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "partial_typo_failure.yaml").write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: partial_typo_failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_title\n"
        "    action: assert_text\n"
        "    text: 客户管理系統\n"
        "  - id: assert_still_missing\n"
        "    action: assert_text\n"
        "    text: still missing\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "partial_typo_failure"})
    return workspace, run


def test_build_failure_evidence_pack_includes_prompt_and_workflow_source(tmp_path) -> None:
    workspace, run = run_failed_workflow(tmp_path)

    pack = build_failure_evidence_pack(workspace.root, run_id=run["run_id"])

    assert pack["status"] == "found"
    assert pack["workflow"] == "failure"
    assert pack["failed_step"]["id"] == "assert_missing"
    assert pack["failed_step"]["failure_diagnosis"]["expected"] == "expected text: missing text"
    assert "assert_missing" in pack["repair_prompt"]
    assert "Workflow YAML excerpt" in pack["repair_prompt"]
    assert pack["workflow_source"]["available"] is True
    assert pack["within_budget"] is True


def test_suggest_workflow_repair_defaults_to_deterministic_advice(tmp_path) -> None:
    workspace, run = run_failed_workflow(tmp_path)

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"])

    assert payload["status"] == "suggested"
    assert payload["source"] == "deterministic"
    assert payload["repair"]["classification"] == "app_bug"
    assert payload["repair"]["workflow_patch"] == ""
    assert payload["repair"]["apply_supported"] is False


def test_model_repair_normalization_adds_non_applicable_candidates() -> None:
    repair = normalize_model_repair(
        {
            "root_cause": "selector changed",
            "classification": "selector_drift",
            "recommended_fix": "Update the workflow selector after review.",
            "workflow_patch": "--- old\n+++ new\n@@\n-#old\n+#new\n",
            "confidence": 0.8,
        },
        provider="openai",
    )

    assert repair["apply_supported"] is False
    assert repair["selected_candidate_id"] == "model_workflow_patch"
    assert repair["candidates"][0]["id"] == "model_workflow_patch"
    assert repair["candidates"][0]["apply_supported"] is False
    assert repair["candidates"][0]["source"] == "openai"


def test_parse_repair_json_preserves_model_candidates() -> None:
    parsed = _parse_repair_json(
        json.dumps(
            {
                "root_cause": "copy changed",
                "classification": "workflow_bug",
                "recommended_fix": "Review text assertion.",
                "confidence": 0.7,
                "candidates": [
                    {
                        "id": "model_text_patch",
                        "kind": "workflow_patch",
                        "recommended_fix": "Change expected text.",
                        "confidence": 0.7,
                    }
                ],
            }
        )
    )
    repair = normalize_model_repair(parsed, provider="anthropic")

    assert repair["candidates"][0]["id"] == "model_text_patch"
    assert repair["candidates"][0]["kind"] == "workflow_patch"
    assert repair["candidates"][0]["source"] == "anthropic"
    assert repair["candidates"][0]["apply_supported"] is False


def test_suggest_workflow_repair_generates_diff_for_close_assert_text_drift(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"])

    assert payload["status"] == "suggested"
    assert payload["repair"]["classification"] == "workflow_bug"
    assert payload["repair"]["apply_supported"] is True
    assert "-    text: 客户管理系統" in payload["repair"]["workflow_patch"]
    assert '+    text: "客户管理系统"' in payload["repair"]["workflow_patch"]
    assert payload["repair"]["selected_candidate_id"] == "deterministic_workflow_patch"
    assert payload["repair"]["candidates"][0]["id"] == "deterministic_workflow_patch"
    assert payload["repair"]["candidates"][0]["apply_supported"] is True
    assert payload["workflow_repair_plan"]["applied"] is False


def test_suggest_workflow_repair_candidate_id_blocks_manual_candidate_apply(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")

    payload = suggest_workflow_repair(
        workspace.root,
        run_id=run["run_id"],
        apply=True,
        candidate_id="app_behavior_check",
    )

    assert payload["status"] == "suggested"
    assert payload["workflow_repair_plan"]["candidate_id"] == "app_behavior_check"
    assert payload["workflow_repair_plan"]["applied"] is False
    assert "not automatically applicable" in payload["workflow_repair_plan"]["reason"]
    assert workflow_path.read_text(encoding="utf-8") == original


def test_suggest_workflow_repair_apply_writes_backup_and_valid_yaml(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True)

    assert payload["status"] == "applied"
    assert payload["workflow_repair_plan"]["applied"] is True
    assert Path(payload["workflow_repair_plan"]["backup_path"]).exists()
    assert "text: \"客户管理系统\"" in workflow_path.read_text(encoding="utf-8")
    assert payload["history"]["status"] == "recorded"
    assert repair_history_path(workspace.root).exists()


def test_repair_history_lists_recent_attempts_and_filters(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    suggest_workflow_repair(workspace.root, run_id=run["run_id"])

    history = list_repair_history(workspace.root, workflow="typo_failure")
    missing = list_repair_history(workspace.root, workflow="other")

    assert history["total_entries"] == 1
    assert history["entries"][0]["workflow"] == "typo_failure"
    assert history["entries"][0]["status"] == "suggested"
    assert history["entries"][0]["classification"] == "workflow_bug"
    assert missing["total_entries"] == 0


def test_repair_health_summarizes_verified_and_rollback_risk(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    verified = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True, verify=True)
    rollback_repair_history_entry(workspace.root, history_id=verified["history"]["history_id"])

    health = build_repair_health(workspace.root)

    assert health["analyzed_entries"] == 2
    assert health["applied_count"] == 1
    assert health["verified_count"] == 1
    assert health["rollback_count"] == 1
    assert health["reliability_score"] == 1.0
    assert health["risk_level"] == "high"
    assert health["latest_entry"]["status"] == "manual_rolled_back"


def test_rollback_repair_history_entry_restores_backup_and_records_history(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")
    applied = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True)
    assert workflow_path.read_text(encoding="utf-8") != original

    payload = rollback_repair_history_entry(workspace.root, history_id=applied["history"]["history_id"])

    assert payload["status"] == "manual_rolled_back"
    assert workflow_path.read_text(encoding="utf-8") == original
    history = list_repair_history(workspace.root)
    assert history["total_entries"] == 2
    assert history["entries"][0]["status"] == "manual_rolled_back"


def test_suggest_workflow_repair_apply_can_verify_repaired_workflow(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True, verify=True)

    assert payload["status"] == "verified"
    assert payload["workflow_repair_plan"]["verification"]["status"] == "passed"
    assert payload["workflow_repair_plan"]["verification"]["run_id"]


def test_auto_repair_failure_applies_verifies_and_includes_health(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"

    payload = auto_repair_failure(workspace.root, run_id=run["run_id"])

    assert payload["status"] == "verified"
    assert payload["source"] == "auto_repair"
    assert payload["auto_repair"]["verify"] is True
    assert payload["auto_repair"]["rollback_on_fail"] is True
    assert payload["repair_result"]["workflow_repair_plan"]["verification"]["status"] == "passed"
    assert payload["repair_health"]["verified_count"] == 1
    assert "客户管理系统" in workflow_path.read_text(encoding="utf-8")


def test_auto_repair_failure_dry_run_does_not_modify_or_verify(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")

    payload = auto_repair_failure(workspace.root, run_id=run["run_id"], dry_run=True)

    assert payload["status"] == "suggested"
    assert payload["auto_repair"]["dry_run"] is True
    assert payload["auto_repair"]["apply"] is False
    assert payload["auto_repair"]["verify"] is False
    assert payload["repair_result"]["workflow_repair_plan"]["applied"] is False
    assert "verification" not in payload["repair_result"]["workflow_repair_plan"]
    assert payload["repair_result"]["repair"]["candidates"][0]["id"] == "deterministic_workflow_patch"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_auto_repair_policy_min_confidence_is_enforced(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")
    write_workspace_auto_repair_policy(workspace, {"min_confidence": 0.99})

    payload = auto_repair_failure(workspace.root, run_id=run["run_id"])

    assert payload["status"] == "suggested"
    assert payload["auto_repair"]["policy"]["min_confidence"] == 0.99
    assert payload["auto_repair"]["min_confidence"] == 0.99
    assert payload["repair_result"]["workflow_repair_plan"]["status"] == "not_applied"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_auto_repair_policy_can_block_medium_risk_history(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")
    suggest_workflow_repair(workspace.root, run_id=run["run_id"])
    write_workspace_auto_repair_policy(workspace, {"max_risk_level": "low"})

    payload = auto_repair_failure(workspace.root, run_id=run["run_id"])

    assert payload["status"] == "blocked"
    assert payload["auto_repair"]["blocked"] is True
    assert payload["preflight_repair_health"]["risk_level"] == "medium"
    assert "exceeds workspace policy" in payload["auto_repair"]["block_reason"]
    assert workflow_path.read_text(encoding="utf-8") == original


def test_auto_repair_policy_can_disallow_force(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")
    write_workspace_auto_repair_policy(workspace, {"allow_force": False})

    payload = auto_repair_failure(workspace.root, run_id=run["run_id"], force=True)

    assert payload["status"] == "blocked"
    assert payload["auto_repair"]["force"] is False
    assert "does not allow force" in payload["auto_repair"]["block_reason"]
    assert workflow_path.read_text(encoding="utf-8") == original


def test_auto_repair_failure_blocks_high_risk_health_without_force(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    verified = auto_repair_failure(workspace.root, run_id=run["run_id"])
    rollback_repair_history_entry(workspace.root, history_id=verified["repair_result"]["history"]["history_id"])
    assert "客户管理系統" in workflow_path.read_text(encoding="utf-8")
    failed_again = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})
    original = workflow_path.read_text(encoding="utf-8")

    payload = auto_repair_failure(workspace.root, run_id=failed_again["run_id"])

    assert payload["status"] == "blocked"
    assert payload["auto_repair"]["blocked"] is True
    assert payload["auto_repair"]["apply"] is False
    assert payload["preflight_repair_health"]["risk_level"] == "high"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_auto_repair_failure_force_bypasses_high_risk_health_gate(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    verified = auto_repair_failure(workspace.root, run_id=run["run_id"])
    rollback_repair_history_entry(workspace.root, history_id=verified["repair_result"]["history"]["history_id"])
    failed_again = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = auto_repair_failure(workspace.root, run_id=failed_again["run_id"], force=True)

    assert payload["status"] == "verified"
    assert payload["auto_repair"]["force"] is True
    assert payload["auto_repair"]["apply"] is True
    assert "客户管理系统" in workflow_path.read_text(encoding="utf-8")


def test_auto_repair_failure_can_promote_verified_failure_to_regression(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    payload = auto_repair_failure(workspace.root, run_id=run["run_id"], promote_regression=True)

    assert payload["status"] == "verified"
    assert payload["regression"]["status"] == "promoted"
    assert Path(payload["regression"]["fixture_path"]).exists()
    assert Path(payload["regression"]["test_path"]).exists()
    assert (workspace.regression_tests_dir / "index.json").exists()


def test_auto_repair_failure_can_run_promoted_regression_tests(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    payload = auto_repair_failure(
        workspace.root,
        run_id=run["run_id"],
        promote_regression=True,
        run_regression=True,
        regression_timeout_seconds=30,
    )

    assert payload["status"] == "verified"
    assert payload["regression"]["status"] == "promoted"
    assert payload["regression"]["test_run"]["status"] == "success"
    assert payload["regression"]["test_run"]["passed_tests"] == 1


def write_workspace_auto_repair_policy(workspace, policy: dict) -> None:
    path = workspace.root / "workspace.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["auto_repair"] = policy
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def test_suggest_workflow_repair_reports_unverified_when_rerun_fails(tmp_path) -> None:
    workspace, run = run_partial_typo_failure_workflow(tmp_path)

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True, verify=True)

    assert payload["status"] == "applied_unverified"
    assert payload["workflow_repair_plan"]["applied"] is True
    assert payload["workflow_repair_plan"]["verification"]["status"] == "failed"
    assert payload["workflow_repair_plan"]["verification"]["failed_steps"][0]["id"] == "assert_still_missing"


def test_suggest_workflow_repair_rolls_back_when_verification_fails(tmp_path) -> None:
    workspace, run = run_partial_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "partial_typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True, verify=True, rollback_on_fail=True)

    assert payload["status"] == "rolled_back"
    assert payload["workflow_repair_plan"]["applied"] is True
    assert payload["workflow_repair_plan"]["verification"]["status"] == "failed"
    assert payload["workflow_repair_plan"]["rollback"]["status"] == "rolled_back"
    assert Path(payload["workflow_repair_plan"]["backup_path"]).exists()
    assert workflow_path.read_text(encoding="utf-8") == original


def test_suggest_workflow_repair_apply_respects_min_confidence(tmp_path) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True, min_confidence=0.99)

    assert payload["status"] == "suggested"
    assert payload["workflow_repair_plan"]["status"] == "not_applied"
    assert payload["workflow_repair_plan"]["applied"] is False


def test_suggest_workflow_repair_generates_diff_for_selector_drift(tmp_path) -> None:
    workspace, run = run_selector_drift_workflow(tmp_path)

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"])

    assert payload["status"] == "suggested"
    assert payload["repair"]["classification"] == "selector_drift"
    assert payload["repair"]["apply_supported"] is True
    assert '-      selector: "#logn"' in payload["repair"]["workflow_patch"]
    assert '+      selector: "#login"' in payload["repair"]["workflow_patch"]
    assert payload["workflow_repair_plan"]["confidence"] >= 0.75


def test_suggest_workflow_repair_apply_selector_drift_patch(tmp_path) -> None:
    workspace, run = run_selector_drift_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "selector_drift.yaml"

    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True)

    assert payload["status"] == "applied"
    assert payload["workflow_repair_plan"]["applied"] is True
    assert Path(payload["workflow_repair_plan"]["backup_path"]).exists()
    text = workflow_path.read_text(encoding="utf-8")
    assert 'selector: "#login"' in text
    assert "#logn" not in text


def test_repair_markdown_contains_prompt_without_applying_patch(tmp_path) -> None:
    workspace, run = run_failed_workflow(tmp_path)
    payload = suggest_workflow_repair(workspace.root, run_id=run["run_id"])

    markdown = repair_to_markdown(payload)

    assert "Workflow Repair Suggestion" in markdown
    assert "Repair Candidates" in markdown
    assert "Failed step" in markdown
    assert "Do not apply changes" in markdown


def test_repair_returns_no_failure_when_workspace_is_clean(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    payload = build_failure_evidence_pack(workspace.root)

    assert payload["status"] == "no_failure"


def test_diagnose_latest_failure_cli_outputs_json(tmp_path, capsys) -> None:
    workspace, run = run_failed_workflow(tmp_path)

    code = main(
        [
            "diagnose-latest-failure",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "found"
    assert payload["failed_step"]["id"] == "assert_missing"


def test_repair_workflow_cli_outputs_markdown(tmp_path, capsys) -> None:
    workspace, run = run_failed_workflow(tmp_path)

    code = main(
        [
            "repair-workflow",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "Workflow Repair Suggestion" in output


def test_repair_workflow_cli_can_apply_high_confidence_patch(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    code = main(
        [
            "repair-workflow",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--apply",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "applied"
    assert payload["workflow_repair_plan"]["backup_path"]


def test_repair_history_cli_outputs_markdown(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    suggest_workflow_repair(workspace.root, run_id=run["run_id"])

    code = main(["repair-history", "--workspace-root", str(workspace.root), "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 0
    assert "Repair History" in output
    assert "typo_failure" in output


def test_repair_health_cli_outputs_markdown(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True, verify=True)

    code = main(["repair-health", "--workspace-root", str(workspace.root), "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 0
    assert "Repair Health" in output
    assert "Reliability: `1.0`" in output


def test_repair_rollback_cli_restores_latest_backup(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")
    suggest_workflow_repair(workspace.root, run_id=run["run_id"], apply=True)

    code = main(["repair-rollback", "--workspace-root", str(workspace.root), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "manual_rolled_back"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_repair_workflow_cli_can_apply_and_verify(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    code = main(
        [
            "repair-workflow",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--apply",
            "--verify",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "verified"
    assert payload["workflow_repair_plan"]["verification"]["status"] == "passed"


def test_auto_repair_cli_applies_and_verifies(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    code = main(
        [
            "auto-repair",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "verified"
    assert payload["repair_result"]["workflow_repair_plan"]["verification"]["status"] == "passed"
    assert payload["repair_health"]["risk_level"] == "low"


def test_auto_repair_cli_dry_run_outputs_preview_without_modifying(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")

    code = main(
        [
            "auto-repair",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--dry-run",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "suggested"
    assert payload["auto_repair"]["dry_run"] is True
    assert workflow_path.read_text(encoding="utf-8") == original


def test_auto_repair_cli_can_promote_regression(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    code = main(
        [
            "auto-repair",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--promote-regression",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "verified"
    assert payload["regression"]["status"] == "promoted"
    assert Path(payload["regression"]["test_path"]).exists()


def test_auto_repair_cli_can_promote_and_run_regression(tmp_path, capsys) -> None:
    workspace, run = run_typo_failure_workflow(tmp_path)

    code = main(
        [
            "auto-repair",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--promote-regression",
            "--run-regression",
            "--regression-timeout-seconds",
            "30",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["regression"]["test_run"]["status"] == "success"
    assert payload["regression"]["test_run"]["passed_tests"] == 1


def test_repair_workflow_cli_can_rollback_on_failed_verification(tmp_path, capsys) -> None:
    workspace, run = run_partial_typo_failure_workflow(tmp_path)
    workflow_path = workspace.workflows_dir / "partial_typo_failure.yaml"
    original = workflow_path.read_text(encoding="utf-8")

    code = main(
        [
            "repair-workflow",
            "--workspace-root",
            str(workspace.root),
            "--run-id",
            run["run_id"],
            "--apply",
            "--verify",
            "--rollback-on-fail",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "rolled_back"
    assert payload["workflow_repair_plan"]["rollback"]["status"] == "rolled_back"
    assert workflow_path.read_text(encoding="utf-8") == original
