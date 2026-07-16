from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

from visual_agent.dashboard import _bind_dashboard_server


def _write_launch(workspace: Path, launch_id: str = "20260713-123242-test") -> None:
    launch_dir = workspace / "pacer_native" / "launches"
    launch_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "launch_id": launch_id,
        "status": "completed",
        "started_at": "2026-07-13T12:32:42+00:00",
        "completed_at": "2026-07-13T12:36:28+00:00",
        "elapsed_seconds": 226.0,
        "repo_root": str(workspace.parent / "private-project"),
        "launch_goal": "never expose prompt SECRET_TOKEN=raw-secret",
        "rollout_telemetry": {
            "status": "captured",
            "attribution_confidence": "high",
            "source_files": 1,
            "usage": {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 20,
                "reasoning_output_tokens": 12,
                "total_tokens": 120,
            },
            "runtime": {
                "provider": "custom",
                "model": "gpt-test",
                "reasoning_effort": "high",
            },
            "agents": {"total": 0},
            "compactions": {"count": 0},
        },
        "pillars": {
            "routing": {
                "active": True,
                "state": "observed",
                "runtime": {"provider": "custom", "model": "gpt-test"},
                "ownership_matched": True,
                "attribution_confidence": "high",
                "mimo_used": False,
                "decision_id": "decision-test",
                "policy_match": True,
                "request_evidence": {
                    "decision_id": "decision-test",
                    "policy_match": True,
                    "decision": {"provider": "custom", "model": "gpt-test"},
                    "request": {"provider": "custom", "model": "gpt-test"},
                },
            },
            "memory": {
                "active": True,
                "state": "loaded_empty",
                "retrieval_succeeded": True,
                "effective_hit": False,
            },
            "managed": {
                "active": False,
                "state": "not_completed",
                "outcome_recorded": True,
            },
            "acceptance": {"active": False, "state": "not_verified"},
            "dogfood": {
                "active": True,
                "state": "verified_source_discipline",
                "verified_batch": True,
                "task_review_valid": True,
                "pacer_on_pacer": True,
                "self_change_attributed": True,
                "installed_artifact_verified": True,
                "artifact_files_verified": True,
                "evidence_digest": "a" * 64,
                "quality_target_met": True,
            },
        },
    }
    (launch_dir / f"{launch_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _serve(workspace: Path):
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_address[1]}"


def _json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def test_observability_launch_list_exposes_deduplicated_usage_without_prompt_or_path(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    _write_launch(workspace)
    server, thread, base = _serve(workspace)
    try:
        payload = _json(base + "/api/observability/launches")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert payload["ok"] is True
    launch = payload["launches"][0]
    assert launch["usage"]["deduplicated_actual"]["total_tokens"] == 120
    assert launch["usage"]["uncached_input_tokens"] == 20
    assert launch["usage"]["cache_ratio"] == 0.8
    assert launch["usage"]["reasoning_included_in_output"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SECRET_TOKEN" not in serialized
    assert "raw-secret" not in serialized
    assert str(tmp_path) not in serialized


def test_observability_launch_detail_exposes_five_pillar_evidence(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    launch_id = "20260713-123242-test"
    _write_launch(workspace, launch_id)
    server, thread, base = _serve(workspace)
    try:
        payload = _json(base + f"/api/observability/launches/{launch_id}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert payload["launch"]["launch_id"] == launch_id
    assert payload["launch"]["detail_status"] == "baseline_unavailable"
    assert {item["kind"] for item in payload["evidence"]} == {
        "routing",
        "memory",
        "managed",
        "acceptance",
        "dogfood",
    }
    statuses = {item["kind"]: item["status"] for item in payload["evidence"]}
    assert statuses == {
        "routing": "passed",
        "memory": "partial",
        "managed": "failed",
        "acceptance": "indeterminate",
        "dogfood": "passed",
    }
    assert payload["launch"]["five_pillars_assessment"]["passed"] is False
    memory = next(item for item in payload["evidence"] if item["kind"] == "memory")
    assert memory["assessment"]["status"] == "partial"
    assert memory["adequacy"] == "insufficient"
    assert memory["reason_codes"] == ["memory_lookup_miss"]


def test_five_pillars_assets_expose_all_strict_statuses() -> None:
    static = Path(__file__).resolve().parents[1] / "src" / "visual_agent" / "dashboard" / "static"
    html = (static / "five-pillars.html").read_text(encoding="utf-8")
    script = (static / "five-pillars.js").read_text(encoding="utf-8")
    stylesheet = (static / "five-pillars.css").read_text(encoding="utf-8")

    for status in ("passed", "failed", "partial", "indeterminate"):
        assert f'data-filter="{status}"' in html
        assert status in script
        assert f".{status}" in stylesheet
    assert "STATUS_LABELS" in script
    assert "acceptance_adequacy" in script
    assert "product_verdict" in script


def test_observability_api_rejects_invalid_ids_and_pagination(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    _write_launch(workspace)
    server, thread, base = _serve(workspace)
    try:
        for path in (
            "/api/observability/launches?limit=999",
            "/api/observability/launches/%2E%2E%2Foutside",
            "/api/observability/sessions/session-one/timeline?launch_id=bad%2Fid&cursor=0&limit=20",
            "/api/observability/sessions/session-one/timeline?launch_id=20260713-123242-test&cursor=-1",
        ):
            try:
                urllib.request.urlopen(base + path, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            else:
                raise AssertionError(f"request unexpectedly succeeded: {path}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_observability_api_returns_404_for_unknown_launch_and_session(tmp_path) -> None:
    workspace = tmp_path / ".agent-workspace"
    launch_id = "20260713-123242-test"
    _write_launch(workspace, launch_id)
    server, thread, base = _serve(workspace)
    try:
        for path in (
            "/api/observability/launches/missing-launch",
            f"/api/observability/sessions/missing-session/timeline?launch_id={launch_id}",
        ):
            try:
                urllib.request.urlopen(base + path, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError(f"request unexpectedly succeeded: {path}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
