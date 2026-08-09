# Pacer

Pacer is a thin launcher for coding agents (Claude Code, Codex, Gemini), plus
the delivery checks those agents skip when they hand work back.

It used to be a visual-automation runtime with a workflow DSL, a desktop GUI, a
web dashboard, an MCP server and a hosting layer. All of that has been removed:
the agents got strong enough that the harness around them had to get thin.

## Install

On Windows from this repo (`xiao/`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_pacer.ps1
pacer --version
```

The core install has two dependencies. Nothing is pulled in for browsers,
screenshots, or a server.

## Launching an agent

```console
pacer claude          # Claude Code, native window; /model and friends work
pacer yolo            # Claude Code with --permission-mode bypassPermissions
pacer codex           # hands the terminal to Codex, argv unchanged
pacer gemini          # Gemini CLI
pacer chat            # Pacer-owned REPL driving Claude headless
```

Anything Pacer does not recognise is forwarded to Codex verbatim.

## Saving subscription quota

Interactive Claude launches get `--autocompact 100000` injected, which caps the
context the engine carries per request. This is in-session and non-destructive:
no process is killed, no `/model` choice is lost, and the engine writes its own
summary.

```console
set PACER_AUTOCOMPACT=off        # disable the injection
set PACER_AUTOCOMPACT=200000     # or pick your own cap
set PACER_CLAUDE_ROTATE=1        # opt in to kill-and-restart rotation
```

Rotation is the aggressive backstop: at a token threshold it kills the session
and restarts it with a distilled handoff. It is off by default because losing
live session state usually costs more than the context it saves.

The same cap can be set once, engine-wide, without Pacer at all — put
`"autoCompactWindow": 100000` in `~/.claude/settings.json`.

## What is left inside

Beyond the launcher, the modules that survived are the evidence side: base-diff
acceptance discrimination, delivery checks, diff review, evidence packs,
release gates. None of them has a CLI entry point yet — they were only ever
reachable through the dispatch layer that was removed. Wiring them up behind a
`pacer verify` command is the next piece of work.

## Tests

```console
python -m pytest tests -q
```
