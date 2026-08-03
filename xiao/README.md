# Pacer / Checkpoint

Pacer is a local-first mission orchestration workbench for AI coding agents.
Checkpoint is its workflow verification engine, with browser, API, desktop, and
audit-oriented execution paths.

## Install

```console
python -m pip install visual-agent
```

On Windows from this repo (`xiao/`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_pacer.ps1
pacer --version
```

The core install includes the MCP runtime required by `pacer`.
`pacer --version` / `pacer --help` stay on Pacer (they no longer fall through to Codex help).
Bare `pacer` still opens the managed Codex session. Use `checkpoint mission start ...` for one-shot tasks.

Install optional capabilities only where they are needed:

```console
python -m pip install "visual-agent[web]"
python -m pip install "visual-agent[cloud]"
python -m pip install "visual-agent[desktop]"
python -m pip install "visual-agent[eval]"
python -m pip install "visual-agent[otel]"
```

The Cloud extra includes the FastAPI service, Celery worker, and Redis
transport. Browser binaries are installed separately with
`python -m playwright install chromium` after installing the `web` extra.

## Commercial API Gateway

The Cloud API now includes a single-node OpenAI-compatible paid gateway with
tenant keys, balance reservations, token billing, upstream failover, request
ledgers, a WeChat Pay Native credit checkout, and an operator workbench. Initialize it with
`python -m cloud_api.setup_gateway`, then use
`docker compose -f docker-compose.gateway.yml up -d --build` and open
`http://127.0.0.1:8000/gateway`. Customers use `/billing` with their
`pacer_sk_*` key after WeChat server credentials and credit packages are configured.

See [Pacer 商业中转站](docs/中转站_商业网关.md) for the supported billing loop,
security model, Native callback setup, and remaining commercial boundaries.

## Quick Start

```console
pacer
Pacer> Fix the login error and run the relevant tests
Pacer> /provider subscription
Pacer> /provider relay custom
Pacer> Continue with boundary tests
```

Pacer initializes its local workspace, runs Codex in a verified delegated
mission, and merges only after acceptance passes and the target branch is clean.
Execution remains inside Codex CLI: use the logged-in subscription or select a
relay provider already configured in Codex. Pacer does not store relay keys or
silently fall back to another model backend.
Use `checkpoint dashboard --workspace-root .agent-workspace` when you want the
full evidence and queue view.

Pacer repositories can check in `.pacer/acceptance.json` as the versioned
product standard. Release automation should lock `.pacer/release.json` to an
externally supplied digest before running any matrix case:

```console
pacer pacer-release-manifest-check --manifest .pacer/release.json --expected-digest <sha256>
pacer pacer-dogfood-policy-check --repo-root .
pacer pacer-dogfood-check --repo-root .
```

The Dogfood check only passes when the canonical evidence file references real,
digest-matching A/B wheels, contracts, verification receipts, and a fresh-install
self-check receipt. The default 95-point gate also requires GitHub Artifact
Attestations for the candidate wheel and canonical evidence; HMAC-only local
evidence is capped at 85. A normal successful repository task remains partial
Dogfood. See `docs/Pacer_Dogfood_专项重构_2026-07-15.md`.

OpenTelemetry projection is optional and disabled by default. Configure a standard
OpenTelemetry SDK/provider, then set `PACER_OTEL_ENABLED=1` to project durable Pacer
events as bounded spans. SDK and exporter failures never change local evidence or
task results; prompts, secrets, and absolute paths are not exported.

The project is in alpha. Use `dry-run` for inspection, and explicitly select a
supervised or approved run profile before allowing workflows to mutate external
systems.

Documentation and source are available in the
[Checkpoint repository](https://github.com/Xiaoyu155/Checkpoint/tree/main/xiao).
