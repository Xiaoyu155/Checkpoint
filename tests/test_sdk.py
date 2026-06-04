from pathlib import Path
import tomllib

import pytest

from visual_agent import ActionStatus, Observation, ProviderKind, VisualSession
from visual_agent.dsl import list_dsl_workflows, run_dsl_workflow, workflow

pytest_plugins = ("visual_agent.pytest_plugin",)


def test_visual_session_requires_context_manager() -> None:
    session = VisualSession()

    with pytest.raises(RuntimeError, match="context manager"):
        session.click_text("x")


def test_visual_session_dry_run_click_text(tmp_path: Path) -> None:
    with VisualSession(workspace=tmp_path, dry_run=True) as session:
        result = session.click_text("确认", mock_text="确认")

    assert result.status == ActionStatus.DRY_RUN
    assert result.action == "click"


def test_visual_session_press_key_dry_run(tmp_path: Path) -> None:
    with VisualSession(workspace=tmp_path, dry_run=True) as session:
        result = session.press_key("enter")

    assert result.action == "press_key"
    assert result.status == ActionStatus.DRY_RUN


def test_visual_session_results_accumulate(tmp_path: Path) -> None:
    with VisualSession(workspace=tmp_path, dry_run=True) as session:
        session.press_key("escape")
        session.click_text("ok", mock_text="ok")

        assert len(session.results) == 2


def test_visual_session_run_dir_exists(tmp_path: Path) -> None:
    with VisualSession(workspace=tmp_path) as session:
        assert session.run_dir.exists()
        assert session.run_dir.parent == tmp_path / "runs"


def test_visual_session_assert_text_visible_fails(tmp_path: Path) -> None:
    with VisualSession(workspace=tmp_path) as session:
        with pytest.raises(AssertionError, match="Text not visible"):
            session.assert_text_visible("不存在的文字", mock_text="其他文字")


def test_visual_session_observe_and_click_dom_target(tmp_path: Path) -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="fixture",
        width=800,
        height=600,
        elements=(
            {
                "tag": "button",
                "role": "button",
                "text": "提交",
                "selector": "#submit",
                "bounds": {"left": 10, "top": 20, "width": 80, "height": 30},
            },
        ),
    )

    with VisualSession(workspace=tmp_path, dry_run=True) as session:
        session._require_context().observations["dom"] = observation
        result = session.click(text="提交", role="button")

    assert result.status == ActionStatus.DRY_RUN
    assert result.provider == ProviderKind.DOM


def test_visual_session_type_text_resolves_dom_target(tmp_path: Path) -> None:
    observation = Observation(
        provider=ProviderKind.DOM,
        source="fixture",
        width=800,
        height=600,
        elements=(
            {
                "tag": "input",
                "role": "input",
                "placeholder": "用户名",
                "selector": "#username",
                "bounds": {"left": 10, "top": 20, "width": 180, "height": 30},
            },
        ),
    )

    with VisualSession(workspace=tmp_path, dry_run=True) as session:
        session._require_context().observations["dom"] = observation
        result = session.type_text("demo", label="用户名", sensitive=False)

    assert result.action == "type"
    assert result.status == ActionStatus.DRY_RUN
    assert result.metadata["text_preview"] == "dem***"


def test_visual_session_fixture(visual_session) -> None:
    assert visual_session.dry_run is True
    result = visual_session.press_key("escape")
    assert result.status == ActionStatus.DRY_RUN


def test_dsl_workflow_runs_visual_session(tmp_path: Path) -> None:
    name = "sdk-test-dsl"

    @workflow(name=name, tags=["sdk"])
    def sample(session: VisualSession) -> None:
        session.press_key("escape")

    results = run_dsl_workflow(name, workspace=str(tmp_path), dry_run=True)

    assert name in list_dsl_workflows()
    assert results[0].action == "press_key"
    assert results[0].status == ActionStatus.DRY_RUN


def test_pyproject_registers_sdk_entrypoints() -> None:
    payload = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert payload["project"]["scripts"]["visual-agent"] == "visual_agent.cli:main"
    assert payload["project"]["entry-points"]["pytest11"]["visual_agent"] == "visual_agent.pytest_plugin"
