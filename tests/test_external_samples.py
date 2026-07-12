import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from visual_agent.external_samples import (
    build_external_sample_batch_plan,
    build_external_sample_batch_failure_summary,
    build_external_sample_batch_rerun_plan,
    build_external_sample_run_plan,
    build_external_sample_run_summary,
    build_external_sample_rerun_plan,
    check_external_samples,
    external_samples_readiness,
    export_external_sample_dry_run_report,
    export_external_sample_live_placeholder,
    export_external_sample_batch_report,
    list_external_sample_batch_reports,
    load_external_sample_batch_report_index,
    list_external_samples,
    run_external_sample,
    submit_external_sample_batch,
    submit_external_sample_batch_reruns,
    submit_external_sample_reruns,
)
from visual_agent.console import build_report_detail, report_detail_to_markdown
from visual_agent.scheduler import list_queue_tasks, run_next_queue_task
from visual_agent.workspace import init_workspace, load_workspace_report_index


def entry_by_id(result: dict, sample_id: str) -> dict:
    return next(entry for entry in result["entries"] if entry["sample_id"] == sample_id)


def skip_if_browser_unavailable(result: dict) -> None:
    if result["status"] != "failed":
        return
    run_dir = Path(result["run_dir"])
    messages = []
    for path in run_dir.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        messages.append(str(payload.get("message") or ""))
    text = "\n".join(messages)
    if "Executable doesn't exist" in text or "playwright install" in text or "Sync API inside the asyncio loop" in text:
        pytest.skip(text)


def test_external_samples_catalog_lists_sample() -> None:
    samples = list_external_samples()
    sample_ids = {sample["id"] for sample in samples}

    assert sample_ids == {
        "external_ecommerce_orders_readonly",
        "external_support_tickets_triage",
        "external_inventory_restock_confirm",
        "external_finance_reconciliation_export",
    }


def test_external_samples_check_accepts_readonly_sample() -> None:
    result = check_external_samples()

    assert result["total_samples"] == 4
    assert result["valid_samples"] == 4
    assert result["invalid_samples"] == 0


def test_external_samples_readiness_reports_account_requirements() -> None:
    result = external_samples_readiness()
    entry = entry_by_id(result, "external_ecommerce_orders_readonly")
    support = entry_by_id(result, "external_support_tickets_triage")
    inventory = entry_by_id(result, "external_inventory_restock_confirm")
    finance = entry_by_id(result, "external_finance_reconciliation_export")

    assert result["ready_samples"] == 1
    assert result["blocked_samples"] == 3
    assert entry["sample_id"] == "external_ecommerce_orders_readonly"
    assert entry["account_environment"] == "sandbox"
    assert entry["allowed_domains"] == ["seller.sandbox.example.com"]
    assert entry["storage_state_policy"] == "required"
    assert entry["storage_state_paths"] == [".agent-auth/seller-sandbox-state.json"]
    assert entry["download_policy"] == "dry-run-only"
    assert entry["requirements"] == ["storage_state_file"]
    assert entry["blockers"] == ["missing_storage_state_file"]
    assert support["ready"] is True
    assert support["storage_state_policy"] == "optional"
    assert support["download_policy"] == "forbidden"
    assert inventory["storage_state_paths"] == [".agent-auth/inventory-sandbox-state.json"]
    assert finance["download_policy"] == "confirm-required"
    assert finance["requirements"] == ["download_confirmation", "storage_state_file"]


