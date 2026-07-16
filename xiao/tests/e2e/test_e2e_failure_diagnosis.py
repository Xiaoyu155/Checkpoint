from __future__ import annotations

from pathlib import Path

from .helpers import ROOT, json_output, run_cli


WORKFLOW = ROOT / "examples" / "failure_diagnosis_workflow.yaml"


def test_failed_workflow_exports_actionable_failure_diagnosis(tmp_path: Path) -> None:
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--run-profile",
        "dry-run",
        "--output-dir",
        str(tmp_path / "runs"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    failed = [step for step in payload["steps"] if step["status"] == "failed"]
    assert len(failed) == 1
    diagnosis = failed[0]["metadata"]["failure_diagnosis"]

    assert diagnosis["expected"] == "expected text: 不存在的成功提示"
    assert "客户管理系统" in diagnosis["actual"]
    assert diagnosis["observation"]["available"] is True
    assert diagnosis["observation"]["element_count"] == 4
    assert diagnosis["dom_excerpt"]
    assert diagnosis["recovery_suggestions"]
    assert "请给出恢复建议" in diagnosis["model_prompt"]


def test_failed_workflow_report_preserves_diagnosis_for_human_review(tmp_path: Path) -> None:
    output_dir = tmp_path / "runs"
    result = run_cli(
        "run-workflow",
        "--file",
        str(WORKFLOW),
        "--run-profile",
        "dry-run",
        "--output-dir",
        str(output_dir),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json_output(result)

    report = run_cli("report-run", "--run-dir", str(output_dir / payload["run_id"]), "--format", "markdown")
    assert report.returncode == 0, report.stdout + report.stderr
    assert "Failure expected" in report.stdout
    assert "expected text" in report.stdout
    assert "Failure actual" in report.stdout
    assert "DOM excerpt" in report.stdout
