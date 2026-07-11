from __future__ import annotations

import json
from pathlib import Path

from visual_agent.cli import main
from visual_agent.chief_queue import list_mission_queue_items
from visual_agent.mission_plan_import import import_development_plan, parse_development_plan
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


def ready_workspace(tmp_path, monkeypatch):
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])
    return workspace


def test_parse_development_plan_prefers_open_tasks_over_headings() -> None:
    text = """# DevPacer Sprint

## Checkout
- [ ] Fix checkout total display
- [x] Replace old copy already done
- Add visible payment error state

## Background
Notes only.
"""

    parsed = parse_development_plan(text)

    assert parsed["title"] == "DevPacer Sprint"
    assert parsed["skipped_completed_tasks"] == 1
    assert [item["objective"] for item in parsed["drafts"]] == [
        "Fix checkout total display",
        "Checkout: Add visible payment error state",
    ]
    assert all(item["source_type"] in {"task", "bullet"} for item in parsed["drafts"])


def test_parse_development_plan_uses_headings_when_no_task_lines() -> None:
    text = """# Roadmap

## Project memory v1

## Mission queue retry support
"""

    parsed = parse_development_plan(text, limit=1)

    assert parsed["total_drafts"] == 1
    assert parsed["drafts"][0]["objective"] == "Project memory v1"
    assert parsed["drafts"][0]["source_type"] == "heading"


def test_parse_development_plan_skips_question_bullets() -> None:
    text = """# Vision Notes

## Gemini Review
- "这个结账页在人类眼里像不像一个正常的成功状态？"
- Add Gemini `visual_review` artifact after verification
"""

    parsed = parse_development_plan(text)

    assert [item["objective"] for item in parsed["drafts"]] == [
        "Gemini Review: Add Gemini visual_review artifact after verification"
    ]


def test_parse_development_plan_preserves_long_task_objectives() -> None:
    long_tail = " ".join(f"criterion-{index}" for index in range(80))
    text = f"# Long Plan\n\n- [ ] Implement checkout reliability with acceptance details {long_tail}\n"

    parsed = parse_development_plan(text)

    objective = parsed["drafts"][0]["objective"]
    assert "criterion-79" in objective
    assert len(objective) > 260


def test_import_development_plan_saves_draft_record(tmp_path) -> None:
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] Fix checkout total display\n- [ ] Add login error state\n", encoding="utf-8")
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)

    payload = import_development_plan(
        source_file=plan_file,
        workspace_root=workspace.root,
        repo_root=tmp_path,
        create=False,
        limit=1,
    )

    assert payload["status"] == "drafted"
    assert payload["total_drafts"] == 1
    assert payload["created_missions"] == []
    assert Path(payload["saved_path"]).exists()
    saved = json.loads(Path(payload["saved_path"]).read_text(encoding="utf-8"))
    assert saved["import_id"] == payload["import_id"]


def test_import_development_plan_creates_and_queues_preview_missions(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] Fix checkout total display\n", encoding="utf-8")

    payload = import_development_plan(
        source_file=plan_file,
        workspace_root=workspace.root,
        repo_root=tmp_path,
        create=True,
        queue=True,
        limit=1,
        run_profile="dry-run",
    )

    assert payload["status"] == "queued"
    assert payload["created_missions"][0]["status"] == "preview"
    assert payload["queued_items"][0]["status"] == "pending"
    queue_payload = list_mission_queue_items(workspace.root)
    assert queue_payload["pending_items"] == 1


def test_import_development_plan_queues_worker_context(tmp_path, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] Fix checkout total display\n", encoding="utf-8")

    payload = import_development_plan(
        source_file=plan_file,
        workspace_root=workspace.root,
        repo_root=tmp_path,
        create=True,
        queue=True,
        limit=1,
        agents=("codex",),
        test_command="python -m pytest -q",
        allow_test_edits=True,
        merge_policy="auto",
    )

    item = payload["queued_items"][0]
    assert item["agent"] == "codex"
    assert item["test_command"] == "python -m pytest -q"
    assert item["allow_test_edits"] is True
    assert item["merge_policy"] == "auto"


def test_mission_import_facade_outputs_json(tmp_path, capsys, monkeypatch) -> None:
    workspace = ready_workspace(tmp_path, monkeypatch)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] Fix checkout total display\n", encoding="utf-8")

    code = main(
        [
            "mission",
            "import",
            "--file",
            str(plan_file),
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--create",
            "--queue",
            "--agent",
            "codex",
            "--test-command",
            "python -m pytest tests/test_security.py",
            "--merge-policy",
            "auto",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "queued"
    assert payload["total_drafts"] == 1
    assert payload["created_missions"][0]["mission_id"]
    assert payload["queued_items"][0]["queue_id"]
    assert payload["queued_items"][0]["agent"] == "codex"
    assert payload["queued_items"][0]["test_command"]
    assert payload["queued_items"][0]["merge_policy"] == "auto"
