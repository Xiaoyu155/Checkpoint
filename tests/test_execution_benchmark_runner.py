from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from visual_agent.execution_benchmark_runner import (
    BenchmarkSetupError,
    build_direct_codex_argv,
    capture_diff_evidence,
    freeze_execution_benchmark_context,
    materialize_frozen_target,
    run_direct_codex_benchmark,
    run_external_process,
)
from visual_agent.execution_benchmarks import load_execution_benchmark_tasks


SOURCE_COMMIT = "a" * 40
SOURCE_TREE = "c" * 40
SYNTHETIC_COMMIT = "b" * 40
SYNTHETIC_TREE = "d" * 40
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_run_external_process_timeout_terminates_isolated_tree(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeProcess:
        pid = 2468
        returncode = None
        communicate_calls = 0

        def communicate(self, input=None, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                raise subprocess.TimeoutExpired("worker", timeout, output="partial-out", stderr="partial-err")
            return "partial-out", "partial-err"

    process = FakeProcess()

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    def fake_terminate(candidate):
        captured["terminated"] = candidate.pid
        candidate.returncode = -9
        return True

    monkeypatch.setattr("visual_agent.execution_benchmark_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "visual_agent.execution_benchmark_runner.isolated_process_group_kwargs",
        lambda: {"start_new_session": True},
    )
    monkeypatch.setattr("visual_agent.execution_benchmark_runner.terminate_process_tree", fake_terminate)

    result = run_external_process(
        ["worker", "--child"],
        cwd=tmp_path,
        timeout_seconds=0.1,
        stdin_text="request",
    )

    assert result["exit_code"] == 124
    assert result["timed_out"] is True
    assert result["stdout"] == "partial-out"
    assert "partial-err" in result["stderr"]
    assert "Timed out after" in result["stderr"]
    assert captured["terminated"] == 2468
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdin"] is subprocess.PIPE


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _write_task(root: Path, task_id: str = "B1") -> Path:
    task_dir = root / "tests" / "benchmarks" / "execution_tasks"
    task_dir.mkdir(parents=True, exist_ok=True)
    seed_patch = root / "tests" / "benchmarks" / "seeds" / "common.patch"
    seed_patch.parent.mkdir(parents=True, exist_ok=True)
    seed_patch.write_text("", encoding="utf-8")
    payload: dict = {
        "schema_version": 1,
        "id": task_id,
        "title": "Focused task",
        "objective": "Fix the focused behavior without unrelated changes.",
        "acceptance_criteria": ["The operator check passes.", "The target contains a real change."],
        "verification": {"commands": ["python -m pytest tests/test_target.py"]},
        "target": {
            "base_revision": SOURCE_COMMIT,
            "seed_patch": "tests/benchmarks/seeds/common.patch",
            "seed_patch_sha256": _sha(seed_patch),
        },
        "protocol": {
            "max_worker_turns": 1,
            "requires_isolated_worktree": True,
            "allow_test_edits": True,
        },
    }
    path = task_dir / f"{task_id.lower()}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _write_b5_task(root: Path) -> Path:
    fixture = root / "tests" / "benchmarks" / "fixtures" / "b5"
    fixture.mkdir(parents=True, exist_ok=True)
    target = fixture / "security_target.py"
    checks = fixture / "public_checks.py"
    target.write_text("def sanitize(value):\n    return value\n", encoding="utf-8")
    checks.write_text("print('public ok')\n", encoding="utf-8")
    verifier = root / "tests" / "benchmarks" / "private_verifiers" / "b5.py"
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text("raise SystemExit(1)\n", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "id": "B5",
        "title": "Repair chain",
        "objective": "Redact persisted credentials while preserving diagnostics.",
        "acceptance_criteria": ["Public and private verification pass after repair."],
        "target": {
            "source_dir": "tests/benchmarks/fixtures/b5",
            "seed_files_sha256": {
                "security_target.py": _sha(target),
                "public_checks.py": _sha(checks),
            },
        },
        "verification": {
            "round_one_commands": ["python -m unittest public_checks.py"],
            "final_commands": ["python -m unittest public_checks.py"],
        },
        "repair_injection": {
            "kind": "operator_private_verifier",
            "verifier": "tests/benchmarks/private_verifiers/b5.py",
            "verifier_sha256": _sha(verifier),
            "command_template": "python tests/benchmarks/private_verifiers/b5.py {target_root}",
            "additional_acceptance": "Also redact Bearer credentials.",
        },
        "protocol": {
            "max_worker_turns": 2,
            "requires_isolated_worktree": True,
            "allow_test_edits": True,
        },
    }
    path = root / "tests" / "benchmarks" / "execution_tasks" / "b5.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class FakeGitRunner:
    def __init__(
        self,
        *,
        tracked: list[str],
        untracked: list[str],
        seed_patch: str = "",
        base_commit: str = SOURCE_COMMIT,
    ) -> None:
        self.tracked = tracked
        self.untracked = untracked
        self.seed_patch = seed_patch
        self.base_commit = base_commit
        self.calls: list[dict] = []

    def __call__(self, command, *, cwd, timeout_seconds, stdin_text=None, shell=False):
        argv = list(command) if isinstance(command, list) else [str(command)]
        self.calls.append({"command": command, "cwd": Path(cwd), "stdin": stdin_text, "shell": shell})
        text = " ".join(argv)
        if "rev-parse HEAD^{tree}" in text:
            value = SYNTHETIC_TREE if (Path(cwd) / ".git").exists() else SOURCE_TREE
            return {"exit_code": 0, "stdout": value + "\n", "stderr": ""}
        if "rev-parse HEAD" in text:
            value = SYNTHETIC_COMMIT if (Path(cwd) / ".git").exists() else self.base_commit
            return {"exit_code": 0, "stdout": value + "\n", "stderr": ""}
        if "ls-files --others" in text:
            return {"exit_code": 0, "stdout": "\0".join(self.untracked) + ("\0" if self.untracked else ""), "stderr": ""}
        if "ls-tree -r --name-only" in text:
            return {"exit_code": 0, "stdout": "\0".join(self.tracked) + ("\0" if self.tracked else ""), "stderr": ""}
        if "diff --binary --full-index HEAD" in text:
            return {"exit_code": 0, "stdout": self.seed_patch, "stderr": ""}
        if argv[:4] == ["git", "worktree", "add", "--detach"]:
            destination = Path(argv[-2])
            destination.mkdir(parents=True)
            (destination / ".git").mkdir()
            for relative in self.tracked:
                source = Path(cwd) / relative
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if argv[:4] == ["git", "worktree", "remove", "--force"]:
            shutil.rmtree(Path(argv[-1]), ignore_errors=True)
        if argv[:3] == ["git", "init", "--quiet"]:
            (Path(cwd) / ".git").mkdir(exist_ok=True)
        return {"exit_code": 0, "stdout": "", "stderr": ""}


def _freeze(
    root: Path,
    output: Path,
    task_files: list[Path],
    runner: FakeGitRunner,
    *,
    project_subdir: str = ".",
    ignore_user_config: bool = True,
    model_provider: str | None = None,
    model_provider_options: dict | None = None,
    service_tier: str | None = None,
    windows_sandbox: str | None = None,
    trust_target: bool = False,
) -> dict:
    return freeze_execution_benchmark_context(
        target_repo=root,
        operator_root=root,
        output_dir=output,
        task_files=task_files,
        model="gpt-fixed",
        reasoning_effort="high",
        sandbox="workspace-write",
        approval="never",
        ignore_user_config=ignore_user_config,
        model_provider=model_provider,
        model_provider_options=model_provider_options,
        service_tier=service_tier,
        windows_sandbox=windows_sandbox,
        trust_target=trust_target,
        codex_version="codex-cli test",
        process_runner=runner,
        project_subdir=project_subdir,
        context_id="context-1",
        created_at="2026-07-10T00:00:00+00:00",
    )


def test_freeze_context_and_materialize_common_and_b5_targets(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    project = repo / "project"
    project.mkdir()
    (project / "app.py").write_text("base = True\n", encoding="utf-8")
    (project / "local.txt").write_text("seed overlay\n", encoding="utf-8")
    b1 = _write_task(repo)
    seed_patch = repo / "tests" / "benchmarks" / "seeds" / "common.patch"
    seed_patch.write_text("diff --git seed\n", encoding="utf-8")
    b1_payload = json.loads(b1.read_text(encoding="utf-8"))
    b1_payload["target"]["seed_patch_sha256"] = _sha(seed_patch)
    b1.write_text(json.dumps(b1_payload, indent=2), encoding="utf-8")
    b5 = _write_b5_task(repo)
    runner = FakeGitRunner(
        tracked=["project/app.py"],
        untracked=["project/local.txt"],
        seed_patch="diff --git seed\n",
    )

    context = _freeze(repo, tmp_path / "frozen", [b1, b5], runner, project_subdir="project")

    assert context["target"]["base_commit"] == SOURCE_COMMIT
    assert context["target"]["seed_patch_sha256"] == hashlib.sha256(b"diff --git seed\n").hexdigest()
    assert context["codex"]["ignore_user_config"] is True
    assert context["codex"]["provider"] == {"id": "", "options": {}, "service_tier": ""}
    assert context["codex"]["runtime"] == {"windows_sandbox": "", "trust_target": False}
    assert json.loads(Path(context["path"]).read_text(encoding="utf-8"))["codex"]["ignore_user_config"] is True
    commands = [call["command"] for call in runner.calls]
    assert ["codex", "exec", "--ignore-user-config", "--help"] in commands
    assert ["codex", "exec", "--ignore-user-config", "resume", "--help"] in commands
    assert len(context["harness"]["bundle_sha256"]) == 64
    assert {item["name"] for item in context["harness"]["files"]} >= {
        "execution_benchmark_runner.py",
        "codex_exec.py",
        "subprocess_window.py",
    }
    assert (tmp_path / "frozen" / "targets" / "common" / "seed_files" / "project" / "local.txt").is_file()
    assert context["tasks"]["B5"]["target"]["kind"] == "fixture"

    common = materialize_frozen_target(
        context=context,
        context_root=tmp_path / "frozen",
        task_id="B1",
        run_dir=tmp_path / "common-run",
        process_runner=runner,
    )
    fixture = materialize_frozen_target(
        context=context,
        context_root=tmp_path / "frozen",
        task_id="B5",
        run_dir=tmp_path / "fixture-run",
        process_runner=runner,
    )

    assert Path(common["root"], "project", "app.py").read_text(encoding="utf-8") == "base = True\n"
    assert Path(common["root"], "project", "local.txt").is_file()
    assert common["project_subdir"] == "project"
    assert common["project_root"] == str(Path(common["root"], "project"))
    assert common["baseline_commit"] == SYNTHETIC_COMMIT
    apply_call = next(call for call in runner.calls if "git apply --binary" in " ".join(call["command"]))
    assert apply_call["stdin"] == "diff --git seed\n"
    assert fixture["kind"] == "fixture"
    assert Path(fixture["root"], "security_target.py").is_file()
    assert not Path(fixture["root"], "app.py").exists()


def test_freeze_context_fails_fast_when_ignore_user_config_is_unsupported(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)

    class UnsupportedResumeRunner(FakeGitRunner):
        def __call__(self, command, **kwargs):
            if list(command) == ["codex", "exec", "--ignore-user-config", "resume", "--help"]:
                return {"exit_code": 2, "stdout": "", "stderr": "unknown option"}
            return super().__call__(command, **kwargs)

    unsupported = UnsupportedResumeRunner(tracked=["app.py"], untracked=[])
    with pytest.raises(BenchmarkSetupError, match="resume --ignore-user-config support"):
        _freeze(repo, tmp_path / "unsupported", [task], unsupported)
    assert not (tmp_path / "unsupported").exists()

    allowed = FakeGitRunner(tracked=["app.py"], untracked=[])
    context = _freeze(
        repo,
        tmp_path / "user-config-allowed",
        [task],
        allowed,
        ignore_user_config=False,
    )
    assert context["codex"]["ignore_user_config"] is False
    assert all("--ignore-user-config" not in call["command"] for call in allowed.calls)


def test_freeze_context_records_secret_free_provider_policy(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)

    context = _freeze(
        repo,
        tmp_path / "provider-context",
        [task],
        FakeGitRunner(tracked=["app.py"], untracked=[]),
        model_provider="custom",
        model_provider_options={
            "name": "custom",
            "base_url": "http://127.0.0.1:8080/v1",
            "wire_api": "responses",
            "requires_openai_auth": True,
        },
        service_tier="fast",
    )

    assert context["codex"]["provider"] == {
        "id": "custom",
        "options": {
            "name": "custom",
            "base_url": "http://127.0.0.1:8080/v1",
            "wire_api": "responses",
            "requires_openai_auth": True,
        },
        "service_tier": "fast",
    }


def test_freeze_context_records_secret_free_runtime_policy(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)

    context = _freeze(
        repo,
        tmp_path / "runtime-context",
        [task],
        FakeGitRunner(tracked=["app.py"], untracked=[]),
        windows_sandbox="elevated",
        trust_target=True,
    )

    assert context["codex"]["runtime"] == {"windows_sandbox": "elevated", "trust_target": True}
    persisted = json.loads(Path(context["path"]).read_text(encoding="utf-8"))
    assert persisted["codex"]["runtime"] == {"windows_sandbox": "elevated", "trust_target": True}


def test_freeze_context_rejects_secret_bearing_provider_options_before_writing(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)
    output = tmp_path / "unsafe-provider"

    with pytest.raises(ValueError, match="secret-bearing fields"):
        _freeze(
            repo,
            output,
            [task],
            FakeGitRunner(tracked=["app.py"], untracked=[]),
            model_provider="custom",
            model_provider_options={"http_headers": {"Authorization": "Bearer secret"}},
        )

    assert not output.exists()


def test_freeze_real_b1_to_b5_contracts_with_operator_artifacts(tmp_path) -> None:
    tasks = load_execution_benchmark_tasks()
    task_files = sorted((REPO_ROOT / "tests" / "benchmarks" / "execution_tasks").glob("*.json"))
    repository_tasks = [task for task in tasks if task["id"] != "B5"]
    base_revision = str(repository_tasks[0]["target"]["base_revision"])
    patch_path = REPO_ROOT / str(repository_tasks[0]["target"]["seed_patch"])
    patch_text = patch_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    target = tmp_path / "target"
    target.mkdir()
    (target / "app.py").write_text("base = True\n", encoding="utf-8")
    runner = FakeGitRunner(
        tracked=["app.py"],
        untracked=[],
        seed_patch=patch_text,
        base_commit=base_revision,
    )

    context = freeze_execution_benchmark_context(
        target_repo=target,
        operator_root=REPO_ROOT,
        output_dir=tmp_path / "frozen-real-contracts",
        task_files=task_files,
        model="gpt-fixed",
        reasoning_effort="high",
        sandbox="workspace-write",
        approval="never",
        codex_version="codex-cli test",
        process_runner=runner,
        context_id="real-contracts",
    )

    assert [task["id"] for task in tasks] == ["B1", "B2", "B3", "B4", "B5"]
    assert set(context["tasks"]) == {"B1", "B2", "B3", "B4", "B5"}
    assert context["target"]["base_commit"] == base_revision
    for task_id in ("B2", "B3", "B4"):
        gate = next(
            item
            for item in context["tasks"][task_id]["operator_gates"]
            if item["id"] == "operator-verifier"
        )
        assert (tmp_path / "frozen-real-contracts" / gate["artifact_path"]).is_file()
    assert context["tasks"]["B5"]["target"]["kind"] == "fixture"


def _fake_materializer(**kwargs) -> dict:
    target = Path(kwargs["run_dir"]) / "target"
    target.mkdir(parents=True)
    project = target if kwargs["task_id"] == "B5" else target / "xiao"
    project.mkdir(exist_ok=True)
    (project / "base.py").write_text("value = 1\n", encoding="utf-8")
    return {
        "root": str(target),
        "project_root": str(project),
        "project_subdir": "." if project == target else "xiao",
        "kind": "common",
        "snapshot_sha256": "snapshot",
        "baseline_commit": "baseline",
        "baseline_tree": "tree",
    }


def _fake_diff(**kwargs) -> dict:
    diff_dir = Path(kwargs["run_dir"]) / "diff"
    diff_dir.mkdir(parents=True, exist_ok=True)
    patch = diff_dir / "final.patch"
    patch.write_text("diff --git a/base.py b/base.py\n", encoding="utf-8")
    return {
        "baseline_commit": kwargs["baseline_commit"],
        "patch_path": "diff/final.patch",
        "patch_sha256": _sha(patch),
        "changed_files": [{"path": "base.py", "status": "M", "lines_added": 1, "lines_removed": 1}],
        "file_count": 1,
    }


def test_direct_run_pins_argv_uses_stdin_and_appends_manifests(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)
    context = _freeze(repo, tmp_path / "frozen", [task], FakeGitRunner(tracked=["app.py"], untracked=[]))
    calls: list[dict] = []

    def runner(command, *, cwd, timeout_seconds, stdin_text=None, shell=False):
        calls.append({"command": command, "cwd": Path(cwd), "stdin": stdin_text, "shell": shell})
        if isinstance(command, list) and "exec" in command:
            Path(cwd, "base.py").write_text("value = 2\n", encoding="utf-8")
            return {
                "exit_code": 0,
                "stdout": (
                    '{"type":"thread.started","thread_id":"thread-1"}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":4,"output_tokens":2}}\n'
                ),
                "stderr": "codex diagnostic\n",
            }
        if Path(cwd, "base.py").read_text(encoding="utf-8") == "value = 1\n":
            return {"exit_code": 1, "stdout": "baseline failure\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "operator passed\n", "stderr": "operator note\n"}

    first = run_direct_codex_benchmark(
        context_path=context["path"],
        task_id="B1",
        artifact_root=tmp_path / "artifacts",
        run_id="run-1",
        process_runner=runner,
        target_materializer=_fake_materializer,
        diff_collector=_fake_diff,
    )
    second = run_direct_codex_benchmark(
        context_path=context["path"],
        task_id="B1",
        artifact_root=tmp_path / "artifacts",
        run_id="run-2",
        process_runner=runner,
        target_materializer=_fake_materializer,
        diff_collector=_fake_diff,
    )

    assert first["status"] == "PASS"
    codex_call = next(call for call in calls if isinstance(call["command"], list) and "exec" in call["command"])
    assert codex_call["command"] == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "exec",
        "--ignore-user-config",
        "--json",
        "--model",
        "gpt-fixed",
        "-c",
        "model_reasoning_effort=high",
        "-",
    ]
    assert "Fix the focused behavior" in codex_call["stdin"]
    assert "pytest" not in codex_call["stdin"]
    assert all("Fix the focused behavior" not in str(item) for item in codex_call["command"])
    assert codex_call["cwd"].name == "xiao"
    invocation = first["codex"]["invocations"][0]
    run_dir = Path(first["manifest_path"]).parent
    assert (run_dir / invocation["stdout_jsonl_path"]).read_text(encoding="utf-8").startswith('{"type":"thread.started"')
    assert (run_dir / invocation["stderr_path"]).read_text(encoding="utf-8") == "codex diagnostic\n"
    assert first["usage"]["turns"][0]["usage"]["cached_input_tokens"] == 4
    assert first["usage"]["total_tokens"] == 12
    assert Path(first["manifest_path"]).is_file()
    assert Path(second["manifest_path"]).is_file()
    index_lines = Path(first["index_path"]).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["run_id"] for line in index_lines] == ["run-1", "run-2"]


def test_direct_run_reports_worker_failure_before_expected_gate_failure(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)
    context = _freeze(repo, tmp_path / "frozen", [task], FakeGitRunner(tracked=["app.py"], untracked=[]))

    def runner(command, *, cwd, timeout_seconds, stdin_text=None, shell=False):
        if isinstance(command, list) and "exec" in command:
            return {"exit_code": 1, "stdout": "", "stderr": "provider unavailable"}
        return {"exit_code": 1, "stdout": "expected gate failure", "stderr": ""}

    result = run_direct_codex_benchmark(
        context_path=context["path"],
        task_id="B1",
        artifact_root=tmp_path / "artifacts",
        run_id="worker-failed",
        process_runner=runner,
        target_materializer=_fake_materializer,
        diff_collector=_fake_diff,
    )

    assert result["status"] == "FAIL"
    assert result["reason"] == "codex_exec_failed"
    assert result["first_verdict"] == "FAIL"


def test_direct_run_trusts_materialized_git_root_not_project_subdir(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)
    context = _freeze(
        repo,
        tmp_path / "frozen",
        [task],
        FakeGitRunner(tracked=["app.py"], untracked=[]),
        windows_sandbox="elevated",
        trust_target=True,
    )
    calls: list[dict] = []

    def runner(command, *, cwd, timeout_seconds, stdin_text=None, shell=False):
        calls.append({"command": command, "cwd": Path(cwd), "stdin": stdin_text, "shell": shell})
        if isinstance(command, list) and "exec" in command:
            Path(cwd, "base.py").write_text("value = 2\n", encoding="utf-8")
            return {
                "exit_code": 0,
                "stdout": (
                    '{"type":"thread.started","thread_id":"thread-1"}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}\n'
                ),
                "stderr": "",
            }
        if Path(cwd, "base.py").read_text(encoding="utf-8") == "value = 1\n":
            return {"exit_code": 1, "stdout": "baseline failure\n", "stderr": ""}
        return {"exit_code": 0, "stdout": "operator passed\n", "stderr": ""}

    result = run_direct_codex_benchmark(
        context_path=context["path"],
        task_id="B1",
        artifact_root=tmp_path / "artifacts",
        run_id="runtime-run",
        process_runner=runner,
        target_materializer=_fake_materializer,
        diff_collector=_fake_diff,
    )

    assert result["status"] == "PASS"
    codex_call = next(call for call in calls if isinstance(call["command"], list) and "exec" in call["command"])
    trusted_root = str(Path(result["target"]["root"]).resolve())
    project_root = str(Path(result["target"]["project_root"]).resolve())
    assert codex_call["cwd"] == Path(project_root)
    assert f"projects.'{trusted_root}'.trust_level='trusted'" in codex_call["command"]
    assert f"projects.'{project_root}'.trust_level='trusted'" not in codex_call["command"]
    assert "windows.sandbox='elevated'" in codex_call["command"]


def test_changed_frozen_task_is_invalid_before_codex_runs(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)
    context = _freeze(repo, tmp_path / "frozen", [task], FakeGitRunner(tracked=["app.py"], untracked=[]))
    frozen_task = tmp_path / "frozen" / context["tasks"]["B1"]["definition_path"]
    frozen_task.write_text("{}\n", encoding="utf-8")
    calls: list = []

    def should_not_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("no external process should run after a frozen task mismatch")

    result = run_direct_codex_benchmark(
        context_path=context["path"],
        task_id="B1",
        artifact_root=tmp_path / "artifacts",
        run_id="invalid-1",
        process_runner=should_not_run,
        target_materializer=_fake_materializer,
        diff_collector=_fake_diff,
    )

    assert result["status"] == "INVALID_SETUP"
    assert "hash mismatch" in result["reason"]
    assert calls == []
    assert json.loads(Path(result["index_path"]).read_text(encoding="utf-8"))["status"] == "INVALID_SETUP"


def test_passing_operator_baseline_blocks_before_codex(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)
    context = _freeze(repo, tmp_path / "frozen", [task], FakeGitRunner(tracked=["app.py"], untracked=[]))
    codex_calls = 0

    def runner(command, *, cwd, timeout_seconds, stdin_text=None, shell=False):
        nonlocal codex_calls
        if isinstance(command, list) and "exec" in command:
            codex_calls += 1
        return {"exit_code": 0, "stdout": "already passing", "stderr": ""}

    result = run_direct_codex_benchmark(
        context_path=context["path"],
        task_id="B1",
        artifact_root=tmp_path / "artifacts",
        run_id="baseline-pass",
        process_runner=runner,
        target_materializer=_fake_materializer,
        diff_collector=_fake_diff,
    )

    assert result["status"] == "INVALID_SETUP"
    assert result["baseline_verification"]["verdict"] == "PASS"
    assert codex_calls == 0


def test_freeze_rejects_wrong_base_and_freezes_operator_verifier(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_task(repo)
    payload = json.loads(task.read_text(encoding="utf-8"))
    payload["target"]["base_revision"] = "e" * 40
    task.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    git = FakeGitRunner(tracked=["app.py"], untracked=[])

    with pytest.raises(ValueError, match="does not match frozen HEAD"):
        _freeze(repo, tmp_path / "wrong", [task], git)

    verifier = repo / "operator_check.py"
    verifier.write_text("raise SystemExit(1)\n", encoding="utf-8")
    payload["target"]["base_revision"] = SOURCE_COMMIT
    payload["operator_verification"] = {
        "kind": "operator_private_verifier",
        "verifier": "operator_check.py",
        "verifier_sha256": _sha(verifier),
        "command_template": "python operator_check.py {target_root}",
    }
    task.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    context = _freeze(repo, tmp_path / "valid", [task], git)
    gate = next(item for item in context["tasks"]["B1"]["operator_gates"] if item["id"] == "operator-verifier")
    assert gate["artifact_sha256"] == _sha(verifier)
    assert (tmp_path / "valid" / gate["artifact_path"]).read_bytes() == verifier.read_bytes()


def test_b5_resumes_same_thread_and_rechecks_private_gate(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("base = True\n", encoding="utf-8")
    task = _write_b5_task(repo)
    context = _freeze(repo, tmp_path / "frozen", [task], FakeGitRunner(tracked=["app.py"], untracked=[]))
    codex_calls: list[dict] = []
    private_calls = 0

    def runner(command, *, cwd, timeout_seconds, stdin_text=None, shell=False):
        nonlocal private_calls
        if isinstance(command, list) and "exec" in command:
            codex_calls.append({"argv": list(command), "stdin": stdin_text})
            return {
                "exit_code": 0,
                "stdout": (
                    '{"type":"thread.started","thread_id":"thread-b5"}\n'
                    '{"type":"turn.completed","usage":{"input_tokens":20,"output_tokens":5}}\n'
                ),
                "stderr": "",
            }
        if "private_verifiers" in str(command) or "operator_artifacts" in str(command):
            private_calls += 1
            if private_calls <= 2:
                return {"exit_code": 1, "stdout": "", "stderr": "Bearer credential remained in persisted text"}
            return {"exit_code": 0, "stdout": "private passed", "stderr": ""}
        return {"exit_code": 0, "stdout": "public passed", "stderr": ""}

    result = run_direct_codex_benchmark(
        context_path=context["path"],
        task_id="B5",
        artifact_root=tmp_path / "artifacts",
        run_id="b5-run",
        process_runner=runner,
        target_materializer=_fake_materializer,
        diff_collector=_fake_diff,
    )

    assert result["status"] == "PASS"
    assert result["first_verdict"] == "FAIL"
    assert result["final_verdict"] == "PASS"
    assert len(codex_calls) == 2
    assert codex_calls[1]["argv"][5:9] == ["exec", "--ignore-user-config", "resume", "--json"]
    assert codex_calls[1]["argv"][-2:] == ["thread-b5", "-"]
    assert "Bearer credential remained" in codex_calls[1]["stdin"]
    assert "Also redact Bearer credentials" in codex_calls[1]["stdin"]
    assert "private_verifiers" not in codex_calls[1]["stdin"]
    assert str(tmp_path / "frozen") not in codex_calls[1]["stdin"]
    assert private_calls == 3
    assert result["usage"]["num_turns"] == 2
    assert result["usage"]["total_tokens"] == 25
    assert result["usage"]["session_ids"] == ["thread-b5"]


def test_capture_diff_evidence_keeps_patch_and_untracked_file(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    untracked = target / "new" / "report.txt"
    untracked.parent.mkdir()
    untracked.write_text("one\ntwo\n", encoding="utf-8")

    def runner(command, *, cwd, timeout_seconds, stdin_text=None, shell=False):
        text = " ".join(command)
        if "status --porcelain" in text:
            return {"exit_code": 0, "stdout": " M app.py\0?? new/report.txt\0", "stderr": ""}
        if "diff --binary" in text:
            return {"exit_code": 0, "stdout": "diff --git a/app.py b/app.py\n", "stderr": ""}
        if "diff --name-status" in text:
            return {"exit_code": 0, "stdout": "M\tapp.py\n", "stderr": ""}
        if "diff --numstat" in text:
            return {"exit_code": 0, "stdout": "2\t1\tapp.py\n", "stderr": ""}
        if "ls-files --others" in text:
            return {"exit_code": 0, "stdout": "new/report.txt\0", "stderr": ""}
        raise AssertionError(command)

    evidence = capture_diff_evidence(
        target_root=target,
        baseline_commit="baseline",
        run_dir=tmp_path / "run",
        process_runner=runner,
    )

    assert evidence["changed_files"] == [
        {"path": "app.py", "status": "M", "lines_added": 2, "lines_removed": 1},
        {"path": "new/report.txt", "status": "A", "lines_added": 2, "lines_removed": 0},
    ]
    assert (tmp_path / "run" / evidence["patch_path"]).read_text(encoding="utf-8").startswith("diff --git")
    manifest = json.loads((tmp_path / "run" / evidence["untracked_manifest_path"]).read_text(encoding="utf-8"))
    assert manifest[0]["sha256"] == hashlib.sha256(untracked.read_bytes()).hexdigest()
    assert (tmp_path / "run" / "diff" / "untracked" / "new" / "report.txt").is_file()


def test_build_direct_codex_argv_requires_frozen_values() -> None:
    argv = build_direct_codex_argv(
        {
            "codex": {
                "executable": "codex",
                "model": "gpt-fixed",
                "reasoning_effort": "high",
                "sandbox": "workspace-write",
                "approval": "never",
                "ignore_user_config": True,
                "provider": {
                    "id": "custom",
                    "options": {
                        "name": "custom",
                        "base_url": "http://127.0.0.1:8080/v1",
                        "wire_api": "responses",
                        "requires_openai_auth": True,
                    },
                    "service_tier": "fast",
                },
            }
        },
        resume_session_id="thread-1",
    )

    assert argv[:9] == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "exec",
        "--ignore-user-config",
        "resume",
        "--json",
    ]
    assert argv[-2:] == ["thread-1", "-"]
    assert "model_provider='custom'" in argv
    assert "model_providers.custom.base_url='http://127.0.0.1:8080/v1'" in argv
    assert "model_providers.custom.requires_openai_auth=true" in argv
    assert "service_tier='fast'" in argv


def test_build_direct_codex_argv_can_use_frozen_user_config_policy() -> None:
    argv = build_direct_codex_argv(
        {
            "codex": {
                "executable": "codex",
                "model": "gpt-fixed",
                "reasoning_effort": "high",
                "sandbox": "workspace-write",
                "approval": "never",
                "ignore_user_config": False,
                "provider": {"id": "", "options": {}, "service_tier": ""},
            }
        }
    )

    assert argv[:7] == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "exec",
        "--json",
    ]
    assert "--ignore-user-config" not in argv


def test_build_direct_codex_argv_applies_runtime_overrides(tmp_path) -> None:
    trusted_root = tmp_path / "target"
    trusted_root.mkdir()

    argv = build_direct_codex_argv(
        {
            "codex": {
                "executable": "codex",
                "model": "gpt-fixed",
                "reasoning_effort": "high",
                "sandbox": "workspace-write",
                "approval": "never",
                "ignore_user_config": True,
                "provider": {"id": "", "options": {}, "service_tier": ""},
                "runtime": {"windows_sandbox": "elevated", "trust_target": True},
            }
        },
        trusted_project_root=trusted_root,
    )

    assert "windows.sandbox='elevated'" in argv
    assert f"projects.'{trusted_root.resolve()}'.trust_level='trusted'" in argv

    with pytest.raises(BenchmarkSetupError, match="requires a trusted project root"):
        build_direct_codex_argv(
            {
                "codex": {
                    "executable": "codex",
                    "model": "gpt-fixed",
                    "reasoning_effort": "high",
                    "sandbox": "workspace-write",
                    "approval": "never",
                    "ignore_user_config": True,
                    "provider": {"id": "", "options": {}, "service_tier": ""},
                    "runtime": {"windows_sandbox": "", "trust_target": True},
                }
            }
        )
