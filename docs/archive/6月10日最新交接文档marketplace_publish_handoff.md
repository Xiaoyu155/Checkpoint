# VS Code Marketplace Publish Handoff

This document captures the current state of the Checkpoint VS Code extension publish flow so the next agent window can continue without re-reading the whole repo.

## Current State

- Product brand is now **Checkpoint**.
- The VS Code extension is already built, tested, and packaged successfully.
- Public-facing extension metadata has been updated:
  - `displayName`: `Checkpoint`
  - description: Checkpoint-focused public copy
  - listing draft: `vscode-extension/MARKETPLACE_LISTING.md`
  - publish checklist: `vscode-extension/PUBLISH_CHECKLIST.md`
- Local verification already passed:
  - `npm test`
  - `npm run package`
- The generated VSIX exists locally:
  - `vscode-extension/visual-agent-0.1.0.vsix`

## What Is Still Pending

The only remaining blocker is Marketplace account setup and publish auth:

1. A Visual Studio Marketplace publisher account must exist.
2. An Azure DevOps personal access token must be created with Marketplace publish scope.
3. `vsce login <publisher>` must succeed.
4. `vsce publish` must be run from `vscode-extension/`.

Optional but not required for first publish:

- Domain verification for a certified publisher badge
- Listing screenshot polishing
- Publisher profile details such as support URL, LinkedIn, and company site

## Recommended Publisher Setup

Use `visual-agent` as the publisher id if possible, because the extension `package.json` already uses:

```json
"publisher": "visual-agent"
```

That avoids a metadata rename before first publish.

## Resume Steps

From a fresh terminal:

```powershell
cd "D:\longxia agent\vscode-extension"
npx vsce login visual-agent
```

Paste the Azure DevOps PAT when prompted.

If login succeeds:

```powershell
npx vsce publish
```

For a patch release:

```powershell
npx vsce publish patch
```

## Pre-Publish Checks Already Done

- `npm install`
- `npm test`
- `npm run package`
- Packaging includes:
  - `MARKETPLACE_LISTING.md`
  - `PUBLISH_CHECKLIST.md`
  - compiled extension output

## Clean Verification Target After Publish

After the extension is published, verify in a clean VS Code profile:

- `Checkpoint: Init Workspace`
- `x-agent: Verify Implementation`
- `Checkpoint: Show Last AI Verification`
- `Checkpoint: Show Latest Failure`
- `Checkpoint: Open Examples`

## Notes For The Next Window

- Do not rework the product docs unless publish fails.
- Do not rename the internal extension package unless there is a strong reason.
- Keep the public brand name as **Checkpoint**.
- The repository is already in a good state for release; this handoff is only about completing Marketplace authentication and publish.
