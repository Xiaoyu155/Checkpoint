from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from time import time

import pytest

from visual_agent.mcp_server import (
    _apply_startup_args,
    call_tool,
    list_run_artifacts_payload,
    list_workflows_payload,
    mcp_tools,
    mcp_workspace_root_allowed,
    require_workspace,
    get_latest_failure_payload,
    get_workspace_dashboard_payload,
    generate_workflow_from_context_payload,
    run_workflow_payload,
    validate_workflow_payload,
    verify_implementation_payload,
    verify_workflow_payload,
)
from visual_agent.pacer_verification import (
    PACER_VERIFICATION_BATCH_KIND,
    PACER_VERIFICATION_POLICY_VERSION,
    PACER_VERIFICATION_SOURCE_TOOL,
    validate_pacer_verification_batch,
)
from visual_agent.workspace import init_workspace


ROOT = Path(__file__).resolve().parent.parent


def content_payload(result):
    return json.loads(result[0].text)


def trusted_verification_summary(run_id: str, *, launch_id: str = "") -> dict[str, object]:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    return {
        "schema_version": 1,
        "kind": PACER_VERIFICATION_BATCH_KIND,
        "source_tool": PACER_VERIFICATION_SOURCE_TOOL,
        "policy_version": PACER_VERIFICATION_POLICY_VERSION,
        "run_id": run_id,
        "launch_id": launch_id,
        "status": "passed",
        "requested_steps": 1,
        "executed_steps": 1,
        "passed": 1,
        "failed": 0,
        "timed_out": 0,
        "not_applicable": 0,
        "step_classes": ["test"],
        "records": [{"name": "tests", "status": "passed", "exit_code": 0, "command": command}],
    }


def passing_unittest_step(repo: Path) -> dict[str, object]:
    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_smoke.py").write_text(
        "import unittest\n\n"
        "class SmokeTest(unittest.TestCase):\n"
        "    def test_passes(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    return {
        "name": "unittest",
        "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    }


def completion_evidence(
    goal: str,
    *,
    path: str = "tests/test_smoke.py",
    step_name: str = "unittest",
    kind: str = "test",
    state: str = "created",
) -> dict[str, object]:
    from visual_agent.task_review import build_task_contract

    requirement_id = str(build_task_contract(goal)["requirements"][0]["id"])
    return {
        "result_kind": kind,
        "claims": [
            {
                "kind": kind,
                "requirement_ids": [requirement_id],
                "requirement": goal,
                "result": f"{goal} verified",
                "files": ([{"path": path, "state": state}] if path else []),
                "verification_steps": [step_name],
            }
        ],
        "unresolved_items": [],
        "known_risks": [],
    }


def active_completion_context(
    tmp_path,
    monkeypatch,
    *,
    launch_id: str = "launch-complete",
    goal: str = "test the project",
    initial_files: dict[str, str] | None = None,
):
    from visual_agent import codex_rollout_telemetry
    from visual_agent.codex_rollout_telemetry import RolloutSnapshot
    from visual_agent.mcp_server import begin_pacer_task_payload, get_pacer_memory_payload
    from visual_agent.pacer_launch_context import (
        initialize_active_launch,
        save_rollout_baseline,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='completion-test'\n", encoding="utf-8")
    for relative_path, content in (initial_files or {}).items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    default_test = repo / "tests" / "test_smoke.py"
    if not default_test.exists():
        default_test.parent.mkdir(parents=True, exist_ok=True)
        default_test.write_text(
            "import unittest\n\n"
            "class SmokeTest(unittest.TestCase):\n"
            "    def test_passes(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": launch_id,
            "repo_root": str(repo),
            "auto_compact_token_limit": 96000,
            "rollout_ownership": {"scheme": "launch_marker_v1", "required": True},
        },
    )
    started = begin_pacer_task_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": goal,
        }
    )
    assert started["source_baseline"]["receipt"]
    memory = get_pacer_memory_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "goal": goal}
    )
    assert memory["pillars"]["memory"]["active"] is True
    save_rollout_baseline(
        workspace_root=workspace,
        launch_id=launch_id,
        snapshot=RolloutSnapshot(tmp_path / "sessions", "2026-07-14T00:00:00+00:00", {}),
    )
    monkeypatch.setattr(
        codex_rollout_telemetry,
        "aggregate_rollout_telemetry",
        lambda *_args, **_kwargs: {
            "schema_version": 1,
            "status": "captured",
            "attribution_confidence": "high",
            "ownership": {"scheme": "launch_marker_v1", "required": True, "matched": True},
            "runtime": {"provider": "custom", "model": "gpt-test", "reasoning_effort": "medium"},
            "usage": {},
            "current_context_usage": {},
            "agents": {},
            "compactions": {"count": 0, "timestamps": []},
        },
    )
    return workspace, repo


def write_workflow(workspace, name: str, *, affects=(), tags=(), steps=()) -> Path:
    import yaml

    path = workspace.workflows_dir / f"{name}.yaml"
    payload = {
        "schema_version": 1,
        "name": name,
        "version": 1,
        "affects": list(affects),
        "tags": list(tags),
        "steps": list(steps),
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_mcp_tools_include_expected_names() -> None:
    names = {tool.name for tool in mcp_tools()}

    assert names == {
        "begin_pacer_task",
        "get_pacer_memory",
        "get_pacer_runtime_telemetry",
        "get_pacer_events",
        "record_pacer_outcome",
        "run_pacer_commands",
        "run_pacer_verification",
        "complete_pacer_task",
        "list_workflows",
        "plan_coverage_repair",
        "draft_coverage_repair",
        "apply_coverage_repair",
        "validate_workflow",
        "run_workflow",
        "verify_workflow",
        "get_run_report",
        "list_run_artifacts",
        "get_workspace_dashboard",
        "get_latest_failure",
        "summarize_latest_failure",
        "diagnose_failure",
        "get_failure_details",
        "repair_workflow",
        "auto_repair_failure",
        "list_repair_history",
        "rollback_repair",
        "get_repair_health",
        "list_benchmarks",
        "build_benchmark_plan",
        "build_benchmark_draft",
        "run_browser_smoke",
        "run_browser_smoke_suite",
        "get_session_context",
        "get_visual_status",
        "save_task_context",
        "run_verification",
        "generate_workflow_from_context",
        "verify_implementation",
        "generate_workflow",
    }


def test_pacer_observation_tools_are_annotated_local_and_non_destructive() -> None:
    tools = {tool.name: tool for tool in mcp_tools()}
    normalized = {}
    for name in ("get_pacer_memory", "get_pacer_runtime_telemetry"):
        annotations = tools[name].annotations
        if hasattr(annotations, "model_dump"):
            annotations = annotations.model_dump(by_alias=True)
        normalized[name] = annotations
        assert annotations["readOnlyHint"] is False
        assert annotations["destructiveHint"] is False
        assert annotations["openWorldHint"] is False
    assert normalized["get_pacer_memory"]["idempotentHint"] is False
    assert normalized["get_pacer_runtime_telemetry"]["idempotentHint"] is True


def test_begin_pacer_task_captures_one_process_trusted_baseline(tmp_path) -> None:
    from visual_agent.mcp_server import begin_pacer_task_payload
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='begin-test'\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-begin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-begin", "repo_root": str(repo), "goal": "begin safely"},
    )

    first = begin_pacer_task_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "goal": "begin safely"}
    )
    second = begin_pacer_task_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "goal": "begin safely"}
    )
    active = read_active_launch(workspace, launch_id="launch-begin")

    assert first["status"] == "started"
    assert second["status"] == "already_started"
    assert first["source_baseline"]["digest"] == second["source_baseline"]["digest"]
    assert first["source_baseline"]["receipt"] == second["source_baseline"]["receipt"]
    assert active["source_baseline_digest"] == first["source_baseline"]["digest"]
    assert active["source_baseline_receipt"] == first["source_baseline"]["receipt"]
    assert first["task_contract"]["requirements"]
    assert active["task_contract"] == first["task_contract"]
    assert active["task_contract_digest"]
    assert active["task_contract_receipt"]


def test_begin_pacer_task_does_not_retrust_existing_baseline_after_process_restart(
    tmp_path,
    monkeypatch,
) -> None:
    from visual_agent import pacer_launch_context
    from visual_agent.mcp_server import begin_pacer_task_payload

    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-begin-restart",
        goal="run restart tests",
    )
    registry = pacer_launch_context._TRUSTED_TASK_SOURCE_BASELINES
    monkeypatch.setattr(
        pacer_launch_context,
        "_TRUSTED_TASK_SOURCE_BASELINES",
        type(registry)(),
    )

    with pytest.raises(ValueError, match="trusted_source_baseline_not_registered"):
        begin_pacer_task_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "goal": "run restart tests",
            }
        )


def test_begin_pacer_task_adopts_launcher_pre_registered_evidence(tmp_path, monkeypatch) -> None:
    from visual_agent import pacer_launch_context
    from visual_agent.codex_launcher import _pre_register_pacer_task
    from visual_agent.mcp_server import begin_pacer_task_payload
    from visual_agent.pacer_launch_context import (
        PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV,
        PRELAUNCH_TASK_CONTRACT_DIGEST_ENV,
        PRELAUNCH_TASK_REQUIRED_ENV,
        initialize_active_launch,
        load_task_source_baseline,
        read_active_launch,
        trusted_task_contract_errors,
        trusted_task_source_baseline_errors,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    launch_id = "launch-pre-registered"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": launch_id, "repo_root": str(repo)},
    )
    evidence = _pre_register_pacer_task(
        workspace_root=workspace,
        repo_root=repo,
        launch_id=launch_id,
        goal="修复登录错误",
    )
    monkeypatch.setattr(
        pacer_launch_context,
        "_TRUSTED_TASK_SOURCE_BASELINES",
        type(pacer_launch_context._TRUSTED_TASK_SOURCE_BASELINES)(),
    )
    monkeypatch.setattr(
        pacer_launch_context,
        "_TRUSTED_TASK_CONTRACTS",
        type(pacer_launch_context._TRUSTED_TASK_CONTRACTS)(),
    )
    monkeypatch.setenv("PACER_LAUNCH_ID", launch_id)
    monkeypatch.setenv(PRELAUNCH_TASK_REQUIRED_ENV, "1")
    monkeypatch.setenv(PRELAUNCH_TASK_CONTRACT_DIGEST_ENV, evidence["task_contract_digest"])
    monkeypatch.setenv(PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV, evidence["source_baseline_digest"])

    started = begin_pacer_task_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "goal": "修复登录错误"}
    )
    active = read_active_launch(workspace, launch_id=launch_id)
    baseline = load_task_source_baseline(active, workspace_root=workspace)

    assert started["status"] == "already_started"
    assert started["source_baseline"]["status"] == "verified"
    assert active["task_contract_receipt"]
    assert active["source_baseline_receipt"]
    assert trusted_task_contract_errors(
        active["task_contract"],
        goal="修复登录错误",
        workspace_root=workspace,
        launch_id=launch_id,
        repo_root=repo,
        trusted_digest=active["task_contract_digest"],
        trusted_receipt=active["task_contract_receipt"],
    ) == ()
    assert trusted_task_source_baseline_errors(
        baseline,
        workspace_root=workspace,
        launch_id=launch_id,
        repo_root=repo,
        trusted_digest=active["source_baseline_digest"],
        trusted_receipt=active["source_baseline_receipt"],
    ) == ()


def test_begin_pacer_task_rejects_tampered_pre_registered_baseline(tmp_path, monkeypatch) -> None:
    from visual_agent.codex_launcher import _pre_register_pacer_task
    from visual_agent.mcp_server import begin_pacer_task_payload
    from visual_agent.pacer_launch_context import (
        PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV,
        PRELAUNCH_TASK_CONTRACT_DIGEST_ENV,
        PRELAUNCH_TASK_REQUIRED_ENV,
        initialize_active_launch,
        task_source_baseline_path,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    launch_id = "launch-pre-tampered"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": launch_id, "repo_root": str(repo)},
    )
    evidence = _pre_register_pacer_task(
        workspace_root=workspace,
        repo_root=repo,
        launch_id=launch_id,
        goal="修复登录错误",
    )
    path = task_source_baseline_path(workspace, launch_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"]["app.py"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("PACER_LAUNCH_ID", launch_id)
    monkeypatch.setenv(PRELAUNCH_TASK_REQUIRED_ENV, "1")
    monkeypatch.setenv(PRELAUNCH_TASK_CONTRACT_DIGEST_ENV, evidence["task_contract_digest"])
    monkeypatch.setenv(PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV, evidence["source_baseline_digest"])

    with pytest.raises(ValueError, match="prelaunch source baseline digest mismatch"):
        begin_pacer_task_payload(
            {"workspace_root": str(workspace), "repo_root": str(repo), "goal": "修复登录错误"}
        )


def test_begin_pacer_task_rejects_replaced_pre_registered_goal_and_contract(tmp_path, monkeypatch) -> None:
    from visual_agent.codex_launcher import _pre_register_pacer_task
    from visual_agent.mcp_server import begin_pacer_task_payload
    from visual_agent.pacer_launch_context import (
        PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV,
        PRELAUNCH_TASK_CONTRACT_DIGEST_ENV,
        PRELAUNCH_TASK_REQUIRED_ENV,
        initialize_active_launch,
        launch_context_path,
        task_contract_digest,
    )
    from visual_agent.task_review import build_task_contract

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    launch_id = "launch-pre-goal-tampered"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": launch_id, "repo_root": str(repo)},
    )
    evidence = _pre_register_pacer_task(
        workspace_root=workspace,
        repo_root=repo,
        launch_id=launch_id,
        goal="修复登录错误",
    )
    path = launch_context_path(workspace, launch_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    replacement_goal = "改做支付页面"
    replacement_contract = build_task_contract(replacement_goal)
    payload["launch_goal"] = replacement_goal
    payload["current_goal"] = replacement_goal
    payload["task_contract"] = replacement_contract
    payload["task_contract_digest"] = task_contract_digest(replacement_contract)
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("PACER_LAUNCH_ID", launch_id)
    monkeypatch.setenv(PRELAUNCH_TASK_REQUIRED_ENV, "1")
    monkeypatch.setenv(PRELAUNCH_TASK_CONTRACT_DIGEST_ENV, evidence["task_contract_digest"])
    monkeypatch.setenv(PRELAUNCH_SOURCE_BASELINE_DIGEST_ENV, evidence["source_baseline_digest"])

    with pytest.raises(ValueError, match="prelaunch task contract digest mismatch"):
        begin_pacer_task_payload(
            {"workspace_root": str(workspace), "repo_root": str(repo), "goal": replacement_goal}
        )


def test_real_mcp_memory_call_registers_baseline_but_direct_preload_does_not(tmp_path) -> None:
    from visual_agent.mcp_server import get_pacer_memory_payload
    from visual_agent.pacer_launch_context import (
        initialize_active_launch,
        load_task_source_baseline,
        read_active_launch,
        task_source_baseline_path,
        trusted_task_source_baseline_errors,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='memory-handshake'\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-memory-handshake.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-memory-handshake",
            "repo_root": str(repo),
            "goal": "load memory safely",
        },
    )

    get_pacer_memory_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "goal": "load memory safely"}
    )
    assert not task_source_baseline_path(workspace, "launch-memory-handshake").exists()

    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {"workspace_root": str(workspace), "repo_root": str(repo), "goal": "load memory safely"},
            )
        )
    )
    active = read_active_launch(workspace, launch_id="launch-memory-handshake")
    baseline = load_task_source_baseline(active, workspace_root=workspace)

    assert memory["status"] == "memory_loaded"
    assert baseline
    assert trusted_task_source_baseline_errors(
        baseline,
        workspace_root=workspace,
        launch_id="launch-memory-handshake",
        repo_root=repo,
        trusted_digest=active["source_baseline_digest"],
        trusted_receipt=active["source_baseline_receipt"],
    ) == ()


def test_pacer_completion_tool_schema_teaches_atomic_argv_contract() -> None:
    tools = {tool.name: tool for tool in mcp_tools()}
    completion = tools["complete_pacer_task"]
    completion_argv = completion.inputSchema["properties"]["steps"]["items"]["properties"]["argv"]
    completion_evidence_schema = completion.inputSchema["properties"]["completion_evidence"]
    claim_schema = completion_evidence_schema["properties"]["claims"]["items"]
    verification_argv = tools["run_pacer_verification"].inputSchema["properties"]["steps"]["items"]["properties"]["argv"]
    outcome_verification = tools["record_pacer_outcome"].inputSchema["properties"]["verification"]
    outcome_schema = tools["record_pacer_outcome"].inputSchema

    assert "argv" in completion.description
    assert "automatically binds" in completion.description
    assert "derives file paths" in completion.description
    assert "never accepts a run_pacer_commands batch" in completion.description
    assert "completion_evidence" in completion.inputSchema["required"]
    assert completion_evidence_schema["required"] == ["claims", "unresolved_items", "known_risks"]
    assert "requirement_ids" in claim_schema["properties"]
    assert "requirement_ids" in claim_schema["required"]
    assert claim_schema["required"] == ["requirement_ids", "result", "verification_steps"]
    assert "Pacer ignores it" in claim_schema["properties"]["files"]["description"]
    assert "do not add Git inspection steps" in claim_schema["properties"]["verification_steps"]["description"]
    assert "begin_pacer_task" in claim_schema["properties"]["requirement_ids"]["description"]
    assert completion_argv["type"] == "array"
    assert completion_argv["items"] == {"type": "string"}
    assert "do not use a command field" in completion_argv["description"]
    assert "do not pass a command field" in verification_argv["description"]
    assert outcome_verification["examples"] == ["run_id=20260714-120000-abcd1234"]
    assert outcome_schema["properties"]["status"]["enum"] == ["failed", "blocked"]
    assert outcome_schema["dependentRequired"] == {"verification": ["verification_receipt"]}


def test_pacer_verification_rejects_arbitrary_execution(tmp_path) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    with pytest.raises(ValueError, match="not allowlisted"):
        run_pacer_verification_payload({
            "workspace_root": str(tmp_path / ".agent-workspace"),
            "repo_root": str(tmp_path),
            "steps": [{"name": "arbitrary", "argv": [sys.executable, "-c", "print('no')"]}],
        })


def test_pacer_verification_allows_python_test_batch(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server

    captured = {}
    monkeypatch.setattr(mcp_server, "run_pacer_commands_payload", lambda args: captured.update(args) or {"status": "passed"})
    payload = mcp_server.run_pacer_verification_payload({
        "workspace_root": str(tmp_path / ".agent-workspace"),
        "repo_root": str(tmp_path),
        "steps": [{"name": "tests", "argv": [sys.executable, "-m", "pytest", "-q"]}],
    })
    assert payload["status"] == "passed"
    assert captured["steps"][0]["name"] == "tests"


def test_pacer_verification_allows_exact_repository_checkpoint(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server

    captured = {}
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_commands_payload",
        lambda args: captured.update(args) or {"status": "passed"},
    )
    payload = mcp_server.run_pacer_verification_payload(
        {
            "workspace_root": str(tmp_path / ".agent-workspace"),
            "repo_root": str(tmp_path),
            "steps": [
                {
                    "name": "checkpoint",
                    "argv": [
                        "python",
                        "-m",
                        "visual_agent.cli",
                        "codex-check",
                        "--workspace-root",
                        ".agent-workspace",
                        "--repo-root",
                        ".",
                    ],
                }
            ],
        }
    )

    assert payload["status"] == "passed"
    assert captured["steps"][0]["argv"][1:] == [
        "-m",
        "visual_agent.cli",
        "codex-check",
        "--workspace-root",
        ".agent-workspace",
        "--repo-root",
        ".",
    ]
    assert captured["_pacer_verification_step_classes"] == ["analyze"]


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-m", "visual_agent.cli", "codex-check"],
        [
            "python",
            "-m",
            "visual_agent.cli",
            "codex-check",
            "--workspace-root",
            "other-workspace",
            "--repo-root",
            ".",
        ],
        [
            "python",
            "-m",
            "visual_agent.cli",
            "codex-check",
            "--workspace-root",
            ".agent-workspace",
            "--repo-root",
            ".",
            "--from-step",
            "shell-step",
        ],
        [
            "python",
            "-m",
            "visual_agent.cli",
            "verify-now",
            "--workspace-root",
            ".agent-workspace",
            "--repo-root",
            ".",
        ],
    ],
)
def test_pacer_verification_rejects_other_checkpoint_cli_forms(
    tmp_path,
    argv,
) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    with pytest.raises(ValueError, match="not allowlisted"):
        run_pacer_verification_payload(
            {
                "workspace_root": str(tmp_path / ".agent-workspace"),
                "repo_root": str(tmp_path),
                "steps": [{"name": "checkpoint", "argv": argv}],
            }
        )


