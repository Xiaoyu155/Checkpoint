# Comparison

Checkpoint is not trying to replace browser automation or test frameworks. It sits above them so AI coding agents can run product checks, respect local permissions, and understand failures.

## Short Version

| Need | Playwright tests | Browser MCP server | Checkpoint |
| --- | --- | --- | --- |
| Script browser actions | Yes | Yes | Yes |
| Version product workflows as YAML | No | No | Yes |
| Run checks through MCP clients | Indirect | Yes | Yes |
| Permission profiles for agent actions | Test-code dependent | Limited | Yes |
| Local reports, screenshots, and audit trails | Custom setup | Partial | Yes |
| AI-readable failure summaries | Custom setup | Partial | Yes |
| Desktop UIA / OCR fallback | No | No | Yes |
| Queue and rerun workflow state | Custom setup | No | Yes |
| Export to regression tests | N/A | No | Yes |

## Versus Playwright

Playwright is the browser engine. Checkpoint uses Playwright for browser observations and live actions, then adds:

- Workflow contracts that agents can discover and rerun.
- Safe execution profiles: `dry-run`, `supervised`, and `approved`.
- Structured reports with failed step, expected value, actual observation, screenshots, and repair hints.
- MCP tools for coding agents.
- Local workspace state that survives chat/session resets.

Use Playwright directly when a developer is writing test code. Use Checkpoint when an AI agent needs a stable local acceptance contract after changing product code.

## Versus Browser MCP Servers

Browser MCP servers are useful for interactive browsing and one-off actions. Checkpoint focuses on repeatable verification:

- Workflows are committed, reviewed, and rerun.
- Reports and screenshots stay in the project workspace.
- Failures become compact context for the next repair attempt.
- Permission profiles prevent generated automation from silently performing real actions.

Use a browser MCP server for exploration. Use Checkpoint for product checks you expect to run again.

## Versus Unit And Integration Tests

Unit and integration tests remain necessary. Checkpoint covers the product-behavior layer:

- visible page copy
- user flows
- redirects
- forms
- empty/error/success states
- UI regressions after AI-generated changes

The right setup is not either/or. Keep unit tests for logic and fast invariants; add Checkpoint for end-user acceptance contracts that an AI coding assistant can run locally.

## When Not To Use Checkpoint

Do not use Checkpoint when:

- A simple unit test can prove the behavior faster and more deterministically.
- You need hosted cross-browser infrastructure as the primary product.
- You want an autonomous agent to bypass human approval.
- You cannot define the product expectation as a repeatable workflow.

Checkpoint is strongest when the expected behavior is stable enough to encode as a workflow and valuable enough to check after code changes.
