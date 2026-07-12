from __future__ import annotations

from pathlib import Path

from visual_agent.cli import main
from visual_agent.integrations import export_workflow_to_playwright, install_integration_snippets


def test_install_integration_snippets_writes_editor_rules(tmp_path: Path) -> None:
    result = install_integration_snippets(tmp_path, workspace_root=".agent-workspace")

    cursor_text = result.cursor_rules.read_text(encoding="utf-8")
    copilot_text = result.copilot_instructions.read_text(encoding="utf-8")
    windsurf_text = result.windsurf_rules.read_text(encoding="utf-8")
    jetbrains_text = result.jetbrains_spec.read_text(encoding="utf-8")

    assert result.cursor_rules.exists()
    assert result.copilot_instructions.exists()
    assert result.windsurf_rules.exists()
    assert result.jetbrains_spec.exists()
    assert "visual-agent workflow-lint" in cursor_text
    assert "visual-agent verify-impl" in cursor_text
    assert "visual-agent export-to-playwright" in cursor_text
    assert "workflow-lint" in copilot_text
    assert "export-to-playwright" in windsurf_text
    assert "JetBrains Plugin Spec for Checkpoint" in jetbrains_text


def test_export_workflow_to_playwright_generates_spec(tmp_path: Path) -> None:
    workflow = tmp_path / "login.yaml"
    workflow.write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: login_flow
version: 1
steps:
  - id: observe_initial
    action: observe_browser
    url: http://localhost:3000/login
  - id: fill_email
    action: type
    target:
      selector: input[name="email"]
    value_from: input.email
  - id: click_submit
    action: click
    target:
      text: Submit
      role: button
  - id: wait_api
    action: wait_for
    condition: response
    url_contains: /api/orders
    method: POST
    status: 201
  - id: wait_multi
    action: wait_for
    conditions:
      - condition: text
        text: Loading
      - condition: selector
        selector: "#ready"
      - condition: url
        url_contains: /login
  - id: assert_api
    action: assert_response
    url_contains: /api/orders
    method: POST
    status: 201
  - id: assert_count_target
    action: assert_count
    target:
      text: Item
    min: 1
    max: 3
  - id: assert_attr_target
    action: assert_attribute
    target:
      label: Submit
      role: button
    attr: disabled
    value: false
  - id: verify_success
    action: assert_text
    text: Welcome
  - id: visual_check
    action: click_visual
    description: blue submit button
""".strip(),
        encoding="utf-8",
    )

    result = export_workflow_to_playwright(workflow)

    assert result.workflow_name == "login_flow"
    assert "import { expect, test } from '@playwright/test';" in result.spec
    assert 'await page.goto("http://localhost:3000/login");' in result.spec
    assert 'resolveInput("email")' in result.spec
    assert "await locatorForTarget(page, {\"text\": \"Submit\", \"role\": \"button\"}).click();" in result.spec
    assert "await page.waitForResponse((response) => response.url().includes(\"/api/orders\") && response.request().method() === \"POST\" && response.status() === 201);" in result.spec
    assert 'await expect(page.getByText("Loading", { exact: true })).toBeVisible();' in result.spec
    assert 'await expect(page.locator("#ready")).toBeVisible();' in result.spec
    assert 'await expect(page).toHaveURL(new RegExp("\\\\/login"));' in result.spec
    assert 'const assert_count_target_count = await page.getByText("Item", { exact: true }).count();' in result.spec
    assert 'await expect(page.getByRole("button", { name: "Submit" })).toHaveAttribute("disabled", "false");' in result.spec
    assert "await expect(page.getByText(\"Welcome\", { exact: true })).toBeVisible();" in result.spec
    assert "click_visual has no direct Playwright Test equivalent" in result.spec
    assert result.unsupported_actions == ("click_visual",)


def test_export_to_playwright_cli_writes_file(tmp_path: Path, capsys) -> None:
    workflow = tmp_path / "login.yaml"
    workflow.write_text(
        """
schema_version: 1
min_runtime_version: "0.1.0"
name: login_flow
version: 1
steps:
  - id: observe_initial
    action: observe_browser
    url: http://localhost:3000/login
  - id: verify_success
    action: assert_text
    text: Welcome
""".strip(),
        encoding="utf-8",
    )
    output = tmp_path / "login.spec.ts"

    code = main(["export-to-playwright", str(workflow), "--output", str(output)])
    stdout = capsys.readouterr().out

    assert code == 0
    assert output.exists()
    assert "test.describe" in output.read_text(encoding="utf-8")
    assert "login_flow" in stdout
    assert "resolveInput(\"email\")" in stdout or "Welcome" in stdout


def test_generate_integrations_cli_writes_files(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "generate-integrations",
            "--root",
            str(tmp_path),
            "--workspace-root",
            ".agent-workspace",
        ]
    )
    payload = capsys.readouterr().out

    assert code == 0
    assert (tmp_path / ".cursorrules").exists()
    assert (tmp_path / ".github" / "copilot-instructions.md").exists()
    assert (tmp_path / ".windsurfrules").exists()
    assert (tmp_path / "docs" / "jetbrains-plugin-spec.md").exists()
    assert "cursor_rules" in payload

