from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import to_jsonable
from .workflow import Workflow, WorkflowStep, parse_workflow_file


@dataclass(frozen=True)
class IntegrationSnippetInstall:
    root: Path
    cursor_rules: Path
    copilot_instructions: Path
    windsurf_rules: Path
    jetbrains_spec: Path


@dataclass(frozen=True)
class PlaywrightExport:
    workflow_path: Path
    workflow_name: str
    output_path: Path | None
    spec: str
    unsupported_actions: tuple[str, ...]
    step_count: int


def install_integration_snippets(
    root: str | Path,
    *,
    workspace_root: str = ".agent-workspace",
    overwrite: bool = False,
) -> IntegrationSnippetInstall:
    target_root = Path(root)
    cursor_path = target_root / ".cursorrules"
    copilot_path = target_root / ".github" / "copilot-instructions.md"
    windsurf_path = target_root / ".windsurfrules"
    jetbrains_path = target_root / "docs" / "jetbrains-plugin-spec.md"

    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    copilot_path.parent.mkdir(parents=True, exist_ok=True)
    windsurf_path.parent.mkdir(parents=True, exist_ok=True)
    jetbrains_path.parent.mkdir(parents=True, exist_ok=True)

    write_text_if_changed(cursor_path, cursor_rules_text(workspace_root), overwrite=overwrite)
    write_text_if_changed(copilot_path, copilot_instructions_text(workspace_root), overwrite=overwrite)
    write_text_if_changed(windsurf_path, windsurf_rules_text(workspace_root), overwrite=overwrite)
    write_text_if_changed(jetbrains_path, jetbrains_plugin_spec_text(workspace_root), overwrite=overwrite)

    return IntegrationSnippetInstall(
        root=target_root,
        cursor_rules=cursor_path,
        copilot_instructions=copilot_path,
        windsurf_rules=windsurf_path,
        jetbrains_spec=jetbrains_path,
    )


def integration_snippets_to_dict(install: IntegrationSnippetInstall) -> dict[str, Any]:
    return {
        "root": str(install.root),
        "cursor_rules": str(install.cursor_rules),
        "copilot_instructions": str(install.copilot_instructions),
        "windsurf_rules": str(install.windsurf_rules),
        "jetbrains_spec": str(install.jetbrains_spec),
    }


def export_workflow_to_playwright(
    workflow_path: str | Path,
    *,
    output_path: str | Path | None = None,
    spec_name: str | None = None,
) -> PlaywrightExport:
    path = Path(workflow_path)
    workflow = parse_workflow_file(path)
    spec = workflow_to_playwright_spec(workflow, spec_name=spec_name)
    output = None
    if output_path is not None:
        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(spec.rstrip() + "\n", encoding="utf-8")
    return PlaywrightExport(
        workflow_path=path.resolve(),
        workflow_name=workflow.name,
        output_path=output,
        spec=spec,
        unsupported_actions=tuple(_unsupported_workflow_actions(workflow)),
        step_count=len(workflow.steps),
    )


def playwright_export_to_dict(export: PlaywrightExport) -> dict[str, Any]:
    return {
        "workflow_path": str(export.workflow_path),
        "workflow_name": export.workflow_name,
        "output_path": str(export.output_path) if export.output_path else None,
        "step_count": export.step_count,
        "unsupported_actions": list(export.unsupported_actions),
        "spec": export.spec,
    }


def workflow_to_playwright_spec(workflow: Workflow, *, spec_name: str | None = None) -> str:
    title = spec_name or workflow.name
    entry_url = _workflow_entry_url(workflow)
    unsupported = _unsupported_workflow_actions(workflow)
    lines = [
        "import { expect, test } from '@playwright/test';",
        "",
        "function locatorForTarget(page, target) {",
        "  if (target.selector) return page.locator(target.selector);",
        "  if (target.test_id) return page.getByTestId(target.test_id);",
        "  if (target.role && (target.text || target.label)) return page.getByRole(target.role, { name: target.text || target.label });",
        "  if (target.label) return page.getByLabel(target.label);",
        "  if (target.text) return page.getByText(target.text, { exact: true });",
        "  if (target.contains_text) return page.getByText(target.contains_text);",
        "  if (target.selector_or_text) return page.locator(target.selector_or_text);",
        "  return page.locator('body');",
        "}",
        "",
        "function resolveInput(name) {",
        "  const envKey = `VISUAL_AGENT_INPUT_${name.replace(/[^a-z0-9]+/gi, '_').toUpperCase()}`;",
        "  return process.env[envKey] ?? `{{${name}}}`;",
        "}",
        "",
        f"test.describe({json.dumps(workflow.name, ensure_ascii=False)}, () => {{",
        f"  test({json.dumps(title, ensure_ascii=False)}, async ({'{'} page {'}'}) => {{",
        f"    await page.goto({json.dumps(entry_url, ensure_ascii=False)});",
    ]

    for step in workflow.steps:
        lines.extend(_step_to_playwright_lines(step, unsupported))

    lines.extend(
        [
            "  });",
            "});",
            "",
        ]
    )
    return "\n".join(lines)


