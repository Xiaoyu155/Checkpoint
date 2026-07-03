# Demo Output

This page shows real Checkpoint CLI output from the local demo workspace. It is meant for developers and coding agents who want to see the signal shape before installing.

## Passing Live Checkout Workflow

Command:

```powershell
checkpoint verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
```

Output excerpt:

```text
## Verification Report
Ran 1 workflows: 1 passed with real interaction, 0 inspection-only, 0 failed
Strict product acceptance (L3+ without blockers): 1/1

### Passed (real interaction)
checkout_verification [L4]
```

The run report contains the lower-level evidence:

```text
# Run Report: checkout_verification

- Run profile: `supervised`
- Status: `success`
- Steps: 10/10 succeeded
- Valid operation receipts: 1
- product_guard: `passed`
- visual_guard: `passed`

### place_order

- Action: `click`
- Status: `success`
- Provider: `dom`
- Target: `Place Order`
- Selector: level `medium`, confidence `0.8`, stability `stable`, fallback path `dom`
- Message: playwright clicked

### assert_order_confirmed

- Action: `assert_text`
- Status: `success`
- Message: text found: Order Confirmed
```

What this proves:

- The workflow opens the local checkout page in a real browser.
- Checkpoint performs a real click through Playwright.
- The post-action confirmation state is asserted.
- The report is structured enough for a coding agent to read without scraping raw logs.

## Failing Checkout Regression

Command:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\public_demo_case.ps1
```

Output excerpt:

```text
# Run Report: checkout_verification

- Run profile: `supervised`
- Status: `failed`
- Failed step: `assert_checkout_button`

### assert_checkout_button

- Action: `assert_text`
- Status: `failed`
- Message: Text not found in observation: Proceed to Checkout
- Failure expected: expected text: Proceed to Checkout
- Failure actual: provider=dom; visible_text=Next Step | Place Order - Standard Delivery
```

What this gives an agent:

- The exact failed step: `assert_checkout_button`.
- The intended product copy: `Proceed to Checkout`.
- The observed changed UI copy: `Next Step`.
- The workflow context needed to repair the product or intentionally update the contract.

## How An Agent Should Respond

If the workflow describes the intended product behavior, fix the product code or fixture so the expected text appears.

If the product behavior intentionally changed, update the workflow contract and run:

```powershell
checkpoint workflow-lint --file .agent-workspace/workflows/checkout_verification.yaml --format markdown
checkpoint verify-now --workspace-root .agent-workspace --workflow checkout_verification --live --format markdown
```

Do not jump to `approved` execution for external sites. The public checkout demo uses `supervised` local browser interaction against a repository fixture.
