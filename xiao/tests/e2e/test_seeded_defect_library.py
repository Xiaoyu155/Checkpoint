"""漏报回归库：the acceptance officer's own acceptance suite.

Every page here carries a seeded defect (or a capability the executor must
drive). If Checkpoint stops catching any of these, this suite turns red. New
escaped bugs from real projects should be distilled into new entries here.
"""

from __future__ import annotations

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

PAGE_HEAD = "<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\"><title>{title}</title><style>body{{margin:24px;font-size:16px}}</style></head>"

# --- seeded defect pages: the engine MUST flag each one -----------------------

DEFECT_PAGES = {
    "console_error": (
        PAGE_HEAD.format(title="控制台报错页")
        + """<body><h1>控制台报错页</h1><p>页面文字一切正常，但控制台有未处理错误。</p>
<script>console.error('Uncaught TypeError: cannot read properties of undefined');</script></body></html>"""
    ),
    "pageerror": (
        PAGE_HEAD.format(title="页面异常页")
        + """<body><h1>页面异常页</h1><p>页面文字一切正常，但脚本抛出了未捕获异常。</p>
<script>setTimeout(function(){ throw new Error('boom: state is broken'); }, 0);</script></body></html>"""
    ),
}


def write_workflow(tmp_path: Path, html: str, title: str, extra_steps: str = "") -> Path:
    page_path = tmp_path / "page.html"
    page_path.write_text(html, encoding="utf-8")
    workflow_path = tmp_path / "check.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "name: seeded_check\n"
        "version: 1\n"
        "tags:\n  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_browser\n"
        f"    url: \"{page_path.as_uri()}\"\n"
        "    wait_after_seconds: 0.3\n"
        "  - id: check_text\n"
        "    action: assert_text\n"
        f"    text: \"{title}\"\n"
        f"{extra_steps}",
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


def failed_steps(payload: dict) -> list[dict]:
    return [step for step in payload["steps"] if step["status"] == "failed"]


@pytest.mark.parametrize("defect", sorted(DEFECT_PAGES))
def test_runtime_errors_are_blocked_by_product_guard(tmp_path: Path, defect: str) -> None:
    title = {"console_error": "控制台报错页", "pageerror": "页面异常页"}[defect]
    payload = run_workflow(tmp_path, write_workflow(tmp_path, DEFECT_PAGES[defect], title))

    guard = next(step for step in payload["steps"] if step["id"] == "product_guard")
    assert guard["status"] == "failed", f"product_guard must block the {defect} page"
    assert payload["run_checks"]["product_guard"]["status"] == "failed"


def test_dead_button_is_caught_by_outcome_assertion(tmp_path: Path) -> None:
    html = (
        PAGE_HEAD.format(title="死按钮页")
        + """<body><h1>死按钮页</h1><p>按钮看起来正常，但点击没有任何效果。</p>
<button id="submit">提交订单</button><div id="status">未保存</div></body></html>"""
    )
    extra = (
        "  - id: submit\n"
        "    action: click\n"
        "    target:\n      text: \"提交订单\"\n      role: button\n"
        "  - id: verify\n"
        "    action: assert_text\n"
        "    text: \"已保存\"\n"
    )
    payload = run_workflow(tmp_path, write_workflow(tmp_path, html, "死按钮页", extra))

    failures = failed_steps(payload)
    assert failures and failures[0]["id"] == "verify", "the dead button must fail the outcome assertion"
    # the run proved interaction (L2) but could not close the loop
    assert payload["acceptance"]["label"] == "L2"
    assert payload["acceptance"]["is_product_acceptance"] is False


def test_blank_page_is_caught_by_browser_ready(tmp_path: Path) -> None:
    html = PAGE_HEAD.format(title="空白页") + "<body></body></html>"
    page_path = tmp_path / "page.html"
    page_path.write_text(html, encoding="utf-8")
    workflow_path = tmp_path / "check.yaml"
    workflow_path.write_text(
        "schema_version: 1\nname: blank_check\nversion: 1\ntags:\n  - verification\nsteps:\n"
        "  - id: observe\n    action: observe_browser\n"
        f"    url: \"{page_path.as_uri()}\"\n"
        "  - id: ready\n    action: assert_browser_ready\n    min_text_length: 1\n",
        encoding="utf-8",
    )
    payload = run_workflow(tmp_path, workflow_path)

    failures = failed_steps(payload)
    assert failures and failures[0]["id"] == "ready"


