# CI/CD

Checkpoint uses three layers for CI coverage:

1. GitHub Actions for pull request and push validation.
2. Repository-local Checkpoint config in `.github/visual-agent.yml`.
3. A pre-push hook under `.githooks/pre-push`.

## Files

- `.github/workflows/ci.yml`: main CI workflow.
- `.github/visual-agent.yml`: repository defaults for verification tags, quality gate profile, and JUnit output.
- `.githooks/pre-push`: fast local gate before push.

## Local Setup

```powershell
git config core.hooksPath .githooks
```

## Fast Gate

The pre-push hook runs:

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --tags fast --max-workflows 5 --run-profile dry-run --wait-lock --format json
python -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run --fail-on-risk-policy-error --fail-on-secret-leak --ci --junit-output .runs/quality_gates/junit.xml
```

## CI Output

`quality-gate --ci` emits JUnit XML. In GitHub Actions, that XML can be uploaded as an artifact or consumed by a test summary step.

## PR Feedback

On pull request runs, the CI workflow uploads `visual-agent-quality-reports` and then posts a PR comment with:

- The failed workflow run summary.
- The quality gate summary.
- The uploaded artifact URL.
- Screenshot paths from the failed run report.

The workflow needs `pull-requests: write` permission for `GITHUB_TOKEN`.

