# Public Demo Case

This is the public demo to show Checkpoint's core loop without requiring a real account or external service.

## Goal

Show a developer this sequence in under five minutes:

1. A product workflow passes with real browser interaction.
2. A UI copy regression is introduced.
3. Checkpoint reports the exact failed product assertion.
4. The regression is restored and the workflow returns to green.

## Run It

From the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\public_demo_case.ps1
```

The script uses:

- Page: `examples/web/checkout_verification_demo.html`
- Workflow: `.agent-workspace/workflows/checkout_verification.yaml`
- Expected copy: `Proceed to Checkout`
- Temporary regression: `Next Step`

It restores the page in a `finally` block, even when verification fails as expected.

## Manual Version

Run the green path:

```powershell
.\.venv\Scripts\checkpoint.exe verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
```

Edit `examples/web/checkout_verification_demo.html` and change:

```html
Proceed to Checkout
```

to:

```html
Next Step
```

Run the workflow again. Expected: Checkpoint fails at `assert_checkout_button`.

Restore the original copy and rerun the workflow. Expected: green again.

## What To Show In A GIF Or Video

Capture these four moments:

- Terminal shows the first passing `verify-now` run.
- Editor shows the one-line copy regression.
- Terminal shows the failed `assert_checkout_button` step.
- Terminal shows the final passing run after restoration.

Keep the recording focused on the terminal and one HTML line. Avoid showing unrelated files, tokens, or local machine details.

## Expected Failure Signal

```text
Failed step: assert_checkout_button
Action: assert_text
Expected: Proceed to Checkout
Actual: Text not found in observation: Proceed to Checkout
```

This is the product promise: the agent does not have to scrape a raw terminal log. It receives a specific workflow step, expected product copy, actual observation, and repair direction.
