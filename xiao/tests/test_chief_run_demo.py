from __future__ import annotations

from pathlib import Path

from visual_agent.chief_run_demo import create_checkout_demo_repo, demo_checkout_worker


def test_create_checkout_demo_repo_seeds_defect(tmp_path) -> None:
    payload = create_checkout_demo_repo(tmp_path)

    assert payload["status"] == "ready"
    repo_root = Path(payload["repo_root"])
    html = repo_root / "examples" / "web" / "checkout_verification_demo.html"
    workflow = repo_root / ".agent-workspace" / "workflows" / "checkout_verification.yaml"
    assert "Next Step" in html.read_text(encoding="utf-8")
    assert "affects:" in workflow.read_text(encoding="utf-8")
    assert payload["baseline_ref"] == "HEAD~1"


def test_demo_checkout_worker_fails_first_then_repairs(tmp_path) -> None:
    html = tmp_path / "examples" / "web" / "checkout_verification_demo.html"
    html.parent.mkdir(parents=True)
    html.write_text("<button>Next Step</button>\n</body>", encoding="utf-8")

    initial = demo_checkout_worker([], tmp_path, 10, tmp_path / "initial.log")
    assert initial["exit_code"] == 0
    assert "Next Step" in html.read_text(encoding="utf-8")
    assert "Initial deterministic worker" in (tmp_path / "initial.log").read_text(encoding="utf-8")

    repair = demo_checkout_worker([], tmp_path, 10, tmp_path / "track-auto-repair-once.log")
    assert repair["exit_code"] == 0
    assert "Proceed to Checkout" in html.read_text(encoding="utf-8")
