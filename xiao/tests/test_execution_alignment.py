from __future__ import annotations

from visual_agent.execution_alignment import (
    audit_prompt_text,
    build_worker_prompt_alignment_check,
    lint_worker_prompt_source,
    main,
)


def test_audit_prompt_text_finds_exploration_and_budget_restrictions_case_insensitively() -> None:
    issues = audit_prompt_text(
        "Conserve MODEL Budget: read only the files you need. "
        "DO NOT scan or grep the whole repository."
    )

    assert {item["code"] for item in issues} == {
        "files_read_restriction",
        "model_budget_conservation",
        "repository_scan_ban",
    }


def test_audit_prompt_text_allows_confident_codebase_exploration() -> None:
    text = (
        "Explore the codebase as much as needed to be confident in your change. "
        "Likely-relevant files are navigation hints, not an exploration boundary."
    )

    assert audit_prompt_text(text) == []


def test_audit_prompt_text_rejects_a_bare_repository_search_ban() -> None:
    issues = audit_prompt_text("Do not grep; trust the likely file list instead.")

    assert [item["code"] for item in issues] == ["repository_scan_ban"]


def test_current_worker_prompt_source_has_no_exploration_budget_bans() -> None:
    assert lint_worker_prompt_source() == []


def test_alignment_check_scans_repair_prompts_and_returns_structured_result(tmp_path) -> None:
    source = tmp_path / "chief_dispatch.py"
    source.write_text(
        "def build_worker_prompt():\n"
        "    return 'Explore the codebase as much as needed.'\n"
        "def _build_dispatch_repair_prompt():\n"
        "    return 'Do not scan the repository.'\n",
        encoding="utf-8",
    )

    check = build_worker_prompt_alignment_check(source)

    assert check["status"] == "blocked"
    assert check["issue_count"] == 1
    assert check["issues"][0]["function"] == "_build_dispatch_repair_prompt"


def test_alignment_check_scans_worker_text_outside_prompt_named_functions(tmp_path) -> None:
    source = tmp_path / "chief_dispatch.py"
    source.write_text(
        "def dispatch_chief_plan():\n"
        "    report_text = 'Do not grep the whole repository.'\n"
        "    return report_text\n"
        "def _run_backend_attempt():\n"
        "    system_text = 'Conserve model budget while implementing.'\n"
        "    return system_text\n",
        encoding="utf-8",
    )

    issues = lint_worker_prompt_source(source)

    assert {(item["function"], item["code"]) for item in issues} == {
        ("dispatch_chief_plan", "repository_scan_ban"),
        ("_run_backend_attempt", "model_budget_conservation"),
    }


def test_main_returns_nonzero_for_prohibited_prompt_source(tmp_path, capsys) -> None:
    source = tmp_path / "chief_dispatch.py"
    source.write_text(
        "def build_worker_prompt():\n"
        "    return 'Never scan the entire codebase to save token budget.'\n",
        encoding="utf-8",
    )

    assert main([str(source)]) == 1
    assert "repository_scan_ban" in capsys.readouterr().err
