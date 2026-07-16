from __future__ import annotations

from visual_agent.chief_engineer import build_chief_plan
from visual_agent.chief_dispatch import build_worker_command
from visual_agent.chief_plans_store import append_worker_record, load_plan, save_plan, save_verification
from visual_agent.cli import main
from visual_agent.missions import append_round, create_mission, default_budget_policy, load_mission, write_final_report
from visual_agent.project_memory import (
    build_project_memory,
    load_instruction_memory,
    project_memory_handoff_notes,
    project_memory_to_markdown,
    score_memory_entry,
)
from visual_agent.program_scheduler import start_program
from visual_agent.programs import create_program_from_plan
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


def seed_evidence_mission(
    tmp_path,
    workspace,
    *,
    mission_id: str,
    objective: str,
    status: str = "verified",
    changed_files: list[str] | None = None,
    functions_touched: list[str] | None = None,
):
    plan_id = f"plan-{mission_id}"
    changed = changed_files or []
    plan = {
        "objective": objective,
        "status": "ready",
        "changed_files": changed,
        "acceptance_criteria": ["The worker lock remains safe while the queue is active."],
        "selected_workflows": ["queue-safety"],
        "verification_commands": ["python -m pytest tests/test_chief_queue.py"],
    }
    save_plan(plan, workspace_root=workspace.root, plan_id=plan_id)
    mission = create_mission(
        workspace_root=workspace.root,
        objective=objective,
        repo_root=tmp_path,
        plan_id=plan_id,
        budget_policy=default_budget_policy(max_rounds=2, max_wall_minutes=30),
        mission_id=mission_id,
        status=status,
    )
    mission["stop_reason"] = "verified" if status == "verified" else "preview_only"
    from visual_agent.missions import save_mission

    save_mission(workspace.root, mission)
    append_worker_record(
        workspace.root,
        plan_id,
        {
            "status": "completed",
            "cwd": str(tmp_path),
            "stdout_tail": (
                "Replaced signal-based PID probing with a read-only Windows process check. "
                "MIMO_API_KEY=dummy-secret-value"
            ),
            "stderr_tail": "tokens used 12,345",
        },
    )
    save_verification(
        workspace.root,
        plan_id,
        {
            "verdict": "pass",
            "command_verification": {
                "command": "python -m pytest tests/test_chief_queue.py",
                "verdict": "pass",
                "exit_code": 0,
            },
            "diff_summary": {
                "changed_files": changed,
                "functions_touched": functions_touched or [],
            },
        },
    )
    return plan_id


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


def test_instruction_memory_loads_pacer_files(tmp_path) -> None:
    (tmp_path / "PACER.md").write_text("Use focused tests before full pytest.\n", encoding="utf-8")
    rules = tmp_path / ".pacer" / "rules"
    rules.mkdir(parents=True)
    (rules / "testing.md").write_text("Never claim done without verification evidence.\n", encoding="utf-8")

    payload = load_instruction_memory(tmp_path)

    assert payload["file_count"] == 2
    paths = [item["path"] for item in payload["files"]]
    assert paths == ["PACER.md", ".pacer/rules/testing.md"]
    assert "focused tests" in payload["files"][0]["excerpt"]


def test_project_memory_includes_instruction_memory_in_handoff(tmp_path) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)
    (tmp_path / "PACER.md").write_text("Prefer smoke tests before full pytest.\n", encoding="utf-8")

    payload = build_project_memory(workspace_root=workspace.root, repo_root=tmp_path, goal="checkout total")
    text = project_memory_to_markdown(payload)
    notes = project_memory_handoff_notes(payload, max_items=4)

    assert payload["instruction_memory"]["file_count"] == 1
    assert "Project Instructions" in text
    assert any("PACER.md" in note for note in notes)


