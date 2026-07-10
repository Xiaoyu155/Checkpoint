# Codex CLI Capability Probe (2026-07)

## Scope and status

- Probe date: 2026-07-10 (Asia/Shanghai)
- Repository: `xiao`
- Product files changed by probe: none
- Installed CLI: `codex-cli 0.144.0`
- Account path exercised: existing ChatGPT/Codex subscription login
- Direct-Codex benchmark baseline: **0/5 accepted**; B1 has 6 preserved attempts

This report records installed-CLI behavior. It does not infer flags from an
older capability profile. The official Codex manual helper was also attempted,
but its response lacked the required `x-content-sha256` header, so it could not
be used as verified evidence. Current-session behavior is authoritative for the
machine covered by this report.

## Capability matrix

| Capability | Result | Evidence level | Important boundary |
|---|---|---|---|
| CLI version | Supported: `0.144.0` | Live local command | Re-probe after CLI upgrades |
| Headless `exec resume <UUID>` | Supported | Live model turn | Preserve execution environment options on resume |
| Headless `exec resume --last` | Present | Installed help | Syntax/cwd semantics checked; no live `--last` turn was run |
| Resume prompt via stdin `-` | Supported | Live model turn | Put `-` after the session id |
| JSON output | Supported JSONL | Live model turn | Session identifier is named `thread_id`, not `session_id` |
| Token usage | Supported | Live model turn | Found at `turn.completed.usage` |
| Reasoning override | `high` accepted | Live model turn | Other values are model-specific |
| `gpt-5.6-sol` effort catalog | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` | Bundled 0.144.0 model catalog | Do not treat this as a global enum for every model |
| Explicit model selection | `-m gpt-5.6-sol` accepted | Live subscription turn | Does not prove arbitrary model access |
| Proactive delegation | Enabled by `ultra` policy | Model-visible prompt probe | No real delegated benchmark task was run |
| JSON model/effort/sandbox fields | Not present | Live JSONL observation | Resolve and record these values outside the event stream |

## Exact probes

### Version and command surface

```powershell
codex --version
```

```text
codex-cli 0.144.0
```

```powershell
codex exec --help
```

Relevant installed output:

```text
Commands:
  resume  Resume a previous session by id or pick the most recent with --last

Options:
      --json
          Print events to stdout as JSONL
```

```powershell
codex exec resume --help
```

Relevant installed output:

```text
Usage: codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]

Arguments:
  [SESSION_ID]
          Conversation/session id (UUID) or thread name. UUIDs take precedence if it parses.
          If omitted, use --last to pick the most recent recorded session
  [PROMPT]
          Prompt to send after resuming the session. If - is used, read from stdin

Options:
      --last
          Resume the most recent recorded session (newest) without specifying an id
      --all
          Show all sessions (disables cwd filtering)
      --json
          Print events to stdout as JSONL
```

### JSONL, thread id, usage, and reasoning override

```powershell
codex -a never -s read-only -C "$env:TEMP" exec `
  --skip-git-repo-check --ignore-rules --json `
  -c model_reasoning_effort=high `
  "Do not use tools. Reply with exactly: PACER_PROBE_OK"
```

Exit code: `0`.

```jsonl
{"type":"thread.started","thread_id":"019f4a34-2fc9-7622-8e24-21c7200fcf8d"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PACER_PROBE_OK"}}
{"type":"turn.completed","usage":{"input_tokens":13912,"cached_input_tokens":11008,"output_tokens":9,"reasoning_output_tokens":0}}
```

The positional-prompt invocation also produced `Reading additional input from
stdin...` in the shell wrapper's combined capture. The production parser should
parse JSONL from stdout line by line and retain stderr separately rather than
assuming every combined line is JSON.

### UUID resume

The first attempt from a non-Git temporary directory intentionally omitted the
repository bypass option:

```powershell
codex -a never -s read-only -C "$env:TEMP" exec resume --json `
  019f4a34-2fc9-7622-8e24-21c7200fcf8d `
  "Do not use tools. Reply with exactly: PACER_RESUME_OK"
```

```text
Not inside a trusted directory and --skip-git-repo-check was not specified.
```

This failed locally before a model request. Repeating with the environment
option succeeded:

```powershell
codex -a never -s read-only -C "$env:TEMP" exec resume `
  --skip-git-repo-check --json --ignore-rules `
  -c model_reasoning_effort=high `
  019f4a34-2fc9-7622-8e24-21c7200fcf8d `
  "Do not use tools. Reply with exactly: PACER_RESUME_OK"
```

```jsonl
{"type":"thread.started","thread_id":"019f4a34-2fc9-7622-8e24-21c7200fcf8d"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PACER_RESUME_OK"}}
{"type":"turn.completed","usage":{"input_tokens":27853,"cached_input_tokens":24064,"output_tokens":18,"reasoning_output_tokens":0}}
```

