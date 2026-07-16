from __future__ import annotations

import json

import pytest

from visual_agent import acceptance_contract
from visual_agent.acceptance_contract import assess_acceptance_contract, build_acceptance_contract


def _task_contract(*, requires_change: bool = True) -> dict[str, object]:
    return {
        "intent": "implementation" if requires_change else "read_only",
        "requires_source_change": requires_change,
        "protected_paths": ["validator.py"],
        "requirements": [
            {
                "text": "修复 validator.py 并使现有测试通过",
                "requires_source_change": requires_change,
                "required_artifact_role": "implementation" if requires_change else "",
            }
        ],
    }


def test_user_goal_with_explicit_command_is_sufficient() -> None:
    contract = build_acceptance_contract(
        goal="修复 validator.py；使用 python -m pytest -q 验证。",
        task_contract=_task_contract(),
    )

    assert contract["standard_source"] == "user_goal"
    assert contract["adequacy"] == "sufficient"
    assert contract["verification"] == {
        "required_step_classes": ["test"],
        "required_commands": ["python -m pytest -q"],
    }
    assert contract["digest"]


def test_template_only_contract_is_insufficient() -> None:
    contract = build_acceptance_contract(goal="修复 validator.py", task_contract=_task_contract())
    assessment = assess_acceptance_contract(
        contract,
        requested_steps=[{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        final_phase=True,
    )

    assert contract["standard_source"] == "template"
    assert contract["adequacy"] == "insufficient"
    assert contract["reason_codes"] == ["acceptance_standard_template_only"]
    assert assessment["digest_verified"] is True
    assert assessment["adequacy"] == "insufficient"
    assert assessment["reason_codes"] == ["acceptance_standard_template_only"]


def test_repository_manifest_has_priority(tmp_path) -> None:
    manifest = tmp_path / ".pacer" / "acceptance.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "observable_outcomes": ["validator returns the expected value"],
                "required_artifact_roles": ["implementation"],
                "verification": {"required_commands": ["python -m pytest -q"]},
                "boundary_cases": ["zero"],
            }
        ),
        encoding="utf-8",
    )

    contract = build_acceptance_contract(
        goal="修复 validator.py",
        task_contract=_task_contract(),
        repo_root=tmp_path,
    )

    assert contract["standard_source"] == "repository"
    assert contract["source_path"] == ".pacer/acceptance.json"
    assert contract["adequacy"] == "sufficient"


def test_assessment_rejects_substitute_command() -> None:
    standard = build_acceptance_contract(
        goal="修复 validator.py；使用 python -m pytest -q 验证。",
        task_contract=_task_contract(),
    )

    assessment = assess_acceptance_contract(
        standard,
        requested_steps=[{"name": "tests", "argv": ["python", "-m", "unittest"]}],
        final_phase=True,
    )

    assert assessment["adequacy"] == "insufficient"
    assert assessment["missing_commands"] == ["python -m pytest -q"]


def test_assessment_accepts_exact_python_command_with_absolute_interpreter() -> None:
    standard = build_acceptance_contract(
        goal="修复 validator.py；使用 python -m pytest -q 验证。",
        task_contract=_task_contract(),
    )

    assessment = assess_acceptance_contract(
        standard,
        requested_steps=[{"name": "tests", "argv": [r"C:\venv\Scripts\python.exe", "-m", "pytest", "-q"]}],
        final_phase=True,
    )

    assert assessment["adequacy"] == "sufficient"
    assert assessment["digest_verified"] is True
    assert assessment["reason_codes"] == []


def test_assessment_rejects_contract_changed_after_digest_lock() -> None:
    standard = build_acceptance_contract(
        goal="修复 validator.py；使用 python -m pytest -q 验证。",
        task_contract=_task_contract(),
    )
    standard["observable_outcomes"] = ["different outcome"]

    assessment = assess_acceptance_contract(
        standard,
        requested_steps=[{"name": "tests", "argv": ["python", "-m", "pytest", "-q"]}],
        final_phase=True,
    )

    assert assessment["adequacy"] == "insufficient"
    assert assessment["digest_verified"] is False
    assert "acceptance_standard_digest_mismatch" in assessment["reason_codes"]


def test_manifest_path_cannot_escape_repository(tmp_path, monkeypatch) -> None:
    outside = tmp_path.parent / "outside-acceptance.json"
    outside.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(acceptance_contract, "ACCEPTANCE_MANIFEST_PATHS", ("../outside-acceptance.json",))

    with pytest.raises(ValueError, match="must stay inside"):
        build_acceptance_contract(
            goal="修复 validator.py",
            task_contract=_task_contract(),
            repo_root=tmp_path,
        )


def test_manifest_path_rejects_symbolic_link(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "acceptance.json").write_text("{}", encoding="utf-8")
    pacer_dir = tmp_path / "repo" / ".pacer"
    pacer_dir.parent.mkdir()
    try:
        pacer_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(ValueError, match="symbolic links"):
        build_acceptance_contract(
            goal="修复 validator.py",
            task_contract=_task_contract(),
            repo_root=pacer_dir.parent,
        )
