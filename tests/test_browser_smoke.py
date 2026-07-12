from __future__ import annotations

import json
from pathlib import Path

from visual_agent.browser_smoke import build_browser_smoke_workflow, browser_smoke_run_dir, browser_smoke_to_markdown, run_browser_smoke
from visual_agent.cli import main
from visual_agent.models import Observation, ProviderKind
from visual_agent.validation import validate_workflow
from visual_agent.workflow import parse_workflow_file


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def click(self):
        self.page.clicked = True
        self.page.text = "Dashboard Ready"
        self.page.url = "https://example.test/dashboard"

    def fill(self, value):
        self.page.fills.append((self.selector, value))


class FakePage:
    viewport_size = {"width": 1280, "height": 720}

    def __init__(self):
        self.url = "https://example.test/login"
        self.clicked = False
        self.text = "Login"
        self.fills = []

    def locator(self, selector):
        self.selector = selector
        return FakeLocator(self, selector)

    def wait_for_timeout(self, _value):
        return None

    def wait_for_function(self, _script, *, arg, timeout):
        value = str(arg).lower()
        if value not in self.text.lower() and value not in self.url.lower():
            raise TimeoutError("text missing")

    def evaluate(self, _script, arg=None):
        if arg is not None:
            text = "Dashboard Ready" if self.clicked else "Login"
            selector = "#done" if self.clicked else "#login"
            return [
                {"role": "input", "label": "用户名", "selector": "#username", "bounds": {"left": 1, "top": 1, "width": 120, "height": 30}},
                {"role": "input", "label": "密码", "selector": "#password", "bounds": {"left": 1, "top": 40, "width": 120, "height": 30}},
                {"role": "button", "text": text, "selector": selector, "bounds": {"left": 1, "top": 80, "width": 80, "height": 30}},
            ]
        return self.text

    def title(self):
        return "Demo"

    def screenshot(self, *, path, full_page=True):
        Path(path).write_bytes(b"fake-png")

    def content(self):
        return f"<html><body>{self.text}</body></html>"


class NoChangeLocator(FakeLocator):
    def click(self):
        self.page.clicked = True


class NoChangePage(FakePage):
    def locator(self, selector):
        self.selector = selector
        return NoChangeLocator(self, selector)


class SecretUrlLocator(FakeLocator):
    def click(self):
        values = {selector: value for selector, value in self.page.fills}
        self.page.clicked = True
        self.page.text = "Dashboard Ready"
        self.page.url = f"https://example.test/dashboard?username={values.get('#username', '')}&password={values.get('#password', '')}"


class SecretUrlPage(FakePage):
    def locator(self, selector):
        self.selector = selector
        return SecretUrlLocator(self, selector)


def fake_observe_browser(params, provider_context):
    page = FakePage()
    provider_context.resources["playwright_page"] = page
    provider_context.resources["network_events"] = []
    provider_context.resources["console_events"] = []
    provider_context.resources["page_errors"] = []
    return Observation(
        provider=ProviderKind.DOM,
        source=params["url"],
        elements=tuple(page.evaluate("collect", "selector")),
        metadata={"url": page.url, "title": "Demo", "visible_text": "Login", "visible_text_length": 5, "interactive_count": 1},
    )


def fake_observe_secret_url_browser(params, provider_context):
    page = SecretUrlPage()
    provider_context.resources["playwright_page"] = page
    provider_context.resources["network_events"] = []
    provider_context.resources["console_events"] = []
    provider_context.resources["page_errors"] = []
    return Observation(
        provider=ProviderKind.DOM,
        source=params["url"],
        elements=tuple(page.evaluate("collect", "selector")),
        metadata={"url": page.url, "title": "Demo", "visible_text": "Login", "visible_text_length": 5, "interactive_count": 1},
    )


def fake_observe_no_change_browser(params, provider_context):
    page = NoChangePage()
    provider_context.resources["playwright_page"] = page
    provider_context.resources["network_events"] = []
    provider_context.resources["console_events"] = []
    provider_context.resources["page_errors"] = []
    return Observation(
        provider=ProviderKind.DOM,
        source=params["url"],
        elements=tuple(page.evaluate("collect", "selector")),
        metadata={"url": params["url"], "title": "Demo", "visible_text": "Login", "visible_text_length": 5, "interactive_count": 3},
    )


def test_browser_smoke_can_click_and_assert_after_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        click_text="Login",
        expect_text_after=["Dashboard Ready"],
    )

    assert payload["status"] == "success"
    assert payload["click"]["selector"] == "#login"
    assert payload["after_click"]["visible_text_length"] == len("Dashboard Ready")
    assert Path(payload["after_click"]["screenshot_path"]).exists()
    assert Path(payload["after_click"]["html_path"]).exists()
    assert Path(payload["after_click"]["visible_text_path"]).exists()
    assert payload["change"]["visible_text_changed"] is True
    assert payload["change"]["url_changed"] is True


