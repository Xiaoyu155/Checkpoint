from __future__ import annotations

import subprocess
from pathlib import Path

from visual_agent.models import ActionStatus, Observation, ProviderKind
from visual_agent.providers import ProviderRegistry
from visual_agent.workflow import WorkflowRuntime, workflow_from_dict


class FakeExecLocator:
    def __init__(self, page, selector, frame=None):
        self.page = page
        self.selector = selector
        self.frame = frame

    def _record(self, call, *args):
        self.page.calls.append({"call": call, "selector": self.selector, "frame": self.frame, "args": list(args)})

    def click(self):
        self._record("click")

    def set_input_files(self, files):
        self._record("set_input_files", files)

    def select_option(self, **kwargs):
        self._record("select_option", kwargs)
        return [str(value) for value in kwargs.values()]

    def drag_to(self, destination):
        self._record("drag_to", destination.selector)

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def is_editable(self):
        return True

    def bounding_box(self):
        return {"x": 1, "y": 2, "width": 100, "height": 20}


class FakeFrameScope:
    def __init__(self, page, frame_selector):
        self.page = page
        self.frame_selector = frame_selector

    def locator(self, selector):
        return FakeExecLocator(self.page, selector, frame=self.frame_selector)


class FakeChooser:
    def __init__(self, page):
        self.page = page

    def set_files(self, files):
        self.page.calls.append({"call": "chooser_set_files", "selector": None, "frame": None, "args": [files]})


class FakeChooserContext:
    def __init__(self, page):
        self.page = page
        self.value = FakeChooser(page)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeExecPage:
    url = "https://example.test/app"
    viewport_size = {"width": 1280, "height": 720}

    def __init__(self):
        self.calls = []
        self.text = "Demo Ready"

    def evaluate(self, _script, arg=None):
        if arg is not None:
            return []
        return self.text

    def title(self):
        return "Demo"

    def screenshot(self, *, path, full_page=True):
        Path(path).write_bytes(b"fake-png")

    def locator(self, selector):
        return FakeExecLocator(self, selector)

    def frame_locator(self, frame_selector):
        return FakeFrameScope(self, frame_selector)

    def expect_file_chooser(self, timeout=None):
        return FakeChooserContext(self)

    def wait_for_timeout(self, _ms):
        return None


def run_steps(tmp_path, steps, *, run_profile="supervised"):
    page = FakeExecPage()

    def observe_browser(_params, provider_context):
        provider_context.resources["playwright_page"] = page
        provider_context.resources["network_events"] = []
        provider_context.resources["console_events"] = []
        provider_context.resources["page_errors"] = []
        return Observation(
            provider=ProviderKind.DOM,
            source=page.url,
            elements=(),
            metadata={"title": "Demo", "url": page.url, "visible_text": page.text, "visible_text_length": len(page.text), "interactive_count": 0},
        )

    providers = ProviderRegistry()
    providers.register("observe_browser", observe_browser)
    workflow = workflow_from_dict(
        {
            "name": "executor-demo",
            "steps": [{"id": "observe", "action": "observe_browser", "url": page.url}, *steps],
        }
    )
    result = WorkflowRuntime(output_dir=tmp_path, providers=providers).run(workflow, run_profile=run_profile)
    return result, page


def test_run_command_uses_hidden_launch_kwargs(tmp_path, monkeypatch) -> None:
    captured = {}

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("visual_agent.workflow.hidden_subprocess_kwargs", lambda: {"creationflags": 12345})
    monkeypatch.setattr(subprocess, "run", fake_run)

    result, _page = run_steps(
        tmp_path,
        [
            {
                "id": "cmd",
                "action": "run_command",
                "command": "npm test",
                "expect_text": "ok",
            }
        ],
    )

    step = next(item for item in result.steps if item.id == "cmd")
    assert step.status == ActionStatus.SUCCESS
    assert captured["kwargs"]["creationflags"] == 12345
    assert captured["kwargs"]["shell"] is True


def test_upload_file_sets_input_files(tmp_path) -> None:
    sample = tmp_path / "avatar.png"
    sample.write_bytes(b"fake-image")

    result, page = run_steps(tmp_path, [{"id": "upload", "action": "upload_file", "selector": "#file", "path": str(sample)}])

    step = next(item for item in result.steps if item.id == "upload")
    assert step.status == ActionStatus.SUCCESS
    call = next(item for item in page.calls if item["call"] == "set_input_files")
    assert call["selector"] == "#file"
    assert call["args"][0] == [str(sample.resolve())]
    assert step.action_result.metadata["files"][0]["name"] == "avatar.png"


