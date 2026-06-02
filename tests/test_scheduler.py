from visual_agent.scheduler import (
    cancel_queue_task,
    list_queue_tasks,
    migrate_queue_to_sqlite,
    rollback_queue_from_sqlite,
    retry_queue_task,
    run_next_queue_task,
    run_queue_worker,
    submit_queue_task,
)
from visual_agent.db import open_workspace_db
from visual_agent.workspace import init_workspace, planner_context, workspace_status


def test_workspace_queue_dir_is_initialized(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    assert workspace.queue_dir.exists()


def test_queue_orders_pending_tasks_by_priority_then_created_at(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")

    low = submit_queue_task(workspace, "local_html_form_workflow", priority=1)
    high = submit_queue_task(workspace, "local_html_form_workflow", priority=10)

    queue = list_queue_tasks(workspace)

    assert queue["total_tasks"] == 2
    assert queue["pending_tasks"] == 2
    assert [entry["task_id"] for entry in queue["entries"]] == [high.task_id, low.task_id]


def test_workspace_status_and_planner_context_include_queue(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")

    status = workspace_status(workspace)
    context = planner_context(workspace)

    assert status["queue_task_count"] == 1
    assert status["pending_queue_tasks"] == 1
    assert status["queue"][0]["task_id"] == task.task_id
    assert context["queue"][0]["task_id"] == task.task_id


def test_queue_can_cancel_and_retry_task(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    task = submit_queue_task(workspace, "local_html_form_workflow")

    canceled = cancel_queue_task(workspace, task.task_id, reason="manual pause")
    retried = retry_queue_task(workspace, task.task_id)
    queue = list_queue_tasks(workspace)

    assert canceled.status == "canceled"
    assert canceled.last_error == "manual pause"
    assert retried.status == "pending"
    assert queue["pending_tasks"] == 1


def test_run_next_queue_task_runs_highest_priority_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json", priority=1)
    high = submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json", priority=5)

    result = run_next_queue_task(workspace)
    queue = list_queue_tasks(workspace)

    assert result["ran"] is True
    assert result["task"]["task_id"] == high.task_id
    assert result["task"]["status"] == "success"
    assert result["task"]["last_run_id"]
    assert queue["pending_tasks"] == 1
    assert queue["finished_tasks"] == 1


def test_run_next_queue_task_retries_failed_workflow(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    failing_workflow = workspace.workflows_dir / "queued_failing.yaml"
    failing_workflow.write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: queued_failing
version: 1
steps:
  - id: observe_html
    action: observe_html
    path: fixtures/login_demo.html
  - id: assert_missing
    action: assert_text
    text: 不存在的文本
""".strip(),
        encoding="utf-8",
    )
    task = submit_queue_task(workspace, "queued_failing", max_retries=1)

    first = run_next_queue_task(workspace)
    second = run_next_queue_task(workspace)

    assert first["task"]["task_id"] == task.task_id
    assert first["task"]["status"] == "pending"
    assert first["task"]["attempts"] == 1
    assert second["task"]["status"] == "failed"
    assert second["task"]["attempts"] == 2
    assert second["task"]["last_error"]


def test_queue_worker_runs_pending_tasks_until_max_tasks(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json", priority=1)
    submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json", priority=2)

    result = run_queue_worker(workspace, poll_seconds=0, max_tasks=2)
    queue = list_queue_tasks(workspace)

    assert result["status"] == "max_tasks_reached"
    assert result["tasks_run"] == 2
    assert len(result["runs"]) == 2
    assert {run["task"]["status"] for run in result["runs"]} == {"success"}
    assert queue["pending_tasks"] == 0
    assert queue["finished_tasks"] == 2


def test_queue_worker_stops_when_stop_file_exists(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json")
    stop_file = workspace.queue_dir / "worker.stop"
    stop_file.write_text("stop", encoding="utf-8")

    result = run_queue_worker(workspace, poll_seconds=0, max_tasks=1, stop_file=stop_file)
    queue = list_queue_tasks(workspace)

    assert result["status"] == "stopped_by_file"
    assert result["tasks_run"] == 0
    assert queue["pending_tasks"] == 1


def test_open_workspace_db_creates_queue_schema(tmp_path) -> None:
    conn = open_workspace_db(tmp_path)
    try:
        conn.execute(
            "INSERT INTO queue_tasks VALUES ('t1','wf','pending',0,'dry-run',1,NULL,NULL,NULL,1.0,1.0,0,0,NULL,NULL)"
        )
        conn.commit()
        rows = conn.execute("SELECT * FROM queue_tasks").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1


def test_sqlite_queue_backend_submit_list_cancel_retry(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest = workspace.root / "workspace.json"
    manifest.write_text('{"name":"agent-workspace","version":1,"queue_backend":"sqlite"}', encoding="utf-8")

    task = submit_queue_task(workspace, "local_html_form_workflow", inputs={"password": "plain-secret"}, priority=3)
    queue = list_queue_tasks(workspace)
    canceled = cancel_queue_task(workspace, task.task_id, reason="manual")
    retried = retry_queue_task(workspace, task.task_id)

    assert (workspace.root / "agent.db").exists()
    assert queue["backend"] == "sqlite"
    assert queue["total_tasks"] == 1
    assert queue["entries"][0]["priority"] == 3
    assert queue["entries"][0]["inputs"]["password"]["redacted"] is True
    assert canceled.status == "canceled"
    assert retried.status == "pending"


def test_sqlite_queue_backend_run_next(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    manifest = workspace.root / "workspace.json"
    manifest.write_text('{"name":"agent-workspace","version":1,"queue_backend":"sqlite"}', encoding="utf-8")
    low = submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json", priority=1)
    high = submit_queue_task(workspace, "local_html_form_workflow", inputs_file="demo_login.json", priority=9)

    result = run_next_queue_task(workspace)
    queue = list_queue_tasks(workspace)

    assert result["ran"] is True
    assert result["task"]["task_id"] == high.task_id
    assert result["task"]["status"] == "success"
    assert queue["pending_tasks"] == 1
    pending = list_queue_tasks(workspace, status="pending")
    assert pending["entries"][0]["task_id"] == low.task_id


def test_queue_migration_to_sqlite_and_rollback_to_json(tmp_path) -> None:
    workspace = init_workspace(tmp_path / "agent-workspace")
    first = submit_queue_task(workspace, "local_html_form_workflow", inputs={"username": "demo", "password": "secret"}, priority=3)
    second = submit_queue_task(workspace, "minimal_testable_workflow", inputs_file="demo_login.json", priority=1)
    cancel_queue_task(workspace, second.task_id, reason="manual")

    migrated = migrate_queue_to_sqlite(workspace)
    sqlite_queue = list_queue_tasks(workspace)
    migrated_again = migrate_queue_to_sqlite(workspace, backup_json=False)

    assert migrated["task_count"] == 2
    assert migrated["history_count"] >= 1
    assert migrated["backup_path"]
    assert sqlite_queue["backend"] == "sqlite"
    assert sqlite_queue["total_tasks"] == 2
    assert sqlite_queue["entries"][0]["task_id"] == first.task_id
    assert sqlite_queue["entries"][0]["inputs"]["password"]["redacted"] is True
    assert migrated_again["task_count"] == 2
    assert list_queue_tasks(workspace)["total_tasks"] == 2

    rolled_back = rollback_queue_from_sqlite(workspace)
    json_queue = list_queue_tasks(workspace)

    assert rolled_back["task_count"] == 2
    assert rolled_back["history_count"] >= 1
    assert json_queue["total_tasks"] == 2
    assert "backend" not in json_queue
    assert {entry["task_id"] for entry in json_queue["entries"]} == {first.task_id, second.task_id}
