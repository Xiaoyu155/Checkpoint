# Agent Handoff Guide

Use this when opening a new Codex window or handing a project to another agent.

## Update Checkpoint

From the Checkpoint checkout:

```powershell
cd "D:\longxia agent"
git pull origin main
.\.venv\Scripts\python.exe -m pip install -e ".[desktop,web,mcp]"
.\.venv\Scripts\python.exe -m visual_agent.cli doctor
```

If an MCP server or Codex integration is already running, restart that process
after updating. Old MCP processes keep using the old imported code.

## Initialize The Project Workspace

From the product/project being edited:

```powershell
python -m visual_agent.cli init --root .agent-workspace
python -m visual_agent.cli show-status --workspace-root .agent-workspace
python -m visual_agent.cli verify-impl --workspace-root .agent-workspace --task-description "Verify the current change" --run-profile dry-run --format markdown
```

Check that `project_root` is the project directory you are editing. Each project
gets its own `.agent-workspace`.

## Resume Context

At the start of a new chat:

```powershell
python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
```

With MCP, call `get_session_context`. If the snapshot shows a failure, call
`summarize_latest_failure`.

## Run Verification

Run only the workflow relevant to the current change when possible:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --workflow <workflow_name> --wait-lock --format markdown
```

For a broader gate:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --tags verification --max-workflows 10 --wait-lock --format markdown
```

## Visual And OCR Workflows

For workflows that need a real desktop window:

```yaml
window:
  title_contains: "Target App"
  bring_to_front: true
```

Checkpoint automatically uses the global visual lock, captures evidence,
minimizes the target window after capture, and restores the previous foreground
window. Use `post_capture: keep` only when the workflow deliberately needs the
target left open.

For OCR text coordinates on Windows, install:

```powershell
.\.venv\Scripts\python.exe -m pip install "screen-ocr[winrt]"
```

Without `screen-ocr`, Checkpoint falls back to Tesseract or mock OCR where
configured.

## Keyboard Actions

Global key actions do not need a target:

```yaml
- id: submit
  action: press_key
  keys: enter
```

Under `dry-run`, keyboard and mouse actions do not touch the desktop.

