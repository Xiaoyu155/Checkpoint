# Ship Now

Use this page when you want the lowest-friction path from local release candidate to public release.

## Already Automated Locally

Run one command from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release_candidate.ps1
```

It runs the core Python tests, release smoke, public demo loop, Python package build, package metadata check, VS Code tests, and VS Code package build.

Expected local artifacts:

- `dist\visual_agent-0.1.1-py3-none-any.whl`
- `dist\visual_agent-0.1.1.tar.gz`
- `vscode-extension\visual-agent-0.1.1.vsix`

## Manual Steps That Cannot Be Skipped

These require account access:

1. GitHub: create release `v0.1.1-preview` and paste the GitHub release draft from `docs\release_announcement.md`.
2. GitHub: attach `vscode-extension\visual-agent-0.1.0.vsix`.
3. PyPI: upload `dist\*` with Twine after signing in.
4. VS Code Marketplace: publish the `.vsix` after signing in.

## If You Want To Skip Publishing

Skip PyPI and Marketplace for now. Publish only the GitHub repository plus the release draft, then ask people to run:

```powershell
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint
powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1 -Step all
powershell -ExecutionPolicy Bypass -File scripts\public_demo_case.ps1
```