def test_pacer_verification_allows_strict_unittest_discovery(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server

    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(mcp_server, "run_pacer_commands_payload", lambda args: captured.update(args) or {"status": "passed"})
    payload = mcp_server.run_pacer_verification_payload({
        "workspace_root": str(tmp_path / ".agent-workspace"),
        "repo_root": str(tmp_path),
        "steps": [
            {
                "name": "unittest",
                "argv": ["python", "-m", "unittest", "discover", "-s", "tests/unit", "-v"],
            }
        ],
    })

    assert payload["status"] == "passed"
    assert captured["steps"][0]["argv"] == [
        str(interpreter.resolve()),
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests/unit",
        "-v",
    ]


def test_pacer_verification_runs_unittest_discovery_batch(tmp_path) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTest(unittest.TestCase):\n"
        "    def test_passes(self):\n"
        "        self.assertEqual(2 + 2, 4)\n",
        encoding="utf-8",
    )

    payload = run_pacer_verification_payload({
        "workspace_root": str(tmp_path / ".agent-workspace"),
        "repo_root": str(tmp_path),
        "steps": [
            {
                "name": "unittest",
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            }
        ],
    })

    assert payload["status"] == "passed"
    assert payload["requested_steps"] == 1
    assert payload["executed_steps"] == 1
    assert payload["passed"] == 1
    assert payload["records"][0]["exit_code"] == 0
    assert payload["kind"] == PACER_VERIFICATION_BATCH_KIND
    assert payload["source_tool"] == PACER_VERIFICATION_SOURCE_TOOL
    assert payload["policy_version"] == PACER_VERIFICATION_POLICY_VERSION
    assert payload["step_classes"] == ["test"]
    persisted = json.loads((Path(payload["run_dir"]) / "summary.json").read_text(encoding="utf-8"))
    validation = validate_pacer_verification_batch(
        persisted,
        workspace_root=tmp_path / ".agent-workspace",
        trusted_receipt=payload["verification_receipt"],
        expected_run_id=payload["run_id"],
    )
    assert validation.valid is True
    assert "verification_receipt" not in persisted


def test_trusted_receipt_binds_workspace_launch_run_and_summary_digest(tmp_path) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    workspace = tmp_path / ".agent-workspace"
    payload = run_pacer_verification_payload({
        "workspace_root": str(workspace),
        "repo_root": str(tmp_path),
        "steps": [passing_unittest_step(tmp_path)],
    })
    persisted = json.loads((Path(payload["run_dir"]) / "summary.json").read_text(encoding="utf-8"))
    receipt = str(payload["verification_receipt"])

    assert validate_pacer_verification_batch(
        persisted,
        workspace_root=workspace,
        trusted_receipt=receipt,
    ).valid is True

    missing_receipt = validate_pacer_verification_batch(
        persisted,
        workspace_root=workspace,
    )
    assert "trusted_receipt_required" in missing_receipt.errors

    wrong_receipt = validate_pacer_verification_batch(
        persisted,
        workspace_root=workspace,
        trusted_receipt="0" * len(receipt),
    )
    assert "trusted_receipt_mismatch" in wrong_receipt.errors

    wrong_workspace = validate_pacer_verification_batch(
        persisted,
        workspace_root=tmp_path / "other-workspace",
        trusted_receipt=receipt,
    )
    assert "trusted_receipt_not_registered" in wrong_workspace.errors

    for field, value in (("launch_id", "other-launch"), ("run_id", "20260714-120000-other123")):
        tampered = json.loads(json.dumps(persisted))
        tampered[field] = value
        validation = validate_pacer_verification_batch(
            tampered,
            workspace_root=workspace,
            trusted_receipt=receipt,
        )
        assert "trusted_receipt_not_registered" in validation.errors

    tampered = json.loads(json.dumps(persisted))
    tampered["records"][0]["exit_code"] = 99
    digest_validation = validate_pacer_verification_batch(
        tampered,
        workspace_root=workspace,
        trusted_receipt=receipt,
    )
    assert "trusted_summary_digest_mismatch" in digest_validation.errors


@pytest.mark.parametrize(
    "unittest_args",
    [
        ["-m", "unittest", "tests.test_service"],
        ["-m", "unittest", "discover", "-s", "../tests", "-v"],
        ["-m", "unittest", "discover", "-s", "C:\\tests", "-v"],
        ["-m", "unittest", "discover", "-s", "/tmp/tests", "-v"],
        ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
        ["-m", "unittest", "discover", "-s", "tests", "-v", "--locals"],
    ],
)
def test_pacer_verification_rejects_other_unittest_forms(tmp_path, unittest_args) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    with pytest.raises(ValueError, match="not allowlisted"):
        run_pacer_verification_payload({
            "workspace_root": str(tmp_path / ".agent-workspace"),
            "repo_root": str(tmp_path),
            "steps": [{"name": "unittest", "argv": [sys.executable, *unittest_args]}],
        })


@pytest.mark.parametrize("option", ["--help", "--collect-only", "--co", "--setup-plan"])
def test_pacer_verification_rejects_non_executing_pytest_modes(tmp_path, option) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    with pytest.raises(ValueError, match="non-executing inspection mode"):
        run_pacer_verification_payload({
            "workspace_root": str(tmp_path / ".agent-workspace"),
            "repo_root": str(tmp_path),
            "steps": [{"name": "not-tests", "argv": [sys.executable, "-m", "pytest", option]}],
        })


def test_pacer_verification_rejects_pure_git_inspection_batch(tmp_path) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    with pytest.raises(ValueError, match="substantive"):
        run_pacer_verification_payload({
            "workspace_root": str(tmp_path / ".agent-workspace"),
            "repo_root": str(tmp_path),
            "steps": [{"name": "status", "argv": ["git", "status", "--short"]}],
        })


def test_pacer_verification_rejects_compile_only_batch(tmp_path) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    with pytest.raises(ValueError, match="compile alone is insufficient"):
        run_pacer_verification_payload({
            "workspace_root": str(tmp_path / ".agent-workspace"),
            "repo_root": str(tmp_path),
            "steps": [{"name": "compile", "argv": [sys.executable, "-m", "compileall", "-q", "."]}],
        })


@pytest.mark.parametrize(
    "argv",
    [
        ["ruff", "check", ".", "--fix"],
        [sys.executable, "-m", "ruff", "check", ".", "--unsafe-fixes"],
        ["git", "diff", "--output=verification.patch"],
        ["git", "diff", "--output", "verification.patch"],
        [sys.executable, "-m", "pytest", "--cache-clear"],
        [sys.executable, "-m", "pytest", "--snapshot-update"],
    ],
)
def test_pacer_verification_rejects_mutating_tool_variants(tmp_path, argv) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    with pytest.raises(ValueError, match="not allowlisted"):
        run_pacer_verification_payload({
            "workspace_root": str(tmp_path / ".agent-workspace"),
            "repo_root": str(tmp_path),
            "steps": [{"name": "must-stay-read-only", "argv": argv}],
        })


def test_pacer_verification_resolves_bare_python_to_project_virtualenv(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server

    repo = tmp_path / "repo"
    repo.mkdir()
    interpreter = repo / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("fixture", encoding="utf-8")
    captured = {}
    monkeypatch.setattr(mcp_server, "run_pacer_commands_payload", lambda args: captured.update(args) or {"status": "passed"})
    mcp_server.run_pacer_verification_payload({
        "workspace_root": str(repo / ".agent-workspace"),
        "repo_root": str(repo),
        "steps": [{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
    })
    assert captured["steps"][0]["argv"][0] == str(interpreter.resolve())


def test_get_pacer_events_filters_by_launch(tmp_path) -> None:
    from visual_agent.mcp_server import get_pacer_events_payload
    from visual_agent.pacer_events import append_pacer_event
    from visual_agent.pacer_launch_context import initialize_active_launch, write_launch_liveness

    workspace = tmp_path / ".agent-workspace"
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=workspace / "pacer_native" / "launches" / "a.json",
        launch={"launch_id": "a", "repo_root": str(tmp_path)},
    )
    write_launch_liveness(
        workspace,
        "a",
        {"state": "stalled", "monitoring": True, "lifecycle_status": "running"},
    )
    append_pacer_event(workspace, "launch_started", launch_id="a")
    append_pacer_event(workspace, "launch_started", launch_id="b")
    append_pacer_event(workspace, "launch_finished", launch_id="a")
    payload = get_pacer_events_payload({
        "workspace_root": str(workspace),
        "repo_root": str(tmp_path),
        "launch_id": "a",
        "limit": 10,
    })
    assert payload["event_count"] == 2
    assert [event["type"] for event in payload["events"]] == ["launch_started", "launch_finished"]
    assert payload["lifecycle_status"] == "running"
    assert payload["liveness"]["state"] == "stalled"


def test_pacer_native_memory_round_trip(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    recorded = content_payload(
        asyncio.run(
            call_tool(
                "record_pacer_outcome",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": "修复登录",
                    "summary": "已修复 token 刷新",
                    "verification": "pytest: 12 passed",
                    "status": "failed",
                },
            )
        )
    )
    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {"workspace_root": str(workspace), "repo_root": str(repo), "goal": "修复登录"},
            )
        )
    )

    assert recorded["status"] == "recorded"
    assert recorded["evidence_level"] == "self_reported"
    assert memory["five_pillars_active"] is False
    assert memory["native_codex_history"][-1]["verification"] == "pytest: 12 passed"
    assert memory["native_codex_history"][-1]["trust"]["trusted"] is False
    assert memory["effective_memory"]["hit"] is False


@pytest.mark.parametrize("environment_launch_id", ["", "../invalid-launch"])
def test_mcp_keeps_resolved_running_launch_when_active_pointer_is_newer_completed(
    tmp_path,
    monkeypatch,
    environment_launch_id,
) -> None:
    from visual_agent import pacer_events
    from visual_agent.pacer_launch_context import (
        active_launch_path,
        initialize_active_launch,
        launch_context_path,
        read_active_launch,
        update_active_launch,
        write_launch_liveness,
    )

    monkeypatch.setattr(pacer_events, "process_exists", lambda _pid: True)

    root = tmp_path / "source"
    repo = root / "app"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    workspace = root / ".agent-workspace"
    launches = workspace / "pacer_native" / "launches"
    launches.mkdir(parents=True)
    manifest_a = launches / "launch-a.json"
    manifest_b = launches / "launch-b.json"
    manifest_a.write_text("{}", encoding="utf-8")
    manifest_b.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest_a,
        launch={
            "launch_id": "launch-a",
            "repo_root": str(root),
            "started_at": "2026-07-13T00:00:00+00:00",
            "goal": "running A",
        },
    )
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest_b,
        launch={
            "launch_id": "launch-b",
            "repo_root": str(root),
            "started_at": "2026-07-13T00:01:00+00:00",
            "goal": "completed B",
        },
    )
    terminal_b = {
        "state": "idle",
        "monitoring": False,
        "lifecycle_status": "completed",
        "stopped_at": "2026-07-13T00:02:00+00:00",
    }
    write_launch_liveness(workspace, "launch-b", terminal_b)
    update_active_launch(
        workspace,
        expected_launch_id="launch-b",
        status="completed",
        completed_at="2026-07-13T00:02:00+00:00",
        liveness=terminal_b,
    )
    context_b_path = launch_context_path(workspace, "launch-b")
    pointer_path = active_launch_path(workspace)
    context_b_before = context_b_path.read_bytes()
    pointer_before = pointer_path.read_bytes()
    assert json.loads(pointer_before)["launch_id"] == "launch-b"
    if environment_launch_id:
        monkeypatch.setenv("PACER_LAUNCH_ID", environment_launch_id)
    else:
        monkeypatch.delenv("PACER_LAUNCH_ID", raising=False)

    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": "continue running A",
                    "detail": "compact",
                },
            )
        )
    )

    assert memory["launch_id"] == "launch-a"
    active_a = read_active_launch(workspace, launch_id="launch-a")
    assert active_a["status"] == "running"
    assert active_a["project_root"] == str(repo.resolve())
    assert active_a["current_goal"] == "continue running A"
    assert active_a["pillars"]["memory"]["state"] == "loaded_empty"
    assert active_a["memory_cache"]["repo_root"] == str(repo.resolve())
    assert context_b_path.read_bytes() == context_b_before
    assert pointer_path.read_bytes() == pointer_before


def test_mcp_prefers_trusted_environment_launch_over_newer_running_pointer(
    tmp_path,
    monkeypatch,
) -> None:
    from visual_agent import pacer_events
    from visual_agent.pacer_launch_context import (
        active_launch_path,
        initialize_active_launch,
        launch_context_path,
        read_active_launch,
    )

    monkeypatch.setattr(pacer_events, "process_exists", lambda _pid: True)

    root = tmp_path / "source"
    repo = root / "app"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    workspace = root / ".agent-workspace"
    launches = workspace / "pacer_native" / "launches"
    launches.mkdir(parents=True)
    for launch_id, started_at in (
        ("launch-a", "2026-07-13T00:00:00+00:00"),
        ("launch-b", "2026-07-13T00:01:00+00:00"),
    ):
        manifest = launches / f"{launch_id}.json"
        manifest.write_text("{}", encoding="utf-8")
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=manifest,
            launch={
                "launch_id": launch_id,
                "repo_root": str(root),
                "started_at": started_at,
                "goal": launch_id,
            },
        )
    context_b_path = launch_context_path(workspace, "launch-b")
    pointer_path = active_launch_path(workspace)
    context_b_before = context_b_path.read_bytes()
    pointer_before = pointer_path.read_bytes()
    assert json.loads(pointer_before)["launch_id"] == "launch-b"
    monkeypatch.setenv("PACER_LAUNCH_ID", "launch-a")

    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": "continue environment-owned A",
                    "detail": "compact",
                },
            )
        )
    )

    assert memory["launch_id"] == "launch-a"
    active_a = read_active_launch(workspace, launch_id="launch-a")
    assert active_a["project_root"] == str(repo.resolve())
    assert active_a["current_goal"] == "continue environment-owned A"
    assert context_b_path.read_bytes() == context_b_before
    assert pointer_path.read_bytes() == pointer_before


def test_pacer_memory_receipt_reuses_once_per_launch_and_preserves_root_goal(tmp_path, monkeypatch) -> None:
    from visual_agent import project_memory
    from visual_agent.pacer_events import list_pacer_events
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-memory.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-memory", "repo_root": str(repo)},
    )
    history = workspace / "pacer_native" / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "recorded_at": "2026-07-13T00:00:00+00:00",
                "repo_root": str(repo.resolve()),
                "goal": "prior task",
                "summary": "prior evidence",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    real_build = project_memory.build_project_memory
    build_calls = []

    def counted_build(**kwargs):
        build_calls.append(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(project_memory, "build_project_memory", counted_build)
    first = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": "root development goal",
                    "detail": "full",
                },
            )
        )
    )
    receipt = first["memory_receipt"]
    reused = []
    reused_response_chars = []
    for goal in ("review child query", "tests child query"):
        result = asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": goal,
                    "known_memory_receipt": receipt,
                    "detail": "full",
                },
            )
        )
        reused_response_chars.append(len(result[0].text))
        reused.append(content_payload(result))

    assert first["status"] == "memory_loaded"
    assert first["cache_status"] == "miss"
    assert len(build_calls) == 1
    assert all(item["status"] == "memory_reused" for item in reused)
    assert all(item["memory_status"] == "not_modified" for item in reused)
    assert all(item["memory_receipt"] == receipt for item in reused)
    assert all(item["memory_goal_digest"] == first["memory_goal_digest"] for item in reused)
    assert len({item["query_goal_digest"] for item in reused}) == 2
    assert all("native_codex_history" not in item and "entries" not in item for item in reused)
    assert all(chars < 1600 for chars in reused_response_chars)
    active = read_active_launch(workspace)
    assert active["launch_goal"] == "root development goal"
    assert active["current_goal"] == "tests child query"
    assert active["query_goal"] == "tests child query"
    events = list_pacer_events(workspace, limit=10)
    assert [item["type"] for item in events] == [
        "task_source_baseline_captured",
        "memory_loaded",
        "memory_reused",
        "memory_reused",
    ]
    baseline_event, *memory_events = events
    assert {"digest", "kind", "complete", "file_count"} <= set(baseline_event["data"])
    for event in memory_events:
        assert {
            "payload_chars",
            "receipt",
            "cache_status",
            "memory_goal_digest",
            "query_goal_digest",
        } <= set(event["data"])
    assert len({event["data"]["memory_goal_digest"] for event in memory_events}) == 1
    assert len({event["data"]["query_goal_digest"] for event in memory_events}) == 3
    assert memory_events[0]["data"]["receipt"] == receipt
    assert memory_events[1]["data"]["payload_chars"] < 800


def test_pacer_memory_receipt_invalidates_for_history_and_project_instruction_changes(tmp_path, monkeypatch) -> None:
    from visual_agent import project_memory
    from visual_agent.pacer_launch_context import initialize_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-memory.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-memory", "repo_root": str(repo)},
    )
    history = workspace / "pacer_native" / "history.jsonl"
    history.write_text("", encoding="utf-8")
    real_build = project_memory.build_project_memory
    build_calls = []

    def counted_build(**kwargs):
        build_calls.append(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(project_memory, "build_project_memory", counted_build)
    root_goal = "root portfolio reliability"
    first = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": root_goal,
                    "detail": "full",
                },
            )
        )
    )
    history.write_text(
        json.dumps(
            {
                "recorded_at": "2026-07-13T00:00:00+00:00",
                "repo_root": str(repo.resolve()),
                "goal": "new history",
                "summary": "new evidence",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    after_history = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": "history review child",
                    "known_memory_receipt": first["memory_receipt"],
                    "detail": "full",
                },
            )
        )
    )
    after_missing_receipt = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": "receipt omitted child",
                    "detail": "full",
                },
            )
        )
    )
    (repo / "AGENTS.md").write_text("Use the project acceptance plan.\n", encoding="utf-8")
    after_instruction = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "goal": "instruction review child",
                    "known_memory_receipt": after_missing_receipt["memory_receipt"],
                    "detail": "full",
                },
            )
        )
    )

    assert len(build_calls) == 4
    assert [item["goal"] for item in build_calls] == [root_goal] * 4
    assert first["goal"] == root_goal
    assert after_history["status"] == "memory_loaded"
    assert after_history["cache_status"] == "invalidated"
    assert after_history["goal"] == root_goal
    assert after_history["memory_goal_digest"] == first["memory_goal_digest"]
    assert after_history["query_goal_digest"] != first["query_goal_digest"]
    assert after_history["memory_receipt"] != first["memory_receipt"]
    assert after_missing_receipt["status"] == "memory_loaded"
    assert after_missing_receipt["cache_status"] == "miss"
    assert after_missing_receipt["goal"] == root_goal
    assert after_missing_receipt["memory_receipt"] == after_history["memory_receipt"]
    assert after_instruction["status"] == "memory_loaded"
    assert after_instruction["cache_status"] == "invalidated"
    assert after_instruction["goal"] == root_goal
    assert after_instruction["memory_receipt"] != after_missing_receipt["memory_receipt"]