def cursor_rules_text(workspace_root: str) -> str:
    return f"""# Checkpoint Cursor Rules

- After UI, route, or form changes, update or add workflows in `workflows/` and keep them tagged `verification`.
- Run `visual-agent workflow-lint <workflow>` before shipping a workflow.
- Run `visual-agent verify-impl --workspace-root {workspace_root} --task-description "<task>" --base-url <url-or-fixture>` after UI changes that should be verified.
- Use `visual-agent export-to-playwright <workflow.yaml> --output <workflow.spec.ts>` when a workflow needs to live in Playwright Test.
- Read `.visual-agent-status.md` first when a workspace status file exists.
- Prefer the smallest workflow change that proves the UI behavior actually works.
"""


def copilot_instructions_text(workspace_root: str) -> str:
    return f"""# Checkpoint Copilot Instructions

When the repository uses Checkpoint:

1. Keep verification workflows in `workflows/` and run `visual-agent workflow-lint` on them.
2. Use `visual-agent verify-impl --workspace-root {workspace_root}` after changing UI, route, or form behavior.
3. Consult `.visual-agent-status.md` before suggesting a fix.
4. Export workflows to Playwright with `visual-agent export-to-playwright` when a test file is needed.
5. Prefer stable targets: selector, test_id, role, label, or text. Avoid fragile coordinates unless the workflow is explicitly desktop-visual.
"""


def windsurf_rules_text(workspace_root: str) -> str:
    return f"""# Checkpoint Windsurf Rules

- Validate UI changes with `visual-agent workflow-lint`.
- Use `visual-agent verify-impl --workspace-root {workspace_root}` for implementation checks.
- Convert workflow YAML to Playwright Test with `visual-agent export-to-playwright`.
- Check `.visual-agent-status.md` for the current verification state before recommending additional work.
"""


def jetbrains_plugin_spec_text(workspace_root: str) -> str:
    return f"""# JetBrains Plugin Spec for Checkpoint

This document describes the minimum JetBrains integration expected for Checkpoint parity.

## Goals

- Surface workflow creation, linting, and execution from the IDE.
- Show the current verification state from `.visual-agent-status.md`.
- Offer one-click export from workflow YAML to Playwright Test.
- Keep the UX consistent with the existing VS Code integration.

## Required commands

- `visual-agent workflow-lint <workflow>`
- `visual-agent verify-impl --workspace-root {workspace_root} --task-description <task> --base-url <url>`
- `visual-agent export-to-playwright <workflow.yaml> --output <workflow.spec.ts>`
- `visual-agent show-status --workspace-root {workspace_root}`

## Required editor features

- Workflow file detection and syntax highlighting.
- Quick actions for running, linting, and exporting a selected workflow.
- Failure details linking back to the latest report, screenshot, and related files.
- A status panel that reads the latest workspace state file.

## Non-goals

- No new execution engine.
- No new workflow schema.
- No replacement for the existing CLI or VS Code extension.

## Acceptance criteria

- The plugin can launch the existing Checkpoint commands.
- The plugin can read and display the workspace status file.
- The plugin can open the exported Playwright spec generated by Checkpoint.
"""