def test_chief_memory_cli_outputs_json(tmp_path, capsys) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)
    (tmp_path / "PACER.md").write_text("Run the selected acceptance gate.\n", encoding="utf-8")

    code = main(
        [
            "chief-memory",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
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
    assert '"instruction_memory"' in out
    assert '"path": "PACER.md"' in out


def test_chief_plan_includes_project_memory_in_worker_prompt(tmp_path, monkeypatch) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)
    (tmp_path / "PACER.md").write_text("Use focused tests before full pytest.\n", encoding="utf-8")
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
    assert "PACER.md" in plan.worker_tracks[0]["command"]
    usage = plan.project_memory["usage"]
    assert usage["retrieval_invoked"] is True
    assert usage["injected"] is True
    assert usage["injected_memory_ids"] == ["mission:mission-checkout"]
    assert usage["injected_instruction_paths"] == ["PACER.md"]
    assert usage["injected_chars"] > 0


def test_memory_v2_ranks_exact_paths_and_symbols_above_generic_preview(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="preview-workbench",
        objective="Preview the worker dashboard and mission queue UI",
        status="preview",
    )
    seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="verified-worker-lock",
        objective="Make Windows worker lock acquisition safe",
        changed_files=["src/visual_agent/chief_queue.py", "tests/test_chief_queue.py"],
        functions_touched=["_acquire_worker_lock", "_pid_is_alive"],
    )

    payload = build_project_memory(
        workspace_root=workspace.root,
        repo_root=tmp_path,
        goal="Fix _acquire_worker_lock in src/visual_agent/chief_queue.py without signalling the current PID",
    )

    assert payload["schema_version"] == 2
    assert payload["entries"][0]["mission_id"] == "verified-worker-lock"
    assert "exact_path" in payload["entries"][0]["match_reasons"]
    assert "exact_symbol" in payload["entries"][0]["match_reasons"]
    assert all(item["mission_id"] != "preview-workbench" for item in payload["entries"])
    assert payload["lookup"] == {
        "status": "succeeded",
        "hit": True,
        "lookup_hit": True,
        "candidate_count": 2,
    }
    assert payload["relevance"]["status"] == "estimated"
    assert payload["relevance"]["hit"] is True
    assert payload["relevance"]["relevant_hit"] is True
    assert payload["relevance"]["ranking"][0]["memory_id"] == "mission:verified-worker-lock"
    assert payload["relevance"]["ranking"][0]["selected"] is True


def test_memory_v2_separates_entry_cache_reuse_from_relevant_hit(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="verified-worker-lock",
        objective="Make Windows worker lock acquisition safe",
        changed_files=["src/visual_agent/chief_queue.py"],
        functions_touched=["_acquire_worker_lock"],
    )

    first = build_project_memory(
        workspace_root=workspace.root,
        repo_root=tmp_path,
        goal="Translate the lunar astronomy almanac into French",
    )
    second = build_project_memory(
        workspace_root=workspace.root,
        repo_root=tmp_path,
        goal="Translate the lunar astronomy almanac into French",
    )

    assert first["entry_cache"]["status"] == "cold"
    assert first["entry_cache"]["reused_entries"] == 0
    assert first["entry_cache"]["rebuilt_entries"] == 1
    assert first["lookup"]["hit"] is True
    assert first["lookup"]["lookup_hit"] is True
    assert first["lookup"]["candidate_count"] == 1
    assert first["relevance"]["hit"] is False
    assert first["relevance"]["relevant_hit"] is False
    assert first["relevance"]["eligible_count"] == 0
    assert first["relevance"]["returned_count"] == 0
    assert first["relevance"]["ranking"][0]["relevant"] is False
    assert first["entry_count"] == 0

    assert second["entry_cache"]["status"] == "warm"
    assert second["entry_cache"]["reused_entries"] == 1
    assert second["entry_cache"]["rebuilt_entries"] == 0
    assert second["index"]["hits"] == 1
    assert second["relevance"]["hit"] is False


def test_memory_v2_without_query_keeps_relevance_unjudged(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="verified-worker-lock",
        objective="Make Windows worker lock acquisition safe",
    )

    payload = build_project_memory(workspace_root=workspace.root, repo_root=tmp_path)

    assert payload["lookup"]["lookup_hit"] is True
    assert payload["relevance"]["status"] == "unjudged"
    assert payload["relevance"]["hit"] is None
    assert payload["relevance"]["relevant_hit"] is None
    assert payload["relevance"]["ranking"][0]["judgment"] == "unjudged"
    assert payload["relevance"]["ranking"][0]["relevant"] is None


def test_memory_v2_public_scorer_returns_rank_evidence_without_cache_semantics() -> None:
    entry = {
        "memory_id": "mission:verified-worker-lock",
        "mission_id": "verified-worker-lock",
        "objective": "Make Windows worker lock acquisition safe",
        "changed_files": ["src/visual_agent/chief_queue.py"],
        "symbols": ["_acquire_worker_lock"],
        "status": "verified",
        "acceptance_criteria": [],
        "evidence": {},
    }

    matched = score_memory_entry(
        "Fix _acquire_worker_lock in src/visual_agent/chief_queue.py",
        entry,
    )
    unrelated = score_memory_entry("Translate a lunar astronomy almanac", entry)
    unjudged = score_memory_entry("", entry)

    assert matched["relevant"] is True
    assert matched["score"] >= matched["threshold"]
    assert {"exact_path", "exact_symbol"} <= set(matched["match_reasons"])
    assert unrelated["relevant"] is False
    assert unrelated["score"] == 0
    assert unjudged["judgment"] == "unjudged"
    assert unjudged["relevant"] is None

    native = score_memory_entry(
        "Make Windows worker lock acquisition safe",
        {"batch_run_id": "native-1", "goal": "Make Windows worker lock acquisition safe"},
    )
    assert native["relevant"] is True
    assert "objective_phrase" in native["match_reasons"]


