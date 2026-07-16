"""Deterministic DevPacer mission demo.

This demo proves the mission loop without spending coding-agent tokens. It uses
the real dispatcher, a real git worktree, and real Checkpoint verification, but
injects a tiny deterministic worker runner that first leaves a seeded checkout
defect unfixed and then repairs it during the auto-repair round.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .chief_dispatch import dispatch_chief_plan
from .chief_run import chief_run_to_markdown, run_chief_mission
from .models import to_jsonable
from .resources import bundled_path
from .workspace import init_workspace


CHECKOUT_WORKFLOW = """schema_version: 1
min_runtime_version: "0.1.0"
name: checkout_verification
version: 1
visibility: private
tags:
  - verification
affects:
  - examples/web/checkout_verification_demo.html
steps:
  - id: observe_page
    action: observe_browser
    url: ../../../examples/web/checkout_verification_demo.html
  - id: assert_title
    action: assert_text
    text: "Checkout Demo"
  - id: assert_product
    action: assert_text
    text: "Premium Widget"
  - id: assert_price
    action: assert_text
    text: "$29.99"
  - id: assert_add_to_cart
    action: assert_text
    text: "Add to Cart"
  - id: assert_checkout_button
    action: assert_text
    text: "Proceed to Checkout"
  - id: assert_shipping
    action: assert_text
    text: "Standard Delivery"
  - id: place_order
    action: click
    post_action_assert_text: "Order Confirmed"
    target:
      text: "Place Order"
      role: button
      preferred: [dom, mock]
  - id: observe_order_result
    action: observe_browser
    reuse_page: true
  - id: assert_order_contract
    action: assert_text_contract
    required_all:
      - "Order Confirmed"
      - "Order #CP-1001"
    forbidden_any:
      - "Payment Failed"
      - "Order Failed"
      - "Checkout Error"
      - "Next Step"
  - id: assert_order_confirmed
    action: assert_text
    text: "Order Confirmed"
  - id: assert_no_error
    action: assert_no_error
