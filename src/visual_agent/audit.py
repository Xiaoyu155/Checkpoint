from __future__ import annotations

import json
from pathlib import Path
from time import strftime
from uuid import uuid4

from .models import RunResult, to_jsonable


class RunAudit:
    def __init__(self, root_dir: str | Path = ".runs") -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def create_run_dir(self) -> tuple[str, Path]:
        run_id = f"{strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
        run_dir = self.root_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_id, run_dir

    def write_result(self, result: RunResult) -> Path:
        path = result.run_dir / "result.json"
        path.write_text(
            json.dumps(to_jsonable(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

