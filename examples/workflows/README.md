# Workflow Examples

The root `examples/*.yaml` files remain for backward-compatible commands. This directory groups commonly used examples by workflow type.

## readonly

Read-only observation and assertion workflows.

- `readonly/minimal_testable_workflow.yaml`
- `readonly/vision_mock_workflow.yaml`

## form-fill

Form entry workflows that should normally run as dry-run unless explicitly approved.

- `form-fill/local_html_form_workflow.yaml`
- `form-fill/browser_form_workflow.yaml`

## download

Download workflows with explicit download policies and file assertions.

- `download/browser_download_workflow.yaml`

## auth

Storage-state save/restore workflows. These should use redacted auth-state metadata and never print cookie/token values.

- `auth/browser_auth_save_workflow.yaml`
- `auth/browser_auth_restore_workflow.yaml`