def test_pacer_memory_filters_unrelated_native_history_and_withholds_untrusted_hits(tmp_path) -> None:
    from visual_agent.mcp_server import get_pacer_memory_payload
    from visual_agent.pacer_launch_context import initialize_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-native-filter.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-native-filter", "repo_root": str(repo)},
    )
    history = workspace / "pacer_native" / "history.jsonl"
    history.write_text(
        "\n".join(
            json.dumps(item)
            for item in (
                {
                    "recorded_at": "2026-07-13T00:00:00+00:00",
                    "repo_root": str(repo.resolve()),
                    "goal": "fix checkout total rounding",
                    "summary": "model claimed this was useful",
                    "status": "completed",
                    "evidence_level": "self_reported",
                },
                {
                    "recorded_at": "2026-07-13T01:00:00+00:00",
                    "repo_root": str(repo.resolve()),
                    "goal": "translate a lunar astronomy almanac",
                    "summary": "unrelated model claim",
                    "status": "completed",
                    "evidence_level": "self_reported",
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    memory = get_pacer_memory_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "fix checkout total rounding",
            "detail": "full",
        }
    )

    assert memory["native_history_total"] == 2
    assert [item["goal"] for item in memory["native_codex_history"]] == ["fix checkout total rounding"]
    assert memory["native_codex_history"][0]["trust"]["trusted"] is False
    assert memory["lookup"]["lookup_hit"] is True
    assert memory["relevance"]["candidate_hit"] is True
    assert memory["relevance"]["relevant_hit"] is False
    assert memory["memory_injection"]["injected_hit"] is False
    assert memory["effective_memory"]["hit"] is False
    assert memory["pillars"]["memory"]["assessment"]["reason_codes"] == [
        "memory_relevance_unverified"
    ]


def test_pacer_memory_binds_trusted_used_ids_to_prior_injection_receipt(tmp_path) -> None:
    from visual_agent.mcp_server import get_pacer_memory_payload
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-memory-use.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-memory-use", "repo_root": str(repo)},
    )
    native = workspace / "pacer_native"
    run_id = "20260715-120000-checkout123"
    unrelated_run_id = "20260715-120100-almanac123"
    history = [
        {
            "recorded_at": "2026-07-15T12:00:00+00:00",
            "repo_root": str(repo.resolve()),
            "goal": "fix checkout total rounding",
            "summary": "verified checkout evidence",
            "verification": f"run_id={run_id}",
            "status": "completed",
            "evidence_level": "verified_batch",
            "batch_run_id": run_id,
        },
        {
            "recorded_at": "2026-07-15T12:01:00+00:00",
            "repo_root": str(repo.resolve()),
            "goal": "translate a lunar astronomy almanac",
            "summary": "verified but unrelated",
            "verification": f"run_id={unrelated_run_id}",
            "status": "completed",
            "evidence_level": "verified_batch",
            "batch_run_id": unrelated_run_id,
        },
    ]
    (native / "history.jsonl").write_text(
        "\n".join(json.dumps(item) for item in history) + "\n",
        encoding="utf-8",
    )
    for batch_run_id in (run_id, unrelated_run_id):
        run_dir = native / "commands" / batch_run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            json.dumps(trusted_verification_summary(batch_run_id)),
            encoding="utf-8",
        )

    first = get_pacer_memory_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "fix checkout total rounding",
            "detail": "full",
        }
    )
    memory_id = f"pacer-native:{run_id}"

    assert [item["goal"] for item in first["native_codex_history"]] == ["fix checkout total rounding"]
    assert first["response_cache"] == {"status": "miss", "reused": False}
    assert first["relevance"]["retrieved_memory_ids"] == [memory_id]
    assert first["memory_injection"]["memory_ids"] == [memory_id]
    assert first["memory_use"]["used_hit"] is False
    assert first["effective_memory"]["hit"] is True
    assert first["pillars"]["memory"]["assessment"]["reason_codes"] == ["memory_use_unverified"]

    reused = get_pacer_memory_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "known_memory_receipt": first["memory_receipt"],
            "memory_ids_used": [memory_id],
            "detail": "full",
        }
    )

    assert reused["response_cache"] == {"status": "hit", "reused": True}
    assert reused["memory_use"]["used_hit"] is True
    assert reused["memory_use"]["memory_ids_used"] == [memory_id]
    assert reused["effective_memory"]["retrieved_memory_ids"] == [memory_id]
    assert reused["effective_memory"]["injected_memory_ids"] == [memory_id]
    assert reused["effective_memory"]["memory_ids_used"] == [memory_id]
    assert reused["memory_assessment"]["status"] == "passed"
    active_memory = read_active_launch(workspace)["pillars"]["memory"]
    assert active_memory["retrieved_memory_ids"] == [memory_id]
    assert active_memory["injected_memory_ids"] == [memory_id]
    assert active_memory["memory_ids_used"] == [memory_id]

    with pytest.raises(ValueError, match="not delivered as trusted memory"):
        get_pacer_memory_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "known_memory_receipt": first["memory_receipt"],
                "memory_ids_used": ["pacer-native:20260715-999999-unknown123"],
                "detail": "full",
            }
        )


def test_pacer_native_memory_normalizes_repo_root_and_dot_workspace(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    recorded = content_payload(
        asyncio.run(
            call_tool(
                "record_pacer_outcome",
                {
                    "workspace_root": str(repo),
                    "repo_root": str(repo),
                    "goal": "keep memory repo-local",
                    "summary": "normalized workspace",
                    "verification": "review evidence",
                    "status": "failed",
                },
            )
        )
    )
    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {"workspace_root": ".", "repo_root": str(repo)},
            )
        )
    )

    expected_history = repo / ".agent-workspace" / "pacer_native" / "history.jsonl"
    assert Path(recorded["path"]) == expected_history.resolve()
    assert memory["native_codex_history"][-1]["goal"] == "keep memory repo-local"
    assert not (repo / "pacer_native").exists()


def test_pacer_native_memory_merges_legacy_repo_history_read_only(tmp_path) -> None:
    repo = tmp_path / "repo"
    workspace_history = repo / ".agent-workspace" / "pacer_native" / "history.jsonl"
    legacy_history = repo / "pacer_native" / "history.jsonl"
    other_repo = tmp_path / "other"
    workspace_history.parent.mkdir(parents=True)
    legacy_history.parent.mkdir(parents=True)
    standard_entry = {
        "recorded_at": "2026-07-13T10:00:00+00:00",
        "repo_root": str(repo.resolve()),
        "goal": "standard",
        "summary": "from standard workspace",
        "verification": "verified",
        "status": "completed",
    }
    legacy_entry = {
        **standard_entry,
        "recorded_at": "2026-07-13T09:00:00Z",
        "goal": "legacy",
        "summary": "from split workspace",
    }
    foreign_entry = {**standard_entry, "repo_root": str(other_repo.resolve()), "goal": "foreign"}
    workspace_history.write_text(json.dumps(standard_entry, ensure_ascii=False) + "\n", encoding="utf-8")
    legacy_content = "\n".join(
        json.dumps(item, ensure_ascii=False) for item in (standard_entry, foreign_entry, legacy_entry)
    ) + "\n"
    legacy_history.write_text(legacy_content, encoding="utf-8")

    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {"workspace_root": ".", "repo_root": str(repo), "detail": "full"},
            )
        )
    )

    assert [item["goal"] for item in memory["native_codex_history"]] == ["legacy", "standard"]
    assert memory["native_history_total"] == 2
    assert legacy_history.read_text(encoding="utf-8") == legacy_content


def test_run_pacer_commands_normalizes_repo_workspace(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    result = content_payload(
        asyncio.run(
            call_tool(
                "run_pacer_commands",
                {
                    "workspace_root": str(repo),
                    "repo_root": str(repo),
                    "steps": [
                        {
                            "name": "probe",
                            "argv": [sys.executable, "-c", "print('workspace-ok')"],
                            "timeout_seconds": 10,
                        }
                    ],
                },
            )
        )
    )

    expected_commands = (repo / ".agent-workspace" / "pacer_native" / "commands").resolve()
    assert result["status"] == "passed"
    assert Path(result["run_dir"]).parent == expected_commands
    assert not (repo / "pacer_native").exists()


def test_run_pacer_commands_runs_all_steps_by_default_and_can_stop_early(tmp_path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / ".agent-workspace"
    repo.mkdir()
    tool = next(item for item in mcp_tools() if item.name == "run_pacer_commands")
    steps = [
        {"name": "failure", "argv": [sys.executable, "-c", "raise SystemExit(3)"]},
        {"name": "success", "argv": [sys.executable, "-c", "print('still-ran')"]},
    ]

    default_result = content_payload(
        asyncio.run(
            call_tool(
                "run_pacer_commands",
                {"workspace_root": str(workspace), "repo_root": str(repo), "steps": steps},
            )
        )
    )
    stop_result = content_payload(
        asyncio.run(
            call_tool(
                "run_pacer_commands",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "steps": steps,
                    "stop_on_failure": True,
                },
            )
        )
    )

    assert tool.inputSchema["properties"]["stop_on_failure"]["default"] is False
    assert default_result["status"] == "failed"
    assert default_result["requested_steps"] == default_result["executed_steps"] == 2
    assert default_result["skipped_steps"] == []
    assert [item["status"] for item in default_result["records"]] == ["failed", "passed"]
    assert stop_result["status"] == "failed"
    assert stop_result["requested_steps"] == 2
    assert stop_result["executed_steps"] == 1
    assert stop_result["skipped_steps"] == ["success"]
    assert stop_result["executed_steps"] + len(stop_result["skipped_steps"]) == stop_result["requested_steps"]
    assert [item["status"] for item in stop_result["records"]] == ["failed"]


def test_pacer_native_preserves_explicit_custom_workspace(tmp_path) -> None:
    repo = tmp_path / "repo"
    custom_workspace = tmp_path / "custom-pacer-data"
    legacy_history = repo / "pacer_native" / "history.jsonl"
    repo.mkdir()

    recorded = content_payload(
        asyncio.run(
            call_tool(
                "record_pacer_outcome",
                {
                    "workspace_root": str(custom_workspace),
                    "repo_root": str(repo),
                    "goal": "custom workspace",
                    "summary": "kept custom location",
                    "verification": "review evidence",
                    "status": "failed",
                },
            )
        )
    )
    legacy_history.parent.mkdir(parents=True)
    legacy_history.write_text(
        json.dumps(
            {
                "recorded_at": "2026-07-13T09:00:00+00:00",
                "repo_root": str(repo.resolve()),
                "goal": "legacy outside custom workspace",
                "summary": "must remain isolated",
                "verification": "review evidence",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {"workspace_root": str(custom_workspace), "repo_root": str(repo), "detail": "full"},
            )
        )
    )

    expected_history = custom_workspace / "pacer_native" / "history.jsonl"
    assert Path(recorded["path"]) == expected_history.resolve()
    assert [item["goal"] for item in memory["native_codex_history"]] == ["custom workspace"]
    assert not (repo / ".agent-workspace" / "pacer_native" / "history.jsonl").exists()


def test_pacer_native_resolves_relative_custom_workspace_from_repo_root(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(tmp_path)

    recorded = content_payload(
        asyncio.run(
            call_tool(
                "record_pacer_outcome",
                {
                    "workspace_root": "custom-pacer-data",
                    "repo_root": str(repo),
                    "goal": "relative custom workspace",
                    "summary": "resolved against repo",
                    "verification": "review evidence",
                    "status": "failed",
                },
            )
        )
    )

    expected = (repo / "custom-pacer-data" / "pacer_native" / "history.jsonl").resolve()
    assert Path(recorded["path"]) == expected
    assert not (tmp_path / "custom-pacer-data").exists()


def test_legacy_outcome_cannot_activate_completion_even_with_verified_batch(tmp_path) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    workspace = tmp_path / ".agent-workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    batch = run_pacer_verification_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "steps": [passing_unittest_step(repo)],
    })
    run_id = str(batch["run_id"])

    recorded = content_payload(
        asyncio.run(
            call_tool(
                "record_pacer_outcome",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                        "goal": "verified task",
                        "summary": "implemented",
                        "verification": f"Pacer batch run_id={run_id}, 1/1 passed",
                        "verification_receipt": batch["verification_receipt"],
                        "status": "completed",
                },
            )
        )
    )
    memory = content_payload(
        asyncio.run(call_tool("get_pacer_memory", {"workspace_root": str(workspace), "repo_root": str(repo)}))
    )

    assert "completed outcomes must use complete_pacer_task" in recorded["error"]
    assert memory["five_pillars_active"] is False
    assert memory["native_codex_history"] == []


def test_manual_trusted_summary_cannot_record_completed_outcome(tmp_path) -> None:
    from visual_agent.mcp_server import record_pacer_outcome_payload
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    launch_id = "launch-manual"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": launch_id, "repo_root": str(repo)},
    )
    run_id = "20260714-120000-manual123"
    run_dir = workspace / "pacer_native" / "commands" / run_id
    run_dir.mkdir(parents=True)
    forged = trusted_verification_summary(run_id, launch_id=launch_id)
    (run_dir / "summary.json").write_text(json.dumps(forged), encoding="utf-8")

    with pytest.raises(ValueError, match="completed outcomes must use complete_pacer_task"):
        record_pacer_outcome_payload({
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "forged completion",
            "summary": "must not close acceptance",
            "verification": f"run_id={run_id}",
            "status": "completed",
        })

    assert not (workspace / "pacer_native" / "history.jsonl").exists()
    pillars = read_active_launch(workspace, launch_id=launch_id)["pillars"]
    assert pillars["acceptance"]["active"] is False
    assert pillars["managed"]["active"] is False
    assert pillars["dogfood"]["active"] is False


def test_generic_command_batch_cannot_activate_delivery_pillars(tmp_path) -> None:
    from visual_agent.mcp_server import record_pacer_outcome_payload, run_pacer_commands_payload
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-1", "repo_root": str(repo)},
    )
    batch = run_pacer_commands_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "steps": [{"name": "simple-pass", "argv": [sys.executable, "-c", "print('pass')"]}],
    })

    assert batch["kind"] == "pacer_command_batch"
    with pytest.raises(ValueError, match="completed outcomes must use complete_pacer_task"):
        record_pacer_outcome_payload({
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "must not fake completion",
            "summary": "a simple command passed",
            "verification": f"run_id={batch['run_id']}",
            "status": "completed",
        })
    pillars = read_active_launch(workspace, launch_id="launch-1")["pillars"]
    assert pillars["managed"]["active"] is False
    assert pillars["acceptance"]["active"] is False
    assert pillars["dogfood"]["active"] is False


def test_completed_outcome_without_launch_still_requires_complete_verification_summary(tmp_path) -> None:
    from visual_agent.mcp_server import record_pacer_outcome_payload

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    run_id = "20260713-120500-incomplete"
    run_dir = workspace / "pacer_native" / "commands" / run_id
    run_dir.mkdir(parents=True)
    incomplete = trusted_verification_summary(run_id)
    incomplete.pop("requested_steps")
    (run_dir / "summary.json").write_text(json.dumps(incomplete), encoding="utf-8")

    with pytest.raises(ValueError, match="completed outcomes must use complete_pacer_task"):
        record_pacer_outcome_payload({
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "incomplete evidence",
            "summary": "must not be recorded as verified",
            "verification": f"run_id={run_id}",
            "status": "completed",
        })


def test_verification_validator_rederives_step_classes_from_commands() -> None:
    payload = trusted_verification_summary("20260713-120600-tampered")
    payload["step_classes"] = ["compile"]

    validation = validate_pacer_verification_batch(payload)

    assert validation.valid is False
    assert validation.step_classes == ("test",)
    assert "step_classes_mismatch" in validation.errors
    assert "trusted_workspace_required" in validation.errors


def test_compile_only_summary_cannot_satisfy_acceptance() -> None:
    payload = trusted_verification_summary("20260713-120700-compileonly")
    command = [sys.executable, "-m", "compileall", "-q", "."]
    payload["step_classes"] = ["compile"]
    payload["records"] = [{"name": "compile", "status": "passed", "exit_code": 0, "command": command}]

    validation = validate_pacer_verification_batch(payload)

    assert validation.valid is False
    assert validation.step_classes == ("compile",)
    assert "behavioral_verification_required" in validation.errors


def test_complete_documentation_task_accepts_requested_compileall(tmp_path, monkeypatch) -> None:
    from visual_agent.mcp_server import complete_pacer_task_payload
    from visual_agent.task_review import build_task_contract

    goal = (
        "更新 README.md，增加 Usage 小节，写明 python app.py 启动命令，只修改该文档"
        "并使用 python -m compileall -q app.py 验证。"
    )
    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-documentation-compile",
        goal=goal,
        initial_files={
            "README.md": "# Sample App\n\nA minimal command-line sample.\n",
            "app.py": "def main():\n    return 0\n",
        },
    )
    (repo / "README.md").write_text(
        "# Sample App\n\nA minimal command-line sample.\n\n"
        "## Usage\n\n```bash\npython app.py\n```\n",
        encoding="utf-8",
    )
    contract = build_task_contract(goal)
    claims = [
        {
            "kind": "change",
            "requirement_ids": [requirement["id"]],
            "requirement": requirement["text"],
            "result": "README Usage 文档已更新，并写明 python app.py 启动命令。",
            "files": [{"path": "README.md", "state": "modified"}],
            "verification_steps": ["compile-app"],
        }
        for requirement in contract["requirements"]
    ]

    payload = complete_pacer_task_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": goal,
            "summary": "README 已增加 Usage 小节和 python app.py 启动命令。",
            "completion_evidence": {
                "result_kind": "change",
                "claims": claims,
                "unresolved_items": [],
                "known_risks": [],
            },
            "steps": [
                {
                    "name": "compile-app",
                    "argv": [sys.executable, "-m", "compileall", "-q", "app.py"],
                }
            ],
        }
    )

    assert payload["status"] == "completed"
    assert payload["verification"]["records"][0]["step_class"] == "compile"
    assert payload["task_review"]["trust"] == "yes"
    assert payload["five_pillars_active"] is False
    assert payload["five_pillars_assessment"]["pillars"]["acceptance"]["status"] == "passed"
    assert payload["pillars"]["acceptance"]["digest_verified"] is True


