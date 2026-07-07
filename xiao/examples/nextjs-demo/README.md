# Next.js Demo

This is a minimal Next.js App Router application used as a Checkpoint SSR target.

## Run

```powershell
npm install
npm run dev
```

The app runs on `http://localhost:3000` by default.

## Known Issue

Next.js development mode can emit a React hydration mismatch warning in the browser console even when the demo UI is usable. Treat this as known framework noise for this demo unless a workflow assertion fails on user-visible content. Use `examples/demo-app` and `demo-workspace-check --run-profile supervised` as the primary real-click evidence path.
When this warning is detected in structured failure output, it is classified as `known_issue`.

## Workflow suite

- `workflows/home_smoke.yaml`
- `workflows/login_smoke.yaml`
- `workflows/login_regression.yaml`
- `workflows/list_smoke.yaml`
- `workflows/list_regression.yaml`
- `workflows/form_smoke.yaml`
- `workflows/form_regression.yaml`

