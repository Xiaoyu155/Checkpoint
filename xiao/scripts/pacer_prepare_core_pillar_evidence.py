"""Prepare runtime-owned evidence for the isolated core-pillar audit.

This helper uses the production model selector and launch-state APIs. It does
not write tracked files or claim a pillar result; the worker's runtime
telemetry and completion path still have to validate the evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from visual_agent.dynamic_model_selector import select_model_for_task, selection_to_dict
from visual_agent.pacer_launch_context import initialize_active_launch, update_active_launch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--model-pool", required=True)
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    repo = Path(args.repo).expanduser().resolve()
    launch_id = str(args.launch_id).strip()
    manifest = workspace / "pacer_native" / "launches" / f"{launch_id}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    if not manifest.exists():
        manifest.write_text("{}\n", encoding="utf-8")

    provider = str(os.environ.get("PACER_PROVIDER_ID") or "").strip()
    model = str(os.environ.get("PACER_PROVIDER_MODEL") or "").strip()
    if not provider or not model:
        raise SystemExit("PACER_PROVIDER_ID and PACER_PROVIDER_MODEL are required")
    pool = Path(args.model_pool).expanduser().resolve()
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": f"audit-{provider}-{model}",
                        "provider": provider,
                        "model": model,
                        "capability": 1.0,
                        "cost": 0.1,
                        "latency": 0.1,
                        "reliability": 1.0,
                        "modes": ["cheap", "standard", "strong"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    initialize_active_launch(
        workspace_root=workspace,
        manifest_path=manifest,
        launch={
            "launch_id": launch_id,
            "repo_root": str(repo),
            "rollout_ownership": {"scheme": "launch_marker_v1", "required": True},
        },
    )
    selection = select_model_for_task(
        objective="Pacer core pillar evidence audit",
        acceptance_criteria=["preserve the configured provider and model identity"],
        workspace_root=workspace,
        config_path=pool,
    )
    if selection.selected is None:
        raise SystemExit(f"model selector did not produce a candidate: {selection.reason}")
    update_active_launch(
        workspace,
        expected_launch_id=launch_id,
        routing_decision=selection_to_dict(selection),
    )
    print(
        json.dumps(
            {
                "launch_id": launch_id,
                "decision_id": selection.decision_id,
                "provider": provider,
                "model": model,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