def test_upload_file_via_native_chooser(tmp_path) -> None:
    sample = tmp_path / "report.pdf"
    sample.write_bytes(b"fake-pdf")

    result, page = run_steps(
        tmp_path,
        [{"id": "upload", "action": "upload_file", "selector": "#trigger", "path": str(sample), "via_chooser": True}],
    )

    assert next(item for item in result.steps if item.id == "upload").status == ActionStatus.SUCCESS
    assert [item["call"] for item in page.calls if item["call"] in ("click", "chooser_set_files")] == ["click", "chooser_set_files"]


def test_upload_file_missing_source_fails_with_clear_message(tmp_path) -> None:
    result, _page = run_steps(
        tmp_path,
        [{"id": "upload", "action": "upload_file", "selector": "#file", "path": str(tmp_path / "nope.png")}],
    )

    step = next(item for item in result.steps if item.id == "upload")
    assert step.status == ActionStatus.FAILED
    assert "not found" in step.message


def test_upload_file_skipped_in_dry_run(tmp_path) -> None:
    sample = tmp_path / "avatar.png"
    sample.write_bytes(b"fake-image")

    result, page = run_steps(
        tmp_path,
        [{"id": "upload", "action": "upload_file", "selector": "#file", "path": str(sample)}],
        run_profile="dry-run",
    )

    step = next(item for item in result.steps if item.id == "upload")
    assert step.status == ActionStatus.DRY_RUN
    assert not page.calls


def test_select_option_by_label_and_inside_iframe(tmp_path) -> None:
    result, page = run_steps(
        tmp_path,
        [
            {"id": "pick", "action": "select_option", "selector": "#city", "label": "上海"},
            {"id": "pick_frame", "action": "select_option", "selector": "#plan", "value": "pro", "frame_selector": "#checkout-frame"},
        ],
    )

    assert all(item.status == ActionStatus.SUCCESS for item in result.steps)
    plain = next(item for item in page.calls if item["selector"] == "#city")
    assert plain["args"][0] == {"label": "上海"}
    assert plain["frame"] is None
    framed = next(item for item in page.calls if item["selector"] == "#plan")
    assert framed["frame"] == "#checkout-frame"


def test_select_option_requires_an_option_argument(tmp_path) -> None:
    result, _page = run_steps(tmp_path, [{"id": "pick", "action": "select_option", "selector": "#city"}])

    step = next(item for item in result.steps if item.id == "pick")
    assert step.status == ActionStatus.FAILED
    assert "value, label, index" in step.message


def test_drag_moves_source_to_destination(tmp_path) -> None:
    result, page = run_steps(
        tmp_path,
        [{"id": "move", "action": "drag", "selector": "#card", "to_selector": "#done-column"}],
    )

    assert next(item for item in result.steps if item.id == "move").status == ActionStatus.SUCCESS
    call = next(item for item in page.calls if item["call"] == "drag_to")
    assert call["selector"] == "#card"
    assert call["args"][0] == "#done-column"


def test_new_actions_count_as_real_interactions_for_acceptance(tmp_path) -> None:
    sample = tmp_path / "avatar.png"
    sample.write_bytes(b"fake-image")

    result, _page = run_steps(
        tmp_path,
        [
            {"id": "check", "action": "assert_text", "text": "Demo Ready"},
            {"id": "upload", "action": "upload_file", "selector": "#file", "path": str(sample), "post_action_assert_text": "Demo Ready"},
            {
                "id": "verify",
                "action": "assert_text_contract",
                "required_all": ["Demo Ready"],
                "forbidden_any": ["Fatal Error"],
            },
        ],
    )

    assert result.acceptance["label"] == "L3"
    assert result.acceptance["is_product_acceptance"] is True


def test_validate_workflow_accepts_new_actions() -> None:
    from visual_agent.validation import validate_workflow
    from visual_agent.workflow import workflow_from_dict

    result = validate_workflow(
        workflow_from_dict(
            {
                "schema_version": 1,
                "name": "executor",
                "version": 1,
                "steps": [
                    {"id": "observe", "action": "observe_browser", "url": "http://localhost"},
                    {"id": "upload", "action": "upload_file", "selector": "#f", "path": "a.png"},
                    {"id": "pick", "action": "select_option", "selector": "#s", "value": "1"},
                    {"id": "move", "action": "drag", "selector": "#a", "to_selector": "#b"},
                ],
            }
        )
    )

    assert not [issue for issue in result.issues if issue.level == "error"], result.issues


def test_validate_workflow_flags_missing_required_params() -> None:
    from visual_agent.validation import validate_workflow
    from visual_agent.workflow import workflow_from_dict

    result = validate_workflow(
        workflow_from_dict(
            {
                "schema_version": 1,
                "name": "executor",
                "version": 1,
                "steps": [
                    {"id": "upload", "action": "upload_file", "selector": "#f"},
                    {"id": "move", "action": "drag", "selector": "#a"},
                ],
            }
        )
    )

    errors = " ".join(issue.message for issue in result.issues if issue.level == "error")
    assert "path" in errors
    assert "to_selector" in errors
