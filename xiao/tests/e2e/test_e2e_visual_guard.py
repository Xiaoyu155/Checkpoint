from __future__ import annotations

import json
from pathlib import Path

import pytest

from .helpers import json_output, playwright_chromium_available, run_cli


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.browser,
    pytest.mark.skipif(
        not playwright_chromium_available(),
        reason="Playwright Chromium is unavailable. Run: python -m playwright install chromium",
    ),
]


SEEDED_DEFECTS_HTML = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>种子缺陷页面</title>
<style>
  body { margin: 0; font-size: 16px; }
  .tiny { font-size: 7px; }
  .wide { width: 3000px; height: 40px; background: #eee; }
  .overlay { position: fixed; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.01); }
</style></head>
<body>
  <h1>种子缺陷页面</h1>
  <p>这一段是正常字号的文本，用来确保页面文字断言可以通过。</p>
  <p class="tiny">这一段字号只有七像素，肉眼基本无法阅读。</p>
  <div class="wide">这个区块刻意超出视口宽度，制造横向滚动。</div>
  <img src="missing.png" alt="broken" width="120" height="80">
  <button id="pay">提交订单</button>
  <div class="overlay"></div>
</body>
</html>
"""

CLEAN_HTML = """<!DOCTYPE html>
<html lang="zh">
<head><meta charset="utf-8"><title>干净页面</title>
<style>body { margin: 24px; font-size: 16px; max-width: 960px; }</style></head>
<body>
  <h1>干净页面</h1>
  <p>这是一个没有视觉缺陷的页面，字号正常，没有横向溢出。</p>
  <p>第二段内容，让视口覆盖率高于空白阈值。这里有足够的文字内容来覆盖视口的格子。</p>
  <p>第三段内容，继续增加可见内容的面积，确保覆盖率检查不会误报。</p>
  <button id="ok">确定</button>
</body>
</html>
"""


def write_workflow(tmp_path: Path, html: str, *, with_visual_step: bool) -> Path:
    page_path = tmp_path / "page.html"
    page_path.write_text(html, encoding="utf-8")
    visual_step = "  - id: visual\n    action: assert_visual_quality\n" if with_visual_step else ""
    title = "种子缺陷页面" if "种子缺陷" in html else "干净页面"
    workflow_path = tmp_path / "visual_check.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "name: visual_check\n"
        "version: 1\n"
        "tags:\n  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_browser\n"
        f"    url: \"{page_path.as_uri()}\"\n"
        "  - id: check_text\n"
        "    action: assert_text\n"
        f"    text: \"{title}\"\n"
        f"{visual_step}",
        encoding="utf-8",
    )
    return workflow_path


def run_workflow(tmp_path: Path, workflow_path: Path) -> dict:
    result = run_cli(
        "run-workflow",
        "--file",
        str(workflow_path),
        "--run-profile",
        "supervised",
        "--output-dir",
        str(tmp_path / "runs"),
        "--no-lock",
        timeout=120.0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json_output(result)


def test_seeded_defects_fail_explicit_visual_assertion(tmp_path: Path) -> None:
    payload = run_workflow(tmp_path, write_workflow(tmp_path, SEEDED_DEFECTS_HTML, with_visual_step=True))

    visual = next(step for step in payload["steps"] if step["id"] == "visual")
    assert visual["status"] == "failed"
    for rule in ("font_too_small", "horizontal_overflow", "broken_image"):
        assert rule in visual["message"], f"{rule} missing from: {visual['message']}"

    artifact_path = Path(payload["run_dir"]) / "visual" / "visual.json"
    assert artifact_path.exists()
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    rules = {item["rule"] for item in artifact["findings"]}
    assert {"font_too_small", "horizontal_overflow", "broken_image", "occluded_control"} <= rules
    # the occluded pay button is a warning, not a blocking error
    occluded = next(item for item in artifact["findings"] if item["rule"] == "occluded_control")
    assert occluded["severity"] == "warning"


def test_seeded_defects_blocked_by_auto_guard_without_visual_step(tmp_path: Path) -> None:
    payload = run_workflow(tmp_path, write_workflow(tmp_path, SEEDED_DEFECTS_HTML, with_visual_step=False))

    authored = [step for step in payload["steps"] if step["id"] in ("observe", "check_text")]
    assert all(step["status"] == "success" for step in authored)
    visual_guard = next(step for step in payload["steps"] if step["id"] == "visual_guard")
    assert visual_guard["status"] == "failed"
    assert visual_guard["metadata"]["visual_audit"]["error_count"] >= 2


def test_clean_page_passes_visual_rules(tmp_path: Path) -> None:
    payload = run_workflow(tmp_path, write_workflow(tmp_path, CLEAN_HTML, with_visual_step=True))

    assert not [step for step in payload["steps"] if step["status"] == "failed"]
    visual = next(step for step in payload["steps"] if step["id"] == "visual")
    assert visual["status"] == "success"
    assert visual["metadata"]["visual_audit"]["error_count"] == 0
    # no real interaction in this workflow: visually clean but still only L1
    assert payload["acceptance"]["label"] == "L1"
    assert payload["acceptance"]["is_product_acceptance"] is False
    assert payload["run_checks"]["visual_guard"]["status"] == "passed"