# --- executor capability pages: the engine MUST be able to drive these --------


def test_upload_capability_reaches_l3(tmp_path: Path) -> None:
    html = (
        PAGE_HEAD.format(title="上传页")
        + """<body><h1>上传页</h1><input type="file" id="file"><div id="status">未上传</div>
<script>document.getElementById('file').addEventListener('change', function (e) {
  document.getElementById('status').textContent = '已上传: ' + e.target.files[0].name;
});</script></body></html>"""
    )
    sample = tmp_path / "头像.png"
    sample.write_bytes(b"fake-image-bytes")
    extra = (
        "  - id: upload\n"
        "    action: upload_file\n"
        "    selector: \"#file\"\n"
        f"    path: \"{str(sample).replace(chr(92), '/')}\"\n"
        "    post_action_assert_text: \"已上传: 头像.png\"\n"
        "  - id: verify\n"
        "    action: assert_text_contract\n"
        "    required_all: [\"已上传: 头像.png\"]\n"
        "    forbidden_any: [\"错误\", \"失败\"]\n"
    )
    payload = run_workflow(tmp_path, write_workflow(tmp_path, html, "上传页", extra))

    assert not failed_steps(payload), failed_steps(payload)
    assert payload["acceptance"]["label"] in ("L3", "L4")
    assert payload["acceptance"]["is_product_acceptance"] is True


def test_select_option_capability(tmp_path: Path) -> None:
    html = (
        PAGE_HEAD.format(title="下拉页")
        + """<body><h1>下拉页</h1>
<select id="city"><option value="">请选择</option><option value="sh">上海</option><option value="bj">北京</option></select>
<div id="status">未选择</div>
<script>document.getElementById('city').addEventListener('change', function (e) {
  document.getElementById('status').textContent = '已选择: ' + e.target.options[e.target.selectedIndex].text;
});</script></body></html>"""
    )
    extra = (
        "  - id: pick\n"
        "    action: select_option\n"
        "    selector: \"#city\"\n"
        "    label: \"上海\"\n"
        "    post_action_assert_text: \"已选择: 上海\"\n"
        "  - id: verify\n"
        "    action: assert_text_contract\n"
        "    required_all: [\"已选择: 上海\"]\n"
        "    forbidden_any: [\"错误\", \"失败\"]\n"
    )
    payload = run_workflow(tmp_path, write_workflow(tmp_path, html, "下拉页", extra))

    assert not failed_steps(payload), failed_steps(payload)
    assert payload["acceptance"]["is_product_acceptance"] is True


def test_drag_capability(tmp_path: Path) -> None:
    html = (
        PAGE_HEAD.format(title="拖拽页")
        + """<body><h1>拖拽页</h1>
<div id="card" style="width:120px;height:60px;background:#cde;margin-bottom:200px;">任务卡片</div>
<div id="dropzone" style="width:240px;height:120px;border:2px dashed #888;">放置区域</div>
<div id="status">未移动</div>
<script>
var dragging = false;
document.getElementById('card').addEventListener('mousedown', function () { dragging = true; });
document.addEventListener('mouseup', function (e) {
  if (!dragging) return;
  dragging = false;
  var rect = document.getElementById('dropzone').getBoundingClientRect();
  if (e.clientX >= rect.left && e.clientX <= rect.right && e.clientY >= rect.top && e.clientY <= rect.bottom) {
    document.getElementById('status').textContent = '已放置完成';
  }
});
</script></body></html>"""
    )
    extra = (
        "  - id: move\n"
        "    action: drag\n"
        "    selector: \"#card\"\n"
        "    to_selector: \"#dropzone\"\n"
        "    post_action_assert_text: \"已放置完成\"\n"
        "  - id: verify\n"
        "    action: assert_text_contract\n"
        "    required_all: [\"已放置完成\"]\n"
        "    forbidden_any: [\"错误\", \"失败\"]\n"
    )
    payload = run_workflow(tmp_path, write_workflow(tmp_path, html, "拖拽页", extra))

    assert not failed_steps(payload), failed_steps(payload)
    assert payload["acceptance"]["is_product_acceptance"] is True