def test_complete_pacer_task_success_does_not_overclaim_unproven_pillars(tmp_path, monkeypatch) -> None:
    from visual_agent.mcp_server import complete_pacer_task_payload

    workspace, repo = active_completion_context(tmp_path, monkeypatch)
    payload = complete_pacer_task_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "goal": "test the project",
        "summary": "project tests verified",
        "completion_evidence": completion_evidence("test the project"),
        "steps": [passing_unittest_step(repo)],
    })

    assert payload["status"] == "completed"
    assert payload["launch_id"] == "launch-complete"
    assert payload["run_id"] == payload["verification"]["run_id"]
    assert payload["verification"]["kind"] == PACER_VERIFICATION_BATCH_KIND
    assert payload["verification"]["passed"] == 1
    assert payload["task_review"]["valid"] is True
    assert payload["task_review"]["verdict"] == "approved"
    assert payload["task_review"]["user_report"]["completed"] == ["test the project verified"]
    assert payload["five_pillars_active"] is False
    assert payload["five_pillars_assessment"]["status"] == "partial"
    assert payload["five_pillars_assessment"]["pillars"]["acceptance"]["status"] == "partial"
    assert payload["five_pillars_assessment"]["pillars"]["dogfood"]["status"] == "partial"
    assert payload["outcome"] == {
        "status": "recorded",
        "outcome_status": "completed",
        "evidence_level": "verified_batch",
        "batch_run_id": payload["run_id"],
        "launch_id": "launch-complete",
    }
    assert "stdout_tail" not in json.dumps(payload)
    persisted = json.loads(
        (workspace / "pacer_native" / "commands" / payload["run_id"] / "summary.json").read_text(encoding="utf-8")
    )
    assert persisted["source_tool"] == PACER_VERIFICATION_SOURCE_TOOL
    history = [json.loads(line) for line in (workspace / "pacer_native" / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(history) == 1
    assert history[-1]["batch_run_id"] == payload["run_id"]
    assert history[-1]["launch_id"] == "launch-complete"
    assert history[-1]["task_review"]["verdict"] == "approved"


def test_complete_pacer_task_accepts_minimal_semantic_claim(tmp_path, monkeypatch) -> None:
    from visual_agent.mcp_server import complete_pacer_task_payload
    from visual_agent.task_review import build_task_contract

    goal = "run existing tests with server-derived completion evidence"
    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-minimal-evidence",
        goal=goal,
    )
    requirement_id = build_task_contract(goal)["requirements"][0]["id"]

    payload = complete_pacer_task_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": goal,
            "summary": "existing tests passed through the trusted verification runner",
            "completion_evidence": {
                "claims": [
                    {
                        "requirement_ids": [requirement_id],
                        "result": "existing project tests passed",
                        "verification_steps": ["unittest"],
                    }
                ],
                "unresolved_items": [],
                "known_risks": [],
            },
            "steps": [passing_unittest_step(repo)],
        }
    )

    assert payload["status"] == "completed"
    assert payload["task_review"]["evidence_origin"] == "server_derived"
    assert payload["task_review"]["source_changes"] == []
    assert payload["task_review"]["legacy_fields_ignored"] == []


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [
        ("tampered_file", "trusted_source_baseline_digest_mismatch"),
        ("missing_receipt", "trusted_source_baseline_receipt_required"),
        ("process_restart", "trusted_source_baseline_not_registered"),
    ],
)
def test_complete_pacer_task_rejects_untrusted_source_baseline_before_verification(
    tmp_path,
    monkeypatch,
    failure_mode,
    expected_error,
) -> None:
    from visual_agent import mcp_server, pacer_launch_context
    from visual_agent.pacer_launch_context import (
        task_source_baseline_path,
        update_active_launch,
    )

    launch_id = f"launch-baseline-{failure_mode}"
    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id=launch_id,
        goal="test the project",
    )
    step = passing_unittest_step(repo)
    if failure_mode == "tampered_file":
        path = task_source_baseline_path(workspace, launch_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["captured_at"] = "tampered"
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif failure_mode == "missing_receipt":
        update_active_launch(
            workspace,
            expected_launch_id=launch_id,
            source_baseline_receipt="",
        )
    else:
        registry = pacer_launch_context._TRUSTED_TASK_SOURCE_BASELINES
        monkeypatch.setattr(
            pacer_launch_context,
            "_TRUSTED_TASK_SOURCE_BASELINES",
            type(registry)(),
        )
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_verification_payload",
        lambda _args: (_ for _ in ()).throw(AssertionError("verification must not run")),
    )

    with pytest.raises(ValueError, match=expected_error):
        mcp_server.complete_pacer_task_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "goal": "test the project",
                "summary": "project tests verified with trusted evidence",
                "completion_evidence": completion_evidence("test the project"),
                "steps": [step],
            }
        )


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [
        ("tampered_contract", "trusted_task_contract_registered_digest_mismatch"),
        ("tampered_goal_and_contract", "trusted_task_contract_goal_mismatch"),
        ("missing_receipt", "trusted_task_contract_receipt_required"),
        ("process_restart", "trusted_task_contract_not_registered"),
    ],
)
def test_complete_pacer_task_rejects_untrusted_task_contract_before_verification(
    tmp_path,
    monkeypatch,
    failure_mode,
    expected_error,
) -> None:
    from visual_agent import mcp_server, pacer_launch_context
    from visual_agent.pacer_launch_context import (
        read_active_launch,
        task_contract_digest,
        write_active_launch,
    )
    from visual_agent.task_review import build_task_contract

    launch_id = f"launch-contract-{failure_mode}"
    original_goal = "test the project"
    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id=launch_id,
        goal=original_goal,
    )
    step = passing_unittest_step(repo)
    active = read_active_launch(workspace, launch_id=launch_id)
    completion_goal = original_goal
    if failure_mode == "tampered_contract":
        contract = json.loads(json.dumps(active["task_contract"]))
        contract["requirements"][0]["text"] = "different requirement"
        active["task_contract"] = contract
        active["task_contract_digest"] = task_contract_digest(contract)
        write_active_launch(workspace, active)
    elif failure_mode == "tampered_goal_and_contract":
        completion_goal = "run different existing tests"
        contract = build_task_contract(completion_goal)
        active["launch_goal"] = completion_goal
        active["task_contract"] = contract
        active["task_contract_digest"] = task_contract_digest(contract)
        write_active_launch(workspace, active)
    elif failure_mode == "missing_receipt":
        active["task_contract_receipt"] = ""
        write_active_launch(workspace, active)
    else:
        registry = pacer_launch_context._TRUSTED_TASK_CONTRACTS
        monkeypatch.setattr(
            pacer_launch_context,
            "_TRUSTED_TASK_CONTRACTS",
            type(registry)(),
        )
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_verification_payload",
        lambda _args: (_ for _ in ()).throw(AssertionError("verification must not run")),
    )

    with pytest.raises(ValueError, match=expected_error):
        mcp_server.complete_pacer_task_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "goal": completion_goal,
                "summary": "project tests verified with trusted task contract",
                "completion_evidence": completion_evidence(completion_goal),
                "steps": [step],
            }
        )

    active = read_active_launch(workspace, launch_id=launch_id)
    assert active["pillars"]["acceptance"]["active"] is False
    assert not (workspace / "pacer_native" / "commands").exists()


def test_complete_pacer_task_rechecks_source_baseline_after_verification(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server
    from visual_agent.pacer_launch_context import task_source_baseline_path
    from visual_agent.task_review import build_task_contract

    goal = "run regression tests"
    launch_id = "launch-baseline-post-verification"
    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id=launch_id,
        goal=goal,
    )
    step = passing_unittest_step(repo)
    evidence = completion_evidence(goal)
    requirement_id = build_task_contract(goal)["requirements"][0]["id"]
    evidence["claims"][0]["requirement_ids"] = [requirement_id]
    real_verification = mcp_server.run_pacer_verification_payload

    def verify_then_tamper(args):
        result = real_verification(args)
        path = task_source_baseline_path(workspace, launch_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["captured_at"] = "tampered-after-verification"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return result

    monkeypatch.setattr(mcp_server, "run_pacer_verification_payload", verify_then_tamper)

    with pytest.raises(ValueError, match="trusted_source_baseline_digest_mismatch"):
        mcp_server.complete_pacer_task_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "goal": goal,
                "summary": "regression tests executed with focused unittest evidence",
                "completion_evidence": evidence,
                "steps": [step],
            }
        )

    assert (workspace / "pacer_native" / "commands").exists()
    assert not (workspace / "pacer_native" / "history.jsonl").exists()


def test_complete_pacer_task_rejects_goal_drift_before_running_verification(
    tmp_path,
    monkeypatch,
) -> None:
    from visual_agent import mcp_server

    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-goal-audit",
        goal="fix the login failure",
    )
    step = passing_unittest_step(repo)
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_verification_payload",
        lambda _args: (_ for _ in ()).throw(AssertionError("verification must not run")),
    )

    with pytest.raises(ValueError, match="completion audit rejected"):
        mcp_server.complete_pacer_task_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "goal": "build a payment page",
                "summary": "payment page implemented",
                "completion_evidence": completion_evidence("build a payment page"),
                "steps": [step],
            }
        )

    assert not (workspace / "pacer_native" / "commands").exists()
    assert not (workspace / "pacer_native" / "history.jsonl").exists()


def test_complete_pacer_task_failure_records_outcome_and_short_tail(tmp_path, monkeypatch) -> None:
    from visual_agent.mcp_server import complete_pacer_task_payload

    failing_test = (
        "import unittest\n\n"
        "class FailureTest(unittest.TestCase):\n"
        "    def test_fails(self):\n"
        "        self.assertEqual('actual', 'expected')\n"
    )
    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-failure",
        goal="run failing regression",
        initial_files={"tests/test_failure.py": failing_test},
    )
    payload = complete_pacer_task_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "goal": "run failing regression",
        "summary": "verification failure captured",
        "completion_evidence": completion_evidence(
            "run failing regression",
            path="tests/test_failure.py",
        ),
        "steps": [
            {
                "name": "unittest",
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            }
        ],
        "tail_chars": 2000,
    })

    assert payload["status"] == "failed"
    assert payload["verification"]["failed"] == 1
    assert payload["outcome"]["outcome_status"] == "failed"
    assert payload["outcome"]["evidence_level"] == "verified_failed_batch"
    assert payload["five_pillars_active"] is False
    assert payload["task_review"]["valid"] is False
    assert payload["task_review"]["user_report"]["can_trust"] == "no"
    record = payload["verification"]["records"][0]
    assert record["status"] == "failed"
    assert 0 < len(record["decisive_tail"]) <= 600
    assert record["logs"]["stderr"].endswith(".stderr.log")
    history = [json.loads(line) for line in (workspace / "pacer_native" / "history.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(history) == 1
    assert history[-1]["status"] == "failed"
    assert history[-1]["batch_run_id"] == payload["run_id"]
    assert history[-1]["task_review"]["verdict"] == "rejected"


def test_complete_pacer_task_pins_launch_across_active_pointer_drift(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch

    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-a",
        goal="run existing tests while staying on launch a",
    )
    real_telemetry = mcp_server.get_pacer_runtime_telemetry_payload
    drifted = False

    def drift_pointer_then_read(args):
        nonlocal drifted
        if not drifted:
            drifted = True
            manifest = workspace / "pacer_native" / "launches" / "launch-b.json"
            manifest.write_text("{}", encoding="utf-8")
            initialize_active_launch(
                workspace_root=workspace,
                manifest_path=manifest,
                launch={"launch_id": "launch-b", "repo_root": str(repo)},
            )
        return real_telemetry(args)

    monkeypatch.setattr(mcp_server, "get_pacer_runtime_telemetry_payload", drift_pointer_then_read)
    payload = mcp_server.complete_pacer_task_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "goal": "run existing tests while staying on launch a",
        "summary": "launch isolation verified",
        "completion_evidence": completion_evidence("run existing tests while staying on launch a"),
        "steps": [passing_unittest_step(repo)],
    })

    assert drifted is True
    assert payload["launch_id"] == "launch-a"
    assert payload["outcome"]["launch_id"] == "launch-a"
    launch_a_acceptance = read_active_launch(workspace, launch_id="launch-a")["pillars"]["acceptance"]
    assert launch_a_acceptance["active"] is False
    assert launch_a_acceptance["assessment"]["status"] == "partial"
    assert read_active_launch(workspace, launch_id="launch-b")["pillars"]["acceptance"]["active"] is False


def test_complete_pacer_task_missing_owned_launch_never_falls_back_to_running_pointer(
    tmp_path,
    monkeypatch,
) -> None:
    from visual_agent import mcp_server
    from visual_agent.pacer_launch_context import launch_context_path, read_active_launch

    workspace, repo = active_completion_context(tmp_path, monkeypatch, launch_id="existing-launch")
    existing_context = launch_context_path(workspace, "existing-launch")
    before = existing_context.read_bytes()
    monkeypatch.setenv("PACER_LAUNCH_ID", "missing-launch")

    with pytest.raises(ValueError, match="active Pacer launch"):
        mcp_server.complete_pacer_task_payload({
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "must not use another launch",
            "summary": "no outcome should be recorded",
            "steps": [passing_unittest_step(repo)],
        })

    assert existing_context.read_bytes() == before
    assert read_active_launch(workspace, launch_id="existing-launch")["pillars"]["acceptance"]["active"] is False
    assert not (workspace / "pacer_native" / "commands").exists()
    assert not (workspace / "pacer_native" / "history.jsonl").exists()


@pytest.mark.parametrize("drift_target", ["before_verification", "during_verification"])
def test_complete_pacer_task_pins_launch_before_verification_starts(
    tmp_path,
    monkeypatch,
    drift_target,
) -> None:
    from visual_agent import mcp_server
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch

    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-a",
        goal="run existing tests while pinning launch before any verification work",
    )
    drifted = False

    def drift_pointer() -> None:
        nonlocal drifted
        if drifted:
            return
        drifted = True
        manifest = workspace / "pacer_native" / "launches" / "launch-b.json"
        manifest.write_text("{}", encoding="utf-8")
        initialize_active_launch(
            workspace_root=workspace,
            manifest_path=manifest,
            launch={"launch_id": "launch-b", "repo_root": str(repo)},
        )

    if drift_target == "before_verification":
        real_verification = mcp_server.run_pacer_verification_payload

        def drift_then_verify(args):
            drift_pointer()
            return real_verification(args)

        monkeypatch.setattr(mcp_server, "run_pacer_verification_payload", drift_then_verify)
    else:
        real_commands = mcp_server.run_pacer_commands_payload

        def drift_during_verification(args):
            drift_pointer()
            return real_commands(args)

        monkeypatch.setattr(mcp_server, "run_pacer_commands_payload", drift_during_verification)

    payload = mcp_server.complete_pacer_task_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "goal": "run existing tests while pinning launch before any verification work",
        "summary": "all completion stages stayed on launch a",
        "completion_evidence": completion_evidence(
            "run existing tests while pinning launch before any verification work"
        ),
        "steps": [passing_unittest_step(repo)],
    })

    assert drifted is True
    assert payload["launch_id"] == "launch-a"
    assert payload["verification"]["run_id"] == payload["run_id"]
    launch_a_acceptance = read_active_launch(workspace, launch_id="launch-a")["pillars"]["acceptance"]
    assert launch_a_acceptance["active"] is False
    assert launch_a_acceptance["assessment"]["status"] == "partial"
    assert read_active_launch(workspace, launch_id="launch-b")["pillars"]["acceptance"]["active"] is False


def test_complete_pacer_task_success_response_stays_small_with_large_test_output(tmp_path, monkeypatch) -> None:
    verbose_test = (
        "import unittest\n\n"
        "class VerboseTest(unittest.TestCase):\n"
        "    def test_verbose(self):\n"
        "        print('X' * 100000)\n"
        "        self.assertTrue(True)\n"
    )
    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-size",
        goal="run existing tests with compact response verification",
        initial_files={"tests/test_verbose.py": verbose_test},
    )
    result = asyncio.run(
        call_tool(
            "complete_pacer_task",
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "goal": "run existing tests with compact response verification",
                "summary": "large output retained only in local logs",
                "completion_evidence": completion_evidence(
                    "run existing tests with compact response verification",
                    path="tests/test_verbose.py",
                ),
                "steps": [
                    {
                        "name": "unittest",
                        "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                    }
                ],
                "tail_chars": 2000,
            },
        )
    )
    payload = content_payload(result)

    assert payload["status"] == "completed"
    assert len(result[0].text) < 6000
    assert "X" * 100 not in result[0].text
    assert "stdout_tail" not in result[0].text
    assert "decisive_tail" not in result[0].text


def test_complete_pacer_task_rejects_generic_command_batch(tmp_path, monkeypatch) -> None:
    from visual_agent import mcp_server

    workspace, repo = active_completion_context(
        tmp_path,
        monkeypatch,
        launch_id="launch-generic",
        goal="read-only review rejecting generic command batch",
    )
    monkeypatch.setattr(
        mcp_server,
        "run_pacer_verification_payload",
        lambda _args: {
            "kind": "pacer_command_batch",
            "run_id": "20260714-120000-generic",
            "launch_id": "launch-1",
            "status": "passed",
        },
    )
    with pytest.raises(ValueError, match="rejects non-verification command batches"):
        mcp_server.complete_pacer_task_payload({
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "read-only review rejecting generic command batch",
            "summary": "must not record",
            "completion_evidence": completion_evidence(
                "read-only review rejecting generic command batch",
                path="",
                step_name="fake",
                kind="review",
            ),
            "steps": [{"name": "fake", "argv": [sys.executable, "-m", "pytest", "-q"]}],
        })


