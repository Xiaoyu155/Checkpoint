from __future__ import annotations

import json
import subprocess
import socketserver
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest

from visual_agent.cloud_server import create_cloud_server
from visual_agent.cli import (
    expand_natural_language_task_argv,
    generate_from_diff_cli_markdown,
    load_inputs,
    main,
    run_workflow_with_progress,
    verify_impl_cli_markdown,
)
from visual_agent.codex_check import CodexCheckResult, CodexWorkflowCheck
from visual_agent.session import load_agent_session, record_cloud_run_usage, update_agent_session
from visual_agent.verification_status import enrich_verification_payload, write_verification_status
from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult
from visual_agent.models import ActionStatus
from visual_agent.workspace import init_workspace, load_workspace_inputs, run_workspace_workflow


def test_codex_check_cli_returns_zero_when_all_selected_workflows_pass(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    calls = []

    def fake_run_codex_check(*_args, **kwargs):
        calls.append(kwargs)
        return CodexCheckResult(
            changed_files=["src/payment/checkout.py"],
            selected_workflows=["checkout"],
            skipped_slow_workflows=[],
            results=[CodexWorkflowCheck(name="checkout", status="passed", step_count=1, elapsed_seconds=0.01)],
        )

    monkeypatch.setattr("visual_agent.cli.run_codex_check", fake_run_codex_check)

    code = main(["codex-check", "--workspace-root", str(workspace), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["selected_workflows"] == ["checkout"]
    assert payload["results"][0]["status"] == "passed"
    assert calls[0]["run_profile"] == "supervised"
    # machine-readable verdict must be present so AI callers cannot misread the result
    assert payload["verdict"] == "pass"
    assert payload["passed"] == 1
    assert payload["inspection_only"] == 0


def test_codex_check_strict_blocks_worker_prompt_alignment_regression(tmp_path, monkeypatch, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    monkeypatch.setattr(
        "visual_agent.cli.run_codex_check",
        lambda *_args, **_kwargs: CodexCheckResult(
            changed_files=["src/app.py"],
            selected_workflows=["app"],
            skipped_slow_workflows=[],
            results=[CodexWorkflowCheck(name="app", status="passed", step_count=1, elapsed_seconds=0.01)],
        ),
    )
    monkeypatch.setattr(
        "visual_agent.cli_verification.build_worker_prompt_alignment_check",
        lambda: {
            "status": "blocked",
            "issue_count": 1,
            "issues": [{"code": "repository_scan_ban", "message": "Do not restrict exploration."}],
        },
    )

    code = main(
        [
            "codex-check",
            "--workspace-root",
            str(workspace.root),
            "--strict",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["verdict"] == "pass"
    assert payload["execution_alignment"]["status"] == "blocked"
    assert payload["execution_alignment"]["issues"][0]["code"] == "repository_scan_ban"


def test_chief_dispatch_cli_defaults_to_two_repairs_and_keeps_once_alias(tmp_path, monkeypatch, capsys) -> None:
    calls = []

    def fake_dispatch(**kwargs):
        calls.append(kwargs)
        return {"status": "preview", "plan_id": kwargs["plan_id"]}

    monkeypatch.setattr("visual_agent.chief_dispatch.dispatch_chief_plan", fake_dispatch)
    base_args = [
        "chief-dispatch",
        "--workspace-root",
        str(tmp_path),
        "--plan",
        "plan-1",
        "--format",
        "json",
    ]

    assert main(base_args) == 0
    capsys.readouterr()
    assert calls[-1]["max_repair_rounds"] == 2

    assert main([*base_args, "--auto-repair-once"]) == 0
    capsys.readouterr()
    assert calls[-1]["max_repair_rounds"] == 1


def test_verify_now_cli_runs_default_verification_path(tmp_path, capsys, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow = workspace.workflows_dir / "checkout.yaml"
    workflow.write_text(
        """
schema_version: 1
name: checkout
version: 1
tags:
  - verification
steps:
  - id: observe
    action: observe_fixture
    path: missing.json
""".strip(),
        encoding="utf-8",
    )
    calls = []

    def fake_run_workspace_workflow(*_args, **kwargs):
        calls.append(kwargs)

        class Result:
            run_id = "run"
            steps = (
                WorkflowStepResult(id="open", action="observe_browser", status=ActionStatus.SUCCESS),
                WorkflowStepResult(id="submit", action="click", status=ActionStatus.SUCCESS),
                WorkflowStepResult(id="confirm", action="assert_text", status=ActionStatus.SUCCESS),
            )
            acceptance = {"label": "L3", "level": 3, "is_product_acceptance": True}

        return Result()

    monkeypatch.setattr("visual_agent.verify.run_workspace_workflow", fake_run_workspace_workflow)

    code = main(["verify-now", "--workspace-root", str(workspace.root)])
    output = capsys.readouterr().out

    assert code == 0
    assert "Verification Report" in output
    assert "Strict product acceptance (L3+ without blockers): 1/1" in output
    assert calls[0]["run_profile"] == "supervised"
    assert calls[0]["queue_when_locked"] is True


def test_load_inputs_file_accepts_utf8_bom(tmp_path) -> None:
    inputs_file = tmp_path / "inputs.json"
    inputs_file.write_text('\ufeff{"username": "demo_user"}', encoding="utf-8")

    assert load_inputs(None, str(inputs_file)) == {"username": "demo_user"}


def test_version_flag_prints_runtime_info(capsys) -> None:
    code = main(["--version"])
    output = capsys.readouterr().out

    assert code == 0
    assert "visual-agent 0.1.2" in output
    assert "Product: Pacer" in output
    assert "Verification engine: Checkpoint" in output
    assert "Python:" in output
    assert "Playwright:" in output
    assert "System:" in output


def test_checkpoint_entrypoint_prints_brand_aligned_version(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["checkpoint", "--version"])

    code = main()
    output = capsys.readouterr().out

    assert code == 0
    assert "checkpoint 0.1.2" in output
    assert "Product: Pacer" in output
    assert "Verification engine: Checkpoint" in output
    assert "Package: visual-agent" in output


def test_empty_cli_prints_concise_getting_started(capsys) -> None:
    code = main([])
    output = capsys.readouterr().out

    assert code == 0
    assert "Pacer" in output
    assert "checkpoint host doctor" in output
    assert "checkpoint host run" in output
    assert "checkpoint --help" in output


def test_pacer_entrypoint_delegates_all_arguments_to_codex(monkeypatch) -> None:
    from visual_agent import codex_launcher

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "resume", "--last"])
    monkeypatch.setattr(codex_launcher, "launch_codex", lambda argv: calls.append(argv) or 37)

    assert main() == 37
    assert calls == [["resume", "--last"]]


def test_pacer_code_alias_delegates_to_codex(monkeypatch) -> None:
    from visual_agent import codex_launcher

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "code", "--sandbox", "danger-full-access"])
    monkeypatch.setattr(codex_launcher, "launch_codex", lambda argv: calls.append(argv) or 37)

    assert main() == 37
    assert calls == [["--sandbox", "danger-full-access"]]


def test_pacer_cc_alias_delegates_to_claude(monkeypatch) -> None:
    import visual_agent.cli as cli
    import visual_agent.process_guard as _pg

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "cc", "--dangerously-skip-permissions"])
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\Tools\claude.exe" if name == "claude" else None)

    # guarded_run uses Popen when the Job Object guard is available (Windows).
    # Patch at the process_guard level so the mock intercepts both paths.
    class _FakePopen:
        def __init__(self, argv, **kw):
            calls.append(list(argv))
            self.pid = 12345
            self.args = argv
            self.returncode = 41
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def communicate(self, input=None, timeout=None): return None, None
        def kill(self): pass
        def wait(self): pass

    monkeypatch.setattr(_pg.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(_pg.subprocess, "run", lambda argv, **kw: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 41))

    assert main() == 41
    assert calls == [[r"C:\Tools\claude.exe", "--permission-mode", "bypassPermissions"]]


def test_pacer_yolo_alias_forces_claude_bypass_permissions(monkeypatch) -> None:
    import visual_agent.cli as cli
    import visual_agent.process_guard as _pg

    calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            r"C:\Python\Scripts\pacer.exe",
            "claude-yolo",
            "--permission-mode",
            "manual",
            "--dangerously-skip-permissions",
            "fix bug",
        ],
    )
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\Tools\claude.exe" if name == "claude" else None)

    class _FakePopen:
        def __init__(self, argv, **kw):
            calls.append(list(argv))
            self.pid = 12345
            self.args = argv
            self.returncode = 43
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def communicate(self, input=None, timeout=None): return None, None
        def kill(self): pass
        def wait(self): pass

    monkeypatch.setattr(_pg.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(_pg.subprocess, "run", lambda argv, **kw: calls.append(list(argv)) or subprocess.CompletedProcess(argv, 43))

    assert main() == 43
    assert calls == [[r"C:\Tools\claude.exe", "--permission-mode", "bypassPermissions", "fix bug"]]


def test_pacer_gm_alias_delegates_to_gemini(monkeypatch) -> None:
    import visual_agent.cli as cli

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "gm", "--yolo"])
    monkeypatch.setattr(cli.shutil, "which", lambda name: r"C:\Tools\gemini.exe" if name == "gemini" else None)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 42),
    )

    assert main() == 42
    assert calls == [[r"C:\Tools\gemini.exe", "--yolo"]]