def write_text_if_changed(path: Path, text: str, *, overwrite: bool) -> None:
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing == text:
            return
        if not overwrite:
            raise FileExistsError(f"File already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def _workflow_entry_url(workflow: Workflow) -> str:
    for step in workflow.steps:
        if step.action in {"observe_browser", "observe_dom"}:
            url = str(step.params.get("url") or "").strip()
            if url:
                return url
    return "http://localhost:3000"


def _unsupported_workflow_actions(workflow: Workflow) -> list[str]:
    unsupported = []
    for step in workflow.steps:
        if step.action in {"click_visual", "assert_visual_text", "set_variable", "run_workflow"}:
            unsupported.append(step.action)
    return unsupported


def _step_to_playwright_lines(step: WorkflowStep, unsupported: list[str]) -> list[str]:
    action = step.action
    indent = "    "
    comment = f"{indent}// {step.id}: {action}"
    lines = [comment]
    params = step.params

    if action in {"observe_browser", "observe_dom"}:
        url = str(params.get("url") or "").strip()
        if url:
            lines.append(f"{indent}await page.goto({json.dumps(url, ensure_ascii=False)});")
        elif bool(params.get("reuse_page")):
            lines.append(f"{indent}await page.reload();")
        else:
            lines.append(f"{indent}await page.waitForLoadState('networkidle');")
        return lines

    if action == "assert_browser_ready":
        lines.append(f"{indent}await expect(page.locator('body')).toBeVisible();")
        return lines

    if action == "click":
        lines.append(f"{indent}await locatorForTarget(page, {json.dumps(to_jsonable(params.get('target') or {}), ensure_ascii=False)}).click();")
        return lines

    if action in {"type", "paste"}:
        target = json.dumps(to_jsonable(params.get("target") or {}), ensure_ascii=False)
        value_expr = _value_expression(params)
        lines.append(f"{indent}await locatorForTarget(page, {target}).fill({value_expr});")
        return lines

    if action == "press_key":
        keys = params.get("keys") or params.get("key")
        if isinstance(keys, list):
            keys_expr = json.dumps("+".join(str(item) for item in keys if str(item).strip()), ensure_ascii=False)
        else:
            keys_expr = json.dumps(str(keys or ""), ensure_ascii=False)
        lines.append(f"{indent}await page.keyboard.press({keys_expr});")
        return lines

    if action == "click_text":
        text = json.dumps(str(params.get("text") or params.get("label") or params.get("contains_text") or ""), ensure_ascii=False)
        lines.append(f"{indent}await page.getByText({text}, {{ exact: true }}).click();")
        return lines

    if action in {"wait_for_text", "assert_text"}:
        text = json.dumps(str(params.get("text") or params.get("contains_text") or ""), ensure_ascii=False)
        lines.append(f"{indent}await expect(page.getByText({text}, {{ exact: true }})).toBeVisible();")
        return lines

    if action == "assert_response":
        lines.append(f"{indent}await page.waitForResponse((response) => {_response_predicate(params)});")
        return lines

    if action == "assert_text_contract":
        required_all = _string_list(params.get("required_all") if "required_all" in params else params.get("text"))
        required_any = _string_list(params.get("required_any"))
        forbidden_any = _string_list(params.get("forbidden_any") if "forbidden_any" in params else params.get("forbidden_text"))
        if required_all:
            for text in required_all:
                lines.append(f"{indent}await expect(page.getByText({json.dumps(text, ensure_ascii=False)}, {{ exact: true }})).toBeVisible();")
        elif required_any:
            first = required_any[0]
            lines.append(f"{indent}await expect(page.getByText({json.dumps(first, ensure_ascii=False)}, {{ exact: true }})).toBeVisible();")
        if forbidden_any:
            for text in forbidden_any:
                lines.append(f"{indent}await expect(page.getByText({json.dumps(text, ensure_ascii=False)}, {{ exact: true }})).toHaveCount(0);")
        if not required_all and not required_any and not forbidden_any:
            lines.append(f"{indent}throw new Error('assert_text_contract requires required_all, required_any, or forbidden_any.');")
        return lines

    if action == "assert_url_contains":
        fragment = str(params.get("fragment") or params.get("url_contains") or "")
        if fragment:
            lines.append(f"{indent}await expect(page).toHaveURL(new RegExp({json.dumps(_regex_escape(fragment), ensure_ascii=False)}));")
        else:
            lines.append(f"{indent}throw new Error('assert_url_contains requires fragment.');")
        return lines

    if action == "assert_count":
        selector = str(params.get("selector") or "")
        target = params.get("target") if isinstance(params.get("target"), dict) else {}
        min_count = params.get("min")
        max_count = params.get("max")
        locator_expr = _locator_expression(selector=selector, target=target)
        if locator_expr and min_count is not None and max_count is not None and int(min_count) == int(max_count):
            lines.append(f"{indent}await expect({locator_expr}).toHaveCount({int(min_count)});")
        elif locator_expr:
            lines.append(f"{indent}const {step.id}_count = await {locator_expr}.count();")
            if min_count is not None:
                lines.append(f"{indent}expect({step.id}_count).toBeGreaterThanOrEqual({int(min_count)});")
            if max_count is not None:
                lines.append(f"{indent}expect({step.id}_count).toBeLessThanOrEqual({int(max_count)});")
        else:
            lines.append(f"{indent}throw new Error('assert_count requires selector or target.');")
        return lines

    if action == "assert_attribute":
        selector = str(params.get("selector") or "")
        target = params.get("target") if isinstance(params.get("target"), dict) else {}
        attr = str(params.get("attr") or params.get("attribute") or "")
        value = params.get("value")
        locator_expr = _locator_expression(selector=selector, target=target)
        if locator_expr and attr:
            if value is None:
                lines.append(f"{indent}await expect({locator_expr}).toHaveAttribute({json.dumps(attr, ensure_ascii=False)});")
            else:
                lines.append(
                    f"{indent}await expect({locator_expr}).toHaveAttribute({json.dumps(attr, ensure_ascii=False)}, {json.dumps(_playwright_attribute_value(value), ensure_ascii=False)});"
                )
        else:
            lines.append(f"{indent}throw new Error('assert_attribute requires selector or target, plus attr.');")
        return lines

    if action in {"click_visual", "assert_visual_text"}:
        unsupported.append(action)
        lines.append(f"{indent}// {action} has no direct Playwright Test equivalent.")
        lines.append(f"{indent}// target: {json.dumps(to_jsonable(params), ensure_ascii=False)}")
        return lines

    if action in {"set_variable", "run_workflow"}:
        unsupported.append(action)
        lines.append(f"{indent}// {action} needs a helper or nested fixture; Playwright Test has no direct equivalent.")
        return lines

    if action == "refresh_browser":
        lines.append(f"{indent}await page.reload();")
        return lines

    if action == "wait_for":
        lines.extend(_wait_for_lines(params, indent))
        return lines

    lines.append(f"{indent}throw new Error({json.dumps(f'Unsupported Checkpoint action: {action}', ensure_ascii=False)});")
    unsupported.append(action)
    return lines


def _wait_for_lines(params: dict[str, Any], indent: str) -> list[str]:
    condition = str(params.get("condition") or "").strip()
    if not condition and isinstance(params.get("conditions"), list):
        condition = "conditions"
    if condition == "text":
        text = json.dumps(str(params.get("text") or ""), ensure_ascii=False)
        return [f"{indent}await expect(page.getByText({text}, {{ exact: true }})).toBeVisible();"]
    if condition == "target":
        target = json.dumps(to_jsonable(params.get("target") or {}), ensure_ascii=False)
        return [f"{indent}await expect(locatorForTarget(page, {target})).toBeVisible();"]
    if condition == "selector":
        selector = json.dumps(str(params.get("selector") or ""), ensure_ascii=False)
        return [f"{indent}await expect(page.locator({selector})).toBeVisible();"]
    if condition == "url":
        url = str(params.get("url") or params.get("url_contains") or "")
        if url:
            return [f"{indent}await expect(page).toHaveURL(new RegExp({json.dumps(_regex_escape(url), ensure_ascii=False)}));"]
    if condition == "response":
        return [f"{indent}await page.waitForResponse((response) => {_response_predicate(params)});"]
    if isinstance(params.get("conditions"), list) and params["conditions"]:
        lines = []
        for index, item in enumerate(params["conditions"], start=1):
            if isinstance(item, dict):
                lines.extend(_wait_condition_lines(item, indent, prefix=f"// condition {index}: "))
        return lines
    return [f"{indent}// wait_for condition '{condition or 'unknown'}' needs a Playwright expectation mapping."]


def _value_expression(params: dict[str, Any]) -> str:
    if "value_from" in params and str(params.get("value_from") or "").startswith("input."):
        input_name = str(params.get("value_from")).removeprefix("input.")
        return f"resolveInput({json.dumps(input_name, ensure_ascii=False)})"
    if "value" in params:
        return json.dumps(str(params.get("value") or ""), ensure_ascii=False)
    return '""'


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _regex_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("/", "\\/")
    escaped = escaped.replace(".", "\\.").replace("?", "\\?").replace("+", "\\+").replace("*", "\\*").replace("(", "\\(").replace(")", "\\)")
    escaped = escaped.replace("[", "\\[").replace("]", "\\]").replace("{", "\\{").replace("}", "\\}").replace("^", "\\^").replace("$", "\\$").replace("|", "\\|")
    return escaped


def _wait_condition_lines(condition: dict[str, Any], indent: str, *, prefix: str = "") -> list[str]:
    condition_type = str(condition.get("condition") or condition.get("type") or "").strip()
    if condition_type == "text":
        text = json.dumps(str(condition.get("text") or ""), ensure_ascii=False)
        return [f"{indent}await expect(page.getByText({text}, {{ exact: true }})).toBeVisible();"]
    if condition_type == "target":
        target = json.dumps(to_jsonable(condition.get("target") or {}), ensure_ascii=False)
        return [f"{indent}await expect(locatorForTarget(page, {target})).toBeVisible();"]
    if condition_type == "selector":
        selector = json.dumps(str(condition.get("selector") or ""), ensure_ascii=False)
        return [f"{indent}await expect(page.locator({selector})).toBeVisible();"]
    if condition_type == "url":
        url = str(condition.get("url") or condition.get("url_contains") or "")
        if url:
            return [f"{indent}await expect(page).toHaveURL(new RegExp({json.dumps(_regex_escape(url), ensure_ascii=False)}));"]
    if condition_type == "response":
        return [f"{indent}await page.waitForResponse((response) => {_response_predicate(condition)});"]
    return [f"{indent}{prefix}// wait_for condition '{condition_type or 'unknown'}' needs a Playwright expectation mapping."]


def _response_predicate(params: dict[str, Any]) -> str:
    checks: list[str] = []
    url_contains = str(params.get("url_contains") or params.get("url") or "").strip()
    if url_contains:
        checks.append(f"response.url().includes({json.dumps(url_contains, ensure_ascii=False)})")
    method = str(params.get("method") or "").strip()
    if method:
        checks.append(f"response.request().method() === {json.dumps(method, ensure_ascii=False)}")
    status = params.get("status")
    if status is not None:
        checks.append(f"response.status() === {int(status)}")
    status_min = params.get("status_min")
    if status_min is not None:
        checks.append(f"response.status() >= {int(status_min)}")
    status_max = params.get("status_max")
    if status_max is not None:
        checks.append(f"response.status() <= {int(status_max)}")
    ok = params.get("ok")
    if ok is not None:
        checks.append(f"response.ok() === {str(bool(ok)).lower()}")
    return " && ".join(checks) if checks else "true"


def _locator_expression(*, selector: str = "", target: dict[str, Any] | None = None) -> str | None:
    if selector:
        return f"page.locator({json.dumps(selector, ensure_ascii=False)})"
    target = target or {}
    if target.get("selector"):
        return f"page.locator({json.dumps(str(target.get('selector')), ensure_ascii=False)})"
    if target.get("test_id"):
        return f"page.getByTestId({json.dumps(str(target.get('test_id')), ensure_ascii=False)})"
    role = str(target.get("role") or "").strip()
    name = str(target.get("text") or target.get("label") or "").strip()
    if role and name:
        return f"page.getByRole({json.dumps(role, ensure_ascii=False)}, {{ name: {json.dumps(name, ensure_ascii=False)} }})"
    if target.get("label"):
        return f"page.getByLabel({json.dumps(str(target.get('label')), ensure_ascii=False)})"
    if target.get("text"):
        return f"page.getByText({json.dumps(str(target.get('text')), ensure_ascii=False)}, {{ exact: true }})"
    if target.get("contains_text"):
        return f"page.getByText({json.dumps(str(target.get('contains_text')), ensure_ascii=False)})"
    if target.get("selector_or_text"):
        return f"page.locator({json.dumps(str(target.get('selector_or_text')), ensure_ascii=False)})"
    return None


def _playwright_attribute_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)

