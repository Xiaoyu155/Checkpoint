# Demo Output

This page shows real Checkpoint CLI output from the local demo workspace. It is meant for developers and coding agents who want to see the signal shape before installing.

## Passing Workflow

Command:

```powershell
checkpoint workspace-run --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json --run-profile dry-run --format markdown
```

Output excerpt:

```text
# Run Report: local_html_form_workflow

- Run profile: `dry-run`
- Status: `success`
- Steps: 6/6 succeeded
- Dry-run actions: 3
- Failed step: none

### fill_username

- Action: `paste`
- Status: `dry_run`
- Provider: `dom`
- Target: `请输入用户名`
- Selector: level `medium`, confidence `0.8`, stability `stable`, fallback path `dom`
- Message: paste skipped by dry-run

### click_login

- Action: `click`
- Status: `dry_run`
- Provider: `dom`
- Target: `登录`
- Selector: level `high`, confidence `0.98`, stability `stable`, fallback path `dom`
- Message: click skipped by dry-run
```

What this proves:

- The workflow can observe the local HTML fixture.
- DOM selectors resolve with confidence and stability metadata.
- `dry-run` protects the machine from real input while still validating the contract.
- The report is structured enough for a coding agent to read without scraping raw logs.

## Failing Workflow

Command:

```powershell
checkpoint workspace-run --root .agent-workspace --workflow failing_regression_demo --run-profile dry-run --format markdown
```

Output excerpt:

```text
# Run Report: failing_regression_demo

- Run profile: `dry-run`
- Status: `failed`
- Steps: 1/2 succeeded
- Failed step: `assert_missing`

### assert_missing

- Action: `assert_text`
- Status: `failed`
- Message: Text not found in observation: 不存在的文本
- Failure expected: expected text: 不存在的文本
- Failure actual: provider=dom; source=fixtures\login_demo.html; elements=3; visible_text=username | password | 登录
- DOM excerpt:
  - `username` role=`input` selector=`#username`
  - `password` role=`input` selector=`#password`
  - `登录` role=`button` selector=`#login`
```

What this gives an agent:

- The exact failed step: `assert_missing`.
- The intended assertion: text should exist.
- The observed page state: only `username`, `password`, and `登录` were visible.
- The provider and source file used for evidence.
- A compact DOM excerpt for the next repair attempt.

## How An Agent Should Respond

If the workflow describes the intended product behavior, fix the product code or fixture so the expected text appears.

If the product behavior intentionally changed, update the workflow contract and run:

```powershell
checkpoint workflow-lint --file .agent-workspace/workflows/failing_regression_demo.yaml --format markdown
checkpoint workspace-run --root .agent-workspace --workflow failing_regression_demo --run-profile dry-run --format markdown
```

Do not jump to `approved` execution. Stay in `dry-run` unless a human explicitly allows real browser or desktop actions.