def test_pacer_entrypoint_intercepts_management_commands(monkeypatch) -> None:
    from visual_agent import pacer_management

    calls = []
    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\pacer.exe", "status", "--json"])
    monkeypatch.setattr(pacer_management, "handle_pacer_management", lambda argv: calls.append(argv) or 19)

    assert main() == 19
    assert calls == [["status", "--json"]]


def test_pacer_management_routes_host_to_pacer_cli(monkeypatch) -> None:
    from visual_agent import cli, pacer_management

    calls = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 23)

    assert pacer_management.handle_pacer_management(["host", "status"]) == 23
    assert calls == [["host", "status"]]


def test_pacer_management_routes_chief_commands_to_pacer_cli(monkeypatch) -> None:
    from visual_agent import cli, pacer_management

    calls = []
    monkeypatch.setattr(cli, "main", lambda argv: calls.append(argv) or 23)

    assert pacer_management.handle_pacer_management(["chief-status", "--mission", "m1"]) == 23
    assert calls == [["chief-status", "--mission", "m1"]]


def test_natural_language_task_expands_to_managed_golden_path() -> None:
    expanded, simple = expand_natural_language_task_argv(
        ["修复登录错误并运行测试"],
        ["mission", "dashboard", "init"],
    )

    assert simple is True
    assert expanded[:4] == ["mission", "start", "--goal", "修复登录错误并运行测试"]
    assert "--execute" in expanded
    assert "--merge" in expanded
    assert "--allow-dirty" in expanded
    assert "--dispatch-mode" in expanded
    assert expanded[expanded.index("--dispatch-mode") + 1] == "delegated"
    assert expanded[expanded.index("--max-rounds") + 1] == "8"


def test_existing_subcommand_is_not_rewritten_as_natural_language() -> None:
    argv = ["mission", "list"]

    assert expand_natural_language_task_argv(argv, ["mission", "dashboard"]) == (argv, False)


def test_quickstart_cli_prints_same_getting_started(capsys) -> None:
    code = main(["quickstart"])
    output = capsys.readouterr().out

    assert code == 0
    assert "checkpoint host doctor" in output
    assert "checkpoint host run" in output
    assert "mission start" in output


