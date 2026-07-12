# Pacer / Checkpoint

Pacer is a local-first mission orchestration workbench for AI coding agents.
Checkpoint is its workflow verification engine, with browser, API, desktop, and
audit-oriented execution paths.

## Install

```console
python -m pip install visual-agent
```

Install optional capabilities only where they are needed:

```console
python -m pip install "visual-agent[web]"
python -m pip install "visual-agent[mcp]"
python -m pip install "visual-agent[cloud]"
python -m pip install "visual-agent[desktop]"
```

The Cloud extra includes the FastAPI service, Celery worker, and Redis
transport. Browser binaries are installed separately with
`python -m playwright install chromium` after installing the `web` extra.

## Quick Start

```console
checkpoint init-workspace --root .agent-workspace
checkpoint doctor
checkpoint dashboard --workspace-root .agent-workspace
```

The project is in alpha. Use `dry-run` for inspection, and explicitly select a
supervised or approved run profile before allowing workflows to mutate external
systems.

Documentation and source are available in the
[Checkpoint repository](https://github.com/Xiaoyu155/Checkpoint/tree/main/xiao).