@pytest.mark.parametrize(
    "summary_payload",
    [
        None,
        {"status": "failed", "executed_steps": 2, "passed": 1, "failed": 1},
        {"status": "passed", "executed_steps": 0, "passed": 0},
        {"status": "passed", "executed_steps": "not-an-integer", "passed": 1},
    ],
)
def test_pacer_memory_revalidates_verified_batch_summary(tmp_path, summary_payload) -> None:
    repo = tmp_path / "repo"
    workspace = repo / ".agent-workspace"
    native = workspace / "pacer_native"
    repo.mkdir()
    native.mkdir(parents=True)
    run_id = "20260713-121000-recheck123"
    entry = {
        "recorded_at": "2026-07-13T12:11:00+00:00",
        "repo_root": str(repo.resolve()),
        "goal": "do not trust history flags",
        "summary": "claimed verified",
        "verification": f"run_id={run_id}",
        "status": "completed",
        "evidence_level": "verified_batch",
        "batch_run_id": run_id,
    }
    (native / "history.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    if summary_payload is not None:
        run_dir = native / "commands" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(json.dumps(summary_payload), encoding="utf-8")

    memory = content_payload(
        asyncio.run(call_tool("get_pacer_memory", {"workspace_root": ".agent-workspace", "repo_root": str(repo)}))
    )

    assert memory["native_codex_history"][-1]["evidence_level"] == "verified_batch"
    assert memory["five_pillars_active"] is False


def test_pacer_memory_canonical_summary_wins_but_conflicting_legacy_summary_blocks_activation(tmp_path) -> None:
    repo = tmp_path / "repo"
    canonical = repo / ".agent-workspace" / "pacer_native"
    legacy = repo / "pacer_native"
    repo.mkdir()
    canonical.mkdir(parents=True)
    legacy.mkdir(parents=True)
    run_id = "20260713-122000-conflict123"
    entry = {
        "recorded_at": "2026-07-13T12:20:00+00:00",
        "repo_root": str(repo.resolve()),
        "goal": "canonical evidence",
        "summary": "verified",
        "verification": f"run_id={run_id}",
        "status": "completed",
        "evidence_level": "verified_batch",
        "batch_run_id": run_id,
    }
    for native in (canonical, legacy):
        (native / "history.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    canonical_run = canonical / "commands" / run_id
    canonical_run.mkdir(parents=True)
    (canonical_run / "summary.json").write_text(
        json.dumps({"status": "passed", "executed_steps": 2, "passed": 2}), encoding="utf-8"
    )

    canonical_only = content_payload(
        asyncio.run(call_tool("get_pacer_memory", {"workspace_root": ".agent-workspace", "repo_root": str(repo)}))
    )
    assert canonical_only["five_pillars_active"] is False

    (legacy / "history.jsonl").write_text(
        json.dumps({**entry, "goal": "conflicting legacy goal"}) + "\n", encoding="utf-8"
    )
    conflicting_history = content_payload(
        asyncio.run(call_tool("get_pacer_memory", {"workspace_root": ".agent-workspace", "repo_root": str(repo)}))
    )
    assert conflicting_history["five_pillars_active"] is False
    (legacy / "history.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")

    legacy_run = legacy / "commands" / run_id
    legacy_run.mkdir(parents=True)
    (legacy_run / "summary.json").write_text(
        json.dumps({"status": "failed", "executed_steps": 2, "passed": 1, "failed": 1}), encoding="utf-8"
    )
    conflicting = content_payload(
        asyncio.run(call_tool("get_pacer_memory", {"workspace_root": ".agent-workspace", "repo_root": str(repo)}))
    )

    assert conflicting["native_history_total"] == 1
    assert conflicting["five_pillars_active"] is False


@pytest.mark.parametrize("outcome_status", ["failed", "blocked"])
def test_pacer_failed_outcome_accepts_verified_failed_batch(tmp_path, outcome_status: str) -> None:
    from visual_agent.mcp_server import run_pacer_verification_payload

    workspace = tmp_path / ".agent-workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_failure.py").write_text(
        "import unittest\n\n"
        "class FailureTest(unittest.TestCase):\n"
        "    def test_fails(self):\n"
        "        self.fail('expected failure')\n",
        encoding="utf-8",
    )
    batch = run_pacer_verification_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "steps": [
            {
                "name": "unittest",
                "argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            }
        ],
    })
    run_id = str(batch["run_id"])
    args = {
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "goal": "failed task",
        "summary": "verification caught a failure",
        "verification": f"Pacer batch run_id={run_id}, 0/1 passed",
        "verification_receipt": batch["verification_receipt"],
    }

    recorded = content_payload(
        asyncio.run(call_tool("record_pacer_outcome", {**args, "status": outcome_status}))
    )
    rejected = content_payload(
        asyncio.run(call_tool("record_pacer_outcome", {**args, "status": "completed"}))
    )
    memory = content_payload(
        asyncio.run(call_tool("get_pacer_memory", {"workspace_root": str(workspace), "repo_root": str(repo)}))
    )

    assert recorded["status"] == "recorded"
    assert recorded["evidence_level"] == "verified_failed_batch"
    assert recorded["five_pillars_active"] is False
    assert "completed outcomes must use complete_pacer_task" in rejected["error"]
    assert memory["native_codex_history"][-1]["evidence_level"] == "verified_failed_batch"
    assert memory["five_pillars_active"] is False


def test_pacer_native_memory_is_scoped_to_repo_root(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    content_payload(
        asyncio.run(
            call_tool(
                "record_pacer_outcome",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo_a),
                    "goal": "repo A only",
                    "summary": "private to A",
                    "verification": "review evidence",
                    "status": "failed",
                },
            )
        )
    )

    memory = content_payload(
        asyncio.run(call_tool("get_pacer_memory", {"workspace_root": str(workspace), "repo_root": str(repo_b)}))
    )

    assert memory["native_history_total"] == 0
    assert memory["native_codex_history"] == []


def test_pacer_memory_recovers_parent_launch_history_for_bound_nested_project(tmp_path) -> None:
    from visual_agent.pacer_launch_context import initialize_active_launch

    root = tmp_path / "portfolio"
    project = root / "app"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='app'\n", encoding="utf-8")
    workspace = root / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-1", "repo_root": str(root)},
    )
    history = workspace / "pacer_native" / "history.jsonl"
    history.write_text(
        json.dumps({"recorded_at": "2026-07-13T00:00:00+00:00", "repo_root": str(root.resolve()), "goal": "parent task", "summary": "baseline", "status": "completed"}) + "\n",
        encoding="utf-8",
    )
    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(project / ".agent-workspace"),
                    "repo_root": str(project),
                    "detail": "full",
                },
            )
        )
    )
    assert memory["launch_id"] == "launch-1"
    assert [entry["goal"] for entry in memory["native_codex_history"]] == ["parent task"]
    assert memory["effective_memory"]["hit"] is False
    assert memory["effective_memory"]["lookup_hit"] is True
    assert memory["effective_memory"]["relevant_hit"] is None
    assert memory["effective_memory"]["injected_hit"] is False
    assert memory["effective_memory"]["total_returned"] == 1
    assert memory["effective_memory"]["formal_entries"] == 0
    assert memory["effective_memory"]["native_history_entries"] == 1
    assert memory["effective_memory"]["sources"] == []
    assert memory["effective_memory"]["returned_sources"] == ["native_history"]
    assert memory["effective_memory"]["duplicates_removed"] == 0


def test_pacer_memory_compact_projection_is_small_and_full_shape_is_unchanged(tmp_path) -> None:
    from visual_agent.mcp_server import get_pacer_memory_payload
    from visual_agent.pacer_launch_context import initialize_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-compact.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-compact", "repo_root": str(repo)},
    )

    compact = get_pacer_memory_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "detail": "compact"}
    )

    assert set(compact) == {
        "schema_version",
        "response_detail",
        "status",
        "memory_status",
        "cache_status",
        "memory_reused",
        "response_cache",
        "memory_receipt",
        "launch_id",
        "goal",
        "lookup",
        "relevance",
        "memory_injection",
        "memory_use",
        "effective_memory",
        "entries",
        "native_codex_history",
        "native_history_total",
        "memory_budget",
        "five_pillars_active",
        "five_pillars_assessment",
        "pillars",
    }
    assert set(compact["effective_memory"]) == {
        "hit",
        "lookup_hit",
        "relevant_hit",
        "injected_hit",
        "used_hit",
        "total_returned",
        "formal_entries",
        "native_history_entries",
    }
    assert set(compact["pillars"]) == {"routing", "memory", "managed", "acceptance", "dogfood"}
    assert compact["pillars"]["memory"]["active"] is True
    assert compact["effective_memory"]["hit"] is False
    assert "runtime" not in compact
    assert len(json.dumps(compact, ensure_ascii=False, indent=2)) < 3000

    full = get_pacer_memory_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "detail": "full"}
    )

    assert set(full) == {
        "schema_version",
        "product",
        "verification_engine",
        "workspace_root",
        "goal",
        "entry_count",
        "entries",
        "instruction_memory",
        "patterns",
        "recommendations",
        "disclosure",
        "entry_cache",
        "index",
        "status",
        "memory_status",
        "cache_status",
        "memory_reused",
        "response_cache",
        "memory_receipt",
        "memory_goal_digest",
        "query_goal_digest",
        "lookup",
        "relevance",
        "memory_injection",
        "memory_use",
        "effective_memory",
        "memory_budget",
        "recovery_capsule",
        "native_codex_history",
        "native_history_total",
        "native_history_returned",
        "native_history_omitted",
        "five_pillars_active",
        "five_pillars_assessment",
        "pillars",
        "launch_id",
        "runtime",
    }
    assert "response_detail" not in full
    assert "formal_raw_entries" in full["effective_memory"]
    assert "python" in full["runtime"]


def test_runtime_telemetry_exposes_active_compaction_policy_and_uncached_usage(tmp_path, monkeypatch) -> None:
    from visual_agent import codex_rollout_telemetry
    from visual_agent.codex_rollout_telemetry import RolloutSnapshot
    from visual_agent.mcp_server import get_pacer_runtime_telemetry_payload
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch, save_rollout_baseline

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-1",
            "repo_root": str(repo),
            "auto_compact_token_limit": 96000,
            "rollout_ownership": {"scheme": "launch_marker_v1", "required": True},
        },
    )
    save_rollout_baseline(
        workspace_root=workspace,
        launch_id="launch-1",
        snapshot=RolloutSnapshot(tmp_path / "sessions", "2026-07-13T00:00:00+00:00", {}),
    )
    captured_telemetry_args = {}

    def fake_owned_telemetry(*_args, **kwargs):
        captured_telemetry_args.update(kwargs)
        return {
            "status": "captured",
            "attribution_confidence": "high",
            "ownership": {"scheme": "launch_marker_v1", "required": True, "matched": True},
            "runtime": {"provider": "custom", "model": "gpt-test"},
            "usage": {"input_tokens": 500000, "cached_input_tokens": 450000},
            "current_context_usage": {"input_tokens": 80000, "cached_input_tokens": 70000, "total_tokens": 81000},
            "compactions": {"count": 0, "timestamps": []},
        }

    monkeypatch.setattr(codex_rollout_telemetry, "aggregate_rollout_telemetry", fake_owned_telemetry)
    payload = get_pacer_runtime_telemetry_payload({"workspace_root": str(workspace), "repo_root": str(repo)})
    assert captured_telemetry_args["launch_id"] == "launch-1"
    assert payload["pillars"]["routing"]["active"] is True
    assert "response_detail" not in payload
    assert payload["context_control"] == {
        "auto_compact_token_limit": 96000,
        "scope": "total",
        "compactions_observed": 0,
        "usage_semantics": "cumulative_session_usage_not_current_context_size",
        "uncached_input_tokens": 10000,
        "cached_input_ratio": 0.875,
        "current_context_input_tokens": 80000,
        "current_context_total_tokens": 81000,
        "context_pressure_ratio": 0.8333,
        "accumulated_uncached_input_tokens": 50000,
    }

    compact = get_pacer_runtime_telemetry_payload(
        {"workspace_root": str(workspace), "repo_root": str(repo), "detail": "compact"}
    )
    assert set(compact) == {
        "schema_version",
        "response_detail",
        "status",
        "attribution_confidence",
        "launch_id",
        "lifecycle_status",
        "runtime",
        "usage",
        "context_control",
        "agents",
        "liveness",
        "pillars",
        "five_pillars_active",
        "five_pillars_assessment",
    }
    assert compact["runtime"] == {
        "provider": "custom",
        "model": "gpt-test",
        "reasoning_effort": "",
    }
    assert compact["usage"] == {
        "input_tokens": 500000,
        "cached_input_tokens": 450000,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    assert compact["context_control"] == {
        "auto_compact_token_limit": 96000,
        "compactions_observed": 0,
        "uncached_input_tokens": 10000,
        "current_context_input_tokens": 80000,
        "context_pressure_ratio": 0.8333,
        "accumulated_uncached_input_tokens": 50000,
    }
    assert compact["pillars"]["routing"]["active"] is True
    assert len(json.dumps(compact, ensure_ascii=False, indent=2)) < 2600
    stored = read_active_launch(workspace, launch_id="launch-1")["rollout_telemetry"]
    assert stored["current_context_usage"]["cached_input_tokens"] == 70000
    assert stored["context_control"]["usage_semantics"] == "cumulative_session_usage_not_current_context_size"

    telemetry_tool = next(item for item in mcp_tools() if item.name == "get_pacer_runtime_telemetry")
    assert telemetry_tool.inputSchema["properties"]["detail"] == {
        "type": "string",
        "enum": ["compact", "full"],
        "default": "full",
    }


def test_legacy_rollout_without_launch_ownership_cannot_activate_routing(tmp_path, monkeypatch) -> None:
    from visual_agent import codex_rollout_telemetry
    from visual_agent.codex_rollout_telemetry import RolloutSnapshot
    from visual_agent.mcp_server import get_pacer_runtime_telemetry_payload
    from visual_agent.pacer_launch_context import initialize_active_launch, save_rollout_baseline

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-legacy.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": "launch-legacy", "repo_root": str(repo)},
    )
    save_rollout_baseline(
        workspace_root=workspace,
        launch_id="launch-legacy",
        snapshot=RolloutSnapshot(tmp_path / "sessions", "2026-07-14T00:00:00+00:00", {}),
    )
    captured = {}

    def fake_legacy(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "status": "captured",
            "attribution_confidence": "high",
            "ownership": {"scheme": "launch_marker_v1", "required": True, "matched": True},
            "runtime": {"provider": "custom", "model": "gpt-test"},
            "usage": {},
            "current_context_usage": {},
            "compactions": {"count": 0, "timestamps": []},
        }

    monkeypatch.setattr(codex_rollout_telemetry, "aggregate_rollout_telemetry", fake_legacy)
    payload = get_pacer_runtime_telemetry_payload({"workspace_root": str(workspace), "repo_root": str(repo)})

    assert captured["launch_id"] == ""
    assert payload["pillars"]["routing"]["active"] is False
    assert payload["five_pillars_active"] is False


def test_five_pillars_require_current_launch_verified_closed_loop(tmp_path, monkeypatch) -> None:
    from visual_agent import codex_rollout_telemetry
    from visual_agent.codex_rollout_telemetry import RolloutSnapshot
    from visual_agent.mcp_server import (
        begin_pacer_task_payload,
        complete_pacer_task_payload,
        get_pacer_memory_payload,
        get_pacer_runtime_telemetry_payload,
    )
    from visual_agent.pacer_launch_context import (
        initialize_active_launch,
        save_rollout_baseline,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_smoke.py").write_text(
        "import unittest\n\n"
        "class SmokeTest(unittest.TestCase):\n"
        "    def test_passes(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": "launch-1",
            "repo_root": str(repo),
            "auto_compact_token_limit": 96000,
            "rollout_ownership": {"scheme": "launch_marker_v1", "required": True},
        },
    )
    begin_pacer_task_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "run existing closed-loop tests",
        }
    )
    memory = get_pacer_memory_payload(
        {
            "workspace_root": str(workspace),
            "repo_root": str(repo),
            "goal": "run existing closed-loop tests",
        }
    )
    assert memory["effective_memory"]["hit"] is False
    assert memory["pillars"]["memory"]["active"] is True
    assert memory["pillars"]["memory"]["state"] == "loaded_empty"
    assert memory["pillars"]["memory"]["effective_hit"] is False
    assert memory["pillars"]["managed"]["active"] is False
    assert memory["pillars"]["dogfood"]["active"] is False
    save_rollout_baseline(
        workspace_root=workspace,
        launch_id="launch-1",
        snapshot=RolloutSnapshot(tmp_path / "sessions", "2026-07-13T00:00:00+00:00", {}),
    )
    monkeypatch.setattr(
        codex_rollout_telemetry,
        "aggregate_rollout_telemetry",
        lambda *_args, **_kwargs: {
            "status": "captured",
            "attribution_confidence": "high",
            "ownership": {"scheme": "launch_marker_v1", "required": True, "matched": True},
            "runtime": {"provider": "custom", "model": "gpt-test", "reasoning_effort": "medium"},
            "usage": {},
            "current_context_usage": {},
            "compactions": {"count": 0, "timestamps": []},
        },
    )
    runtime = get_pacer_runtime_telemetry_payload({"workspace_root": str(workspace), "repo_root": str(repo)})
    assert runtime["pillars"]["routing"]["active"] is True
    step = passing_unittest_step(repo)
    outcome = complete_pacer_task_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "goal": "run existing closed-loop tests",
        "summary": "completed in the real source project",
        "completion_evidence": completion_evidence("run existing closed-loop tests"),
        "steps": [step],
    })
    assert outcome["five_pillars_active"] is False
    assert outcome["five_pillars_assessment"]["status"] == "partial"
    assert {
        item["status"]
        for item in outcome["five_pillars_assessment"]["pillars"].values()
    } == {"partial"}


def test_empty_memory_marks_capability_active_but_mimo_route_blocks_closed_loop(tmp_path, monkeypatch) -> None:
    from visual_agent import codex_rollout_telemetry
    from visual_agent.codex_rollout_telemetry import RolloutSnapshot
    from visual_agent.mcp_server import get_pacer_memory_payload, get_pacer_runtime_telemetry_payload
    from visual_agent.pacer_launch_context import initialize_active_launch, read_active_launch, save_rollout_baseline

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname='repo'\n", encoding="utf-8")
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(workspace_root=workspace, manifest_path=manifest, launch={"launch_id": "launch-1", "repo_root": str(repo)})
    memory = get_pacer_memory_payload({"workspace_root": str(workspace), "repo_root": str(repo)})
    assert memory["effective_memory"]["hit"] is False
    assert memory["pillars"]["memory"]["active"] is True
    assert memory["pillars"]["memory"]["state"] == "loaded_empty"
    assert memory["pillars"]["memory"]["effective_hit"] is False
    reused = get_pacer_memory_payload({
        "workspace_root": str(workspace),
        "repo_root": str(repo),
        "known_memory_receipt": memory["memory_receipt"],
    })
    assert reused["status"] == "memory_reused"
    assert reused["effective_memory"]["hit"] is False
    reused_pillar = read_active_launch(workspace, launch_id="launch-1")["pillars"]["memory"]
    assert reused_pillar["active"] is True
    assert reused_pillar["state"] == "reused_empty"
    assert reused_pillar["effective_hit"] is False
    save_rollout_baseline(workspace_root=workspace, launch_id="launch-1", snapshot=RolloutSnapshot(tmp_path / "sessions", "", {}))
    monkeypatch.setattr(
        codex_rollout_telemetry,
        "aggregate_rollout_telemetry",
        lambda *_args, **_kwargs: {
            "status": "captured",
            "runtime": {"provider": "mimo", "model": "mimo-worker"},
            "usage": {}, "current_context_usage": {}, "compactions": {"count": 0},
        },
    )
    runtime = get_pacer_runtime_telemetry_payload({"workspace_root": str(workspace), "repo_root": str(repo)})
    assert runtime["pillars"]["routing"]["active"] is False
    assert runtime["pillars"]["routing"]["mimo_used"] is True


def test_completed_outcome_rejects_batch_from_another_launch(tmp_path) -> None:
    from visual_agent.mcp_server import record_pacer_outcome_payload
    from visual_agent.pacer_launch_context import initialize_active_launch

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    manifest = workspace / "pacer_native" / "launches" / "launch-1.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(workspace_root=workspace, manifest_path=manifest, launch={"launch_id": "launch-1", "repo_root": str(repo)})
    run_id = "20260713-120000-otherlaunch"
    run_dir = workspace / "pacer_native" / "commands" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({
            "status": "passed", "launch_id": "launch-2", "requested_steps": 1,
            "executed_steps": 1, "passed": 1, "failed": 0, "timed_out": 0, "not_applicable": 0,
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="completed outcomes must use complete_pacer_task"):
        record_pacer_outcome_payload({
            "workspace_root": str(workspace), "repo_root": str(repo), "goal": "wrong batch",
            "summary": "must reject", "verification": f"run_id={run_id}", "status": "completed",
        })


def test_terminal_launch_rejects_late_verification_and_outcome_without_mutation(tmp_path) -> None:
    from visual_agent.mcp_server import record_pacer_outcome_payload, run_pacer_verification_payload
    from visual_agent.pacer_launch_context import (
        active_launch_path,
        initialize_active_launch,
        launch_context_path,
        update_active_launch,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    launch_id = "launch-terminal"
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={"launch_id": launch_id, "repo_root": str(repo)},
    )
    update_active_launch(
        workspace,
        expected_launch_id=launch_id,
        status="completed",
        completed_at="2026-07-13T12:00:00+00:00",
    )
    pointer_before = active_launch_path(workspace).read_bytes()
    context_before = launch_context_path(workspace, launch_id).read_bytes()

    with pytest.raises(ValueError, match="terminal launch evidence is immutable"):
        run_pacer_verification_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "steps": [{"name": "late", "argv": ["python", "-m", "pytest", "-q"]}],
            }
        )
    with pytest.raises(ValueError, match="terminal launch evidence is immutable"):
        record_pacer_outcome_payload(
            {
                "workspace_root": str(workspace),
                "repo_root": str(repo),
                "goal": "late subagent outcome",
                "summary": "must not replace the root outcome",
                "status": "failed",
            }
        )

    assert active_launch_path(workspace).read_bytes() == pointer_before
    assert launch_context_path(workspace, launch_id).read_bytes() == context_before
    assert not (workspace / "pacer_native" / "history.jsonl").exists()
    assert not (workspace / "pacer_native" / "commands").exists()


