import json

from visual_agent.models import ActionStatus
from visual_agent.runner import VisualAgentRunner


def test_runner_writes_audited_dry_run_result(tmp_path) -> None:
    result = VisualAgentRunner(output_dir=tmp_path).click_target(
        "登录",
        dry_run=True,
        synthetic_on_capture_fail=True,
    )

    result_path = result.run_dir / "result.json"
    payload = json.loads(result_path.read_text(encoding="utf-8"))

    assert result.action.status == ActionStatus.DRY_RUN
    assert result.resolved_target.click_point.x > 0
    assert result_path.exists()
    assert payload["action"]["status"] == "dry_run"
    assert payload["resolved_target"]["evidence"]["provider"] == "mock"

