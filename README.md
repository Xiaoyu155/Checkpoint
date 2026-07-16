# Pacer / Checkpoint Workspace

The product source tree lives in `xiao/`.

Use `xiao/` as the project root for development commands:

```powershell
cd xiao
pip install -e .
python -m pytest -q
```

Local runtime data, archived notes, exported source snapshots, and worker
worktrees are intentionally ignored from this repository root.
