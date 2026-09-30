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
pacer task            # one real native Claude window per task, model auto-picked
```

Anything Pacer does not recognise is forwarded to Codex verbatim.

## One native window per task (`pacer task`)

`pacer task` was built after checking exactly what Claude Code exposes for
external orchestration (2026-08-13): no hook or flag lets a wrapper pick the
model per turn, or restart the session, while staying inside one native
interactive TUI process. So it doesn't try. Every window it opens **is** the
real `claude` binary — genuine stdio inheritance, native rendering, real
permission prompts, `/model` still works inside it. The automation happens
*between* windows, not inside them:

1. Pacer asks for your next task at its own prompt (plain terminal, not
   Claude). One window per task means reconfirming permissions every time
   would defeat the point, so it launches with `--permission-mode
   bypassPermissions` by default; pass your own `--permission-mode` to get
   normal confirmations back.
2. It classifies the task with the same rule-based router used elsewhere
   (cheap / standard / strong keyword signals) and picks a model — `sonnet`
   for light and normal work, `opus` for heavy work (refactors, concurrency,
   payments/auth, or a retry after a prior failure).
3. It launches `claude --model <picked> "<your task>"` — a full native
   session, unmodified.
4. When you finish and exit that window (Ctrl+D or `/exit`), Pacer reads
   Claude's own on-disk session transcript (`~/.claude/projects/...`, the
   same file Claude writes regardless of how it was launched) and distills a
   handoff — goal, touched files, last reply — with zero extra model calls.
5. It asks for the next task, carries that handoff into the new window's
   opening message, and repeats.

Take over the model for one window with `/model fable` (or any model name)
at Pacer's prompt; `/model auto` restores routing. `/exit` quits the loop.

```console
set PACER_TASK_MODEL_MAP=strong=fable,cheap=haiku   # override the tier->model map
```

The trade-offs, stated plainly: "task done" is signalled by you exiting the
window, not detected automatically — Claude Code has no "turn finished" event
external to the session. And the model is picked once, from the task's first
message, not switched mid-task by content (use native `/model` inside the
window for that, same as always).

## Saving subscription quota

Billing follows the context carried on each request, so the quota lever is
keeping that context small — without paying for it in waiting time. Engine-side
compaction (`--autocompact` / `autoCompactWindow`) turned out to cost
minutes-long blocking progress bars per compaction, so it is no longer the
default anywhere. `pacer task` sidesteps this by construction: each window is
a fresh session, so context never accumulates past one task.

```console
set PACER_CLAUDE_ROTATE_CONTEXT=200000   # move the rotation threshold (pacer claude only)
set PACER_AUTOCOMPACT=100000             # opt back in to engine compaction
set PACER_CLAUDE_ROTATE=1                # opt in to kill-and-restart rotation
```

Kill-and-restart rotation for `pacer claude` remains the aggressive backstop
and stays off by default: losing live session state usually costs more than
the context it saves.

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
