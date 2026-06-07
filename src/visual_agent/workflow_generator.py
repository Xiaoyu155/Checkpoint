from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .workflow import parse_workflow_file, workflow_from_dict


DEFAULT_MODEL = "claude-haiku-4-5-20251001"

WORKFLOW_SYSTEM_PROMPT = """You generate Visual Agent workflow YAML for local UI verification.

Return only valid YAML, without markdown fences or explanation.

Use this schema:
schema_version: 1
min_runtime_version: "0.1.0"
name: snake_case_name
version: 1
description: "Human readable workflow description"
tags: [verification, fast]
visibility: private
author: ""
license: ""
steps:
  - id: observe_page
    action: observe_browser
    url: "http://localhost:3000"
  - id: browser_ready
    action: assert_browser_ready
    min_text_length: 1
    min_interactive: 1
  - id: fill_email
    action: type
    target:
      label: "Email"
      role: input
    value: "demo@example.com"
  - id: click_submit
    action: click
    target:
      text: "Submit"
      role: button
    wait_after_seconds: 0.5
    browser_post_action_observe: true
  - id: observe_result
    action: observe_browser
    reuse_page: true
  - id: verify_result
    action: assert_text
    text: "Success"

Available actions:
- observe_browser: url or reuse_page
- observe_dom: url
- click: target with selector, text, label, role, contains_text, row_text, or column_header
- type: target plus value
- paste: target plus value
- press_key: keys
- click_text: text, label, or contains_text
- wait_for_text: text or contains_text
- assert_text: text
- assert_no_error
- assert_browser_ready
- assert_product_contract
- request_api

Rules:
1. For browser workflows, start with observe_browser.
2. Add assert_browser_ready after the initial browser observation so blank pages fail.
3. After mutating browser actions, set browser_post_action_observe: true or add observe_browser with reuse_page: true before assertions.
4. Always include tags with verification. Add fast for short checks.
5. Always include visibility: private, author: "", and license: "".
6. Use only Visual Agent actions listed above.
7. Prefer semantic targets over coordinates.
8. End with assertions that verify the expected outcome.
"""


def generate_workflow_yaml(
    *,
    description: str,
    workspace_root: Path,
    output_path: Path | None = None,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> dict[str, Any]:
    clean_description = description.strip()
    if not clean_description:
        return {"status": "error", "message": "description is required"}

    yaml_text: str
    source = "anthropic"
    try:
        yaml_text = _generate_with_anthropic(clean_description, model=model)
    except ImportError:
        source = "template_fallback"
        yaml_text = _template_workflow(clean_description)
    except (TypeError, ValueError) as exc:
        if not _is_auth_configuration_error(exc):
            return {"status": "error", "message": f"workflow generation failed: {type(exc).__name__}: {exc}"}
        source = "template_fallback"
        yaml_text = _template_workflow(clean_description)
    except Exception as exc:
        return {"status": "error", "message": f"workflow generation failed: {type(exc).__name__}: {exc}"}

    yaml_text = _strip_markdown_fences(yaml_text)
    validation = _validate_generated_yaml(yaml_text)
    if validation.get("status") != "success":
        return {
            "status": "error",
            "message": "generated YAML is not a valid Visual Agent workflow",
            "validation": validation,
            "yaml": yaml_text,
            "source": source,
        }

    saved_to: str | None = None
    if not dry_run:
        if output_path is None:
            name = str(validation["workflow_name"])
            output_path = workspace_root.parent / "workflows" / f"{name}.yaml"
        output_path = output_path.resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(yaml_text.rstrip() + "\n", encoding="utf-8")
        saved_to = str(output_path)
        parse_workflow_file(output_path)

    return {
        "status": "success",
        "source": source,
        "model": model if source == "anthropic" else None,
        "workflow_name": validation["workflow_name"],
        "yaml": yaml_text,
        "saved_to": saved_to,
        "message": f"Saved to: {saved_to}" if saved_to else "Generated workflow YAML.",
    }


def _generate_with_anthropic(description: str, *, model: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=1600,
        system=WORKFLOW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": description}],
    )
    content = getattr(message, "content", [])
    if not content:
        raise RuntimeError("Anthropic returned an empty response.")
    first = content[0]
    text = getattr(first, "text", None)
    if text is None and isinstance(first, dict):
        text = first.get("text")
    if not text:
        raise RuntimeError("Anthropic response did not contain text.")
    return str(text)


def _template_workflow(description: str) -> str:
    name = _description_to_name(description)
    quoted_description = json.dumps(description, ensure_ascii=False)
    return f"""schema_version: 1
min_runtime_version: "0.1.0"
name: {name}
version: 1
description: {quoted_description}
tags: [verification, fast]
visibility: private
author: ""
license: ""
steps:
  - id: observe_page
    action: observe_browser
    url: "http://localhost:3000"
  - id: browser_ready
    action: assert_browser_ready
    min_text_length: 1
  - id: verify_expected_text
    action: assert_text
    text: "Expected text here"
"""


def _validate_generated_yaml(yaml_text: str) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        return {"status": "error", "message": f"PyYAML is required: {exc}"}
    try:
        payload = yaml.safe_load(yaml_text)
        if not isinstance(payload, dict):
            return {"status": "error", "message": "YAML root must be an object."}
        workflow = workflow_from_dict(payload)
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "success",
        "workflow_name": workflow.name,
        "step_count": len(workflow.steps),
        "tags": list(workflow.tags),
    }


def _description_to_name(description: str) -> str:
    text = description.lower()
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        words = ["generated", "workflow"]
    name = "_".join(words[:6])[:50].strip("_")
    return name or "generated_workflow"


def _strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^```(?:yaml|yml)?\s*", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _is_auth_configuration_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(part in text for part in ("api_key", "auth_token", "authentication method", "credentials"))