def test_pacer_memory_compacts_native_history_to_budget(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    repo = tmp_path / "repo"
    repo.mkdir()
    for index in range(5):
        content_payload(
            asyncio.run(
                call_tool(
                    "record_pacer_outcome",
                    {
                        "workspace_root": str(workspace),
                        "repo_root": str(repo),
                        "goal": f"task-{index}",
                        "summary": "s" * 2000,
                        "verification": "v" * 2000,
                        "status": "failed",
                    },
                )
            )
        )

    memory = content_payload(
        asyncio.run(
            call_tool(
                "get_pacer_memory",
                {
                    "workspace_root": str(workspace),
                    "repo_root": str(repo),
                    "memory_budget_chars": 1500,
                },
            )
        )
    )

    assert memory["native_history_total"] == 5
    assert memory["effective_memory"]["native_history_entries"] == 1
    assert memory["memory_budget"]["native_omitted"] == 4
    assert memory["native_codex_history"][0]["goal"] == "task-4"
    assert memory["memory_budget"]["used_chars"] <= memory["memory_budget"]["limit_chars"]


def test_memory_deduplicates_formal_entries_and_enforces_shared_budget() -> None:
    from visual_agent.mcp_server import _budget_memory_sources, _dedupe_formal_memory, _same_formal_memory_goal

    formal = [
        {"objective": "same task", "updated_at": "2026-07-12", "relevance_score": 1},
        {"objective": "same   task", "updated_at": "2026-07-13", "relevance_score": 2},
        {"objective": "different task", "updated_at": "2026-07-13", "relevance_score": 3},
    ]
    unique = _dedupe_formal_memory(formal)
    assert len(unique) == 2
    assert next(item for item in unique if item["objective"].startswith("same"))["updated_at"] == "2026-07-13"
    kept_formal, kept_native, usage = _budget_memory_sources(
        [{"source": "formal", "objective": "x" * 3000}],
        [{"source": "native_history", "goal": "y" * 3000}],
        budget_chars=1000,
    )
    assert kept_formal or kept_native
    assert usage["used_chars"] <= 1000
    assert _same_formal_memory_goal(
        "Pacer dogfood phase 1 create report and tests alpha beta gamma delta epsilon",
        "Pacer dogfood phase 1 create report and tests alpha beta gamma delta zeta",
    ) is True
    assert _same_formal_memory_goal(
        "Pacer dogfood phase 1 create report and tests alpha beta gamma delta",
        "Pacer dogfood phase 2 create report and tests alpha beta gamma delta",
    ) is False


def test_mcp_generate_workflow_from_context_returns_quality_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    html = (
        "<form action='/dashboard'>"
        "<label for='email'>Email</label><input id='email' name='email' type='email' required minlength='6'>"
        "<button type='submit'>Sign in</button>"
        "</form><p>Welcome to Dashboard</p>"
    )

    result = generate_workflow_from_context_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify login redirects to dashboard",
            "base_url": "fixtures/login.html",
            "dry_run": True,
            "code_changes": [{"file_path": "login.html", "before": None, "after": html, "change_type": "added"}],
        }
    )

    assert result["status"] == "success"
    assert result["workflow_path"] is None
    assert result["quality"]["score"] >= 0.6
    assert result["quality"]["forbidden_error_assertions"] == 0
    assert result["quality"]["text_from_input_references"] == 0
    assert result["quality"]["invalid_text_from_references"] == []
    assert result["framework_detected"] == "html"
    assert result["fields"] == ["email"]
    assert result["semantic_summary"]["framework"] == "html"
    assert result["semantic_summary"]["field_count"] == 1
    assert result["semantic_summary"]["required_field_count"] == 1
    assert result["semantic_summary"]["validation_rule_count"] == 3
    assert result["semantic_summary"]["data_display_count"] == 0
    assert result["semantic_summary"]["data_displays"] == []
    assert result["semantic_summary"]["matched_data_displays"] == []
    assert result["semantic_summary"]["unmatched_data_displays"] == []
    assert result["semantic_summary"]["negative_input_case_count"] == 3
    assert len(result["negative_input_cases"]) == 3
    assert result["negative_input_cases"][0]["mode"] == "draft_only"
    assert "generation_trace" in result
    assert len(result["generation_trace"]) <= 10
    assert "field email -> paste input.email" in result["generation_trace"]
    assert "success url /dashboard -> wait_for url" in result["generation_trace"]
    assert result["semantic_summary"]["success_state_count"] >= 1
    assert "yaml" in result


def test_mcp_generate_workflow_from_context_returns_data_display_match_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    jsx = """
    function Profile() {
      return (
        <form>
          <input name="displayName" placeholder="Display name" />
          <button type="submit">Save</button>
          <p>Profile saved successfully</p>
          <p>{profile.displayName}</p>
          <p>{profile.timezone}</p>
        </form>
      );
    }
    """

    result = generate_workflow_from_context_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify profile saves",
            "base_url": "fixtures/profile.html",
            "dry_run": True,
            "code_changes": [{"file_path": "Profile.jsx", "before": None, "after": jsx, "change_type": "added"}],
        }
    )

    assert result["semantic_summary"]["data_displays"] == ["profile.displayName", "profile.timezone"]
    assert result["semantic_summary"]["matched_data_displays"] == ["profile.displayName"]
    assert result["semantic_summary"]["unmatched_data_displays"] == ["profile.timezone"]
    assert result["quality"]["data_display_assertions"] == 1
    assert result["quality"]["text_from_input_references"] == 1
    assert "display displayName -> assert_text text_from input.displayName" in result["generation_trace"]
    assert "display profile.timezone -> semantic_summary only" in result["generation_trace"]
    assert "text_from: input.displayName" in result["yaml"]
    assert "input.timezone" not in result["yaml"]


def test_mcp_generate_workflow_from_context_can_read_git_diff(tmp_path) -> None:
    init_git_repo(tmp_path)
    workspace = init_workspace(tmp_path / ".agent-workspace", with_demo=False)
    page = tmp_path / "web" / "login.html"
    page.parent.mkdir()
    page.write_text("<form><input name='email'></form>\n", encoding="utf-8")
    git(tmp_path, "add", "web/login.html")
    git(tmp_path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    page.write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>\n",
        encoding="utf-8",
    )

    result = generate_workflow_from_context_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify login redirects to dashboard",
            "base_url": "web/login.html",
            "repo_root": str(tmp_path),
            "include_untracked": False,
            "dry_run": True,
        }
    )

    assert result["status"] == "success"
    assert result["fields"] == ["email"]
    assert result["quality"]["score"] >= 0.6
    assert result["semantic_summary"]["fields"] == ["email"]
    assert "url_contains: /dashboard" in result["yaml"]


def test_mcp_verify_implementation_dry_run_writes_status(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    status_path = workspace.root / ".vscode-agent-status.json"

    assert result["result"] == "pass"
    assert result["workflow_path"]
    assert result["run_id"]
    assert result["inputs_path"]
    assert result["inputs_source"] == "explicit"
    assert result["report_path"].endswith(f"{result['run_id']}.json")
    assert result["report_markdown_path"].endswith(f"{result['run_id']}.md")
    assert "get_run_report" in result["report_hint"]
    assert result["next_action"].startswith("Implementation verified")
    assert result["semantic_summary"]["framework"] == "html"
    assert result["semantic_summary"]["field_count"] == 1
    assert result["semantic_summary"]["required_field_count"] == 0
    assert status_path.exists()
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["result"] == "pass"
    assert status["report_path"] == result["report_path"]
    assert status["semantic_summary"]["field_count"] == 1


def test_mcp_verify_implementation_uses_generated_inputs_when_not_supplied(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    assert result["result"] == "pass"
    assert result["inputs_path"]
    assert result["inputs_source"] == "generated_template"
    assert Path(result["inputs_path"]).exists()
    assert "negative_verification" not in result


def test_mcp_verify_implementation_can_opt_into_negative_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    html = (
        "<form><label for='email'>Email</label><input id='email' name='email' type='email' required>"
        "<button type='submit'>Save</button></form><p>Saved successfully</p>"
    )
    (workspace.fixtures_dir / "simple_form.html").write_text(html, encoding="utf-8")

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "run_negative": True,
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": html,
                    "change_type": "added",
                }
            ],
        }
    )

    assert result["result"] == "pass"
    assert result["negative_verification"]["requested"] is True
    assert result["negative_verification"]["status"] == "skipped"
    assert result["negative_verification"]["reason"] == "no_negative_oracle"
    assert result["negative_verification"]["workflow_name"].endswith("_negative_draft")
    assert result["negative_verification"]["workflow_path"].endswith("_negative_draft.yaml")
    assert result["negative_verification"]["reset_strategy"] == "fresh_observe_per_case"
    assert "validation error text" in result["negative_verification"]["next_action"]


def test_negative_workflow_report_passes_with_error_oracle(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from visual_agent.mcp_server import run_negative_workflow_verification
    from visual_agent.models import ActionStatus
    from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult

    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    generation = SimpleNamespace(
        workflow_name="simple_form_verification",
        negative_workflow_path=str(workspace.workflows_dir / "simple_form_verification_negative_draft.yaml"),
        negative_input_cases=({"expected_error_texts": ["Invalid input"]},),
        negative_workflow_ready=True,
        negative_workflow_reason="ready",
        negative_workflow_reset_strategy="fresh_observe_per_case",
        negative_oracles=({"text": "Invalid input", "source": "html:text"},),
    )

    def fake_run(_workspace, workflow_name, *, inputs, dry_run, run_profile, timeout_seconds):
        assert workflow_name == "simple_form_verification_negative_draft"
        assert inputs == {}
        assert dry_run is True
        return WorkflowRunResult(
            run_id="run-negative",
            run_dir=workspace.runs_dir / "run-negative",
            workflow_name=workflow_name,
            steps=(WorkflowStepResult(id="assert_error", action="assert_text_contract", status=ActionStatus.DRY_RUN),),
            run_profile=run_profile,
        )

    monkeypatch.setattr("visual_agent.mcp_server.run_workspace_workflow_with_timeout", fake_run)

    report = run_negative_workflow_verification(workspace, generation, run_profile="dry-run", timeout_seconds=30)

    assert report["requested"] is True
    assert report["status"] == "pass"
    assert report["run_id"] == "run-negative"
    assert report["reset_strategy"] == "fresh_observe_per_case"
    assert report["oracles"] == [{"text": "Invalid input", "source": "html:text"}]
    assert report["report_path"].endswith("run-negative.json")
    assert report["report_markdown_path"].endswith("run-negative.md")
    assert "get_run_report" in report["report_hint"]
    assert report["next_action"].startswith("Negative validation passed")
    assert report["steps_passed"] == 1
    assert report["steps_total"] == 1


def test_negative_workflow_report_failure_has_next_action_and_artifacts(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from visual_agent.mcp_server import run_negative_workflow_verification
    from visual_agent.models import ActionStatus
    from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult

    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    generation = SimpleNamespace(
        workflow_name="simple_form_verification",
        negative_workflow_path=str(workspace.workflows_dir / "simple_form_verification_negative_draft.yaml"),
        negative_workflow_ready=True,
        negative_workflow_reason="ready",
        negative_workflow_reset_strategy="fresh_observe_per_case",
        negative_oracles=({"text": "Invalid input", "source": "html:text"},),
    )

    def fake_run(_workspace, workflow_name, *, inputs, dry_run, run_profile, timeout_seconds):
        return WorkflowRunResult(
            run_id="run-negative-fail",
            run_dir=workspace.runs_dir / "run-negative-fail",
            workflow_name=workflow_name,
            steps=(WorkflowStepResult(id="assert_error", action="assert_text_contract", status=ActionStatus.FAILED, message="missing error"),),
            run_profile=run_profile,
        )

    monkeypatch.setattr("visual_agent.mcp_server.run_workspace_workflow_with_timeout", fake_run)

    report = run_negative_workflow_verification(workspace, generation, run_profile="dry-run", timeout_seconds=30)

    assert report["status"] == "fail"
    assert report["failed_step"]["id"] == "assert_error"
    assert report["report_path"].endswith("run-negative-fail.json")
    assert "negative verification report" in report["next_action"]


def test_negative_workflow_report_redacts_oracle_secrets(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    from visual_agent.mcp_server import run_negative_workflow_verification
    from visual_agent.models import ActionStatus
    from visual_agent.workflow import WorkflowRunResult, WorkflowStepResult

    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    generation = SimpleNamespace(
        workflow_name="simple_form_verification",
        negative_workflow_path=str(workspace.workflows_dir / "simple_form_verification_negative_draft.yaml"),
        negative_workflow_ready=True,
        negative_workflow_reason="ready",
        negative_workflow_reset_strategy="fresh_observe_per_case",
        negative_oracles=({"text": "Invalid api_key=sk-secret-value", "source": "html:text"},),
    )

    def fake_run(_workspace, workflow_name, *, inputs, dry_run, run_profile, timeout_seconds):
        return WorkflowRunResult(
            run_id="run-negative-redacted",
            run_dir=workspace.runs_dir / "run-negative-redacted",
            workflow_name=workflow_name,
            steps=(WorkflowStepResult(id="assert_error", action="assert_text_contract", status=ActionStatus.DRY_RUN),),
            run_profile=run_profile,
        )

    monkeypatch.setattr("visual_agent.mcp_server.run_workspace_workflow_with_timeout", fake_run)

    report = run_negative_workflow_verification(workspace, generation, run_profile="dry-run", timeout_seconds=30)

    raw = str(report)
    assert report["oracles"][0]["text"] == "Invalid api_key=[REDACTED]"
    assert "sk-secret-value" not in raw


def test_mcp_verify_implementation_blocks_low_quality_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    status = json.loads((workspace.root / ".vscode-agent-status.json").read_text(encoding="utf-8"))

    assert result["result"] == "needs_workflow_improvement"
    assert result["quality_score"] < result["min_quality_score"]
    assert result["run_id"] is None
    assert result["quality"]["gaps"]
    assert result["semantic_summary"]["confidence"] >= 0.5
    assert result["next_action"]
    assert status["result"] == "needs_workflow_improvement"
    assert status["quality"]["gaps"]
    assert status["semantic_summary"]["framework"] == "html"
    assert status["next_action"] == result["next_action"]


def test_mcp_verify_implementation_can_lower_quality_threshold(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "simple_form.html").write_text(
        "<form><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Save</button></form>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify simple form can submit",
            "base_url": "fixtures/simple_form.html",
            "run_profile": "dry-run",
            "min_quality_score": 0.0,
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "simple_form.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "simple_form.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    assert result["result"] == "pass"
    assert result["run_id"]


def test_mcp_verify_implementation_timeout_before_run_writes_status(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.fixtures_dir / "login.html").write_text(
        "<form action='/dashboard'><label for='email'>Email</label><input id='email' name='email'>"
        "<button type='submit'>Sign in</button></form><p>Welcome Dashboard</p>",
        encoding="utf-8",
    )

    result = verify_implementation_payload(
        {
            "workspace_root": str(workspace.root),
            "task_description": "Verify login redirects",
            "base_url": "fixtures/login.html",
            "run_profile": "dry-run",
            "timeout_seconds": 0,
            "inputs": {"email": "demo@example.com"},
            "code_changes": [
                {
                    "file_path": "login.html",
                    "before": None,
                    "after": (workspace.fixtures_dir / "login.html").read_text(encoding="utf-8"),
                    "change_type": "added",
                }
            ],
        }
    )

    status = json.loads((workspace.root / ".vscode-agent-status.json").read_text(encoding="utf-8"))

    assert result["result"] == "timeout"
    assert result["workflow_path"]
    assert result["run_id"] is None
    assert result["timeout_seconds"] == 0
    assert result["next_action"].startswith("Workflow 执行超时")
    assert "--timeout-seconds" in result["next_action"]
    assert result["semantic_summary"]["success_state_count"] >= 1
    assert status["result"] == "timeout"
    assert status["next_action"] == result["next_action"]


def init_git_repo(path: Path) -> None:
    try:
        git(path, "init")
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("git is required for this test")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def test_mcp_list_workflows_returns_workspace_workflows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = list_workflows_payload({"workspace_root": str(workspace.root)})

    assert result["workflow_count"] >= 1
    assert any(item["name"] == "local_html_form_workflow" for item in result["workflows"])
    workflow = next(item for item in result["workflows"] if item["name"] == "local_html_form_workflow")
    assert workflow["visibility"] == "private"
    assert workflow["quality"]["score"] >= 0
    assert workflow["agent_readiness"]["status"] in {
        "acceptance_candidate",
        "inspection_only",
        "needs_assertions",
        "weak",
    }
    assert workflow["agent_readiness"]["next_action"]


def test_mcp_list_workflows_marks_strong_acceptance_candidate(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "strong.yaml").write_text(
        "schema_version: 1\n"
        "name: strong\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_browser\n"
        "    url: http://localhost:3000\n"
        "  - id: assert_ready\n"
        "    action: assert_text_contract\n"
        "    required_all: [Ready]\n"
        "    forbidden_any: [Error]\n"
        "  - id: click_save\n"
        "    action: click\n"
        "    target:\n"
        "      text: Save\n"
        "      role: button\n"
        "  - id: assert_saved\n"
        "    action: assert_text_contract\n"
        "    required_all: [Saved]\n"
        "    forbidden_any: [Error]\n",
        encoding="utf-8",
    )

    result = list_workflows_payload({"workspace_root": str(workspace.root)})
    workflow = result["workflows"][0]

    assert workflow["name"] == "strong"
    assert workflow["quality"]["has_interaction"] is True
    assert workflow["quality"]["has_strict_contract"] is True
    assert workflow["agent_readiness"]["status"] == "acceptance_candidate"
    assert workflow["agent_readiness"]["acceptance_candidate"] is True


def test_mcp_list_workflows_recommends_by_changed_files(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "checkout",
        affects=("src/payment/",),
        tags=("verification",),
        steps=[
            {"id": "observe", "action": "observe_browser", "url": "http://localhost:3000"},
            {"id": "assert_ready", "action": "assert_text_contract", "required_all": ["Ready"], "forbidden_any": ["Error"]},
            {"id": "click_save", "action": "click", "target": {"text": "Save", "role": "button"}},
            {"id": "assert_saved", "action": "assert_text_contract", "required_all": ["Saved"], "forbidden_any": ["Error"]},
        ],
    )
    write_workflow(
        workspace,
        "profile",
        affects=("src/profile/",),
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000/profile"}],
    )

    result = list_workflows_payload(
        {
            "workspace_root": str(workspace.root),
            "changed_files": ["src/payment/checkout.py"],
        }
    )

    assert result["recommendations"]["enabled"] is True
    assert result["recommendations"]["recommended_workflows"] == ["checkout"]
    assert result["recommendations"]["primary_recommended_workflows"] == ["checkout"]
    assert result["recommendations"]["fallback_no_affects_workflows"] == []
    assert result["recommendations"]["acceptance_candidate_workflows"] == ["checkout"]
    assert result["recommendations"]["coverage"]["status"] == "covered"
    assert result["recommendations"]["coverage"]["precise_covered_files"] == ["src/payment/checkout.py"]
    assert result["recommendations"]["coverage"]["uncovered_files"] == []
    checkout = next(item for item in result["workflows"] if item["name"] == "checkout")
    profile = next(item for item in result["workflows"] if item["name"] == "profile")
    assert checkout["diff_recommendation"]["recommended"] is True
    assert checkout["diff_recommendation"]["matched_patterns"] == ["src/payment/"]
    assert profile["diff_recommendation"]["recommended"] is False


def test_mcp_list_workflows_reports_no_diff_match(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "profile",
        affects=("src/profile/",),
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000/profile"}],
    )

    result = list_workflows_payload(
        {
            "workspace_root": str(workspace.root),
            "changed_files": ["src/payment/checkout.py"],
        }
    )

    assert result["recommendations"]["recommended_workflows"] == []
    assert result["recommendations"]["primary_recommended_workflows"] == []
    assert result["recommendations"]["fallback_no_affects_workflows"] == []
    assert result["recommendations"]["coverage"]["status"] == "uncovered"
    assert result["recommendations"]["coverage"]["uncovered_files"] == ["src/payment/checkout.py"]
    assert result["recommendations"]["coverage"]["suggested_new_workflows"] == [
        {
            "changed_file": "src/payment/checkout.py",
            "suggested_name": "src_payment_verification",
            "affects": ["src/payment/"],
            "reason": "no workflow precisely covers this changed file",
        }
    ]
    assert "generate or record" in result["recommendations"]["next_action"]


def test_mcp_list_workflows_separates_no_affects_fallbacks(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "fallback",
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000"}],
    )

    result = list_workflows_payload(
        {
            "workspace_root": str(workspace.root),
            "changed_files": ["src/payment/checkout.py"],
        }
    )

    assert result["recommendations"]["recommended_workflows"] == ["fallback"]
    assert result["recommendations"]["primary_recommended_workflows"] == []
    assert result["recommendations"]["fallback_no_affects_workflows"] == ["fallback"]
    assert result["recommendations"]["coverage"]["status"] == "fallback_only"
    assert result["recommendations"]["coverage"]["fallback_only_files"] == ["src/payment/checkout.py"]
    assert result["recommendations"]["coverage"]["uncovered_files"] == []
    assert result["recommendations"]["coverage"]["suggested_affects"][0]["workflow"] == "fallback"
    assert result["recommendations"]["coverage"]["suggested_affects"][0]["add_affects"] == ["src/payment/"]


def test_mcp_coverage_suggests_directory_affects_for_root_level_files(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "fallback",
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000"}],
    )

    result = list_workflows_payload({"workspace_root": str(workspace.root), "changed_files": ["tests/test_cli.py"]})

    assert result["recommendations"]["coverage"]["suggested_affects"][0]["add_affects"] == ["tests/"]


def test_mcp_plan_coverage_repair_returns_compact_agent_plan(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "fallback",
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000"}],
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "plan_coverage_repair",
                {
                    "workspace_root": str(workspace.root),
                    "changed_files": ["src/payment/checkout.py"],
                },
            )
        )
    )

    assert payload["status"] == "fallback_only"
    assert payload["ready_to_verify"] is False
    assert payload["fallback_only_files"] == ["src/payment/checkout.py"]
    assert payload["suggested_affects"][0]["workflow"] == "fallback"
    assert payload["suggested_affects"][0]["add_affects"] == ["src/payment/"]
    assert "Apply suggested_affects" in payload["agent_instruction"]


def test_mcp_draft_coverage_repair_drafts_affects_patch(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "fallback",
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000"}],
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "draft_coverage_repair",
                {
                    "workspace_root": str(workspace.root),
                    "changed_files": ["src/payment/checkout.py"],
                },
            )
        )
    )

    assert payload["status"] == "fallback_only"
    assert payload["patch_count"] == 1
    assert payload["patches"][0]["kind"] == "add_affects"
    assert payload["patches"][0]["applied"] is False
    assert "+affects:" in payload["patches"][0]["diff"]
    assert "+- src/payment/" in payload["patches"][0]["diff"] or "+  - src/payment/" in payload["patches"][0]["diff"]


