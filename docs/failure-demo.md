# Failure Demo

This demo shows Checkpoint's core loop: run a product contract, break the UI, get an actionable failure, fix it, and verify again.

For the scripted version, run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\public_demo_case.ps1
```

The script performs the same green -> red -> green flow and restores the demo page automatically.

## 1. Bootstrap

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
```

The bootstrap path installs dependencies, initializes `.agent-workspace`, runs `doctor`, and executes the local demo smoke check.

## 2. Run The Passing Contract

```powershell
checkpoint verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
```

Expected result: the checkout verification workflow passes.

## 3. Break The UI Copy

Open:

```text
examples/web/checkout_verification_demo.html
```

Change the checkout button text from:

```html
Proceed to Checkout
```

to:

```html
Next Step
```

## 4. Run The Contract Again

```powershell
checkpoint verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
```

Checkpoint should report a failing assertion. The important part is not only that it fails, but that it returns evidence a coding agent can use:

```text
Failed step: assert_checkout_button
Action: assert_text
Expected: Proceed to Checkout
Actual: Text not found in observation: Proceed to Checkout
```

## 5. Read Compact Context

```powershell
checkpoint context-snapshot --workspace-root .agent-workspace --format markdown
```

The snapshot is designed for coding agents. It keeps the failure short enough to paste into a new chat while preserving the next useful repair signal.

## 6. Fix And Re-Verify

Restore the button text to:

```html
Proceed to Checkout
```

Run the workflow again:

```powershell
checkpoint verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
```

Expected result: the workflow returns to green.

## Why This Matters

This is the Checkpoint contract:

- Product expectations live in versioned workflows.
- Agents can run those workflows after code changes.
- Failures include structured evidence instead of vague terminal output.
- Humans decide whether to fix product code or intentionally update the workflow.
