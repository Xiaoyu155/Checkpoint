from pathlib import Path

from visual_agent.dogfood_evidence import assess_dogfood_evidence
from visual_agent.dogfood_local import run_local_dogfood


def test_skipping_artifacts_cannot_pass() -> None:
    result = assess_dogfood_evidence(
        {
            "schema_version": 1,
            "source_repo": {"product": "pacer"},
            "orchestrator": {},
            "contract": {},
            "verification": {},
            "candidate": {},
            "bootstrap": {},
        },
        require_artifacts=False,
    )
    assert result["passed"] is False
    assert result["status"] == "failed"
    assert "dogfood_artifacts_required" in result["reason_codes"]


def test_local_wheel_installs_and_runs_in_a_clean_env(tmp_path: Path) -> None:
    project = tmp_path / "pkg"
    package = project / "src" / "tiny_pkg"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n\n"
        "[project]\nname='tiny-pkg'\nversion='0.0.1'\n\n"
        "[tool.setuptools.packages.find]\nwhere=['src']\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("MARKER = 'dogfood-ok'\n", encoding="utf-8")
    (project / "src" / "tiny_pkg").mkdir(exist_ok=True)
    report = run_local_dogfood(project, smoke_code="import tiny_pkg; raise SystemExit(0 if tiny_pkg.MARKER=='dogfood-ok' else 1)")
    assert report["status"] == "passed", report.get("detail") or report.get("reason")
    assert report["installed_artifact_verified"] is True
    assert len(report["wheel_sha256"]) == 64


def test_dogfood_verifies_patched_files_match_wheel_content(tmp_path: Path) -> None:
    """When patched_files is provided, the wheel content must match the
    workspace source for each file. A mismatch means the change didn't
    ship in the artifact — verification must fail."""
    project = tmp_path / "pkg"
    package = project / "src" / "tiny_pkg"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n\n"
        "[project]\nname='tiny-pkg'\nversion='0.0.1'\n\n"
        "[tool.setuptools.packages.find]\nwhere=['src']\n",
        encoding="utf-8",
    )
    init_file = package / "__init__.py"
    init_file.write_text("MARKER = 'verified'\n", encoding="utf-8")
    (project / "src" / "tiny_pkg").mkdir(exist_ok=True)

    # Patched file matches — should pass
    report = run_local_dogfood(
        project,
        smoke_code="import tiny_pkg; raise SystemExit(0)",
        patched_files=["src/tiny_pkg/__init__.py"],
    )
    assert report["installed_artifact_verified"] is True
    assert "src/tiny_pkg/__init__.py" in report.get("patched_files_verified", [])

    # Now change workspace content AFTER wheel would be built — simulate mismatch
    # by passing a file path that doesn't exist in the project
    report_mismatch = run_local_dogfood(
        project,
        smoke_code="import tiny_pkg; raise SystemExit(0)",
        patched_files=["src/tiny_pkg/nonexistent.py"],
    )
    assert report_mismatch["installed_artifact_verified"] is False
    assert len(report_mismatch.get("patched_files_mismatched", [])) > 0