def test_mcp_draft_coverage_repair_drafts_new_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "profile",
        affects=("src/profile/",),
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000/profile"}],
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "draft_coverage_repair",
                {
                    "workspace_root": str(workspace.root),
                    "changed_files": ["src/payment/checkout.py"],
                },
            )
        )
    )

    assert payload["status"] == "uncovered"
    assert payload["patch_count"] == 1
    assert payload["patches"][0]["kind"] == "new_workflow"
    assert payload["patches"][0]["path"] == "workflows/src_payment_verification.yaml"
    assert "--- /dev/null" in payload["patches"][0]["diff"]
    assert "+name: src_payment_verification" in payload["patches"][0]["diff"]


def test_mcp_apply_coverage_repair_defaults_to_dry_run(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    path = write_workflow(
        workspace,
        "fallback",
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000"}],
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "apply_coverage_repair",
                {
                    "workspace_root": str(workspace.root),
                    "changed_files": ["src/payment/checkout.py"],
                },
            )
        )
    )

    assert payload["status"] == "dry_run"
    assert payload["apply"] is False
    assert "src/payment/" not in path.read_text(encoding="utf-8")


def test_mcp_apply_coverage_repair_applies_affects_patch(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    path = write_workflow(
        workspace,
        "fallback",
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000"}],
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "apply_coverage_repair",
                {
                    "workspace_root": str(workspace.root),
                    "changed_files": ["src/payment/checkout.py"],
                    "apply": True,
                },
            )
        )
    )

    assert payload["status"] == "applied"
    assert payload["applied_count"] == 1
    assert payload["coverage_fixed"] is True
    assert payload["post_apply_plan"]["status"] == "covered"
    assert payload["post_apply_plan"]["primary_recommended_workflows"] == ["fallback"]
    text = path.read_text(encoding="utf-8")
    assert "affects:" in text
    assert "src/payment/" in text


def test_mcp_apply_coverage_repair_creates_new_workflow_for_uncovered_path(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    write_workflow(
        workspace,
        "profile",
        affects=("src/profile/",),
        tags=("verification",),
        steps=[{"id": "observe", "action": "observe_browser", "url": "http://localhost:3000/profile"}],
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "apply_coverage_repair",
                {
                    "workspace_root": str(workspace.root),
                    "changed_files": ["src/payment/checkout.py"],
                    "apply": True,
                },
            )
        )
    )

    target = workspace.workflows_dir / "src_payment_verification.yaml"
    assert payload["status"] == "applied"
    assert payload["applied"][0]["kind"] == "new_workflow"
    assert payload["coverage_fixed"] is True
    assert payload["post_apply_plan"]["status"] == "covered"
    assert payload["post_apply_plan"]["primary_recommended_workflows"] == ["src_payment_verification"]
    assert target.exists()
    assert "src/payment/" in target.read_text(encoding="utf-8")


def test_mcp_list_workflows_truncates_large_response(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for index in range(250):
        (workspace.workflows_dir / f"workflow_{index:03d}_with_long_name_for_budget.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: workflow_{index:03d}_with_long_name_for_budget\n"
            "version: 1\n"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_screen\n",
            encoding="utf-8",
        )

    result = asyncio.run(call_tool("list_workflows", {"workspace_root": str(workspace.root)}))
    payload = content_payload(result)

    assert len(result[0].text) <= 8000
    assert payload["workflow_count"] == 250
    assert payload["truncated"] is True
    assert payload["omitted_count"] > 0


def test_mcp_validate_workflow_returns_validation_and_preflight(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = validate_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow"})

    assert result["valid"] is True
    assert result["preflight"]["ok"] is True


def test_mcp_validate_missing_observation_workflow_returns_not_valid(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "bad.yaml").write_text(
        "schema_version: 1\nmin_runtime_version: '0.1.0'\nname: bad\nversion: 1\nsteps:\n  - id: click\n    action: click\n",
        encoding="utf-8",
    )

    result = validate_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "bad"})

    assert result["valid"] is False


