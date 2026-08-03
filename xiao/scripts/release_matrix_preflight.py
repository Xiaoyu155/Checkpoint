#!/usr/bin/env python3
"""Fail-closed preflight for the Pacer release matrix.

This does NOT execute the 15 managed samples and never claims release-ready.
It only:

1. Locks and validates `.pacer/release.json` digest.
2. Lists the 15 managed_sample + deterministic + dogfood cases.
3. Proves `run_release_matrix` refuses missing runners / wrong digests.
4. Optionally runs a deterministic payload dry-assessment shape check.

Exit codes:
  0 = preflight ready (matrix still not executed)
  1 = preflight failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from visual_agent.release_gate import (
    assess_release_case,
    release_manifest_digest,
    run_release_matrix,
    validate_release_manifest,
)


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / ".pacer" / "release.json"
DEFAULT_DIGEST = "aaa50981eb0ed72d2b1402303b6010828f022aef3da198ee7563dbcb5c84802a"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--expected-digest", default=DEFAULT_DIGEST)
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)

    path = Path(args.manifest).expanduser().resolve()
    raw = path.read_text(encoding="utf-8-sig")
    manifest = json.loads(raw)
    digest = release_manifest_digest(manifest)
    validation = validate_release_manifest(manifest)
    expected = str(args.expected_digest or "").strip().lower()
    digest_ok = digest == expected and bool(expected)

    cases = [c for c in manifest.get("cases", []) if isinstance(c, dict)]
    managed = [c for c in cases if c.get("kind") == "managed_sample"]
    dogfood = [c for c in cases if c.get("kind") == "dogfood"]
    deterministic = [c for c in cases if c.get("kind") == "deterministic"]

    missing_runners = run_release_matrix(
        manifest,
        expected_manifest_digest=digest,
        runners={},
    )
    wrong_digest = run_release_matrix(
        manifest,
        expected_manifest_digest="0" * 64,
        runners={str(c.get("case_id")): (lambda: {"status": "passed"}) for c in cases},
    )

    # Shape check only: a synthetic deterministic pass payload is assessable.
    det_case = deterministic[0] if deterministic else {"case_id": "deterministic-core", "kind": "deterministic"}
    det_assessment = assess_release_case(
        det_case,
        {"status": "passed", "warnings": [], "retry_count": 0, "exit_code": 0},
    )

    reasons: list[str] = []
    if not digest_ok:
        reasons.append("release_manifest_digest_mismatch")
    if not validation.get("passed"):
        reasons.extend(list(validation.get("reason_codes") or []))
    if "release_case_runner_missing" not in list(missing_runners.get("reason_codes") or []):
        reasons.append("release_matrix_missing_runner_gate_inactive")
    if "release_manifest_digest_mismatch" not in list(wrong_digest.get("reason_codes") or []):
        reasons.append("release_matrix_digest_gate_inactive")
    if not det_assessment.get("clean"):
        reasons.append("deterministic_payload_shape_untrusted")
    if len(managed) != 15:
        reasons.append(f"managed_sample_count_expected_15_got_{len(managed)}")

    passed = not reasons
    report = {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "release_ready": False,
        "reason_codes": reasons,
        "manifest_path": str(path),
        "manifest_digest": digest,
        "expected_digest": expected,
        "digest_locked": digest_ok,
        "validation": validation,
        "case_counts": {
            "total": len(cases),
            "deterministic": len(deterministic),
            "managed_sample": len(managed),
            "dogfood": len(dogfood),
        },
        "managed_case_ids": [str(c.get("case_id")) for c in managed],
        "gates": {
            "missing_runners_blocked": "release_case_runner_missing"
            in list(missing_runners.get("reason_codes") or []),
            "wrong_digest_blocked": "release_manifest_digest_mismatch"
            in list(wrong_digest.get("reason_codes") or []),
            "deterministic_shape_clean": bool(det_assessment.get("clean")),
        },
        "next_steps": [
            "Do not claim release-ready from this preflight.",
            "Implement per-case runners and execute the 15 managed samples serially.",
            "Bind existing three dogfood OIDC runs only after managed samples pass.",
            "Use unique concurrency keys when re-triggering dogfood workflows.",
        ],
    }

    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("## Pacer Release Matrix Preflight")
        print()
        print(f"- Status: `{'passed' if passed else 'failed'}`")
        print("- Release ready: `false` (preflight only)")
        print(f"- Digest locked: `{digest_ok}`")
        print(f"- Digest: `{digest}`")
        print(
            f"- Cases: deterministic={len(deterministic)}, "
            f"managed_sample={len(managed)}, dogfood={len(dogfood)}"
        )
        print(
            f"- Gate missing-runners: `{report['gates']['missing_runners_blocked']}`"
        )
        print(f"- Gate wrong-digest: `{report['gates']['wrong_digest_blocked']}`")
        print(
            f"- Deterministic payload shape: `{report['gates']['deterministic_shape_clean']}`"
        )
        if reasons:
            print("- Reason codes:")
            for item in reasons:
                print(f"  - `{item}`")
        print("- Managed case order:")
        for case_id in report["managed_case_ids"]:
            print(f"  - `{case_id}`")
        print("- Next:")
        for step in report["next_steps"]:
            print(f"  - {step}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
