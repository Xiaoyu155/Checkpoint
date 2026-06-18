# First-User Feedback Plan

Do not optimize for stars first. Optimize for successful first runs.

## Target Users

Recruit 10 to 20 developers who already use one of:

- Codex
- Claude Code
- Cursor
- VS Code with AI coding extensions
- Playwright or browser automation

Prefer users with active UI work. Checkpoint is easiest to understand when they have recently seen an AI agent break a visible workflow.

## If You Cannot Recruit 10 Users Yet

Do not wait for the list to fill up. Run an asynchronous validation loop that creates proof external users can inspect later:

- Re-run the public demo from a clean checkout and save the terminal output.
- Record a 60 to 90 second video or GIF of the green -> red -> green demo.
- Open a GitHub Discussion or pinned issue titled `Try the 60-second Checkpoint demo` with the exact commands and expected output. Point users to the `60-second demo feedback` issue template.
- Post the demo link in small, relevant places where coding-agent users already are: GitHub Discussions, Cursor or Claude Code communities, Playwright channels, and AI coding tool groups.
- Ask for one narrow reply: did setup pass, and did the failing assertion make sense?
- Treat every public comment, issue, failed install log, or clone-and-run attempt as one feedback sample.

Use this as the temporary target:

- 5 clean self-run installs on different machines, shells, or fresh virtual environments.
- 3 external asynchronous replies, even if they are from people who did not complete the run.
- 1 public demo artifact that proves the product catches a real UI regression.

When the asynchronous loop starts producing replies, move those people into the 10-user trial below.

## The Trial Task

Ask every user to run the same task:

```text
Clone Checkpoint, run the 60-second start, run the public demo case, and explain what failed when the checkout button copy changed.
```

Commands:

```powershell
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
powershell -ExecutionPolicy Bypass -File scripts\public_demo_case.ps1
```

## What To Measure

Record these fields for every user:

| Field | Good signal | Bad signal |
| --- | --- | --- |
| Bootstrap finished | No manual environment edits | Python, pip, Playwright, or path blocker |
| First live verification | Passes on first run | Browser binary or workflow failure |
| Failure understood | User can name `assert_checkout_button` | User only sees generic failure |
| Product value understood | User says it verifies AI changes | User thinks it is just browser control |
| Next action clear | User knows how to add a workflow | User asks what to do after demo |

## Questions To Ask

Use the same questions each time:

1. What did you think Checkpoint did before running it?
2. What did you think it did after the public demo?
3. Where did setup slow down or feel risky?
4. Did the failure output tell you what to fix?
5. Would you use this after an AI coding agent changes UI code?
6. What would stop you from adding it to a real project?

## Success Criteria

Before broader promotion, target:

- 8 of 10 users finish bootstrap without manual environment edits.
- 8 of 10 users get the public demo to green -> red -> green.
- 7 of 10 users can explain how Checkpoint differs from Playwright alone.
- At least 3 users try it on their own project after the demo.

## Follow-Up Work

Turn repeated failures into product tasks:

- Setup failures become bootstrap or doctor fixes.
- Confusing output becomes report wording fixes.
- Missing workflow examples become templates.
- Repeated comparison questions become README or docs changes.

Do not treat low star count as the primary signal until the first-run success rate is healthy.