def test_external_samples_readiness_accepts_existing_storage_state(tmp_path) -> None:
    auth_dir = tmp_path / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "seller-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    (auth_dir / "inventory-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    (auth_dir / "finance-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    result = external_samples_readiness(workspace_root=tmp_path)
    entry = entry_by_id(result, "external_ecommerce_orders_readonly")

    assert result["ready_samples"] == 4
    assert result["blocked_samples"] == 0
    assert result["auth_blocked_samples"] == 3
    assert entry["ready"] is True
    assert entry["blockers"] == []
    assert entry["auth_state_ready"] is False
    assert entry["storage_state_files"][0]["status"] == "empty"


def test_external_samples_readiness_can_require_live_auth_metadata(tmp_path) -> None:
    auth_dir = tmp_path / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "seller-sandbox-state.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "secret",
                        "domain": ".other.sandbox.example.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    (auth_dir / "inventory-sandbox-state.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "secret",
                        "domain": ".inventory.sandbox.example.com",
                        "path": "/",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    (auth_dir / "finance-sandbox-state.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "session",
                        "value": "secret",
                        "domain": ".finance.sandbox.example.com",
                        "path": "/",
                        "expires": 1,
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )

    result = external_samples_readiness(workspace_root=tmp_path, require_live_auth=True)
    ecommerce = entry_by_id(result, "external_ecommerce_orders_readonly")
    inventory = entry_by_id(result, "external_inventory_restock_confirm")
    finance = entry_by_id(result, "external_finance_reconciliation_export")

    assert result["require_live_auth"] is True
    assert result["ready_samples"] == 2
    assert "auth_state_not_ready" in ecommerce["blockers"]
    assert ecommerce["storage_state_files"][0]["status"] == "domain_mismatch"
    assert inventory["auth_state_ready"] is True
    assert inventory["storage_state_files"][0]["matched_allowed_domains"] == ["inventory.sandbox.example.com"]
    assert "auth_state_not_ready" in finance["blockers"]
    assert finance["storage_state_files"][0]["status"] == "expired"


def test_external_samples_check_rejects_inline_secret_and_live_execution(tmp_path) -> None:
    root = tmp_path / "external_samples"
    root.mkdir()
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "samples": [
                    {
                        "id": "bad",
                        "workflow": "bad.yaml",
                        "live_execution_allowed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "bad.yaml").write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: bad_external_sample
version: 1
steps:
  - id: observe
    action: observe_browser
    url: https://seller.example.com/orders
  - id: assert
    action: assert_text
    text: Orders
  - id: fill_password
    action: paste
    target: Password
    value: plain-secret
    password: plain-secret
""".strip(),
        encoding="utf-8",
    )

    result = check_external_samples(root)
    issue_codes = {issue["code"] for issue in result["checks"][0]["issues"]}

    assert result["invalid_samples"] == 1
    assert "live_execution_not_disabled" in issue_codes
    assert "inline_secret" in issue_codes
    assert "unsafe_mutating_step" in issue_codes


def test_external_samples_check_rejects_missing_account_contract(tmp_path) -> None:
    root = tmp_path / "external_samples"
    root.mkdir()
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "samples": [
                    {
                        "id": "bad_contract",
                        "workflow": "bad_contract.yaml",
                        "owner": "qa",
                        "data_classification": "test",
                        "live_execution_allowed": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "bad_contract.yaml").write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: bad_contract
version: 1
steps:
  - id: observe
    action: observe_browser
    url: https://seller.example.com/orders
  - id: assert
    action: assert_text
    text: Orders
""".strip(),
        encoding="utf-8",
    )

    result = check_external_samples(root)
    issue_codes = {issue["code"] for issue in result["checks"][0]["issues"]}

    assert result["invalid_samples"] == 1
    assert "invalid_account_environment" in issue_codes


def test_external_samples_catalog_policy_supplies_default_contract(tmp_path) -> None:
    root = tmp_path / "external_samples"
    root.mkdir()
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": {
                    "allowed_domains": ["seller.sandbox.example.com"],
                    "storage_state_policy": "optional",
                    "download_policy": "forbidden",
                    "mutating_action_policy": "dry-run-or-confirm",
                    "live_execution_allowed": False,
                },
                "samples": [
                    {
                        "id": "policy_inherited",
                        "workflow": "policy_inherited.yaml",
                        "owner": "qa",
                        "data_classification": "test",
                        "account_environment": "sandbox",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "policy_inherited.yaml").write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: policy_inherited
version: 1
steps:
  - id: observe
    action: observe_browser
    url: https://seller.sandbox.example.com/orders
  - id: assert
    action: assert_text
    text: Orders
""".strip(),
        encoding="utf-8",
    )

    result = check_external_samples(root)
    readiness = external_samples_readiness(root)

    assert result["valid_samples"] == 1
    assert result["policy"]["download_policy"] == "forbidden"
    assert readiness["entries"][0]["allowed_domains"] == ["seller.sandbox.example.com"]
    assert readiness["entries"][0]["storage_state_policy"] == "optional"
    assert readiness["entries"][0]["mutating_action_policy"] == "dry-run-or-confirm"


def test_external_samples_mutating_policy_can_require_confirm(tmp_path) -> None:
    root = tmp_path / "external_samples"
    root.mkdir()
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": {
                    "allowed_domains": ["seller.sandbox.example.com"],
                    "storage_state_policy": "optional",
                    "download_policy": "forbidden",
                    "mutating_action_policy": "confirm-required",
                    "live_execution_allowed": False,
                },
                "samples": [
                    {
                        "id": "confirm_required",
                        "workflow": "confirm_required.yaml",
                        "owner": "qa",
                        "data_classification": "test",
                        "account_environment": "sandbox",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "confirm_required.yaml").write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: confirm_required
version: 1
steps:
  - id: observe
    action: observe_browser
    url: https://seller.sandbox.example.com/orders
  - id: dry_run_click
    action: click
    dry_run: true
    target: Submit
  - id: assert
    action: assert_text
    text: Orders
""".strip(),
        encoding="utf-8",
    )

    result = check_external_samples(root)
    issue_codes = {issue["code"] for issue in result["checks"][0]["issues"]}

    assert result["invalid_samples"] == 1
    assert "mutating_action_requires_confirm" in issue_codes


def test_external_samples_check_enforces_storage_domain_and_download_policy(tmp_path) -> None:
    root = tmp_path / "external_samples"
    root.mkdir()
    (root / "catalog.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "samples": [
                    {
                        "id": "bad_workflow",
                        "workflow": "bad_workflow.yaml",
                        "owner": "qa",
                        "data_classification": "test",
                        "account_environment": "sandbox",
                        "allowed_domains": ["seller.sandbox.example.com"],
                        "storage_state_policy": "required",
                        "download_policy": "dry-run-only",
                        "live_execution_allowed": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "bad_workflow.yaml").write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: bad_workflow
version: 1
steps:
  - id: observe
    action: observe_browser
    url: https://seller.example.com/orders
  - id: assert
    action: assert_text
    text: Orders
  - id: export
    action: expect_download
    target:
      text: Export
      role: button
""".strip(),
        encoding="utf-8",
    )

    result = check_external_samples(root)
    readiness = external_samples_readiness(root)
    issue_codes = {issue["code"] for issue in result["checks"][0]["issues"]}

    assert result["invalid_samples"] == 1
    assert "url_outside_allowed_domains" in issue_codes
    assert "missing_storage_state" in issue_codes
    assert "download_must_be_dry_run" in issue_codes
    assert "missing_storage_state" in readiness["entries"][0]["blockers"]


def test_external_sample_run_plan_blocks_missing_readiness() -> None:
    plan = build_external_sample_run_plan("external_ecommerce_orders_readonly")

    assert plan["ready"] is False
    assert plan["run_profile"] == "dry-run"
    assert plan["dry_run"] is True
    assert "missing_storage_state_file" in plan["blockers"]


def test_external_sample_run_plan_rejects_approved_profile() -> None:
    with pytest.raises(ValueError):
        build_external_sample_run_plan("external_ecommerce_orders_readonly", run_profile="approved")


def test_external_sample_run_plan_accepts_ready_storage_state(tmp_path) -> None:
    auth_dir = tmp_path / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "seller-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    (auth_dir / "inventory-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    (auth_dir / "finance-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    plan = build_external_sample_run_plan("external_ecommerce_orders_readonly", workspace_root=tmp_path, run_profile="supervised")

    assert plan["ready"] is True
    assert plan["dry_run"] is False
    assert plan["requires_confirmation"] is True
    assert plan["allowed_domains"] == ["seller.sandbox.example.com"]


def test_external_sample_run_plan_accepts_optional_storage_sample() -> None:
    plan = build_external_sample_run_plan("external_support_tickets_triage")

    assert plan["ready"] is True
    assert plan["dry_run"] is True
    assert plan["storage_state_policy"] == "optional"
    assert plan["download_policy"] == "forbidden"


def test_external_sample_batch_plan_lists_ready_and_blocked_samples(tmp_path) -> None:
    batch = build_external_sample_batch_plan(workspace_root=tmp_path)

    assert batch["total_samples"] == 4
    assert batch["ready_samples"] == 1
    assert batch["blocked_samples"] == 3
    ready = [plan["sample_id"] for plan in batch["plans"] if plan["ready"]]
    assert ready == ["external_support_tickets_triage"]


def test_submit_external_sample_batch_queues_only_ready_samples(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = submit_external_sample_batch(workspace, priority=7, max_retries=1)
    queue = list_queue_tasks(workspace)

    assert result["submitted_count"] == 1
    assert result["skipped_count"] == 3
    assert result["submitted"][0]["sample_id"] == "external_support_tickets_triage"
    assert queue["pending_tasks"] == 1
    assert queue["entries"][0]["workflow"] == "workflows/external_samples/support_tickets_triage.yaml"
    assert queue["entries"][0]["priority"] == 7
    assert queue["entries"][0]["max_retries"] == 1
    assert queue["entries"][0]["metadata"]["external_sample"]["sample_id"] == "external_support_tickets_triage"
    assert "support_tickets_demo.html" in (workspace.root / queue["entries"][0]["workflow"]).read_text(encoding="utf-8")


def test_external_sample_run_summary_merges_readiness_queue_and_reports(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_result = submit_external_sample_batch(workspace)

    summary = build_external_sample_run_summary(workspace)
    support = next(item for item in summary["entries"] if item["sample_id"] == "external_support_tickets_triage")
    ecommerce = next(item for item in summary["entries"] if item["sample_id"] == "external_ecommerce_orders_readonly")

    assert summary["total_samples"] == 4
    assert summary["queued_samples"] == 1
    assert support["status"] == "pending"
    assert support["latest_queue_task"]["task_id"] == submit_result["submitted"][0]["task_id"]
    assert ecommerce["status"] == "blocked"
    assert "missing_storage_state_file" in ecommerce["blockers"]

    def fake_run_workspace_workflow(workspace_arg, workflow_name, **kwargs):
        run_id = "external-summary-run"
        run_dir = workspace.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "workflow_result.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "workflow_name": "external_support_tickets_triage",
                    "workflow_schema_version": 1,
                    "runtime_version": "0.1.0",
                    "run_profile": kwargs["run_profile"],
                    "steps": [],
                }
            ),
            encoding="utf-8",
        )
        from visual_agent.workspace import export_workspace_run_report

        export_workspace_run_report(workspace, run_dir)
        return SimpleNamespace(steps=(), run_id=run_id, run_dir=run_dir)

    monkeypatch.setattr("visual_agent.external_samples.run_workspace_workflow", fake_run_workspace_workflow)
    run_external_sample(workspace, "external_support_tickets_triage")

    after_run = build_external_sample_run_summary(workspace)
    support_after_run = next(item for item in after_run["entries"] if item["sample_id"] == "external_support_tickets_triage")
    assert after_run["with_reports"] == 1
    assert support_after_run["status"] == "success"
    assert support_after_run["latest_report"]["run_id"] == "external-summary-run"


def write_external_sample_report(
    workspace,
    run_id: str,
    sample_id: str,
    *,
    status: str = "failed",
    failed_action: str = "observe_browser",
    failed_message: str | None = None,
) -> None:
    workspace.reports_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "workflow_name": sample_id,
        "status": status,
        "run_profile": "dry-run",
        "total_steps": 1,
        "succeeded_steps": 0 if status == "failed" else 1,
        "failed_step": "observe" if status == "failed" else None,
        "dry_run_actions": 0,
        "elapsed_seconds": 0.1,
        "artifacts": {},
        "downloads": [],
        "steps": [
            {
                "id": "observe",
                "action": failed_action,
                "status": status,
                "message": failed_message or status,
            }
        ],
        "external_sample": {
            "schema_version": 1,
            "sample_id": sample_id,
            "run_status": status,
            "run_profile": "dry-run",
            "ready_at_run": True,
            "blockers_at_run": [],
            "requirements": [],
            "allowed_domains": [],
            "storage_state_policy": "optional",
            "download_policy": "forbidden",
        },
    }
    (workspace.reports_dir / f"{run_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    from visual_agent.workspace import write_workspace_report_index

    write_workspace_report_index(workspace)


def test_external_sample_rerun_plan_and_submit_only_ready_failed_samples(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_external_sample_report(workspace, "failed-support", "external_support_tickets_triage", status="failed")
    write_external_sample_report(workspace, "failed-ecommerce", "external_ecommerce_orders_readonly", status="failed")

    plan = build_external_sample_rerun_plan(workspace)
    result = submit_external_sample_reruns(workspace, priority=9)
    queue = list_queue_tasks(workspace)

    assert plan["candidate_count"] == 1
    assert plan["candidates"][0]["sample_id"] == "external_support_tickets_triage"
    assert plan["skipped_count"] == 1
    assert plan["skipped"][0]["sample_id"] == "external_ecommerce_orders_readonly"
    assert result["submitted_count"] == 1
    assert queue["pending_tasks"] == 1
    assert queue["entries"][0]["workflow"] == "workflows/external_samples/support_tickets_triage.yaml"
    assert queue["entries"][0]["priority"] == 9
    assert queue["entries"][0]["metadata"]["external_sample"]["sample_id"] == "external_support_tickets_triage"


def test_external_sample_queue_run_annotates_report_metadata(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_external_sample_batch(workspace)

    def fake_run_workspace_workflow(workspace_arg, workflow_name, **kwargs):
        run_id = "queued-external-run"
        run_dir = workspace.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "workflow_result.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "workflow_name": "external_support_tickets_triage",
                    "workflow_schema_version": 1,
                    "runtime_version": "0.1.0",
                    "run_profile": kwargs["run_profile"],
                    "steps": [],
                }
            ),
            encoding="utf-8",
        )
        from visual_agent.workspace import export_workspace_run_report

        export_workspace_run_report(workspace, run_dir)
        return SimpleNamespace(steps=(), run_id=run_id, run_dir=run_dir)

    monkeypatch.setattr("visual_agent.scheduler.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_next_queue_task(workspace)
    report = json.loads((workspace.reports_dir / f"{result['result']['run_id']}.json").read_text(encoding="utf-8"))
    detail = build_report_detail(workspace, result["result"]["run_id"])

    assert result["task"]["status"] == "success"
    assert report["external_sample"]["sample_id"] == "external_support_tickets_triage"
    assert detail["external_sample"]["sample_id"] == "external_support_tickets_triage"


def test_external_sample_batch_report_exports_json_and_markdown(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_external_sample_batch(workspace)
    write_external_sample_report(workspace, "failed-support", "external_support_tickets_triage", status="failed")

    result = export_external_sample_batch_report(workspace)
    json_path = Path(result["json_report"])
    markdown_path = Path(result["markdown_report"])
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")

    assert result["report_id"].startswith("external-samples-")
    assert json_path.exists()
    assert markdown_path.exists()
    assert payload["summary"]["total_samples"] == 4
    assert payload["summary"]["with_reports"] == 1
    assert payload["summary"]["queued_samples"] == 1
    assert "- Batch status: `failed`" in markdown
    assert "## Failure Summary" in markdown
    assert "## Rerun Commands" in markdown
    assert "external-sample-batch-rerun-submit --workspace-root .agent-workspace --report-id" in markdown
    assert "## Blocked Samples" in markdown
    assert "Import the required storage_state with auth-state-import" in markdown
    assert "| external_support_tickets_triage | failed | True | pending | failed |  |" in markdown
    assert "Check route fixtures, allowed domain, storage_state readiness" in markdown


def test_external_sample_dry_run_report_runs_ready_and_records_blocked(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    calls = []

    def fake_run_external_sample(workspace_arg, sample_id, **kwargs):
        calls.append((workspace_arg, sample_id, kwargs))
        return {
            "schema_version": 1,
            "sample_id": sample_id,
            "status": "success",
            "run_id": "dry-run-support",
            "workflow": "workflows/external_samples/support_tickets_triage.yaml",
            "report": {
                "json_report": str(workspace.reports_dir / "dry-run-support.json"),
                "markdown_report": str(workspace.reports_dir / "dry-run-support.md"),
            },
        }

    monkeypatch.setattr("visual_agent.external_samples.run_external_sample", fake_run_external_sample)

    result = export_external_sample_dry_run_report(workspace)
    payload = json.loads(Path(result["json_report"]).read_text(encoding="utf-8"))
    markdown = Path(result["markdown_report"]).read_text(encoding="utf-8")

    assert result["report_id"].startswith("external-samples-dry-run-")
    assert result["report_type"] == "dry_run_integration"
    assert result["summary"]["attempted_samples"] == 1
    assert result["summary"]["success_samples"] == 1
    assert result["summary"]["blocked_samples"] == 3
    assert calls == [(workspace, "external_support_tickets_triage", {"root": Path("examples/external_samples"), "run_profile": "dry-run", "preflight": True, "require_live_auth": False})]
    assert payload["summary"]["entries"][0]["sample_id"] == "external_ecommerce_orders_readonly"
    assert any(entry["status"] == "success" for entry in payload["summary"]["entries"])
    assert "# External Sample Dry-Run Report" in markdown
    assert "## Blocked Samples" in markdown
    assert "external_support_tickets_triage | success | True | True | dry-run-support" in markdown


def test_external_sample_dry_run_report_records_run_errors(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    def fake_run_external_sample(workspace_arg, sample_id, **kwargs):
        raise RuntimeError("browser unavailable")

    monkeypatch.setattr("visual_agent.external_samples.run_external_sample", fake_run_external_sample)

    result = export_external_sample_dry_run_report(workspace)
    markdown = Path(result["markdown_report"]).read_text(encoding="utf-8")
    failed = [entry for entry in result["summary"]["entries"] if entry["status"] == "failed"]

    assert result["summary"]["attempted_samples"] == 1
    assert result["summary"]["failed_samples"] == 1
    assert failed[0]["sample_id"] == "external_support_tickets_triage"
    assert failed[0]["error"] == "RuntimeError: browser unavailable"
    assert "## Failed Dry-Runs" in markdown
    assert "RuntimeError: browser unavailable" in markdown


def test_external_sample_live_placeholder_exports_skipped_requirements(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    result = export_external_sample_live_placeholder(workspace)
    payload = json.loads(Path(result["json_report"]).read_text(encoding="utf-8"))
    markdown = Path(result["markdown_report"]).read_text(encoding="utf-8")
    ecommerce = next(entry for entry in result["summary"]["entries"] if entry["sample_id"] == "external_ecommerce_orders_readonly")

    assert result["report_id"].startswith("external-samples-live-placeholder-")
    assert result["report_type"] == "live_account_placeholder"
    assert result["status"] == "skipped"
    assert result["summary"]["skipped_samples"] == 4
    assert payload["status"] == "skipped"
    assert "live_execution_not_authorized" in ecommerce["blockers"]
    assert "missing_storage_state_file" in ecommerce["blockers"]
    assert "placeholder_allowed_domain" in ecommerce["blockers"]
    assert ecommerce["required_accounts"] == ["sandbox account for seller.sandbox.example.com"]
    assert "valid non-expired authenticated session" in ecommerce["required_permissions"]
    assert "# External Sample Live Account Placeholder" in markdown
    assert "Current state: skipped until the blockers above are resolved." in markdown


def test_external_sample_batch_report_markdown_notes_clean_batch(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    for name in ("seller-sandbox-state.json", "inventory-sandbox-state.json", "finance-sandbox-state.json"):
        (auth_dir / name).write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    result = export_external_sample_batch_report(workspace)
    markdown = Path(result["markdown_report"]).read_text(encoding="utf-8")

    assert "- Batch status: `ready`" in markdown
    assert "## Review Notes" in markdown
    assert "No failed or blocked samples in this batch." in markdown
    assert "## Rerun Commands" not in markdown


def test_external_sample_batch_report_index_lists_and_filters_reports(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_external_sample_batch(workspace)
    queued_report = export_external_sample_batch_report(workspace)
    write_external_sample_report(workspace, "failed-support", "external_support_tickets_triage", status="failed")
    failed_report = export_external_sample_batch_report(workspace)

    index = load_external_sample_batch_report_index(workspace, rebuild=True)
    failed = list_external_sample_batch_reports(workspace, status="failed")
    support = load_external_sample_batch_report_index(
        workspace,
        rebuild=True,
        sample_id="external_support_tickets_triage",
    )

    assert index["total_reports"] == 2
    assert index["failed_reports"] == 1
    assert index["queued_reports"] == 1
    assert index["latest"]["report_id"] == failed_report["report_id"]
    assert failed[0]["report_id"] == failed_report["report_id"]
    assert failed[0]["failed_samples"] == ["external_support_tickets_triage"]
    assert support["total_reports"] == 2
    assert queued_report["index"].endswith("index.json")


def test_external_sample_batch_failure_summary_and_rerun_submit_respect_readiness(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_external_sample_report(workspace, "failed-support", "external_support_tickets_triage", status="failed")
    write_external_sample_report(workspace, "failed-ecommerce", "external_ecommerce_orders_readonly", status="failed")
    batch = export_external_sample_batch_report(workspace)

    failures = build_external_sample_batch_failure_summary(workspace, batch["report_id"])
    plan = build_external_sample_batch_rerun_plan(workspace, batch["report_id"])
    result = submit_external_sample_batch_reruns(workspace, batch["report_id"], priority=5, max_retries=1)
    queue = list_queue_tasks(workspace)

    assert failures["failed_count"] == 2
    assert failures["ready_failed_count"] == 1
    assert failures["blocked_failed_count"] == 1
    assert {item["sample_id"] for item in failures["failures"]} == {
        "external_support_tickets_triage",
        "external_ecommerce_orders_readonly",
    }
    assert plan["candidate_count"] == 1
    assert plan["candidates"][0]["sample_id"] == "external_support_tickets_triage"
    assert plan["candidates"][0]["rerun_suggestion"]["category"] == "inspect_observation_then_rerun"
    assert plan["skipped"][0]["sample_id"] == "external_ecommerce_orders_readonly"
    assert plan["skipped"][0]["rerun_suggestion"]["category"] == "fix_readiness"
    assert result["submitted_count"] == 1
    assert result["skipped_count"] == 1
    assert queue["entries"][0]["priority"] == 5
    assert queue["entries"][0]["max_retries"] == 1
    assert queue["entries"][0]["metadata"]["source_batch_report_id"] == batch["report_id"]


def test_external_sample_failure_summary_suggests_runtime_fix(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    write_external_sample_report(
        workspace,
        "failed-support",
        "external_support_tickets_triage",
        status="failed",
        failed_message="Executable doesn't exist. Please run playwright install.",
    )
    batch = export_external_sample_batch_report(workspace)

    failures = build_external_sample_batch_failure_summary(workspace, batch["report_id"])
    suggestion = failures["failures"][0]["rerun_suggestion"]

    assert suggestion["category"] == "fix_runtime_then_rerun"
    assert suggestion["confidence"] == "high"
    assert suggestion["commands"] == ["python -m playwright install"]


def test_submit_external_sample_batch_queues_all_ready_after_auth_states(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    for name in ("seller-sandbox-state.json", "inventory-sandbox-state.json", "finance-sandbox-state.json"):
        (auth_dir / name).write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    result = submit_external_sample_batch(workspace)
    queue = list_queue_tasks(workspace)

    assert result["submitted_count"] == 4
    assert result["skipped_count"] == 0
    assert queue["pending_tasks"] == 4


def test_run_external_sample_rejects_blocked_sample(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    with pytest.raises(RuntimeError):
        run_external_sample(workspace, "external_ecommerce_orders_readonly")


def test_run_external_sample_copies_workflow_and_uses_workspace_runner(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "seller-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
    calls = []

    def fake_run_workspace_workflow(workspace_arg, workflow_name, **kwargs):
        calls.append((workspace_arg, workflow_name, kwargs))
        return SimpleNamespace(steps=(), run_id="external-run-1", run_dir=workspace.root / "runs" / "external-run-1")

    monkeypatch.setattr("visual_agent.external_samples.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_external_sample(workspace, "external_ecommerce_orders_readonly", run_profile="dry-run")

    assert result["status"] == "success"
    assert result["run_id"] == "external-run-1"
    assert result["dry_run"] is True
    assert result["workflow"] == "workflows/external_samples/ecommerce_orders_readonly.yaml"
    assert (workspace.root / result["workflow"]).exists()
    assert calls[0][1] == "workflows/external_samples/ecommerce_orders_readonly.yaml"
    assert calls[0][2]["dry_run"] is True
    assert calls[0][2]["run_profile"] == "dry-run"
    materialized = (workspace.root / result["workflow"]).read_text(encoding="utf-8")
    assert "body_from_file:" in materialized
    assert "ecommerce_orders_demo.html" in materialized


def test_run_external_sample_annotates_report_and_gui_detail(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    def fake_run_workspace_workflow(workspace_arg, workflow_name, **kwargs):
        run_id = "external-run-report"
        run_dir = workspace.root / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "workflow_result.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "workflow_name": "external_support_tickets_triage",
                    "workflow_schema_version": 1,
                    "runtime_version": "0.1.0",
                    "run_profile": kwargs["run_profile"],
                    "steps": [
                        {
                            "id": "observe_tickets",
                            "action": "observe_browser",
                            "status": "success",
                            "message": "ok",
                            "metadata": {"elapsed_seconds": 0.01},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        from visual_agent.workspace import export_workspace_run_report

        export_workspace_run_report(workspace, run_dir)
        return SimpleNamespace(steps=(), run_id=run_id, run_dir=run_dir)

    monkeypatch.setattr("visual_agent.external_samples.run_workspace_workflow", fake_run_workspace_workflow)

    result = run_external_sample(workspace, "external_support_tickets_triage")
    report = json.loads((workspace.reports_dir / f"{result['run_id']}.json").read_text(encoding="utf-8"))
    markdown = (workspace.reports_dir / f"{result['run_id']}.md").read_text(encoding="utf-8")
    detail = build_report_detail(workspace, result["run_id"])
    index = load_workspace_report_index(workspace, rebuild=True)

    assert report["external_sample"]["sample_id"] == "external_support_tickets_triage"
    assert report["external_sample"]["ready_at_run"] is True
    assert "## External Sample" in markdown
    assert detail["external_sample"]["sample_id"] == "external_support_tickets_triage"
    assert "external_support_tickets_triage" in report_detail_to_markdown(detail)
    assert index["entries"][0]["external_sample"]["sample_id"] == "external_support_tickets_triage"


def test_external_support_sample_runs_with_local_route_when_browser_available(tmp_path) -> None:
    pytest.importorskip("playwright")
    workspace = init_workspace(tmp_path / "agent-workspace")

    try:
        result = run_external_sample(workspace, "external_support_tickets_triage", run_profile="dry-run")
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(str(exc))
        raise

    skip_if_browser_unavailable(result)
    assert result["status"] == "success"
    assert result["sample_id"] == "external_support_tickets_triage"
    assert result["dry_run"] is True


def test_external_finance_sample_downloads_with_local_route_when_browser_available(tmp_path) -> None:
    pytest.importorskip("playwright")
    workspace = init_workspace(tmp_path / "agent-workspace")
    auth_dir = workspace.root / ".agent-auth"
    auth_dir.mkdir()
    (auth_dir / "finance-sandbox-state.json").write_text('{"cookies":[],"origins":[]}', encoding="utf-8")

    try:
        result = run_external_sample(workspace, "external_finance_reconciliation_export", run_profile="supervised")
    except Exception as exc:
        if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
            pytest.skip(str(exc))
        raise

    skip_if_browser_unavailable(result)
    assert result["status"] == "success"
    run_dir = workspace.root / "runs" / result["run_id"]
    assert (run_dir / "downloads" / "reconciliation-export.csv").exists()