def test_browser_smoke_run_dir_avoids_existing_timestamp_directory(tmp_path, monkeypatch) -> None:
    class FixedDateTime:
        @classmethod
        def now(cls, _tz):
            return cls()

        def strftime(self, _format):
            return "20260605-000000-000000"

    monkeypatch.setattr("visual_agent.browser_smoke.datetime", FixedDateTime)

    first = browser_smoke_run_dir(tmp_path)
    second = browser_smoke_run_dir(tmp_path)

    assert first.name == "browser-smoke-20260605-000000-000000"
    assert second.name == "browser-smoke-20260605-000000-000000-1"


def test_browser_smoke_can_assert_url_before_and_after_click(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        expect_url_contains=["/login"],
        click_text="Login",
        wait_for_url_contains_after=["/dashboard"],
        expect_url_contains_after=["/dashboard"],
    )

    assert payload["status"] == "success"
    assert payload["waits"][0]["type"] == "wait_for_url_contains_after"
    assert payload["after_click"]["url"] == "https://example.test/dashboard"


def test_browser_smoke_reports_missing_url_fragment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        expect_url_contains=["/orders"],
    )

    assert payload["status"] == "failed"
    assert payload["issues"][0]["type"] == "missing_url_fragment"


def test_browser_smoke_can_require_click_to_change_page(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_no_change_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        click_text="Login",
        require_change_after_click=True,
    )

    assert payload["status"] == "failed"
    assert payload["issues"][0]["type"] == "no_change_after_click"
    assert payload["change"]["changed"] is False


def test_browser_smoke_can_fill_inputs_before_click(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        fill=["用户名=demo_user"],
        fill_selector=["#password=demo_password"],
        click_text="Login",
        expect_text_after=["Dashboard Ready"],
    )
    markdown = browser_smoke_to_markdown(payload)

    assert payload["status"] == "success"
    assert payload["fills"] == [
        {"status": "filled", "target": "用户名", "selector": "#username", "value_length": len("demo_user")},
        {"status": "filled", "target": "#password", "selector": "#password", "value_length": len("demo_password")},
    ]
    assert "## Fills" in markdown


def test_browser_smoke_redacts_fill_values_from_payload_urls_and_waits(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_secret_url_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        fill=["用户名=demo_user"],
        fill_selector=["#password=demo_password"],
        click_text="Login",
        wait_for_url_contains_after=["username=demo_user"],
        expect_url_contains_after=["password=demo_password"],
    )
    raw = json.dumps(payload, ensure_ascii=False)
    markdown = browser_smoke_to_markdown(payload)

    assert payload["status"] == "success"
    assert payload["after_click"]["url"] == "https://example.test/dashboard?username=[REDACTED]&password=[REDACTED]"
    assert payload["waits"][0]["text"] == "username=[REDACTED]"
    assert "demo_user" not in raw
    assert "demo_password" not in raw
    assert "demo_user" not in markdown
    assert "demo_password" not in markdown


def test_browser_smoke_can_save_reusable_workflow_without_fill_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)
    workflow_path = tmp_path / "workflows" / "login_smoke.yaml"

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        fill=["用户名=Dashboard Ready"],
        fill_selector=["#password=demo_password"],
        click_text="Login",
        wait_for_text_after=["Dashboard Ready"],
        expect_text_after=["Dashboard Ready"],
        save_workflow=workflow_path,
    )
    workflow_text = workflow_path.read_text(encoding="utf-8")
    inputs_template_path = tmp_path / "workflows" / "login_smoke.inputs.example.json"
    inputs_template_text = inputs_template_path.read_text(encoding="utf-8")
    workflow = parse_workflow_file(workflow_path)
    validation = validate_workflow(workflow, strict=True)

    assert payload["status"] == "success"
    assert payload["workflow_export"]["path"] == str(workflow_path.resolve())
    assert payload["workflow_export"]["inputs_template_path"] == str(inputs_template_path)
    assert payload["workflow_export"]["inputs_template"] == {"username": "", "password": ""}
    assert payload["workflow_export"]["sensitive_fields"] == ["password"]
    assert payload["workflow_export"]["parameterized_assertions"] == [
        {"field": "expect_text_after", "source": "input.username", "reason": "contains_fill_value"},
        {"field": "wait_for_text_after", "source": "input.username", "reason": "contains_fill_value"},
    ]
    assert workflow.steps[0].action == "observe_browser"
    assert any(step.action == "assert_browser_ready" for step in workflow.steps)
    assert any(step.action == "paste" and step.params.get("value_from") == "input.password" for step in workflow.steps)
    assert any(step.action == "assert_text" and step.params.get("text_from") == "input.username" for step in workflow.steps)
    assert validation.valid
    assert "Dashboard Ready" not in workflow_text
    assert "demo_password" not in workflow_text
    assert json.loads(inputs_template_text) == {"username": "", "password": ""}
    assert "Dashboard Ready" not in inputs_template_text
    assert "demo_password" not in inputs_template_text