The identical `thread_id` establishes that the headless turn resumed the same
session rather than starting a new one.

### Resume stdin, JSON option ordering, and explicit model

```powershell
'Do not use tools. Reply with exactly: PACER_STDIN_MODEL_OK' |
  codex -a never -s read-only -C "$env:TEMP" exec resume `
    --skip-git-repo-check --json --ignore-rules `
    -c model_reasoning_effort=high `
    -m gpt-5.6-sol `
    019f4a34-2fc9-7622-8e24-21c7200fcf8d -
```

Exit code: `0`.

```jsonl
{"type":"thread.started","thread_id":"019f4a34-2fc9-7622-8e24-21c7200fcf8d"}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"PACER_STDIN_MODEL_OK"}}
{"type":"turn.completed","usage":{"input_tokens":41825,"cached_input_tokens":37120,"output_tokens":28,"reasoning_output_tokens":0}}
```

The canonical argv layout established by help and the live invocation is:

```text
codex <root-options> exec resume <resume-options> <SESSION_ID> -
```

Root execution options such as approval, sandbox, and working directory belong
before `exec`. Resume options such as `--json`, `-c`, and `-m` should precede
the session id. The stdin marker is the final prompt argument.

### Model catalog and delegation policy

The relevant user configuration during the probe was:

```toml
model = "gpt-5.6-sol"
model_reasoning_effort = "ultra"
```

The catalog was queried without a model generation request:

```powershell
$model = (codex debug models --bundled | ConvertFrom-Json).models |
  Where-Object { $_.slug -eq 'gpt-5.6-sol' }
[pscustomobject]@{
  slug = $model.slug
  default_reasoning_level = $model.default_reasoning_level
  supported_reasoning_levels = @($model.supported_reasoning_levels | ForEach-Object { $_.effort })
  tool_mode = $model.tool_mode
  multi_agent_version = $model.multi_agent_version
} | ConvertTo-Json -Depth 4
```

```json
{
  "slug": "gpt-5.6-sol",
  "default_reasoning_level": "low",
  "supported_reasoning_levels": ["low", "medium", "high", "xhigh", "max", "ultra"],
  "tool_mode": "code_mode_only",
  "multi_agent_version": "v2"
}
```

```powershell
codex features list
```

Relevant output:

```text
enable_fanout    under development  false
multi_agent      stable             true
multi_agent_v2   under development  false
```

`codex debug prompt-input -c model_reasoning_effort=ultra "Pacer delegation
probe"` included:

```text
<multi_agent_mode>Proactive multi-agent delegation is active. ...
Use sub-agents when parallel work would materially improve speed or quality.
</multi_agent_mode>
```

The same prompt-input probe with `model_reasoning_effort=high` included:

```text
<multi_agent_mode>Do not spawn sub-agents unless the user ... explicitly ask[s] ...
</multi_agent_mode>
```

Therefore no separate `enable_fanout` switch is required for the verified
`gpt-5.6-sol` ultra policy. This proves the model-visible policy change, not the
quality or frequency of delegation on real development tasks; P4 must measure
that separately.

## Configuration caveats

`--strict-config` is not a safe default for this machine. A strict exec probe
failed on the user's existing configuration before reaching the requested
override:

```text
unknown configuration field `disable_response_storage`
```

With `--ignore-user-config`, an unknown override key was rejected, while an
arbitrary string value for the known `model_reasoning_effort` key reached prompt
validation. The CLI parser alone is therefore insufficient enum validation.
Use the model catalog for user-facing choices and keep runtime handling tolerant
of catalog changes.

## Implementation consequences

1. Capture `thread.started.thread_id` from stdout and persist it as the resume id.
2. Capture each `turn.completed.usage` object for the ledger.
3. Record model, effort, sandbox, and approval from resolved config/argv because
   the JSONL events do not contain them.
4. Feed prompts through stdin and keep resume options before the session id.
5. If resume fails because the session is missing, expired, or locally rejected,
   fall back to a fresh process with the original failure evidence.
6. Default model and reasoning policy to `inherit`; an explicit override is an
   auditable user choice, not a required compatibility setting.

## Benchmark status

Five task definitions now live under `tests/benchmarks/execution_tasks/`. The
current frozen direct context is
`.runs/execution-benchmark/context-20260710-06/context.json`
(`execution-20260710-e858309-06`).

B1 has six append-only direct-Codex attempts under
`.runs/execution-benchmark/direct-codex-20260710-01/runs`. The latest attempt,
`direct-B1-20260710-06`, replayed the custom provider without user config,
trusted only the synthetic target root, produced the correct one-line test fix,
and passed the operator gate. It still is not an accepted benchmark PASS because
the Codex invocation timed out at 600 seconds and did not emit
`turn.completed` usage. No tracked-Pacer or delegated-Pacer baseline has been
run yet.