AI_PAGE = (
    PAGE_HEAD.format(title="AI功能页")
    + """<body><h1>AI功能页</h1><button id="ask">生成行程</button><div id="answer">等待生成</div>
<script>document.getElementById('ask').addEventListener('click', function () {
  fetch('https://app.example.test/api/ai/plan').then(function (r) { return r.text(); }).then(function (text) {
    document.getElementById('answer').textContent = text;
  });
});</script></body></html>"""
)

AI_ANSWER = "根据您的需求，建议第一天游览西湖，第二天前往灵隐寺，整体安排可以按上午下午分段进行。"


def write_ai_workflow(tmp_path: Path, ai_source: str) -> Path:
    page_path = tmp_path / "page.html"
    page_path.write_text(AI_PAGE, encoding="utf-8")
    workflow_path = tmp_path / "check.yaml"
    workflow_path.write_text(
        "schema_version: 1\n"
        "name: ai_source_check\n"
        "version: 1\n"
        "tags:\n  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_browser\n"
        f"    url: \"{page_path.as_uri()}\"\n"
        "    routes:\n"
        "      - url: \"**/api/ai/**\"\n"
        f"        body: \"{AI_ANSWER}\"\n"
        "        content_type: \"text/plain; charset=utf-8\"\n"
        "        headers:\n"
        f"          x-ai-source: \"{ai_source}\"\n"
        "          access-control-allow-origin: \"*\"\n"
        "  - id: ask\n"
        "    action: click\n"
        "    target:\n      text: \"生成行程\"\n      role: button\n"
        "    wait_after_seconds: 0.5\n"
        "  - id: wait_answer\n"
        "    action: assert_text\n"
        "    text: \"建议第一天游览西湖\"\n"
        "    contains_text: \"建议\"\n"
        "  - id: ai_quality\n"
        "    action: assert_ai_response_quality\n"
        "    require_real_ai: true\n"
        "    ai_url_contains: \"/api/ai/\"\n",
        encoding="utf-8",
    )
    return workflow_path


def test_fallback_ai_path_cannot_claim_real_ai_pass(tmp_path: Path) -> None:
    payload = run_workflow(tmp_path, write_ai_workflow(tmp_path, "fallback"))

    failures = failed_steps(payload)
    assert failures and failures[0]["id"] == "ai_quality", failures
    assert "fallback" in failures[0]["message"]


def test_real_ai_path_passes_authenticity_check(tmp_path: Path) -> None:
    payload = run_workflow(tmp_path, write_ai_workflow(tmp_path, "real"))

    assert not failed_steps(payload), failed_steps(payload)
    ai_step = next(step for step in payload["steps"] if step["id"] == "ai_quality")
    assert ai_step["metadata"]["ai_response_quality"]["ai_source"] == "real"


def test_iframe_capability(tmp_path: Path) -> None:
    inner = (
        PAGE_HEAD.format(title="内嵌表单")
        + """<body>
<select id="plan"><option value="">请选择</option><option value="pro">专业版</option></select>
<script>document.getElementById('plan').addEventListener('change', function (e) {
  parent.postMessage('已订阅: ' + e.target.options[e.target.selectedIndex].text, '*');
});</script></body></html>"""
    )
    (tmp_path / "inner.html").write_text(inner, encoding="utf-8")
    outer = (
        PAGE_HEAD.format(title="主页面")
        + """<body><h1>主页面</h1>
<iframe id="form-frame" src="inner.html" style="width:400px;height:200px;"></iframe>
<div id="status">未订阅</div>
<script>window.addEventListener('message', function (e) {
  document.getElementById('status').textContent = String(e.data);
});</script></body></html>"""
    )
    extra = (
        "  - id: pick\n"
        "    action: select_option\n"
        "    selector: \"#plan\"\n"
        "    frame_selector: \"#form-frame\"\n"
        "    label: \"专业版\"\n"
        "    wait_after_seconds: 0.5\n"
        "    post_action_assert_text: \"已订阅: 专业版\"\n"
        "  - id: verify\n"
        "    action: assert_text_contract\n"
        "    required_all: [\"已订阅: 专业版\"]\n"
        "    forbidden_any: [\"错误\", \"失败\"]\n"
    )
    payload = run_workflow(tmp_path, write_workflow(tmp_path, outer, "主页面", extra))

    assert not failed_steps(payload), failed_steps(payload)
    assert payload["acceptance"]["is_product_acceptance"] is True