"""


def run_chief_demo(
    *,
    workspace_root: str | Path = ".agent-workspace",
    demo_root: str | Path | None = None,
    keep_repo: bool = True,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    root = Path(demo_root).expanduser().resolve() if demo_root else workspace_path / "demo_repos"
    setup = create_checkout_demo_repo(root)
    if setup.get("status") != "ready":
        return {
            "schema_version": 1,
            "product": "DevPacer",
            "verification_engine": "Checkpoint",
            "status": "blocked",
            "stop_reason": "demo_setup_failed",
            "message": setup.get("reason", "Demo setup failed."),
            "demo": setup,
        }

    repo_root = Path(setup["repo_root"])
    demo_workspace = repo_root / ".agent-workspace"

    def demo_dispatch_runner(**kwargs: Any) -> dict[str, Any]:
        return dispatch_chief_plan(
            **kwargs,
            command_runner=demo_checkout_worker,
            failure_evidence_builder=demo_failure_evidence,
        )

    payload = run_chief_mission(
        goal=(
            "Repair the checkout demo so the checkout button reads "
            "Proceed to Checkout and supervised checkout verification passes."
        ),
        workspace_root=demo_workspace,
        repo_root=repo_root,
        base="HEAD~1",
        max_rounds=2,
        max_wall_minutes=20,
        max_worker_minutes=5,
        execute=True,
        dry_run=False,
        run_profile="supervised",
        allow_dirty=False,
        allow_coverage_gap=False,
        dispatch_runner=demo_dispatch_runner,
    )
    payload["demo"] = {
        **setup,
        "keep_repo": keep_repo,
        "expected_flow": "seeded defect -> initial verification fail -> repair prompt -> repair worker -> verification pass",
    }
    return payload


def create_checkout_demo_repo(demo_root: Path) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    repo_root = (demo_root / f"devpacer-checkout-demo-{stamp}").resolve()
    try:
        repo_root.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return {"status": "failed", "reason": f"Could not create demo repo: {type(exc).__name__}: {exc}"}

    try:
        source_html = bundled_path("examples", "web", "checkout_verification_demo.html")
        target_html = repo_root / "examples" / "web" / "checkout_verification_demo.html"
        target_html.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_html, target_html)

        workspace = init_workspace(repo_root / ".agent-workspace", with_demo=False)
        (workspace.workflows_dir / "checkout_verification.yaml").write_text(CHECKOUT_WORKFLOW, encoding="utf-8")

        _git(repo_root, "init")
        _git(repo_root, "config", "user.email", "devpacer-demo@example.test")
        _git(repo_root, "config", "user.name", "DevPacer Demo")
        _git(repo_root, "add", ".")
        _git(repo_root, "commit", "-m", "green checkout baseline")

        text = target_html.read_text(encoding="utf-8")
        if "Proceed to Checkout" not in text:
            raise RuntimeError("Demo source does not contain Proceed to Checkout.")
        target_html.write_text(text.replace("Proceed to Checkout", "Next Step", 1), encoding="utf-8")
        _git(repo_root, "add", "examples/web/checkout_verification_demo.html")
        _git(repo_root, "commit", "-m", "seed checkout button regression")
    except Exception as exc:
        return {
            "status": "failed",
            "repo_root": str(repo_root),
            "reason": f"{type(exc).__name__}: {exc}",
        }

    return {
        "status": "ready",
        "repo_root": str(repo_root),
        "workspace_root": str(repo_root / ".agent-workspace"),
        "seeded_file": "examples/web/checkout_verification_demo.html",
        "baseline_ref": "HEAD~1",
        "defect_ref": "HEAD",
    }


def demo_checkout_worker(argv: list[str], cwd: Path, timeout_seconds: float, log_path: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    del argv, timeout_seconds, env
    log_path.parent.mkdir(parents=True, exist_ok=True)
    html_path = cwd / "examples" / "web" / "checkout_verification_demo.html"
    if "auto-repair" in log_path.name:
        text = html_path.read_text(encoding="utf-8")
        repaired = text.replace("Next Step", "Proceed to Checkout")
        html_path.write_text(repaired, encoding="utf-8")
        message = "Repaired checkout button label from Next Step to Proceed to Checkout."
    else:
        text = html_path.read_text(encoding="utf-8")
        marker = "<!-- devpacer demo initial attempt touched this file but did not fix the seeded defect -->"
        if marker not in text:
            text = text.replace("</body>", f"  {marker}\n</body>")
            html_path.write_text(text, encoding="utf-8")
        message = "Initial deterministic worker left the seeded checkout label defect in place."
    log_path.write_text(message + "\n", encoding="utf-8")
    return {"exit_code": 0, "stdout_tail": message, "stderr_tail": ""}


def demo_failure_evidence(_workspace_root: Path, *, max_chars: int) -> dict[str, Any]:
    del max_chars
    return {
        "status": "found",
        "run_id": "demo-seeded-failure",
        "workflow": "checkout_verification",
        "failed_step": {"id": "assert_checkout_button", "action": "assert_text"},
        "message": "The checkout button still says Next Step instead of Proceed to Checkout.",
        "repair_prompt": (
            "Repair the seeded checkout regression. In examples/web/checkout_verification_demo.html, "
            "change the checkout button label from Next Step back to Proceed to Checkout. "
            "Keep the workflow and mission records unchanged."
        ),
    }


def _git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or f"git {' '.join(args)} failed").strip())
    return completed.stdout


def chief_demo_to_markdown(payload: dict[str, Any]) -> str:
    lines = [chief_run_to_markdown(payload)]
    demo = payload.get("demo") if isinstance(payload.get("demo"), dict) else {}
    if demo:
        lines.extend(
            [
                "",
                "### Demo Repo",
                "",
                f"- Repo: `{demo.get('repo_root')}`",
                f"- Workspace: `{demo.get('workspace_root')}`",
                f"- Seeded file: `{demo.get('seeded_file')}`",
                f"- Flow: {demo.get('expected_flow')}",
            ]
        )
    return "\n".join(lines).rstrip()


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2)
