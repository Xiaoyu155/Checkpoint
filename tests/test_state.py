from visual_agent.state import StateStore, WorkflowState
from visual_agent.workflow import WorkflowRuntime, parse_workflow_file


def test_state_store_round_trips_checkpoint(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = WorkflowState(
        run_id="run-1",
        workflow_name="demo",
        completed_steps=("observe", "click"),
        failed_step=None,
    )

    store.save(state)
    loaded = store.load()

    assert loaded == state


def test_resume_hydrates_context_after_failed_input(tmp_path) -> None:
    workflow = parse_workflow_file("examples/local_html_form_workflow.yaml")
    first = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        inputs={"username": "demo_user"},
    )

    assert first.steps[-1].id == "fill_password"

    resumed = WorkflowRuntime(output_dir=tmp_path).run(
        workflow,
        dry_run=True,
        inputs={"username": "demo_user", "password": "demo_password"},
        resume_from=first.run_dir,
    )

    assert resumed.steps[0].metadata["resumed"] is True
    assert resumed.steps[-1].id == "click_login"
    assert resumed.steps[-1].status.value == "dry_run"