def test_mcp_run_workflow_defaults_to_dry_run_and_audits(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    args = {"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"}
    result = run_workflow_payload(args)
    audit = workspace.root / "gui" / "actions.jsonl"

    assert result["status"] == "success"
    assert result["run_profile"] == "dry-run"
    assert result["run_id"]
    assert not audit.exists()

    async_result = asyncio.run(call_tool("run_workflow", args))
    payload = content_payload(async_result)

    assert payload["status"] == "success"
    assert audit.exists()
    assert "mcp:run_workflow" in audit.read_text(encoding="utf-8")


def test_mcp_run_workflow_handler_runs_outside_asyncio_loop(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    def fake_run_workflow_payload(_args):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return {"status": "success", "threaded": True}
        return {"status": "failed", "threaded": False}

    monkeypatch.setattr("visual_agent.mcp_server.run_workflow_payload", fake_run_workflow_payload)

    result = asyncio.run(
        call_tool(
            "run_workflow",
            {"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow"},
        )
    )
    payload = content_payload(result)

    assert payload["status"] == "success"
    assert payload["threaded"] is True


def test_mcp_run_workflow_defaults_to_compact_report_and_supports_verbose(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    args = {"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"}

    compact = run_workflow_payload(args)
    verbose = run_workflow_payload({**args, "verbose": True})

    assert compact["status"] == "success"
    assert compact["workflow"] == "local_html_form_workflow"
    assert "steps" in compact
    assert "failed_steps" not in compact
    assert verbose["status"] == "success"
    assert "failed_steps" in verbose


def test_mcp_verify_workflow_returns_verification_contract(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = verify_workflow_payload(
        {"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"}
    )

    assert result["schema_version"] == 1
    assert result["result"] == "pass"
    assert result["workflow"] == "local_html_form_workflow"
    assert result["run_profile"] == "dry-run"
    assert result["run_id"]
    assert result["steps_passed"] == result["steps_total"]
    assert result["structured_failure"] is None


def test_mcp_verify_workflow_error_returns_structured_failure(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    result = verify_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "missing"})

    assert result["result"] == "error"
    assert result["structured_failure"]["schema_version"] == 1
    assert result["structured_failure"]["root_cause"] == "env_error"
    assert result["suggestion"]


def test_mcp_run_workflow_rejects_approved_outside_whitelist(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    with pytest.raises(ValueError):
        run_workflow_payload(
            {
                "workspace_root": str(workspace.root),
                "workflow_name": "local_html_form_workflow",
                "run_profile": "approved",
                "inputs_file": "demo_login.json",
            }
        )


def test_mcp_run_workflow_rejects_approved_when_whitelist_empty_and_when_outside(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "approved"
    data["mcp"]["approved_workflows"] = ["other_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(ValueError):
        run_workflow_payload(
            {
                "workspace_root": str(workspace.root),
                "workflow_name": "local_html_form_workflow",
                "run_profile": "approved",
                "inputs_file": "demo_login.json",
            }
        )


def test_mcp_run_workflow_allows_approved_when_whitelisted_and_max_profile_allows(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "approved"
    data["mcp"]["approved_workflows"] = ["local_html_form_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_workflow_payload(
        {
            "workspace_root": str(workspace.root),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "approved",
            "inputs_file": "demo_login.json",
        }
    )

    assert result["run_profile"] == "approved"


def test_mcp_run_workflow_downgrades_approved_to_max_profile_when_whitelisted(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    manifest = workspace.root / "workspace.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["mcp"]["max_run_profile"] = "supervised"
    data["mcp"]["approved_workflows"] = ["local_html_form_workflow"]
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    result = run_workflow_payload(
        {
            "workspace_root": str(workspace.root),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "approved",
            "inputs_file": "demo_login.json",
        }
    )

    assert result["requested_run_profile"] == "approved"
    assert result["run_profile"] == "supervised"


def test_mcp_get_run_report_markdown_is_redacted(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    payload = content_payload(result)
    text = payload["content"]

    assert "Report Detail" in text
    assert "secret" not in text.lower()
    assert "cookie" not in text.lower()


def test_mcp_get_run_report_blocks_old_history_on_free_tier(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VISUAL_AGENT_LICENSE_TIER", raising=False)
    monkeypatch.delenv("VISUAL_AGENT_LICENSE_FILE", raising=False)
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    old_timestamp = time() - 8 * 86400
    for suffix in (".json", ".md"):
        os.utime(workspace.reports_dir / f"{run['run_id']}{suffix}", (old_timestamp, old_timestamp))

    result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "json"}))
    payload = content_payload(result)
    artifacts = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert payload["status"] == "upgrade_required"
    assert payload["history_access"]["reason"] == "history_window_exceeded"
    assert artifacts["status"] == "upgrade_required"


def test_mcp_get_run_report_scrubs_sensitive_field_names_and_values(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    report_path = workspace.reports_dir / f"{run['run_id']}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["artifacts"] = {
        "password": "plain-password",
        "token": "plain-token",
        "cookie": "session-cookie",
        "api_key": "sk-testsecret123456",
        "authorization": "Bearer abcdefghijklmnop",
        "bearer": "abcdefghijklmnop",
        "message": "token=abc12345",
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    json_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "json"}))
    json_payload = content_payload(json_result)
    markdown_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    markdown_payload = content_payload(markdown_result)
    combined = json.dumps(json_payload, ensure_ascii=False) + markdown_payload["content"]

    assert "plain-password" not in combined
    assert "plain-token" not in combined
    assert "session-cookie" not in combined
    assert "sk-testsecret123456" not in combined
    assert "abcdefghijklmnop" not in combined
    assert "[REDACTED]" in combined or '"redacted": true' in combined.lower()


def test_mcp_get_run_report_is_budgeted_when_report_is_large(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    report_path = workspace.reports_dir / f"{run['run_id']}.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["steps"] = [
        {
            "id": f"step_{index}",
            "action": "assert_text",
            "status": "success",
            "message": "large report line " + ("x" * 500),
            "elapsed_seconds": 0.01,
        }
        for index in range(80)
    ]
    report["total_steps"] = 80
    report["succeeded_steps"] = 80
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    markdown_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "markdown"}))
    markdown_payload = content_payload(markdown_result)
    json_result = asyncio.run(call_tool("get_run_report", {"workspace_root": str(workspace.root), "run_id": run["run_id"], "format": "json"}))
    json_payload = content_payload(json_result)

    assert markdown_payload["truncated"] is True
    assert markdown_payload["within_budget"] is True
    assert len(markdown_result[0].text) <= 8000
    assert json_payload["truncated"] is True
    assert json_payload["within_budget"] is True
    assert len(json_result[0].text) <= 8000


def test_mcp_list_run_artifacts_paths_stay_inside_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert result["artifact_count"] > 0
    for artifact in result["artifacts"]:
        assert str(artifact["path"]).startswith(str(workspace.root))
        assert ".." not in artifact["relative_path"]


def test_mcp_list_run_artifacts_truncates_large_response(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    artifact_dir = workspace.runs_dir / run["run_id"] / "many"
    artifact_dir.mkdir(parents=True)
    for index in range(300):
        (artifact_dir / f"artifact_{index:03d}_with_long_name_for_budget.txt").write_text("x", encoding="utf-8")

    result = asyncio.run(call_tool("list_run_artifacts", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    payload = content_payload(result)

    assert len(result[0].text) <= 8000
    assert payload["artifact_count"] >= 300
    assert payload["truncated"] is True
    assert payload["omitted_count"] > 0


def test_mcp_list_run_artifacts_skips_symlink_that_escapes_workspace(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside", encoding="utf-8")
    link = workspace.runs_dir / run["run_id"] / "outside-link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this Windows environment.")

    result = list_run_artifacts_payload({"workspace_root": str(workspace.root), "run_id": run["run_id"]})

    assert all("outside-link.txt" not in artifact["relative_path"] for artifact in result["artifacts"])
    assert all(str(outside.resolve()) != artifact["path"] for artifact in result["artifacts"])


def test_mcp_workspace_dashboard_returns_agent_readable_health(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "local_html_form_workflow", "inputs_file": "demo_login.json"})

    result = get_workspace_dashboard_payload({"workspace_root": str(workspace.root), "format": "markdown"})

    assert result["format"] == "markdown"
    assert "Workspace Dashboard" in result["content"]
    assert "Workflows" in result["content"]


def test_mcp_latest_failure_returns_none_when_clean(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = get_latest_failure_payload({"workspace_root": str(workspace.root), "format": "json"})

    assert result["status"] == "none"
    assert result["report"] is None


def test_mcp_latest_failure_returns_failed_report_with_diagnosis(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    result = get_latest_failure_payload({"workspace_root": str(workspace.root), "format": "json"})

    assert result["status"] == "found"
    assert result["report"]["status"] == "failed"
    assert result["report"]["failure"]["diagnosis"]["expected"]
    assert result["report"]["failure"]["diagnosis"]["structured_failure"]["root_cause"] in {
        "assertion_wrong",
        "element_missing",
    }


def test_mcp_session_context_and_failure_summary_stay_within_budget(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    context = content_payload(asyncio.run(call_tool("get_session_context", {"workspace_root": str(workspace.root)})))
    summary = content_payload(asyncio.run(call_tool("summarize_latest_failure", {"workspace_root": str(workspace.root)})))
    combined = json.dumps(context, ensure_ascii=False) + json.dumps(summary, ensure_ascii=False)

    assert context["within_budget"] is True
    assert len(context["snapshot"]) <= 2000
    assert summary["status"] == "found"
    assert len(json.dumps(summary, ensure_ascii=False)) <= 2000
    for keyword in ("password", "cookie", "Bearer ", "demo123"):
        assert keyword not in combined


def test_mcp_diagnose_failure_and_repair_workflow_return_ai_ready_payloads(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})

    diagnosis = content_payload(
        asyncio.run(call_tool("diagnose_failure", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )
    details = content_payload(
        asyncio.run(call_tool("get_failure_details", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )
    repair = content_payload(
        asyncio.run(call_tool("repair_workflow", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )

    assert diagnosis["status"] == "found"
    assert diagnosis["failed_step"]["id"] == "assert_missing"
    assert "repair_prompt" in diagnosis
    assert details["status"] == "found"
    assert details["structured_failure"]["step_id"] == "assert_missing"
    assert details["structured_failure"]["root_cause"] in {"assertion_wrong", "element_missing"}
    assert details["structured_failure"]["suggested_fix"]
    assert repair["status"] == "suggested"
    assert repair["repair"]["classification"] == "app_bug"
    assert repair["source"] == "deterministic"
    assert repair["repair"]["candidates"][0]["id"] == "manual_investigation"
    assert repair["repair"]["candidates"][0]["apply_supported"] is False


def test_mcp_get_failure_details_marks_nextjs_hydration_mismatch_as_known_issue(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    run_id = "nextjs-hydration"
    run_dir = workspace.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("workflow_result.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "workflow_name": "nextjs_demo_login_smoke",
                "workflow_schema_version": 1,
                "runtime_version": "0.1.0",
                "run_profile": "dry-run",
                "status": "failed",
                "total_steps": 2,
                "succeeded_steps": 1,
                "failed_step": "assert_ready",
                "dry_run_actions": 0,
                "elapsed_seconds": 2.0,
                "artifacts": {},
                "downloads": [],
                "steps": [
                    {
                        "id": "observe",
                        "action": "observe_browser",
                        "status": "success",
                        "message": "",
                        "metadata": {},
                    },
                    {
                        "id": "assert_ready",
                        "action": "assert_text",
                        "status": "failed",
                        "message": "Hydration mismatch warning surfaced.",
                        "metadata": {
                            "failure_diagnosis": {
                                "step_id": "assert_ready",
                                "action": "assert_text",
                                "expected": "checkout ready",
                                "actual": (
                                    "A tree hydrated but some attributes of the server rendered HTML didn't match the client "
                                    "properties. https://react.dev/link/hydration-mismatch"
                                ),
                                "observation": {
                                    "visible_text": [
                                        "A tree hydrated but some attributes of the server rendered HTML didn't match the client properties.",
                                        "https://react.dev/link/hydration-mismatch",
                                    ]
                                },
                                "artifacts": {},
                            }
                        },
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    details = content_payload(
        asyncio.run(call_tool("get_failure_details", {"workspace_root": str(workspace.root), "run_id": run_id}))
    )

    assert details["status"] == "found"
    assert details["structured_failure"]["root_cause"] == "known_issue"
    assert "hydration mismatch" in details["structured_failure"]["suggested_fix"].lower()
    assert details["report_path"].endswith(f"reports\\{run_id}.json")


def test_mcp_list_repair_history_returns_recorded_attempts(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})
    content_payload(asyncio.run(call_tool("repair_workflow", {"workspace_root": str(workspace.root), "run_id": run["run_id"]})))

    history = content_payload(asyncio.run(call_tool("list_repair_history", {"workspace_root": str(workspace.root)})))

    assert history["total_entries"] == 1
    assert history["entries"][0]["workflow"] == "failure"
    assert history["entries"][0]["status"] == "suggested"


def test_mcp_rollback_repair_restores_recorded_backup(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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
    original = workflow_path.read_text(encoding="utf-8")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})
    repair = content_payload(
        asyncio.run(
            call_tool(
                "repair_workflow",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "apply": True},
            )
        )
    )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "rollback_repair",
                {"workspace_root": str(workspace.root), "history_id": repair["history"]["history_id"]},
            )
        )
    )

    assert payload["status"] == "manual_rolled_back"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_mcp_get_repair_health_summarizes_history(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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
    content_payload(
        asyncio.run(
            call_tool(
                "repair_workflow",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "apply": True, "verify": True},
            )
        )
    )

    health = content_payload(asyncio.run(call_tool("get_repair_health", {"workspace_root": str(workspace.root)})))

    assert health["applied_count"] == 1
    assert health["verified_count"] == 1
    assert health["risk_level"] == "low"
    assert health["status_counts"]["verified"] == 1


def test_mcp_auto_repair_failure_applies_verifies_and_returns_health(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"]},
            )
        )
    )

    assert payload["status"] == "verified"
    assert payload["repair_result"]["workflow_repair_plan"]["verification"]["status"] == "passed"
    assert payload["repair_health"]["risk_level"] == "low"
    assert "客户管理系统" in workflow_path.read_text(encoding="utf-8")


def test_mcp_auto_repair_failure_dry_run_does_not_modify_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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
    original = workflow_path.read_text(encoding="utf-8")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "dry_run": True},
            )
        )
    )

    assert payload["status"] == "suggested"
    assert payload["auto_repair"]["dry_run"] is True
    assert payload["repair_result"]["workflow_repair_plan"]["applied"] is False
    assert workflow_path.read_text(encoding="utf-8") == original


def test_mcp_auto_repair_failure_blocks_high_risk_health(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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
    content_payload(
        asyncio.run(call_tool("auto_repair_failure", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )
    history = content_payload(asyncio.run(call_tool("list_repair_history", {"workspace_root": str(workspace.root)})))
    content_payload(
        asyncio.run(
            call_tool(
                "rollback_repair",
                {"workspace_root": str(workspace.root), "history_id": history["entries"][0]["history_id"]},
            )
        )
    )
    failed_again = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(call_tool("auto_repair_failure", {"workspace_root": str(workspace.root), "run_id": failed_again["run_id"]}))
    )

    assert payload["status"] == "blocked"
    assert payload["auto_repair"]["blocked"] is True
    assert payload["auto_repair"]["apply"] is False
    assert payload["preflight_repair_health"]["risk_level"] == "high"


def test_mcp_auto_repair_failure_respects_workspace_policy(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    manifest_path = workspace.root / "workspace.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["auto_repair"] = {"min_confidence": 0.99}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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
    original = workflow_path.read_text(encoding="utf-8")
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "typo_failure"})

    payload = content_payload(
        asyncio.run(call_tool("auto_repair_failure", {"workspace_root": str(workspace.root), "run_id": run["run_id"]}))
    )

    assert payload["status"] == "suggested"
    assert payload["auto_repair"]["policy"]["min_confidence"] == 0.99
    assert payload["repair_result"]["workflow_repair_plan"]["status"] == "not_applied"
    assert workflow_path.read_text(encoding="utf-8") == original


def test_mcp_auto_repair_failure_can_promote_regression(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {"workspace_root": str(workspace.root), "run_id": run["run_id"], "promote_regression": True},
            )
        )
    )

    assert payload["status"] == "verified"
    assert payload["regression"]["status"] == "promoted"
    assert Path(payload["regression"]["test_path"]).exists()


def test_mcp_auto_repair_failure_can_promote_and_run_regression(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow_path = workspace.workflows_dir / "typo_failure.yaml"
    workflow_path.write_text(
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

    payload = content_payload(
        asyncio.run(
            call_tool(
                "auto_repair_failure",
                {
                    "workspace_root": str(workspace.root),
                    "run_id": run["run_id"],
                    "promote_regression": True,
                    "run_regression": True,
                    "regression_timeout_seconds": 30,
                },
            )
        )
    )

    assert payload["status"] == "verified"
    assert payload["regression"]["test_run"]["status"] == "success"
    assert payload["regression"]["test_run"]["passed_tests"] == 1


def test_mcp_session_context_includes_latest_repair_summary(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    failure_workflow = workspace.workflows_dir / "failure.yaml"
    failure_workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: failure\n"
        "version: 1\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n"
        "  - id: assert_missing\n"
        "    action: assert_text\n"
        "    text: missing text\n",
        encoding="utf-8",
    )
    run = run_workflow_payload({"workspace_root": str(workspace.root), "workflow_name": "failure"})
    content_payload(asyncio.run(call_tool("repair_workflow", {"workspace_root": str(workspace.root), "run_id": run["run_id"]})))

    context = content_payload(asyncio.run(call_tool("get_session_context", {"workspace_root": str(workspace.root)})))

    assert "Latest Repair" in context["snapshot"]
    assert "Workflow: failure" in context["snapshot"]


def test_mcp_list_benchmarks_returns_public_references(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    payload = content_payload(asyncio.run(call_tool("list_benchmarks", {"workspace_root": str(workspace.root)})))

    assert payload["status"] == "ready"
    assert payload["benchmark_count"] >= 4
    assert any(item["id"] == "stagehand_act_extract" for item in payload["benchmarks"])


def test_mcp_build_benchmark_plan_returns_scenarios(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    payload = content_payload(
        asyncio.run(
            call_tool(
                "build_benchmark_plan",
                {"workspace_root": str(workspace.root), "benchmark_id": "healenium_locator_repair"},
            )
        )
    )

    assert payload["status"] == "ready"
    assert payload["benchmark_count"] == 1
    assert payload["scenario_count"] >= 1
    assert payload["scenarios"][0]["benchmark_id"] == "healenium_locator_repair"


def test_mcp_build_benchmark_draft_can_save_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    payload = content_payload(
        asyncio.run(
            call_tool(
                "build_benchmark_draft",
                {
                    "workspace_root": str(workspace.root),
                    "scenario_id": "healenium_locator_repair_1",
                    "save": True,
                },
            )
        )
    )

    assert payload["status"] == "success"
    assert Path(payload["saved_to"]).exists()
    assert payload["workflow_name"].startswith("benchmark_healenium_locator_repair")


def test_mcp_run_browser_smoke_returns_diagnostics(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    def fake_run_browser_smoke(**kwargs):
        return {
            "status": "success",
            "url": kwargs["url"],
            "run_dir": str(workspace.root / "browser-smoke-runs" / "fake"),
            "initial": {"visible_text_length": 5, "interactive_count": 1, "screenshot_path": "fake.png"},
            "after_click": None,
            "click": None,
            "issues": [],
        }

    monkeypatch.setattr("visual_agent.browser_smoke.run_browser_smoke", fake_run_browser_smoke)
    payload = content_payload(
        asyncio.run(
            call_tool(
                "run_browser_smoke",
                {"workspace_root": str(workspace.root), "url": "https://example.test/login", "expect_text": ["Login"]},
            )
        )
    )

    assert payload["status"] == "success"
    assert payload["workspace"] == str(workspace.root)
    assert payload["url"] == "https://example.test/login"


def test_mcp_run_browser_smoke_suite_returns_summary(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    suite = workspace.root / "suite.json"
    suite.write_text('{"cases":[{"id":"home","url":"https://example.test/home"}]}', encoding="utf-8")

    def fake_run_browser_smoke_suite(*_args, **_kwargs):
        return {
            "status": "success",
            "suite_name": "suite",
            "run_dir": str(workspace.root / "browser-smoke-suite-runs" / "fake"),
            "case_count": 1,
            "passed_count": 1,
            "failed_count": 0,
            "results": [{"case_id": "home", "status": "success"}],
        }

    monkeypatch.setattr("visual_agent.browser_smoke_suite.run_browser_smoke_suite", fake_run_browser_smoke_suite)
    payload = content_payload(
        asyncio.run(
            call_tool(
                "run_browser_smoke_suite",
                {"workspace_root": str(workspace.root), "suite_file": "suite.json"},
            )
        )
    )

    assert payload["status"] == "success"
    assert payload["workspace"] == str(workspace.root)
    assert payload["case_count"] == 1


def test_mcp_run_verification_returns_ai_ready_report(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    workflow = workspace.workflows_dir / "verification.yaml"
    workflow.write_text(
        "schema_version: 1\n"
        "min_runtime_version: '0.1.0'\n"
        "name: verification\n"
        "version: 1\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_fixture\n"
        f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
        encoding="utf-8",
    )

    payload = content_payload(asyncio.run(call_tool("run_verification", {"workspace_root": str(workspace.root)})))

    assert payload["total"] == 1
    assert payload["passed"] == 1
    assert payload["failed"] == 0
    assert payload["within_budget"] is True
    assert "Verification Report" in payload["content"]


def test_mcp_run_verification_can_target_one_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for name in ("slow_visual_contract", "fast_smoke_contract"):
        (workspace.workflows_dir / f"{name}.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: {name}\n"
            "version: 1\n"
            "tags:\n"
            "  - verification\n"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_fixture\n"
            f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
            encoding="utf-8",
        )

    payload = content_payload(
        asyncio.run(
            call_tool(
                "run_verification",
                {
                    "workspace_root": str(workspace.root),
                    "workflow": ["fast_smoke_contract"],
                    "max_workflows": 1,
                },
            )
        )
    )

    assert payload["total"] == 1
    assert "fast_smoke_contract" in payload["content"]
    assert "slow_visual_contract" not in payload["content"]


def test_mcp_run_verification_skips_slow_by_default_and_includes_when_requested(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    for name, extra_tags in (("slow_visual_contract", "  - slow\n"), ("fast_smoke_contract", "")):
        (workspace.workflows_dir / f"{name}.yaml").write_text(
            "schema_version: 1\n"
            "min_runtime_version: '0.1.0'\n"
            f"name: {name}\n"
            "version: 1\n"
            "tags:\n"
            "  - verification\n"
            f"{extra_tags}"
            "steps:\n"
            "  - id: observe\n"
            "    action: observe_fixture\n"
            f"    path: {str(ROOT / 'examples' / 'fixtures' / 'login_page_observation.json').replace(chr(92), '/')}\n",
            encoding="utf-8",
        )

    default_payload = content_payload(asyncio.run(call_tool("run_verification", {"workspace_root": str(workspace.root)})))
    included_payload = content_payload(
        asyncio.run(call_tool("run_verification", {"workspace_root": str(workspace.root), "include_slow": True}))
    )

    assert default_payload["total"] == 1
    assert "fast_smoke_contract" in default_payload["content"]
    assert "slow_visual_contract" not in default_payload["content"]
    assert included_payload["total"] == 2
    assert "slow_visual_contract" in included_payload["content"]


def test_mcp_list_workflows_skips_slow_by_default_and_includes_when_requested(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)
    (workspace.workflows_dir / "fast.yaml").write_text(
        "schema_version: 1\nname: fast\nversion: 1\ntags:\n  - verification\nsteps:\n  - id: observe\n    action: observe_ocr\n    mock_text: ready\n",
        encoding="utf-8",
    )
    (workspace.workflows_dir / "slow.yaml").write_text(
        "schema_version: 1\nname: slow\nversion: 1\ntags:\n  - verification\n  - slow\nsteps:\n  - id: observe\n    action: observe_ocr\n    mock_text: ready\n",
        encoding="utf-8",
    )

    default_payload = list_workflows_payload({"workspace_root": str(workspace.root)})
    included_payload = list_workflows_payload({"workspace_root": str(workspace.root), "include_slow": True})

    assert [item["name"] for item in default_payload["workflows"]] == ["fast"]
    assert {item["name"] for item in included_payload["workflows"]} == {"fast", "slow"}
    assert next(item for item in included_payload["workflows"] if item["name"] == "slow")["tags"] == ["verification", "slow"]


def test_mcp_workspace_root_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(ValueError):
        require_workspace({"workspace_root": str(tmp_path / ".." / "workspace")})


def test_mcp_workspace_root_uses_environment_default(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    monkeypatch.setenv("VISUAL_AGENT_WORKSPACE", str(workspace.root))

    resolved = require_workspace({})

    assert resolved.root == workspace.root


def test_mcp_startup_args_set_workspace_environment(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    monkeypatch.delenv("VISUAL_AGENT_WORKSPACE", raising=False)

    _apply_startup_args(["--workspace-root", str(workspace.root)])

    assert os.environ["VISUAL_AGENT_WORKSPACE"] == str(workspace.root)


def test_mcp_workspace_root_rejects_system_path_with_structured_error() -> None:
    system_path = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    if not system_path.exists():
        pytest.skip("System32 path is unavailable on this environment.")

    assert mcp_workspace_root_allowed(system_path) is False
    result = asyncio.run(call_tool("list_workflows", {"workspace_root": str(system_path)}))
    payload = content_payload(result)

    assert "error" in payload
    assert "outside allowed MCP roots" in payload["error"]


def test_mcp_unknown_workflow_returns_structured_error(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    result = asyncio.run(call_tool("validate_workflow", {"workspace_root": str(workspace.root), "workflow_name": "missing"}))
    payload = content_payload(result)

    assert "error" in payload
    assert "Workflow not found" in payload["error"]


def test_mcp_call_audit_writes_entry_and_exit_events(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    asyncio.run(call_tool("list_workflows", {"workspace_root": str(workspace.root)}))
    audit_path = workspace.root / "gui" / "actions.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert len(events) >= 2
    assert events[-2]["action"] == "mcp:list_workflows"
    assert events[-2]["status"] == "started"
    assert events[-1]["action"] == "mcp:list_workflows"
    assert events[-1]["status"] == "success"


def assert_mcp_tool_audited(workspace, tool_name: str, args: dict[str, object]) -> None:
    asyncio.run(call_tool(tool_name, {"workspace_root": str(workspace.root), **args}))
    audit_path = workspace.root / "gui" / "actions.jsonl"
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]

    assert events[-2]["action"] == f"mcp:{tool_name}"
    assert events[-2]["status"] == "started"
    assert events[-1]["action"] == f"mcp:{tool_name}"
    assert events[-1]["status"] in {
        "success",
        "none",
        "found",
        "no_failure",
        "saved",
        "suggested",
        "needs_model",
        "ready",
        "applied",
        "verified",
        "applied_unverified",
        "rolled_back",
        "rollback_failed",
        "manual_rolled_back",
        "not_found",
        "blocked",
    }


def test_mcp_get_session_context_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "get_session_context", {})


def test_mcp_summarize_latest_failure_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "summarize_latest_failure", {})


def test_mcp_diagnose_failure_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "diagnose_failure", {})


def test_mcp_repair_workflow_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "repair_workflow", {})


def test_mcp_auto_repair_failure_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "auto_repair_failure", {})


def test_mcp_list_repair_history_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "list_repair_history", {})


def test_mcp_rollback_repair_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "rollback_repair", {})


def test_mcp_get_repair_health_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "get_repair_health", {})


def test_mcp_list_benchmarks_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "list_benchmarks", {})


def test_mcp_build_benchmark_plan_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "build_benchmark_plan", {})


def test_mcp_build_benchmark_draft_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "build_benchmark_draft", {"scenario_id": "stagehand_act_extract_1"})


def test_mcp_run_browser_smoke_writes_audit_entry(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    monkeypatch.setattr(
        "visual_agent.browser_smoke.run_browser_smoke",
        lambda **_kwargs: {"status": "success", "url": "https://example.test", "run_dir": "fake", "issues": []},
    )
    assert_mcp_tool_audited(workspace, "run_browser_smoke", {"url": "https://example.test"})


def test_mcp_run_browser_smoke_suite_writes_audit_entry(tmp_path, monkeypatch) -> None:
    workspace = init_workspace(tmp_path / "workspace")
    (workspace.root / "suite.json").write_text('{"cases":[{"id":"home","url":"https://example.test/home"}]}', encoding="utf-8")

    monkeypatch.setattr(
        "visual_agent.browser_smoke_suite.run_browser_smoke_suite",
        lambda *_args, **_kwargs: {"status": "success", "suite_name": "suite", "case_count": 1, "passed_count": 1, "failed_count": 0, "results": []},
    )
    assert_mcp_tool_audited(workspace, "run_browser_smoke_suite", {"suite_file": "suite.json"})


def test_mcp_save_task_context_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "save_task_context", {"task": "Fix checkout"})


def test_mcp_run_verification_writes_audit_entry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    assert_mcp_tool_audited(workspace, "run_verification", {})


def test_mcp_generate_workflow_dry_run_returns_valid_yaml(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    payload = content_payload(
        asyncio.run(
            call_tool(
                "generate_workflow",
                {
                    "workspace_root": str(workspace.root),
                    "description": "Verify the user can log in and see the dashboard",
                    "dry_run": True,
                },
            )
        )
    )

    assert payload["status"] == "success"
    assert payload["saved_to"] is None
    assert "observe_browser" in payload["yaml"]
    assert "visibility: private" in payload["yaml"]
    assert payload["quality_score"] >= 0


def test_mcp_generate_workflow_accepts_page_type_hint(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace", with_demo=False)

    payload = content_payload(
        asyncio.run(
            call_tool(
                "generate_workflow",
                {
                    "workspace_root": str(workspace.root),
                    "description": "Verify product page loads",
                    "page_type": "ecommerce",
                    "dry_run": True,
                },
            )
        )
    )

    assert payload["status"] == "success"
    assert "ecommerce" in payload["yaml"]
    assert "[page_type:" not in payload["yaml"]


def test_mcp_save_task_context_updates_session_context(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "workspace")

    saved = content_payload(
        asyncio.run(
            call_tool(
                "save_task_context",
                {
                    "workspace_root": str(workspace.root),
                    "task": "Fix checkout export",
                    "analyzed_files": ["src/checkout.py", "tests/test_checkout.py"],
                    "root_cause": "button handler missing",
                    "plan": "patch handler and rerun verification",
                    "tried": ["ran unit tests"],
                },
            )
        )
    )
    context = content_payload(asyncio.run(call_tool("get_session_context", {"workspace_root": str(workspace.root)})))

    assert saved["status"] == "saved"
    assert "Fix checkout export" in context["snapshot"]
    assert "src/checkout.py" in context["snapshot"]
    assert context["within_budget"] is True
