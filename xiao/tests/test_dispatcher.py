from visual_agent.dispatcher import ActionDispatchContext, ActionDispatcher
from visual_agent.models import ActionResult, ActionStatus, LocationEvidence, Point, ProviderKind, ResolvedTarget, Target
from visual_agent.workflow_types import WorkflowContext


def test_action_dispatcher_exposes_default_actions() -> None:
    dispatcher = ActionDispatcher()

    assert dispatcher.actions_available == (
        "click",
        "click_text",
        "drag",
        "paste",
        "press_key",
        "refresh_browser",
        "select_option",
        "type",
        "upload_file",
        "wait_for_text",
    )


def test_action_dispatcher_supports_custom_action(tmp_path) -> None:
    dispatcher = ActionDispatcher()
    target = Target.from_text("登录")
    resolved = ResolvedTarget(
        target=target,
        evidence=LocationEvidence(
            provider=ProviderKind.MOCK,
            confidence=1,
            reason="test",
            point=Point(1, 2),
        ),
    )

    def custom(resolved_target, params, context):
        return context.workflow_context.actions["seed"]

    context = WorkflowContext(run_id="run", run_dir=tmp_path)
    seed = dispatcher.execute(
        "click",
        resolved,
        {},
        ActionDispatchContext(workflow_context=context, dry_run=True),
    )
    context.actions["seed"] = seed
    dispatcher.register("custom", custom)

    result = dispatcher.execute(
        "custom",
        resolved,
        {},
        ActionDispatchContext(workflow_context=context, dry_run=True),
    )

    assert result.action == "click"


class FakeLocator:
    def __init__(self, calls):
        self.calls = calls

    def click(self):
        self.calls.append(("click", None))

    def fill(self, value):
        self.calls.append(("fill", value))


class FakePage:
    def __init__(self):
        self.calls = []
        self.url = "https://example.test/app"

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return FakeLocator(self.calls)

    def wait_for_timeout(self, value):
        self.calls.append(("wait_for_timeout", value))

    def reload(self, **kwargs):
        self.calls.append(("reload", kwargs))


def test_action_dispatcher_uses_playwright_page_for_click_and_fill(tmp_path) -> None:
    dispatcher = ActionDispatcher()
    context = WorkflowContext(run_id="run", run_dir=tmp_path, inputs={"username": "demo"})
    page = FakePage()
    context.resources["playwright_page"] = page
    resolved = ResolvedTarget(
        target=Target(label="用户名", role="input"),
        evidence=LocationEvidence(
            provider=ProviderKind.DOM,
            confidence=1,
            reason="dom",
            handle="#username",
        ),
    )

    fill = dispatcher.execute(
        "paste",
        resolved,
        {"value_from": "input.username", "dry_run": False},
        ActionDispatchContext(workflow_context=context, dry_run=True),
    )
    click = dispatcher.execute(
        "click",
        resolved,
        {"dry_run": False},
        ActionDispatchContext(workflow_context=context, dry_run=True),
    )

    assert fill.point is None
    assert fill.metadata["execution"] == "playwright"
    assert click.metadata["selector"] == "#username"
    assert page.calls == [
        ("locator", "#username"),
        ("fill", "demo"),
        ("locator", "#username"),
        ("click", None),
    ]


def test_action_dispatcher_refreshes_playwright_page(tmp_path) -> None:
    dispatcher = ActionDispatcher()
    context = WorkflowContext(run_id="run", run_dir=tmp_path)
    page = FakePage()
    context.resources["playwright_page"] = page
    resolved = ResolvedTarget(
        target=Target(label="refresh_browser"),
        evidence=LocationEvidence(provider=ProviderKind.MOCK, confidence=1, reason="global"),
    )

    result = dispatcher.execute(
        "refresh_browser",
        resolved,
        {"dry_run": False, "wait_until": "load", "timeout_ms": 1234},
        ActionDispatchContext(workflow_context=context, dry_run=True),
    )

    assert result.status == ActionStatus.SUCCESS
    assert result.metadata["execution"] == "playwright"
    assert page.calls == [
        ("reload", {"wait_until": "load", "timeout": 1234}),
    ]


def test_action_dispatcher_keeps_explicit_browser_settle_wait(tmp_path) -> None:
    dispatcher = ActionDispatcher()
    context = WorkflowContext(run_id="run", run_dir=tmp_path)
    page = FakePage()
    context.resources["playwright_page"] = page
    resolved = ResolvedTarget(
        target=Target(label="submit", role="button"),
        evidence=LocationEvidence(
            provider=ProviderKind.DOM,
            confidence=1,
            reason="dom",
            handle="#submit",
        ),
    )

    dispatcher.execute(
        "click",
        resolved,
        {"dry_run": False, "wait_after_seconds": 0.25},
        ActionDispatchContext(workflow_context=context, dry_run=True),
    )

    assert page.calls[-1] == ("wait_for_timeout", 250)


def test_action_dispatcher_blocks_real_desktop_text_input_without_opt_in(tmp_path) -> None:
    dispatcher = ActionDispatcher()
    context = WorkflowContext(run_id="run", run_dir=tmp_path)
    resolved = ResolvedTarget(
        target=Target(label="chat box"),
        evidence=LocationEvidence(
            provider=ProviderKind.OCR,
            confidence=1,
            reason="ocr",
            point=Point(1, 2),
        ),
    )

    try:
        dispatcher.execute(
            "paste",
            resolved,
            {"value": "demo_password"},
            ActionDispatchContext(workflow_context=context, dry_run=False),
        )
    except RuntimeError as exc:
        assert "blocked by default" in str(exc)
    else:
        raise AssertionError("desktop paste should require allow_desktop_input")


def test_action_dispatcher_allows_real_desktop_text_input_with_opt_in(tmp_path) -> None:
    class FakeActions:
        def paste_text(self, *_args, **_kwargs):
            return ActionResult(action="paste", status=ActionStatus.SUCCESS, target="desktop input")

    dispatcher = ActionDispatcher(actions=FakeActions())
    context = WorkflowContext(run_id="run", run_dir=tmp_path)
    resolved = ResolvedTarget(
        target=Target(label="desktop input"),
        evidence=LocationEvidence(
            provider=ProviderKind.OCR,
            confidence=1,
            reason="ocr",
            point=Point(1, 2),
        ),
    )

    result = dispatcher.execute(
        "paste",
        resolved,
        {"value": "demo", "allow_desktop_input": True},
        ActionDispatchContext(workflow_context=context, dry_run=False),
    )

    assert result.status == ActionStatus.SUCCESS
