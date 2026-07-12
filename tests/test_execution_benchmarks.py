from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from visual_agent.execution_benchmarks import (
    _verify_sha256,
    execution_benchmark_commands,
    load_execution_benchmark_tasks,
    render_execution_benchmark_worker_prompt,
    validate_execution_benchmark_task,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def frozen_repository_baseline(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tasks = load_execution_benchmark_tasks()
    repository_tasks = [task for task in tasks if task["id"] in {"B1", "B2", "B3", "B4"}]
    targets = [task["target"] for task in repository_tasks]
    revisions = {str(target["base_revision"]) for target in targets}
    patches = {str(target["seed_patch"]) for target in targets}
    assert len(revisions) == 1
    assert len(patches) == 1

    destination = tmp_path_factory.mktemp("execution-benchmark-frozen-baseline")
    archive = destination / "baseline.zip"
    extracted = destination / "target"
    extracted.mkdir()
    completed = subprocess.run(
        ["git", "archive", "--format=zip", f"--output={archive}", revisions.pop()],
        cwd=REPO_ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    shutil.unpack_archive(archive, extracted)

    patch_path = REPO_ROOT / patches.pop()
    completed = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch_path)],
        cwd=extracted,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return extracted


def test_execution_benchmark_suite_loads_exactly_b1_through_b5_without_quiet_verification() -> None:
    tasks = load_execution_benchmark_tasks()

    assert [task["id"] for task in tasks] == ["B1", "B2", "B3", "B4", "B5"]
    for task in tasks:
        commands = execution_benchmark_commands(task)
        assert commands
        assert all(" -q" not in command and not command.startswith("pytest ") for command in commands)
        assert task["protocol"]["allow_test_edits"] is True

    for task in tasks[1:4]:
        operator = task["operator_verification"]
        assert operator["kind"] == "operator_private_verifier"
        assert operator["verifier"].startswith("tests/benchmarks/private_verifiers/")
        assert len(operator["verifier_sha256"]) == 64
        assert "{target_root}" in operator["command_template"]


def test_worker_prompt_does_not_leak_operator_verification_or_private_repair_evidence() -> None:
    tasks = load_execution_benchmark_tasks()

    for task in tasks:
        prompt = render_execution_benchmark_worker_prompt(task)
        assert str(task["objective"]) in prompt
        for command in execution_benchmark_commands(task):
            assert command not in prompt
        assert "likely_relevant_files" not in prompt
        assert "private_verifiers" not in prompt

    b5_prompt = render_execution_benchmark_worker_prompt(tasks[-1]).lower()
    assert "bearer" not in b5_prompt
    assert "access_token" not in b5_prompt
    assert str(tasks[-1]["repair_injection"]["additional_acceptance"]).lower() not in b5_prompt


def test_validator_rejects_tampered_operator_verifier_hash() -> None:
    task = copy.deepcopy(load_execution_benchmark_tasks()[1])
    task["operator_verification"]["verifier_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="hash mismatch"):
        validate_execution_benchmark_task(task, repo_root=REPO_ROOT)


def test_validator_requires_explicit_allow_test_edits() -> None:
    task = copy.deepcopy(load_execution_benchmark_tasks()[0])
    task["protocol"].pop("allow_test_edits")

    with pytest.raises(ValueError, match="allow_test_edits"):
        validate_execution_benchmark_task(task, repo_root=REPO_ROOT)


def test_artifact_hash_is_stable_across_git_line_endings(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_bytes(b"first\r\nsecond\r\n")
    expected = hashlib.sha256(b"first\nsecond\n").hexdigest()

    _verify_sha256(artifact, expected, label="line-ending-test")


def test_b2_private_verifier_enforces_non_doctor_flag_policy() -> None:
    task = load_execution_benchmark_tasks()[1]
    verifier_path = REPO_ROOT / task["operator_verification"]["verifier"]
    spec = importlib.util.spec_from_file_location("b2_private_verifier_policy_test", verifier_path)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    def fake_cli(policy: str):
        def invoke(args: list[str]) -> int:
            action = args[1]
            flagged = "--installed-only" in args
            if flagged and policy == "reject":
                print("--installed-only is doctor-only", file=sys.stderr)
                return 2
            print(json.dumps({"action": action, "agent": "codex"}, sort_keys=True))
            if flagged and policy == "warn":
                print("--installed-only ignored: doctor-only option", file=sys.stderr)
            return 0

        return invoke

    verifier._assert_non_doctor_flag_policy(fake_cli("reject"))
    verifier._assert_non_doctor_flag_policy(fake_cli("warn"))
    with pytest.raises(AssertionError, match="silently ignored"):
        verifier._assert_non_doctor_flag_policy(fake_cli("silent"))


@pytest.mark.parametrize(
    ("task_id", "failure_marker"),
    [
        ("B2", "--installed-only JSON was not accepted"),
        ("B3", "original raw failure tail is missing"),
        ("B4", "recycled PID with mismatched identity"),
    ],
)
def test_b2_through_b4_private_verifiers_fail_on_frozen_baseline(
    frozen_repository_baseline: Path,
    task_id: str,
    failure_marker: str,
) -> None:
    task = next(task for task in load_execution_benchmark_tasks() if task["id"] == task_id)
    verifier = REPO_ROOT / task["operator_verification"]["verifier"]

    completed = subprocess.run(
        [sys.executable, str(verifier), str(frozen_repository_baseline)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert failure_marker in completed.stderr


def test_b5_private_verifier_reproduces_a_real_seed_failure() -> None:
    target = REPO_ROOT / "tests" / "benchmarks" / "fixtures" / "b5_secret_redaction"
    verifier = REPO_ROOT / "tests" / "benchmarks" / "private_verifiers" / "b5_secret_redaction.py"

    completed = subprocess.run(
        [sys.executable, str(verifier), str(target)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "Bearer credential remained in persisted text" in completed.stderr
