from __future__ import annotations

from difflib import unified_diff
from pathlib import Path
from typing import Any


def workflow_text_diff(path: Path, proposed_text: str, *, relative_path: str) -> str:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    before = current.splitlines()
    after = proposed_text.splitlines()
    return "\n".join(
        unified_diff(
            before,
            after,
            fromfile=f"a/{relative_path}" if path.exists() else "/dev/null",
            tofile=f"b/{relative_path}",
            lineterm="",
        )
    )


def workflow_save_diff(path: Path, proposed_text: str, *, relative_path: str) -> dict[str, Any]:
    return {
        "path": relative_path,
        "target_exists": path.exists(),
        "diff": workflow_text_diff(path, proposed_text, relative_path=relative_path),
    }


def workflow_diff_to_markdown(diff: str, *, title: str = "Save Diff") -> str:
    text = str(diff or "").strip()
    if not text:
        text = "# no changes"
    return "\n".join([f"## {title}", "", "```diff", text, "```", ""])
