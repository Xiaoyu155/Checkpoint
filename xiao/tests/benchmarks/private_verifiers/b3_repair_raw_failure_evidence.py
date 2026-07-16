from __future__ import annotations

import sys
from pathlib import Path


def _project_root(target_root: Path) -> Path:
    for candidate in (target_root, target_root / "xiao"):
        if (candidate / "src" / "visual_agent" / "chief_dispatch.py").is_file():
            return candidate
    raise AssertionError(f"Could not locate the xiao project under {target_root}")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: b3_repair_raw_failure_evidence.py <target-root>", file=sys.stderr)
        return 2

    project = _project_root(Path(argv[0]).expanduser().resolve())
    sys.path.insert(0, str(project / "src"))

    from visual_agent import chief_dispatch
    from visual_agent.command_verification import command_repair_brief

    failed_command = "python -m pytest tests/test_example.py"
    end_marker = "ORIGINAL-RAW-UNICODE-END"
    raw_output = "discarded-prefix\n" + ("\u9519" * 20000) + "\n" + end_marker
    command_result = {
        "verdict": "fail",
        "command": failed_command,
        "exit_code": 1,
        "failure_kind": "command_failed",
        "output_tail": "LOSSY-SUMMARY-INDEX",
        "raw_output_tail": raw_output,
    }
    verification = {
        "verdict": "fail",
        "command_verification": command_result,
        "repair_brief": command_repair_brief(command_result),
    }

    prompt_builder = getattr(chief_dispatch, "_build_dispatch_repair_prompt", None)
    if callable(prompt_builder):
        prompt = prompt_builder(
            plan={"objective": "Preserve raw verification evidence", "acceptance_criteria": []},
            verification=verification,
            verification_command=failed_command,
            repair_round=1,
            resume=False,
            worker_record=None,
        )
    else:
        prompt = str(verification["repair_brief"].get("repair_prompt") or "")

    assert failed_command in prompt, "failed command is missing from the repair handoff"
    assert "LOSSY-SUMMARY-INDEX" in prompt, "compact failure summary is missing"
    assert end_marker in prompt, "original raw failure tail is missing from the repair handoff"

    evidence_builder = getattr(chief_dispatch, "_repair_evidence_text", None)
    evidence = evidence_builder(verification) if callable(evidence_builder) else prompt
    assert end_marker in evidence
    assert "\u9519" in evidence, "Unicode failure output was corrupted"
    assert len(evidence.encode("utf-8")) <= 32768, "raw evidence exceeds the 32768-byte transport cap"

    print("B3_PRIVATE_VERIFIER_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except AssertionError as exc:
        print(f"B3_PRIVATE_VERIFIER_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
