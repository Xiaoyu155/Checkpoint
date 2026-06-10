# Workflow Schema

Checkpoint workflows are YAML or JSON files with a stable top-level shape.

```yaml
schema_version: 1
min_runtime_version: "0.1.0"
name: login_basic
version: 1
description: Verify login reaches the dashboard.
tags: [verification, fast, auth]
visibility: private
author: ""
license: ""
steps:
  - id: observe_login
    action: observe_browser
    url: "http://localhost:3000/login"
  - id: browser_ready
    action: assert_browser_ready
    min_text_length: 1
    min_interactive: 1
  - id: verify_dashboard
    action: assert_text
    text: Dashboard
```

## Top-Level Fields

- `schema_version`: required schema version. Current value: `1`.
- `min_runtime_version`: minimum Checkpoint runtime.
- `name`: stable snake_case workflow name.
- `version`: workflow revision integer.
- `description`: human-readable purpose.
- `tags`: tags for selection, sharing, and CI filtering.
- `affects`: optional source paths this workflow validates.
- `visibility`: `private` or `public`.
- `author`: workflow author.
- `license`: recommended `cc-by-4.0` for public workflows.
- `steps`: non-empty ordered step list.

## Common Actions

- `observe_browser`: open or reuse a browser page.
- `observe_dom`: inspect structured DOM.
- `observe_ocr`: inspect screen or image text.
- `click`, `type`, `paste`, `press_key`: interact with a target.
- `wait_for`, `wait_for_text`: wait for text, selector, URL, or response.
- `assert_browser_ready`: fail blank or non-interactive pages.
- `assert_text`, `assert_text_contract`, `assert_no_error`: validate UI state.
- `request_api`, `assert_response`: validate network/API behavior.

## Versioning

- `schema_version` is the compatibility gate for the file format.
- `version` is the workflow revision number and can change without a schema migration.
- If a future runtime needs to reject a workflow, it should do so on `schema_version` first and preserve the workflow payload in the error output.
- See [Long-Term Vision](long_term_vision.md) for the planned compatibility contract.

## Targets

Prefer stable semantic targets:

```yaml
target:
  test_id: submit
  role: button
```

Other supported target fields include `selector`, `text`, `label`, `contains_text`, `row_text`, and `column_header`.

## Validation

```powershell
python -m visual_agent.cli validate-workflow --file workflows/login_basic.yaml
python -m visual_agent.cli workflow-lint workflows/login_basic.yaml --format markdown
```