def test_browser_smoke_workflow_export_parameterizes_url_assertions() -> None:
    workflow, export = build_browser_smoke_workflow(
        url="https://example.test/login",
        timeout_ms=10_000,
        wait_until="domcontentloaded",
        min_text_length=1,
        min_interactive=0,
        expect_text=[],
        expect_url_contains=[],
        expect_text_after=[],
        expect_url_contains_after=["password=demo_password"],
        wait_for_text_after=[],
        wait_for_url_contains_after=["username=demo_user"],
        wait_timeout_seconds=5.0,
        click_text="Login",
        click_selector=None,
        fill=["用户名=demo_user"],
        fill_selector=["#password=demo_password"],
        require_change_after_click=True,
        wait_after_seconds=0.5,
        workflow_name="login_smoke",
    )

    assert export["parameterized_assertions"] == [
        {"field": "expect_url_contains_after", "source": "input.password", "reason": "contains_fill_value"},
        {"field": "wait_for_url_contains_after", "source": "input.username", "reason": "contains_fill_value"},
    ]
    assert any(step.get("url_contains_from") == "input.password" for step in workflow["steps"])
    assert any(step.get("url_contains_from") == "input.username" for step in workflow["steps"])
    assert "demo_user" not in json.dumps(workflow, ensure_ascii=False)
    assert "demo_password" not in json.dumps(workflow, ensure_ascii=False)


def test_browser_smoke_waits_for_text_after_click(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        click_text="Login",
        wait_for_text_after=["Dashboard Ready"],
        expect_text_after=["Dashboard Ready"],
    )

    assert payload["status"] == "success"
    assert payload["waits"] == [
        {"status": "found", "type": "wait_for_text_after", "text": "Dashboard Ready", "timeout_seconds": 5.0}
    ]


def test_browser_smoke_wait_timeout_is_reported_as_issue(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    payload = run_browser_smoke(
        url="https://example.test/login",
        output_dir=tmp_path,
        click_text="Login",
        wait_for_text_after=["Never Appears"],
        wait_timeout_seconds=0.1,
    )

    assert payload["status"] == "failed"
    assert payload["waits"][0]["status"] == "timeout"
    assert payload["issues"][0]["type"] == "wait_for_text_after"


def test_browser_smoke_reports_missing_expected_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    payload = run_browser_smoke(url="https://example.test/login", output_dir=tmp_path, expect_text=["Orders"])

    assert payload["status"] == "failed"
    assert payload["issues"][0]["type"] == "missing_text"
    assert "Orders" in browser_smoke_to_markdown(payload)


def test_browser_smoke_cli_outputs_json(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)

    code = main(
        [
            "browser-smoke",
            "--url",
            "https://example.test/login",
            "--output-dir",
            str(tmp_path),
            "--fill",
            "用户名=demo_user",
            "--click-selector",
            "#login",
            "--require-change-after-click",
            "--wait-for-text-after",
            "Dashboard Ready",
            "--wait-for-url-contains-after",
            "/dashboard",
            "--expect-text-after",
            "Dashboard Ready",
            "--expect-url-contains-after",
            "/dashboard",
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "success"
    assert payload["fills"][0]["selector"] == "#username"
    assert payload["click"]["selector"] == "#login"
    assert payload["waits"][0]["status"] == "found"
    assert payload["waits"][1]["type"] == "wait_for_url_contains_after"
    assert payload["change"]["changed"] is True


def test_browser_smoke_cli_can_save_workflow(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.setattr("visual_agent.browser_smoke.observe_browser", fake_observe_browser)
    workflow_path = tmp_path / "login_smoke.yaml"

    code = main(
        [
            "browser-smoke",
            "--url",
            "https://example.test/login",
            "--output-dir",
            str(tmp_path),
            "--fill-selector",
            "#password=demo_password",
            "--click-selector",
            "#login",
            "--expect-text-after",
            "Dashboard Ready",
            "--save-workflow",
            str(workflow_path),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert workflow_path.exists()
    assert (tmp_path / "login_smoke.inputs.example.json").exists()
    assert payload["workflow_export"]["inputs_template"] == {"password": ""}
    assert "demo_password" not in workflow_path.read_text(encoding="utf-8")
