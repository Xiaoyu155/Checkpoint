from __future__ import annotations

import json
import subprocess

from visual_agent.chief_engineer import assess_goal_clarity, build_chief_plan, chief_plan_to_markdown
from visual_agent.cli import main
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


def test_build_chief_plan_selects_affected_workflow(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    write_verification_workflow(workspace, "profile", affects="src/profile/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
        agents=("codex", "claude-code"),
    )

    assert plan.status == "ready"
    assert plan.changed_files == ["src/payment/checkout.py"]
    assert plan.selected_workflows == ["checkout"]
    assert plan.coverage["status"] == "covered"
    assert plan.permission_plan["decision"] == "allow"
    assert plan.worker_tracks[0]["agent"] == "codex"
    assert plan.worker_tracks[1]["agent"] == "claude-code"
    assert "python -m visual_agent.cli codex-check" in plan.verification_commands[0]
    assert "--workflow checkout" in plan.verification_commands[1]
    assert "Fix checkout total display" in plan.handoff_prompt
    assert "Permission plan: allow / low" in plan.handoff_prompt


def test_build_chief_plan_reports_coverage_gap(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "profile", affects="src/profile/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    assert plan.status == "needs_workflow_coverage"
    assert plan.selected_workflows == []
    assert plan.coverage["status"] == "uncovered"
    assert plan.coverage["suggested_new_workflows"][0]["suggested_name"] == "src_payment_verification"
    assert any(risk["area"] == "coverage" for risk in plan.risks)


def test_build_chief_plan_filters_checkpoint_runtime_artifacts(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "xiao"
    workspace = init_workspace(repo / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr(
        "visual_agent.chief_engineer.changed_files",
        lambda **_kwargs: [
            ".agent-workspace/runs/latest/report.json",
            "artifacts/pacer-dogfood/latest/report.json",
            "artifacts/random-dashboard/screenshot.png",
            "xiao/artifacts/pacer-dogfood/latest/report.json",
            "xiao/.agent-workspace/runs/latest/report.json",
            ".visual-agent-status.md",
            "强制测试记录.md",
            "src/payment/checkout.py",
        ],
    )

    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=repo,
    )

    assert plan.changed_files == ["src/payment/checkout.py"]
    assert plan.status == "ready"
    assert plan.selected_workflows == ["checkout"]


def test_build_chief_plan_filters_non_product_dirty_paths(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr(
        "visual_agent.chief_engineer.changed_files",
        lambda **_kwargs: [
            "_graveyard_2026_07/server_compose_current.yml",
            "_open_source_cases/remotion-tiktok",
            "repo.checkpoint-worktrees/old/track/src/payment/checkout.py",
            "src/payment/checkout.py",
        ],
    )

    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    assert plan.changed_files == ["src/payment/checkout.py"]
    assert plan.status == "ready"


def test_build_chief_plan_ignores_generated_gitignore_bootstrap(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=tmp_path, check=True, capture_output=True, text=True)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    (tmp_path / ".gitignore").write_text(
        "*.pyc\n# DevPacer / Checkpoint generated runtime files\n.agent-workspace/\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "payment.py").write_text("total = 1\n", encoding="utf-8")

    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    assert ".gitignore" not in plan.changed_files
    assert plan.changed_files == ["src/payment.py"]


def test_chief_plan_markdown_contains_handoff_and_risks(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: [])

    plan = build_chief_plan(goal="Improve dashboard empty state", workspace_root=workspace.root, repo_root=tmp_path)
    text = chief_plan_to_markdown(plan)

    assert "## Chief Engineer Plan" in text
    assert "Improve dashboard empty state" in text
    assert "Worker Handoff Prompt" in text
    assert "Permission decision" in text
    assert "No verification-tagged workflows" in text


def test_chief_plan_blocks_vague_goal_with_clarifying_questions(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(goal="改一下", workspace_root=workspace.root, repo_root=tmp_path)

    assert plan.status == "needs_clarification"
    assert plan.worker_tracks == []
    assert plan.handoff_prompt == ""
    assert plan.clarifying_questions
    assert any(risk["area"] == "requirements" for risk in plan.risks)


def test_assess_goal_clarity_requires_concrete_anchor() -> None:
    clarity = assess_goal_clarity("让我在不懂命令的情况下也能发起一次检查。")
    assert clarity["ok"] is False
    assert any("可验证结果" in q for q in clarity["questions"])


def test_chief_plan_answers_satisfy_clarity_gate(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="改一下",
        answers=("Change the home title from Hello to Welcome",),
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    assert plan.status != "needs_clarification"
    assert any("Clarified requirement" in item for item in plan.acceptance_criteria)


def test_chief_plan_infers_domain_acceptance_criteria(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="Fix checkout total display so the order total is correct",
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    joined = " ".join(plan.acceptance_criteria).lower()
    assert "order total" in joined or "totals" in joined
    assert "currency" in joined


def test_chief_plan_worker_tracks_use_capability_profiles(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    (workspace.root / "model_pool.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "local-economy",
                        "provider": "local",
                        "model": "economy",
                        "capability": 0.55,
                        "cost": 0.10,
                        "modes": ["cheap", "standard"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="Fix checkout total display discounted price",
        agents=("codex", "claude-code"),
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    codex_track = plan.worker_tracks[0]
    assert codex_track["model"] == "economy"
    assert codex_track["model_selection"]["status"] == "selected"
    assert codex_track["model_selection"]["selected"]["id"] == "local-economy"
    assert codex_track["routing_decision_id"]
    assert codex_track["selected_provider"] == "local"
    assert "--model economy" in codex_track["command"]
    assert "codex exec" in codex_track["command"]
    assert "--sandbox" in codex_track["command"]
    claude_track = plan.worker_tracks[1]
    assert claude_track["model"] == "sonnet"
    # This fixture's explicit pool only declares a Codex backend, so Claude
    # falls back to its own profile while the cross-backend pool is rejected.
    assert claude_track["model_selection"]["status"] == "blocked"
    assert "--model sonnet" in claude_track["command"]
    # Claude expresses sandbox + approval through one --permission-mode; not duplicated.
    assert claude_track["command"].count("--permission-mode") == 1


def test_chief_plan_defaults_to_one_coding_worker(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="Fix checkout total display",
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    # One coding worker by default, and it is Codex (runs on the user's
    # existing codex login/subscription).
    assert [track["agent"] for track in plan.worker_tracks] == ["codex"]


def test_chief_plan_marks_gemini_as_inspection_lane(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    plan = build_chief_plan(
        goal="Review checkout visual evidence for confusing total display",
        agents=("codex", "gemini"),
        workspace_root=workspace.root,
        repo_root=tmp_path,
    )

    gemini = plan.worker_tracks[1]
    assert gemini["agent"] == "gemini"
    assert gemini["track_kind"] == "inspection"
    assert gemini["workspace_strategy"] == "read-only evidence lane"
    assert "Do not edit code" in gemini["command"]


def test_chief_plan_cli_outputs_json(tmp_path, capsys, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_verification_workflow(workspace, "checkout", affects="src/payment/")
    monkeypatch.setattr("visual_agent.chief_engineer.changed_files", lambda **_kwargs: ["src/payment/checkout.py"])

    code = main(
        [
            "chief-plan",
            "--goal",
            "Fix checkout total display",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "ready"
    assert payload["selected_workflows"] == ["checkout"]
    assert payload["worker_tracks"][0]["workspace_strategy"] == "git worktree per agent before merging"


def test_assess_goal_clarity_passes_diagnosis_goals() -> None:
    # V7 dogfood finding (2026-07-05): the owner's first real goal was a
    # diagnosis question and the gate bounced it twice. A diagnosis goal's
    # definition of done is inherent — a root-cause report — so it must pass.
    from visual_agent.chief_engineer import assess_goal_clarity, is_diagnosis_goal

    goal = "帮我看一下这个项目，有一个每天自动推送公众号推文到草稿的脚本，为什么今天不给推送了"
    assert is_diagnosis_goal(goal)
    verdict = assess_goal_clarity(goal)
    assert verdict["ok"] is True
    assert verdict["signals"]["diagnosis"] is True
    assert verdict["questions"] == []

    assert is_diagnosis_goal("排查登录接口报错")
    assert is_diagnosis_goal("investigate why the nightly job stopped")
    assert not is_diagnosis_goal("把按钮改成蓝色")
