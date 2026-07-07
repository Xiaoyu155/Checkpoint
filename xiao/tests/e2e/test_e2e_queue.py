from __future__ import annotations

from pathlib import Path

from .helpers import json_output, run_cli


def test_workspace_queue_submits_runs_and_links_report(e2e_workspace: Path) -> None:
    submit = run_cli(
        "workspace-queue-submit",
        "--root",
        str(e2e_workspace),
        "--workflow",
        "local_html_form_workflow",
        "--inputs-file",
        "demo_login.json",
        "--run-profile",
        "dry-run",
        "--priority",
        "10",
    )
    assert submit.returncode == 0, submit.stdout + submit.stderr
    task = json_output(submit)
    assert task["status"] == "pending"
    assert task["inputs_file"] == "demo_login.json"

    pending = run_cli("workspace-queue-list", "--root", str(e2e_workspace), "--status", "pending")
    assert pending.returncode == 0, pending.stdout + pending.stderr
    pending_payload = json_output(pending)
    assert pending_payload["pending_tasks"] == 1

    run_next = run_cli("workspace-queue-run-next", "--root", str(e2e_workspace))
    assert run_next.returncode == 0, run_next.stdout + run_next.stderr
    run_payload = json_output(run_next)
    assert run_payload["ran"] is True
    assert run_payload["task"]["status"] == "success"
    run_id = run_payload["task"]["last_run_id"]
    assert run_id

    report = run_cli("workspace-report-detail", "--root", str(e2e_workspace), "--run-id", run_id)
    assert report.returncode == 0, report.stdout + report.stderr
    report_payload = json_output(report)
    assert report_payload["status"] == "success"
    assert report_payload["run_profile"] == "dry-run"
    assert report_payload["summary"]["dry_run_actions"] >= 1


def test_workspace_queue_worker_once_processes_one_task(e2e_workspace: Path) -> None:
    submit = run_cli(
        "workspace-queue-submit",
        "--root",
        str(e2e_workspace),
        "--workflow",
        "local_html_form_workflow",
        "--inputs-file",
        "demo_login.json",
    )
    assert submit.returncode == 0, submit.stdout + submit.stderr

    worker = run_cli("workspace-queue-worker", "--root", str(e2e_workspace), "--once")
    assert worker.returncode == 0, worker.stdout + worker.stderr
    payload = json_output(worker)
    assert payload["status"] == "once_completed"
    assert payload["tasks_run"] == 1
    assert payload["runs"][0]["task"]["status"] == "success"

    queue = run_cli("workspace-queue-list", "--root", str(e2e_workspace))
    assert queue.returncode == 0, queue.stdout + queue.stderr
    queue_payload = json_output(queue)
    assert queue_payload["pending_tasks"] == 0
    assert queue_payload["finished_tasks"] == 1
