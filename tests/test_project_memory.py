from __future__ import annotations

from visual_agent.chief_engineer import build_chief_plan
from visual_agent.chief_plans_store import save_plan
from visual_agent.cli import main
from visual_agent.missions import append_round, create_mission, default_budget_policy, write_final_report
from visual_agent.project_memory import build_project_memory, project_memory_handoff_notes, project_memory_to_markdown
from visual_agent.workspace import init_workspace


def write_verification_workflow(workspace, name: str, *, affects: str = "src/payment/") -> None:
    workspace.workflows_dir.joinpath(f"{name}.yaml").write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        "version: 1\n"
        "affects:\n"
        f"  - {affects}\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )


def seed_failed_checkout_mission(tmp_path):
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan = {
        "objective": "Fix checkout total display",
        "status": "ready",
        "acceptance_criteria": ["Order total and currency display correctly."],
        "selected_workflows": ["checkout"],
    }
    saved = save_plan(plan, workspace_root=workspace.root, plan_id="plan-checkout")
    mission = create_mission(
        workspace_root=workspace.root,
        objective="Fix checkout total display",
        repo_root=tmp_path,
        plan_id=saved["plan_id"],
        budget_policy=default_budget_policy(max_rounds=2, max_wall_minutes=30),
        mission_id="mission-checkout",
        status="stopped",
    )
    mission["stop_reason"] = "same_failure_repeated"
    from visual_agent.missions import save_mission

    save_mission(workspace.root, mission)
    append_round(
        workspace.root,
        "mission-checkout",
        {
            "round": 1,
            "type": "verification",
            "status": "fail",
            "failed_signature": "checkout|assert_total|expected total 128",
        },
    )
    write_final_report(workspace.root, "mission-checkout", "## Failed checkout mission")
    return workspace


def test_project_memory_builds_recommendations_from_mission_evidence(tmp_path) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)

    payload = build_project_memory(workspace_root=workspace.root, goal="Fix checkout total rounding")

    assert payload["entry_count"] == 1
    assert payload["entries"][0]["mission_id"] == "mission-checkout"
    assert payload["entries"][0]["relevance_score"] > 0
    assert "same failure" in " ".join(payload["recommendations"]).lower()
    assert "checkout|assert_total|expected total 128" in payload["entries"][0]["evidence"]["failed_signatures"]


def test_project_memory_markdown_and_handoff_notes(tmp_path) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)
    payload = build_project_memory(workspace_root=workspace.root, goal="checkout total")

    text = project_memory_to_markdown(payload)
    notes = project_memory_handoff_notes(payload)

    assert "mission-checkout" in text
    assert "Recommendations" in text
    assert notes
    assert "mission-checkout" in notes[0]


def test_chief_memory_cli_outputs_json(tmp_path, capsys) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)

    code = main(
        [
            "chief-memory",
            "--workspace-root",
            str(workspace.root),
            "--goal",
            "checkout total",
            "--format",
            "json",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert '"mission_id": "mission-checkout"' in out
    assert '"recommendations"' in out


def test_chief_plan_includes_project_memory_in_worker_prompt(tmp_path, monkeypatch) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="Fix checkout total rounding",
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    assert plan.project_memory["entry_count"] == 1
    assert plan.project_memory["entries"][0]["mission_id"] == "mission-checkout"
    assert "Project memory" in plan.worker_tracks[0]["command"]
    assert "Project memory" in plan.handoff_prompt
