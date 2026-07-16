from __future__ import annotations

from types import SimpleNamespace

from visual_agent.workbench_audit import (
    DEFAULT_ENTRY_AUDIT_ROUNDS,
    audit_report_to_markdown,
    default_audit_output_path,
    handle_workbench_audit_command,
    run_workbench_entry_audit,
)


def test_default_entry_audit_has_ten_rounds() -> None:
    assert len(DEFAULT_ENTRY_AUDIT_ROUNDS) == 10
    assert all(str(spec.goal).strip() for spec in DEFAULT_ENTRY_AUDIT_ROUNDS)


def test_audit_report_marks_rounds_and_renders_markdown(tmp_path) -> None:
    project_dir = tmp_path / "yuansi_app"
    workspace_root = tmp_path / ".agent-workspace"
    project_dir.mkdir()
    workspace_root.mkdir()

    report = run_workbench_entry_audit(
        project_dir=project_dir,
        workspace_root=workspace_root,
        enable_model=False,
    )
    text = audit_report_to_markdown(report)

    assert report.summary()["round_count"] == 10
    assert "Round 10" in text
    assert "入口审查文档" in text
    assert "目标接待" in text


def test_default_audit_output_path_uses_project_name(tmp_path) -> None:
    workspace_root = tmp_path / ".agent-workspace"
    project_dir = tmp_path / "yuansi_app"
    workspace_root.mkdir()
    project_dir.mkdir()

    path = default_audit_output_path(workspace_root=workspace_root, project_dir=project_dir)

    assert path.parent == workspace_root / "reports"
    assert path.name.startswith("workbench_entry_audit_yuansi_app_")
    assert path.suffix == ".md"


def test_handle_workbench_audit_command_writes_report(tmp_path) -> None:
    project_dir = tmp_path / "yuansi_app"
    workspace_root = tmp_path / ".agent-workspace"
    project_dir.mkdir()
    workspace_root.mkdir()

    args = SimpleNamespace(
        project_dir=str(project_dir),
        workspace_root=str(workspace_root),
        output=None,
        no_model=True,
        format="markdown",
    )

    exit_code = handle_workbench_audit_command(args)

    assert exit_code == 0
    reports = list((workspace_root / "reports").glob("workbench_entry_audit_yuansi_app_*.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Round 1" in text
    assert "Round 10" in text
