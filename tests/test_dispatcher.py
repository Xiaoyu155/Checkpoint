from visual_agent.dispatcher import ActionDispatchContext, ActionDispatcher
from visual_agent.models import LocationEvidence, Point, ProviderKind, ResolvedTarget, Target
from visual_agent.workflow_types import WorkflowContext


def test_action_dispatcher_exposes_default_actions() -> None:
    dispatcher = ActionDispatcher()

    assert dispatcher.actions_available == ("click", "paste", "press_key", "type")


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

    def locator(self, selector):
        self.calls.append(("locator", selector))
        return FakeLocator(self.calls)


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