def test_model_select_cli_uses_configured_pool(tmp_path, capsys) -> None:
    pool = tmp_path / "model_pool.json"
    pool.write_text(
        json.dumps(
            {
                "models": [
                    {"id": "cheap", "provider": "x", "model": "mini", "capability": 0.4, "cost": 0.05, "modes": ["cheap", "standard"]},
                    {"id": "strong", "provider": "x", "model": "max", "capability": 0.9, "cost": 0.9, "modes": ["strong"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    code = main(["model-select", "--goal", "fix typo in title", "--model-pool", str(pool), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["selected"]["id"] == "cheap"


def test_hourly_plan_cli_reads_tasks_file(tmp_path, capsys) -> None:
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps({"tasks": [{"task_id": "t1", "objective": "Update docs", "worker_tier": "cheap"}]}), encoding="utf-8")

    code = main(["hourly-plan", "--tasks-file", str(tasks), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["scheduled"][0]["task_id"] == "t1"


def test_notify_cli_builds_dry_run(tmp_path, capsys) -> None:
    cfg = tmp_path / "notify.json"
    cfg.write_text(
        json.dumps(
            {
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "username": "sender@example.com",
                "password": "secret",
                "sender": "sender@example.com",
                "recipient": "user@example.com",
            }
        ),
        encoding="utf-8",
    )

    code = main(["notify", "--event", "quota_warning", "--payload", '{"quota":"5h 91%"}', "--config", str(cfg), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "planned"
    assert payload["to"] == "user@example.com"


def test_portfolio_dashboard_cli_invokes_server(tmp_path, monkeypatch) -> None:
    captured = {}

    def fake_serve(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("visual_agent.portfolio_dashboard.serve_portfolio_dashboard", fake_serve)

    code = main([
        "portfolio-dashboard",
        "--project",
        str(tmp_path / "a"),
        "--project",
        str(tmp_path / "b"),
        "--port",
        "9999",
        "--no-open",
    ])

    assert code == 0
    assert captured["project_roots"] == [str(tmp_path / "a"), str(tmp_path / "b")]
    assert captured["port"] == 9999
    assert captured["open_browser"] is False


def test_dashboard_cli_rejects_non_loopback_host(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["dashboard", "--host", "0.0.0.0", "--no-open"])

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_codex_check_cli_returns_one_when_any_workflow_fails(tmp_path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def fake_run_codex_check(*_args, **_kwargs):
        return CodexCheckResult(
            changed_files=["src/payment/checkout.py"],
            selected_workflows=["checkout"],
            skipped_slow_workflows=["visual_checkout"],
            results=[
                CodexWorkflowCheck(
                    name="checkout",
                    status="failed",
                    step_count=2,
                    elapsed_seconds=0.01,
                    failed_step="assert_total",
                    message="Text not found",
                )
            ],
        )

    monkeypatch.setattr("visual_agent.cli.run_codex_check", fake_run_codex_check)

    code = main(["codex-check", "--workspace-root", str(workspace), "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 1
    assert "FAILED at 'assert_total'" in output
    assert "Skipping slow workflows: visual_checkout" in output


def test_share_workflow_cli_marks_local_index_public(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    code = main(["share-workflow", "--workspace-root", str(workspace.root), "--name", "local_html_form_workflow"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["workflow"] == "local_html_form_workflow"
    assert payload["visibility"] == "public"
    assert payload["license"] == "cc-by-4.0"
    assert "local workflow library" in payload["message"]


def test_publish_workflow_cli_validates_quality_and_license(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "public_profile.yaml"
    workflow_path.write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: public_profile
version: 1
description: Public profile save flow
tags: [verification, profile]
visibility: public
author: visual-agent-team
license: cc-by-4.0
steps:
  - id: observe
    action: observe_html
    path: fixtures/profile.html
  - id: wait_ready
    action: wait_for
    condition: text
    text: Ready
  - id: assert_ready
    action: assert_text
    text: Ready
  - id: assert_no_error
    action: assert_no_error
""".strip(),
        encoding="utf-8",
    )

    code = main(["publish-workflow", "--workspace-root", str(workspace.root), "--name", "public_profile", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "published"
    assert payload["name"] == "public_profile"
    assert payload["quality_score"] >= 60
    assert payload["url"].endswith("/public_profile")
    assert (workspace.root / "workflow_index.json").exists()


def test_list_and_search_workflows_cli_use_index(tmp_path, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    main(["share-workflow", "--workspace-root", str(workspace.root), "--name", "local_html_form_workflow"])
    capsys.readouterr()

    code = main(["list-workflows", "--workspace-root", str(workspace.root), "--visibility", "public", "--format", "json"])
    listed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert listed["workflow_count"] == 1
    assert listed["workflows"][0]["name"] == "local_html_form_workflow"

    code = main(["search-workflows", "html form", "--workspace-root", str(workspace.root), "--format", "json"])
    searched = json.loads(capsys.readouterr().out)
    assert code == 0
    assert any(item["name"] == "local_html_form_workflow" for item in searched["workflows"])


def test_generate_from_diff_cli_dry_run_outputs_context_workflow(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = tmp_path / "src" / "login.html"
    page.parent.mkdir()
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", "src/login.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>\n",
        encoding="utf-8",
    )

    code = main(
        [
            "generate-from-diff",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--task-description",
            "Verify login redirects",
            "--base-url",
            "fixtures/login.html",
            "--dry-run",
            "--no-untracked",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["changed_files"] == ["src/login.html"]
    assert payload["quality"]["score"] >= 0.6
    assert payload["quality"]["data_display_assertions"] == 0
    assert payload["quality"]["forbidden_error_assertions"] == 0
    assert payload["quality"]["invalid_text_from_references"] == []
    assert payload["semantic_summary"]["framework"] == "html"
    assert payload["semantic_summary"]["field_count"] == 1
    assert payload["semantic_summary"]["required_field_count"] == 1
    assert payload["semantic_summary"]["validation_rule_count"] == 2
    assert payload["semantic_summary"]["data_display_count"] == 0
    assert payload["semantic_summary"]["data_displays"] == []
    assert payload["semantic_summary"]["matched_data_displays"] == []
    assert payload["semantic_summary"]["unmatched_data_displays"] == []
    assert payload["semantic_summary"]["negative_input_case_count"] == 2
    assert len(payload["negative_input_cases"]) == 2
    assert payload["negative_workflow_ready"] is False
    assert payload["negative_workflow_reason"] == "no_negative_oracle"
    assert payload["negative_workflow_reset_strategy"] == "fresh_observe_per_case"
    assert payload["negative_oracles"] == []
    assert len(payload["generation_trace"]) <= 10
    assert "field email -> paste input.email" in payload["generation_trace"]
    assert payload["semantic_summary"]["success_state_count"] >= 1
    assert "url_contains: /dashboard" in payload["yaml"]


def test_generate_from_diff_cli_appends_audit_log(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = tmp_path / "src" / "login.html"
    page.parent.mkdir()
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", "src/login.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>\n",
        encoding="utf-8",
    )
    audit_log = workspace.root / "audit" / "context_parse.jsonl"

    for _ in range(2):
        code = main(
            [
                "generate-from-diff",
                "--workspace-root",
                str(workspace.root),
                "--repo-root",
                str(tmp_path),
                "--task-description",
                "Verify login redirects",
                "--base-url",
                "fixtures/login.html",
                "--dry-run",
                "--no-untracked",
                "--audit-log",
                str(audit_log),
            ]
        )
        json.loads(capsys.readouterr().out)
        assert code == 0

    entries = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]

    assert len(entries) == 2
    assert entries[0]["task"] == "Verify login redirects"
    assert entries[0]["framework"] == "html"
    assert entries[0]["confidence"] >= 0.5
    assert entries[0]["method"]
    assert entries[0]["fields"] == ["email"]
    assert entries[0]["submit_actions"] == ["Sign in"]
    assert entries[0]["success_states"]
    assert entries[0]["unmatched_data_displays"] == []
    assert isinstance(entries[0]["warnings"], list)
    assert entries[0]["quality_score"] >= 0.6


def test_init_workspace_auto_detect_nextjs(tmp_path: Path, capsys) -> None:
    (tmp_path / "package.json").write_text('{"dependencies":{"next":"13.0.0","react":"18.0.0"}}', encoding="utf-8")
    workspace_root = tmp_path / ".agent-workspace"

    code = main(
        [
            "init",
            "--root",
            str(workspace_root),
            "--auto-detect",
            "--repo-root",
            str(tmp_path),
            "--no-demo",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["framework_hint"] == "nextjs"
    assert (workspace_root / "fixtures" / "nextjs_demo.html").exists()
    assert (workspace_root / "workflows" / "nextjs_verification.yaml").exists()
    assert payload["next_steps"][0].startswith("visual-agent show-status --workspace-root")


def test_init_short_alias_initializes_workspace(tmp_path: Path, capsys) -> None:
    workspace_root = tmp_path / "project with space" / ".agent-workspace"

    code = main(
        [
            "init",
            "--root",
            str(workspace_root),
            "--no-demo",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["root"] == str(workspace_root)
    quoted_root = f'"{workspace_root}"'
    assert payload["next_steps"][:3] == [
        f"visual-agent show-status --workspace-root {quoted_root}",
        f"visual-agent verify-impl --workspace-root {quoted_root} --task-description \"Verify the current change\" --run-profile dry-run",
        f"visual-agent workspace-status --root {quoted_root}",
    ]


def test_init_short_alias_uses_default_root(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(["init", "--no-demo"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["root"] == str((tmp_path / ".agent-workspace").resolve())


def test_workspace_run_outputs_markdown_when_requested(tmp_path: Path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace")

    code = main(
        [
            "workspace-run",
            "--root",
            str(workspace.root),
            "--workflow",
            "local_html_form_workflow",
            "--inputs-file",
            "demo_login.json",
            "--run-profile",
            "dry-run",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "# Run Report: local_html_form_workflow" in output
    assert "- Status: `success`" in output


def test_model_credentials_inspect_suggests_anthropic_fallback(tmp_path: Path, capsys) -> None:
    source = tmp_path / "model_api_keys.txt"
    source.write_text("anthropic api_key=sk-ant-test12345678901234567890\n", encoding="utf-8")

    code = main(["model-credentials-inspect", "--source", str(source), "--preferred", "openai", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["preferred_available"] is False
    assert "Anthropic key detected" in payload["suggestion"]


def test_generate_completions_writes_bash_and_zsh_scripts(tmp_path: Path, capsys) -> None:
    code = main(["generate-completions", "--output-dir", str(tmp_path)])
    payload = json.loads(capsys.readouterr().out)

    bash_path = tmp_path / "_visual_agent_completion.sh"
    zsh_path = tmp_path / "_visual_agent_completion.zsh"

    assert code == 0
    assert payload["status"] == "success"
    assert Path(payload["scripts"]["bash"]) == bash_path.resolve()
    assert Path(payload["scripts"]["zsh"]) == zsh_path.resolve()
    assert bash_path.exists()
    assert zsh_path.exists()
    assert "visual-agent" in bash_path.read_text(encoding="utf-8")
    assert "generate-workflow" in bash_path.read_text(encoding="utf-8")
    assert "compdef _visual_agent_complete visual-agent" in zsh_path.read_text(encoding="utf-8")


def test_run_workflow_progress_reporting_writes_heartbeat(capsys) -> None:
    class SlowRuntime:
        def run(self, workflow, *, progress_state=None, **_kwargs):
            progress_state["workflow_name"] = workflow.name
            progress_state["stage"] = "running"
            progress_state["current_step"] = "login_step (click)"
            progress_state["current_index"] = 0
            progress_state["total_steps"] = 1
            progress_state["message"] = "working"
            time.sleep(0.03)
            progress_state["stage"] = "finished"
            return {"status": "success"}

    workflow = SimpleNamespace(name="demo_workflow", steps=[SimpleNamespace(id="login_step", action="click")])
    result = run_workflow_with_progress(SlowRuntime(), workflow, progress_interval_seconds=0.01)
    stderr = capsys.readouterr().err

    assert result["status"] == "success"
    assert "Step 1/1: login_step (click) [running] working" in stderr


def test_generate_from_diff_markdown_prints_warnings() -> None:
    output = generate_from_diff_cli_markdown(
        {
            "status": "success",
            "workflow_path": ".agent-workspace/workflows/verify_profile.yaml",
            "generation_method": "static",
            "quality": {"score": 0.71},
            "semantic_summary": {
                "framework": "nextjs",
                "confidence": 0.82,
                "field_count": 2,
                "required_field_count": 1,
                "success_state_count": 1,
                "data_display_count": 1,
                "warnings": ["Unrecognized field: <DatePicker name=\"birthdate\">"],
            },
        }
    )

    assert "[generate-from-diff] Framework: nextjs" in output
    assert "Parse warnings (1):" in output
    assert "DatePicker" in output


def test_workflow_lint_flags_low_quality_workflow(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "weak.yaml"
    workflow.write_text(
        """
schema_version: 1
name: weak
version: 1
steps:
  - id: observe
    action: observe_html
    path: page.html
""".strip(),
        encoding="utf-8",
    )

    code = main(["workflow-lint", str(workflow), "--min-quality-score", "0.95"])
    output = capsys.readouterr().out

    assert code == 1
    assert "Workflow: weak" in output
    assert "Quality score:" in output
    assert "no success state assertion" in output
    assert "wait_for_text" in output


def test_workflow_lint_json_passes_strong_workflow(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "strong.yaml"
    workflow.write_text(
        """
schema_version: 1
name: strong
version: 1
steps:
  - id: observe
    action: observe_html
    path: page.html
  - id: assert_success
    action: assert_text
    text: Saved successfully
  - id: assert_no_error
    action: assert_text_contract
    forbidden_any:
      - Error
      - Failed
""".strip(),
        encoding="utf-8",
    )

    code = main(["workflow-lint", "--file", str(workflow), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["workflow_name"] == "strong"
    assert payload["quality"]["score"] >= 0.6
    assert payload["quality"]["covers_success_path"] is True
    assert payload["quality"]["covers_error_path"] is True


def test_workflow_lint_flags_visual_workflow_without_visual_assertion(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "desktop.yaml"
    workflow.write_text(
        """
schema_version: 1
name: desktop
version: 1
steps:
  - id: observe
    action: observe_screen
  - id: click_login
    action: click_visual
    description: Login button
  - id: verify
    action: assert_text
    text: Dashboard
""".strip(),
        encoding="utf-8",
    )

    code = main(["workflow-lint", str(workflow), "--min-quality-score", "0.95"])
    output = capsys.readouterr().out

    assert code == 1
    assert "visual workflow has no visual assertion" in output
    assert "assert_visual_text" in output


def test_workflow_add_step_inserts_wait_for_text_after_step(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "profile.yaml"
    workflow.write_text(
        """
schema_version: 1
name: profile
version: 1
steps:
  - id: observe
    action: observe_html
    path: page.html
  - id: click_submit
    action: click
    target:
      text: Save
""".strip(),
        encoding="utf-8",
    )

    code = main(
        [
            "workflow-add-step",
            "--workflow",
            str(workflow),
            "--after",
            "click_submit",
            "--action",
            "wait_for_text",
            "--text",
            "Saved successfully",
            "--timeout-ms",
            "10000",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    text = workflow.read_text(encoding="utf-8")

    assert code == 0
    assert payload["status"] == "updated"
    assert payload["step"]["id"] == "wait_for_text"
    assert payload["step"]["text"] == "Saved successfully"
    assert payload["step"]["timeout_ms"] == 10000
    assert "id: wait_for_text" in text
    assert text.index("id: click_submit") < text.index("id: wait_for_text")


def test_workflow_add_step_dry_run_does_not_write(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "profile.yaml"
    original = """
schema_version: 1
name: profile
version: 1
steps:
  - id: observe
    action: observe_html
    path: page.html
""".strip()
    workflow.write_text(original, encoding="utf-8")

    code = main(
        [
            "workflow-add-step",
            "--workflow",
            str(workflow),
            "--after",
            "observe",
            "--action",
            "assert_text",
            "--text",
            "Ready",
            "--dry-run",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "preview"
    assert "id: assert_text" in payload["yaml"]
    assert workflow.read_text(encoding="utf-8") == original


def test_workflow_add_step_missing_after_returns_error(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "profile.yaml"
    workflow.write_text(
        "schema_version: 1\nname: profile\nversion: 1\nsteps:\n  - id: observe\n    action: observe_html\n    path: page.html\n",
        encoding="utf-8",
    )

    code = main(
        [
            "workflow-add-step",
            "--workflow",
            str(workflow),
            "--after",
            "missing",
            "--action",
            "assert_text",
            "--text",
            "Ready",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["status"] == "error"
    assert "missing" in payload["message"]


def test_generate_workflow_missing_description_shows_try_hint(capsys) -> None:
    code = main(["generate-workflow", "--format", "json"])
    stderr = capsys.readouterr().err

    assert code == 1
    assert "Try:" in stderr
    assert "visual-agent generate-workflow --description" in stderr


def test_run_workflow_bad_inputs_file_shows_try_hint(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "workflow.yaml"
    inputs = tmp_path / "inputs.json"
    workflow.write_text(
        "schema_version: 1\nname: profile\nversion: 1\nsteps:\n  - id: observe\n    action: observe_html\n    path: page.html\n",
        encoding="utf-8",
    )
    inputs.write_text("{not json}", encoding="utf-8")

    code = main(["run-workflow", "--file", str(workflow), "--inputs-file", str(inputs)])
    stderr = capsys.readouterr().err

    assert code == 1
    assert "Try:" in stderr
    assert "run-workflow" in stderr


def test_run_workflow_accepts_workflow_argument(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(
        "schema_version: 1\nname: profile\nversion: 1\nsteps:\n  - id: observe\n    action: observe_ocr\n    mock_text: Ready\n  - id: assert_ready\n    action: assert_text\n    text: Ready\n",
        encoding="utf-8",
    )

    code = main(["run-workflow", "--workflow", str(workflow), "--run-profile", "dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["workflow_name"] == "profile"


def test_context_snapshot_json_has_stable_entry_fields(tmp_path: Path, capsys) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    workspace_root.mkdir()

    code = main(["context-snapshot", "--workspace-root", str(workspace_root), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert tuple(payload.keys()) == ("schema_version", "workspace", "format", "snapshot", "token_estimate", "within_budget")
    assert payload["schema_version"] == 1
    assert payload["workspace"] == str(workspace_root.resolve())


def test_verify_impl_cli_dry_run_writes_status(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = tmp_path / "src" / "simple_form.html"
    page.parent.mkdir()
    fixture = workspace.fixtures_dir / "simple_form.html"
    initial_html = "<form><input name='email'></form>\n"
    page.write_text(initial_html, encoding="utf-8")
    fixture.write_text(initial_html, encoding="utf-8")
    git(tmp_path, "add", "src/simple_form.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    final_html = (
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>\n"
    )
    page.write_text(final_html, encoding="utf-8")
    fixture.write_text(final_html, encoding="utf-8")
    code = main(
        [
            "verify-impl",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--task-description",
            "Verify simple form submits",
            "--base-url",
            "fixtures/simple_form.html",
            "--run-profile",
            "dry-run",
            "--min-quality-score",
            "0",
            "--no-untracked",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["result"] == "pass"
    assert payload["inputs_source"] == "generated_template"
    assert payload["inputs_path"]
    assert payload["semantic_summary"]["framework"] == "html"
    assert payload["semantic_summary"]["field_count"] == 1
    assert (workspace.root / ".vscode-agent-status.json").exists()


def test_verify_impl_cli_infers_fixture_base_url(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = tmp_path / "src" / "simple_form.html"
    page.parent.mkdir()
    fixture = workspace.fixtures_dir / "simple_form.html"
    initial_html = "<form><input name='email'></form>\n"
    page.write_text(initial_html, encoding="utf-8")
    fixture.write_text(initial_html, encoding="utf-8")
    git(tmp_path, "add", "src/simple_form.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    final_html = (
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>\n"
    )
    page.write_text(final_html, encoding="utf-8")
    fixture.write_text(final_html, encoding="utf-8")

    code = main(
        [
            "verify-impl",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--task-description",
            "Verify simple form submits",
            "--run-profile",
            "dry-run",
            "--min-quality-score",
            "0",
            "--no-untracked",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["result"] == "pass"
    assert payload["base_url"] == "fixtures/simple_form.html"


def test_verify_impl_cli_can_run_negative_workflow_when_requested(tmp_path: Path, capsys) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = tmp_path / "src" / "simple_form.html"
    page.parent.mkdir()
    fixture = workspace.fixtures_dir / "simple_form.html"
    initial_html = "<form><input name='email'></form>\n"
    page.write_text(initial_html, encoding="utf-8")
    fixture.write_text(initial_html, encoding="utf-8")
    git(tmp_path, "add", "src/simple_form.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    final_html = (
        "<form><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>\n"
    )
    page.write_text(final_html, encoding="utf-8")
    fixture.write_text(final_html, encoding="utf-8")

    code = main(
        [
            "verify-impl",
            "--workspace-root",
            str(workspace.root),
            "--repo-root",
            str(tmp_path),
            "--task-description",
            "Verify simple form submits",
            "--base-url",
            "fixtures/simple_form.html",
            "--run-profile",
            "dry-run",
            "--min-quality-score",
            "0",
            "--run-negative",
            "--no-untracked",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["result"] == "pass"
    assert payload["negative_verification"]["requested"] is True
    assert payload["negative_verification"]["status"] == "skipped"
    assert payload["negative_verification"]["reason"] == "no_negative_oracle"
    assert payload["negative_verification"]["workflow_path"].endswith("_negative_draft.yaml")


def test_verify_impl_cli_markdown_includes_inputs_source() -> None:
    output = verify_impl_cli_markdown(
        {
            "result": "pass",
            "workflow_name": "login_verification",
            "quality_score": 0.9,
            "inputs_path": "inputs/login_verification_inputs.json",
            "inputs_source": "generated_template",
            "generation_trace": ["field email -> paste input.email"],
            "negative_verification": {
                "status": "skipped",
                "reason": "no_negative_oracle",
                "workflow_name": "login_verification_negative_draft",
                "reset_strategy": "fresh_observe_per_case",
                "oracles": [{"text": "Invalid input", "source": "html:text"}],
                "next_action": "Add or expose parsed validation error text before treating negative verification as executable.",
            },
            "message": "All steps passed.",
        }
    )

    assert "[verify-impl] Inputs: inputs/login_verification_inputs.json" in output
    assert "[verify-impl] Inputs source: generated_template" in output
    assert "[verify-impl] Generation trace: field email -> paste input.email" in output
    assert "[verify-impl] Negative: skipped workflow=login_verification_negative_draft" in output
    assert "[verify-impl] Negative reason: no_negative_oracle" in output
    assert "[verify-impl] Negative reset: fresh_observe_per_case" in output
    assert "[verify-impl] Negative oracles: 1" in output
    assert "[verify-impl] Negative next: Add or expose parsed validation error text" in output


def test_agent_status_cli_reads_status_file_as_markdown_and_json(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / ".agent-workspace"
    workspace.mkdir()
    payload = enrich_verification_payload(
        {
            "result": "pass",
            "workflow_name": "profile_verification",
            "workflow_path": str(workspace / "workflows" / "profile.yaml"),
            "run_id": "run-123",
            "quality_score": 0.91,
            "quality": {
                "score": 0.91,
                "data_display_assertions": 1,
                "forbidden_error_assertions": 1,
                "text_from_input_references": 1,
                "invalid_text_from_references": [],
                "gaps": [],
                "recommendation": "Workflow quality is good.",
            },
            "semantic_summary": {
                "framework": "nextjs",
                "confidence": 0.82,
                "generation_method": "static",
                "field_count": 1,
                "required_field_count": 1,
                "validation_rule_count": 2,
                "success_state_count": 1,
                "data_display_count": 1,
                "negative_input_case_count": 2,
                "data_displays": ["profile.displayName"],
                "matched_data_displays": ["profile.displayName"],
                "unmatched_data_displays": [],
                "warnings": [],
            },
            "inputs_path": str(workspace / "inputs" / "profile_inputs.json"),
            "inputs_source": "generated_template",
            "generation_trace": ["display displayName -> assert_text text_from input.displayName"],
            "negative_verification": {
                "requested": True,
                "status": "skipped",
                "reason": "no_negative_oracle",
                "reset_strategy": "fresh_observe_per_case",
                "oracles": [],
                "next_action": "Add parsed validation error text before enabling negative verification.",
            },
            "message": "ok",
        },
        workspace_root=workspace,
    )
    write_verification_status(workspace, payload)

    markdown_code = main(["agent-status", "--workspace-root", str(workspace), "--format", "markdown"])
    markdown = capsys.readouterr().out
    json_code = main(["agent-status", "--workspace-root", str(workspace), "--format", "json"])
    json_payload = json.loads(capsys.readouterr().out)

    assert markdown_code == 0
    assert "Result: pass" in markdown
    assert "Report Hint: Use get_run_report with run_id='run-123'" in markdown
    assert "Negative Verification:" in markdown
    assert "- status: skipped" in markdown
    assert "- matched display: profile.displayName" in markdown
    assert "display displayName -> assert_text text_from input.displayName" in markdown
    assert json_code == 0
    assert json_payload["result"] == "pass"
    assert json_payload["report_hint"].startswith("Use get_run_report")
    assert json_payload["negative_verification"]["status"] == "skipped"


def test_agent_status_cli_reports_missing_status(tmp_path: Path, capsys) -> None:
    code = main(["agent-status", "--workspace-root", str(tmp_path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["status"] == "missing"


def test_usage_status_cli_reports_usage_and_license_without_secret(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    update_agent_session(workspace, cli_run_result("checkout"))
    record_cloud_run_usage(workspace, count=2)
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_TIER", "pro")
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_KEY", "va_secret_key_value")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ORG", "team-a")

    code = main(["usage-status", "--workspace-root", str(workspace), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["license"]["tier"] == "pro"
    assert payload["license"]["key_present"] is True
    assert payload["usage"]["runs_this_month"] == 1
    assert payload["usage"]["cloud_runs_used"] == 2
    assert payload["usage"]["cloud_run_quota"]["limit"] is None
    assert payload["usage"]["cloud_run_quota"]["remaining"] is None
    assert payload["feature_access"]["cloud_run"] is True
    assert payload["cloud_config"]["available"] is True
    assert payload["cloud_config"]["api_key_present"] is True
    assert payload["cloud_config"]["endpoint"] == "https://cloud.visualagent.test"
    assert payload["remote_request_preview"]["status"] == "ready"
    assert payload["remote_request_preview"]["workflow_name"] == "example_workflow"
    assert payload["remote_request_preview"]["inputs"]["provided"] is False
    assert payload["remote_request_preview"]["network_probe"] == "not_run"
    assert "va_secret_key_value" not in json.dumps(payload)
    assert "va_cloud_secret_key" not in json.dumps(payload)


def test_usage_status_cli_outputs_markdown(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    update_agent_session(workspace, cli_run_result("checkout"))

    code = main(["usage-status", "--workspace-root", str(workspace), "--format", "markdown"])
    output = capsys.readouterr().out

    assert code == 0
    assert "# Checkpoint Usage" in output
    assert "Local runs this month: `1`" in output
    assert "Cloud run limit: `50`" in output
    assert "Cloud run remaining: `50`" in output
    assert "## Cloud Config" in output
    assert "Blockers: missing_endpoint, missing_api_key" in output
    assert "cloud_run" in output


def test_usage_cli_alias_matches_usage_status(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "workspace"
    update_agent_session(workspace, cli_run_result("checkout"))

    code = main(["usage", "--workspace-root", str(workspace), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["license"]["tier"] == "free"
    assert payload["usage"]["runs_this_month"] == 1


def test_activate_cli_writes_local_license_file(tmp_path: Path, capsys, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VISUAL_AGENT_HOME", str(home))

    code = main(["activate", "--key", "va_test_license_key", "--tier", "pro", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    license_path = home / "license.json"
    assert code == 0
    assert payload["status"] == "success"
    assert payload["license"]["tier"] == "pro"
    assert payload["license"]["key_present"] is True
    assert license_path.exists()
    assert json.loads(license_path.read_text(encoding="utf-8"))["license_key"] == "va_test_license_key"


def test_cloud_run_plan_cli_outputs_blocked_request_without_reading_inputs(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    inputs_dir = workspace / "inputs"
    inputs_dir.mkdir(parents=True)
    (inputs_dir / "checkout.json").write_text('{"password": "demo_password"}', encoding="utf-8")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run-plan",
            "--workspace-root",
            str(workspace),
            "--workflow",
            "checkout",
            "--run-profile",
            "approved",
            "--inputs-file",
            "checkout.json",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    raw = json.dumps(payload)

    assert code == 0
    assert payload["workflow_name"] == "checkout"
    assert payload["request"]["status"] == "blocked"
    assert payload["request"]["run_profile"] == "approved"
    assert payload["request"]["inputs_file"] == "checkout.json"
    assert payload["request"]["inputs"]["provided"] is False
    assert payload["adapter_diagnostic"]["status"] == "blocked"
    assert "demo_password" not in raw
    assert "va_cloud_secret_key" not in raw


def test_cloud_run_plan_cli_outputs_markdown_ready_without_network(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run-plan",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "# Cloud Run Plan" in output
    assert "Request status: `ready`" in output
    assert "Adapter Diagnostic" in output
    assert "transport is not enabled" in output
    assert "va_cloud_secret_key" not in output


def test_cloud_run_cli_defaults_to_plan_without_network(tmp_path: Path, capsys, monkeypatch) -> None:
    inputs_file = tmp_path / "checkout.json"
    inputs_file.write_text('{"password": "demo_password"}', encoding="utf-8")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--inputs-file",
            str(inputs_file),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    raw = json.dumps(payload)

    assert code == 0
    assert payload["execution_requested"] is False
    assert payload["network_sent"] is False
    assert payload["request"]["status"] == "ready"
    assert payload["request"]["inputs_file"] == str(inputs_file)
    assert payload["request"]["inputs"]["provided"] is False
    assert payload["adapter_diagnostic"]["status"] == "blocked"
    assert "transport is not enabled" in payload["adapter_diagnostic"]["message"]
    assert load_agent_session(tmp_path) is None
    assert "demo_password" not in raw
    assert "va_cloud_secret_key" not in raw


def test_cloud_run_cli_resolves_marketplace_workflow_id(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    def fake_marketplace_transport(*_args, **_kwargs):
        return lambda path: {
            "schema_version": 1,
            "status": "success",
            "workflow": {
                "id": "wf_000123",
                "name": "market_demo",
                "workflow_yaml": "schema_version: 1\nname: market_demo\nvisibility: public\nsteps: []\n",
            },
        }

    monkeypatch.setattr("visual_agent.cloud.build_http_marketplace_transport", fake_marketplace_transport)

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "fallback-name",
            "--workflow-id",
            "wf_000123",
            "--marketplace-endpoint",
            "https://marketplace.visualagent.test",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["workflow_id"] == "wf_000123"
    assert payload["workflow_name"] == "market_demo"
    assert payload["workflow_source"] == "marketplace"
    assert payload["request"]["workflow_yaml_provided"] is True
    assert payload["request"]["workflow_yaml"].startswith("schema_version: 1")
    assert payload["request"]["workflow_source"] == "marketplace"
    assert payload["request"]["workflow_id"] == "wf_000123"


def test_cloud_run_cli_execute_without_transport_blocks_without_usage(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--execute",
            "--format",
            "markdown",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "# Cloud Run" in output
    assert "Execution requested: `True`" in output
    assert "Network sent: `False`" in output
    assert "Status: `blocked`" in output
    assert "transport is not enabled" in output
    assert "va_cloud_secret_key" not in output
    assert load_agent_session(tmp_path) is None


def test_cloud_pull_workflow_cli_downloads_marketplace_yaml(tmp_path: Path, capsys, monkeypatch) -> None:
    def fake_marketplace_transport(*_args, **_kwargs):
        return lambda path: {
            "schema_version": 1,
            "status": "success",
            "workflow": {
                "id": "wf_000123",
                "name": "market_demo",
                "workflow_yaml": "schema_version: 1\nname: market_demo\nvisibility: public\nsteps: []\n",
            },
        }

    monkeypatch.setattr("visual_agent.cloud.build_http_marketplace_transport", fake_marketplace_transport)

    code = main(
        [
            "cloud-pull-workflow",
            "--workspace-root",
            str(tmp_path / "workspace"),
            "--workflow-id",
            "wf_000123",
            "--marketplace-endpoint",
            "https://marketplace.visualagent.test",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    path = Path(payload["path"])

    assert code == 0
    assert payload["workflow_name"] == "market_demo"
    assert path.exists()
    assert "workflow_yaml" not in payload or payload.get("workflow_yaml") is None
    assert "market_demo" in path.read_text(encoding="utf-8")


def test_withdraw_workflow_cli_marks_yaml_private(tmp_path: Path, capsys) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "public_profile.yaml"
    workflow_path.write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: public_profile
version: 1
description: Public profile save flow
tags: [verification, profile]
visibility: public
author: visual-agent-team
license: cc-by-4.0
steps:
  - id: observe
    action: observe_html
    path: fixtures/profile.html
""".strip(),
        encoding="utf-8",
    )

    code = main(["withdraw-workflow", "--workspace-root", str(workspace.root), "--name", "public_profile", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "withdrawn"
    assert "visibility: private" in workflow_path.read_text(encoding="utf-8")


def test_cloud_run_cli_execute_blocks_when_free_quota_exceeded(tmp_path: Path, capsys, monkeypatch) -> None:
    record_cloud_run_usage(tmp_path, count=50)
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", "https://cloud.visualagent.test")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "va_cloud_secret_key")

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--execute",
            "--transport",
            "http",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["network_sent"] is False
    assert payload["result"]["status"] == "upgrade_required"
    assert payload["result"]["reason"] == "quota_exceeded"
    assert payload["result"]["quota"]["remaining"] == 0
    assert load_agent_session(tmp_path).cloud_runs_used == 50


def test_cloud_run_cli_execute_http_without_config_blocks_without_network(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_TIER", "pro")
    monkeypatch.delenv("VISUAL_AGENT_CLOUD_ENDPOINT", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_CLOUD_API_KEY", raising=False)

    code = main(
        [
            "cloud-run",
            "--workspace-root",
            str(tmp_path),
            "--workflow",
            "checkout",
            "--execute",
            "--transport",
            "http",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["transport"] == "http"
    assert payload["execution_requested"] is True
    assert payload["network_sent"] is False
    assert payload["request"]["status"] == "blocked"
    assert payload["request"]["cloud_config"]["blockers"] == ["missing_endpoint", "missing_api_key"]
    assert payload["result"]["status"] == "blocked"
    assert payload["result"]["usage_recorded"] is False
    assert load_agent_session(tmp_path) is None


def test_cloud_run_cli_execute_http_calls_local_cloud_server(tmp_path: Path, capsys, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    (workspace.fixtures_dir / "ready.html").write_text("<p>Ready</p>", encoding="utf-8")
    (workspace.workflows_dir / "ready.yaml").write_text(
        """
schema_version: 1
name: ready
version: 1
steps:
  - id: observe
    action: observe_html
    path: fixtures/ready.html
  - id: assert_ready
    action: assert_text
    text: Ready
""".strip(),
        encoding="utf-8",
    )
    server = create_cloud_server(workspace_root=workspace.root, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VISUAL_AGENT_LICENSE_TIER", "pro")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_ENDPOINT", f"http://127.0.0.1:{server.server_port}/v1/run")
    monkeypatch.setenv("VISUAL_AGENT_CLOUD_API_KEY", "local-test-key")
    try:
        code = main(
            [
                "cloud-run",
                "--workspace-root",
                str(workspace.root),
                "--workflow",
                "ready",
                "--execute",
                "--transport",
                "http",
                "--format",
                "json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert code == 0
    assert payload["network_sent"] is True
    assert payload["result"]["status"] == "success"
    assert payload["result"]["run_id"]
    assert payload["result"]["workflow_source"] == "workspace"
    assert payload["result"].get("workflow_id", "") == ""
    assert payload["result"]["usage_recorded"] is True


def test_release_trial_cli_outputs_json(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "visual_agent.cli.run_release_trial",
        lambda **kwargs: {
            "schema_version": 1,
            "workspace_root": str(kwargs["workspace_root"]),
            "status": "success",
            "run_profile": kwargs["run_profile"],
            "cloud_org": kwargs["cloud_org"],
            "cloud_user": kwargs["cloud_user"],
            "checks": [{"id": "demo_workspace_check", "status": "success", "run_id": "demo-run"}],
            "failed_count": 0,
            "demo_workspace_check": {"status": "success"},
            "mcp_smoke": {"status": "success"},
            "cloud_run": {"status": "success", "result": {"run_id": "cloud-run"}},
        },
    )

    code = main(["release-trial", "--workspace-root", str(tmp_path / "workspace"), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["checks"][0]["id"] == "demo_workspace_check"
    assert payload["run_profile"] == "supervised"


def test_quality_gate_cli_outputs_junit_xml(tmp_path: Path, capsys, monkeypatch) -> None:
    junit_path = tmp_path / "quality" / "junit.xml"
    summary_path = tmp_path / "summary.md"
    summary_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    code = main(
        [
            "quality-gate",
            "--profile",
            "ci",
            "--ci",
            "--junit-output",
            str(junit_path),
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert output.startswith("<?xml")
    assert "testsuite" in output
    assert junit_path.exists()
    assert junit_path.read_text(encoding="utf-8").startswith("<?xml")
    assert "Checkpoint Quality Gate" in summary_path.read_text(encoding="utf-8")


def test_generate_report_cli_writes_static_html_report(tmp_path: Path, capsys) -> None:
    workspace = init_workspace(tmp_path / ".agent-workspace")
    inputs = load_workspace_inputs(workspace, None, "demo_login.json")
    failing_workflow = workspace.workflows_dir / "failing_report_demo.yaml"
    failing_workflow.write_text(
        """
schema_version: 1
name: failing_report_demo
version: 1
steps:
  - id: observe
    action: observe_ocr
    mock_text: Ready
  - id: assert_missing
    action: assert_text
    text: Missing
""".strip(),
        encoding="utf-8",
    )
    run_workspace_workflow(workspace, "local_html_form_workflow", inputs=inputs, dry_run=True)
    run_workspace_workflow(workspace, "failing_report_demo", dry_run=True)

    output = tmp_path / "report" / "run-history.html"
    code = main(
        [
            "generate-report",
            "--workspace-root",
            str(workspace.root),
            "--output",
            str(output),
            "--share",
            "--summary-provider",
            "none",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    html = output.read_text(encoding="utf-8")

    assert code == 0
    assert payload["output_path"] == str(output.resolve())
    assert payload["recent_run_count"] == 2
    assert payload["share"]["local_url"].startswith("file:///")
    assert payload["share"]["share_status"] == "placeholder"
    assert payload["ai_summary"]["source"] == "deterministic"
    assert payload["ai_summary"]["text"]
    assert output.exists()
    assert "<title>Checkpoint Run Report" in html
    assert "Summary" in html
    assert "local_html_form_workflow" in html
    assert "failing_report_demo" in html
    assert "Run trend chart" in html
    assert "Recent Runs" in html
    assert "data:image/" in html or "No screenshot" in html


def test_env_check_cli_writes_status_file(tmp_path: Path, capsys) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text('{"dependencies":{"next":"13.0.0"}}', encoding="utf-8")
    dist = root / ".next"
    dist.mkdir()
    (dist / "index.html").write_text("<html>ok</html>", encoding="utf-8")

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:  # type: ignore[override]
            self.request.recv(1)

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code = main(
                [
                    "env-check",
                    "--workspace-root",
                    str(root),
                    "--port",
                    str(server.server_address[1]),
                    "--format",
                    "json",
                ]
            )
            payload = json.loads(capsys.readouterr().out)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    assert code == 0
    assert payload["ok"] is True
    assert payload["project_type"] == "nextjs"
    assert (root / ".visual-agent-status.md").exists()
    text = (root / ".visual-agent-status.md").read_text(encoding="utf-8")
    assert "Environment" in text
    assert "project_type: nextjs" in text


def test_test_plan_cli_outputs_focused_pytest_command(tmp_path: Path, capsys, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_security.py").write_text("def test_security():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.verification_profiles._git_changed_paths", lambda root, base: ["tests/test_security.py"])

    code = main(["test-plan", "--repo-root", str(tmp_path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert str(Path(sys.executable)) in payload["command"]
    assert payload["command"].endswith(" -m pytest -q tests/test_security.py")
    assert payload["profiles"][0]["name"] == "pytest-focused"


def test_permission_plan_cli_outputs_command_risk(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "permission-plan",
            "--repo-root",
            str(tmp_path),
            "--command",
            "python -m pytest -q",
            "--command",
            "npm publish",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["decision"] == "ask"
    assert payload["checks"][0]["decision"] == "allow"
    assert payload["checks"][1]["decision"] == "ask"


def test_slash_test_dispatches_to_test_plan(tmp_path: Path, capsys, monkeypatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_security.py").write_text("def test_security():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("visual_agent.verification_profiles._git_changed_paths", lambda root, base: ["tests/test_security.py"])

    code = main(["slash", "test", "--repo-root", str(tmp_path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert str(Path(sys.executable)) in payload["command"]
    assert payload["command"].endswith(" -m pytest -q tests/test_security.py")


def test_generate_fixture_cli_writes_template(tmp_path: Path, capsys) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    output = workspace_root / "fixtures" / "auth_login.yaml"

    code = main(
        [
            "generate-fixture",
            "--workspace-root",
            str(workspace_root),
            "--page",
            "/login",
            "--name",
            "auth_login",
            "--type",
            "standard",
            "--output",
            str(output),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output.exists()
    assert payload["path"] == str(output.resolve())
    assert "fixture_type: standard" in output.read_text(encoding="utf-8")


def init_git_repo(path: Path) -> None:
    try:
        git(path, "init")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is required for this test")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def cli_run_result(workflow: str) -> WorkflowRunResult:
    return WorkflowRunResult(
        run_id="run-usage",
        run_dir=Path("runs") / "run-usage",
        workflow_name=workflow,
        steps=(WorkflowStepResult(id="observe", action="observe_fixture", status=ActionStatus.SUCCESS, message="ok"),),
        run_profile="dry-run",
    )



def test_usage_timeline_cli_reports_missions_across_workspaces(tmp_path, capsys) -> None:
    workspace = tmp_path / "project" / ".agent-workspace"
    mission_dir = workspace / "missions" / "20260804-demo"
    mission_dir.mkdir(parents=True)
    (mission_dir / "mission.json").write_text(
        json.dumps(
            {
                "mission_id": "20260804-demo",
                "objective": "add a version endpoint",
                "status": "verified",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (mission_dir / "journey.json").write_text(
        json.dumps(
            {
                "mission_id": "20260804-demo",
                "status": "completed",
                "can_claim_verified": True,
                "can_claim_delivered": True,
                "phases": [{"id": "routing", "status": "passed", "details": {"provider": "openai", "model": "gpt-5.5"}}],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["usage-timeline", "--base", str(tmp_path), "--days", "14"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "Pacer 使用时间线" in out
    assert "add a version endpoint" in out


def test_worktrees_cli_defaults_to_reporting_without_removing(tmp_path, capsys) -> None:
    exit_code = main(["worktrees", "--repo-root", str(tmp_path), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["dry_run"] is True
    assert payload["removed_count"] == 0