def test_memory_v2_recalls_a_short_chinese_domain_term() -> None:
    entry = {
        "memory_id": "memory-login",
        "objective": "继续登录开发并补充边界处理",
        "status": "completed",
    }

    matched = score_memory_entry("修复登录", entry)
    unrelated = score_memory_entry("更新支付结算", entry)

    assert matched["relevant"] is True
    assert matched["score"] >= matched["threshold"]
    assert unrelated["relevant"] is False


def test_memory_v2_prioritizes_an_explicit_mission_id(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="older-exact-memory",
        objective="Update a verified project record",
        changed_files=["docs/progress.md"],
    )
    seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="newer-generic-memory",
        objective="Update a verified project record",
        changed_files=["docs/progress.md"],
    )

    payload = build_project_memory(
        workspace_root=workspace.root,
        repo_root=tmp_path,
        goal="Reuse mission:older-exact-memory for this report",
    )

    assert payload["entries"][0]["mission_id"] == "older-exact-memory"
    assert "explicit_memory_id" in payload["entries"][0]["match_reasons"]


def test_memory_v2_keeps_instruction_and_episode_notes_under_budget(tmp_path) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)
    (tmp_path / "AGENTS.md").write_text("Run focused checks and inspect repository files freely.\n", encoding="utf-8")
    payload = build_project_memory(workspace_root=workspace.root, repo_root=tmp_path, goal="checkout total")

    notes = project_memory_handoff_notes(payload, max_items=3, max_chars=360)
    joined = " ".join(notes)

    assert len(joined) <= 360
    assert "AGENTS.md" in joined
    assert "mission-checkout" in joined
    assert payload["disclosure"]["advisory_only"] is True


def test_memory_v2_extracts_changed_files_symbols_and_verification(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="verified-worker-lock",
        objective="Make Windows worker lock acquisition safe",
        changed_files=["src/visual_agent/chief_queue.py"],
        functions_touched=["_acquire_worker_lock"],
    )

    payload = build_project_memory(workspace_root=workspace.root, repo_root=tmp_path, goal="chief_queue worker lock")
    entry = payload["entries"][0]

    assert entry["changed_files"] == ["src/visual_agent/chief_queue.py"]
    assert entry["symbols"] == ["_acquire_worker_lock"]
    assert entry["verification"]["command"] == "python -m pytest tests/test_chief_queue.py"
    assert entry["verification"]["verdict"] == "pass"
    assert "read-only Windows process check" in entry["worker_outcome"]["summary"]
    assert "dummy-secret-value" not in entry["worker_outcome"]["summary"]
    assert "[redacted]" in entry["worker_outcome"]["summary"]
    assert entry["source_paths"]


def test_memory_v2_reuses_and_invalidates_incremental_index(tmp_path) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    plan_id = seed_evidence_mission(
        tmp_path,
        workspace,
        mission_id="verified-worker-lock",
        objective="Make Windows worker lock acquisition safe",
        changed_files=["src/visual_agent/chief_queue.py"],
    )

    first = build_project_memory(workspace_root=workspace.root, repo_root=tmp_path, goal="worker lock")
    second = build_project_memory(workspace_root=workspace.root, repo_root=tmp_path, goal="worker lock")
    save_verification(workspace.root, plan_id, {"verdict": "fail"})
    third = build_project_memory(workspace_root=workspace.root, repo_root=tmp_path, goal="worker lock")

    assert first["index"]["misses"] == 1
    assert second["index"]["hits"] == 1
    assert third["index"]["misses"] == 1
    assert first["entry_cache"]["status"] == "cold"
    assert second["entry_cache"]["status"] == "warm"
    assert third["entry_cache"]["status"] == "cold"


def test_autonomous_pacer_program_injects_retrieved_memory_into_delegated_worker(tmp_path) -> None:
    workspace = seed_failed_checkout_mission(tmp_path)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    plan_file = tmp_path / "plan.md"
    plan_file.write_text("- [ ] Fix checkout total rounding\n", encoding="utf-8")
    program = create_program_from_plan(
        source_file=plan_file,
        workspace_root=workspace.root,
        repo_root=tmp_path,
        autonomous=True,
    )

    started = start_program(
        workspace_root=workspace.root,
        program_id=program["program_id"],
        hours=1,
    )

    created = started["created_missions"][0]
    mission = load_mission(workspace.root, created["mission_id"])
    chief_plan = load_plan(workspace.root, mission["plan_id"])
    worker_command = build_worker_command(
        plan=chief_plan,
        track=chief_plan["worker_tracks"][0],
        worktree=tmp_path,
        verification_command="python -m pytest",
        dispatch_mode="delegated",
    )
    usage = chief_plan["project_memory"]["usage"]
    prompt = worker_command["stdin"]

    assert mission["dispatch_mode"] == "delegated"
    assert usage["retrieval_invoked"] is True
    assert usage["injected_memory_ids"] == ["mission:mission-checkout"]
    assert usage["dispatch_injected"] is True
    assert usage["dispatch_memory_ids"] == ["mission:mission-checkout"]
    assert "mission:mission-checkout" in prompt
    assert started["queued_items"][0]["dispatch_mode"] == "delegated"
