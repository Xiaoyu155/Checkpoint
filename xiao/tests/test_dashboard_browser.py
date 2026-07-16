"""Real-browser smoke test for the Pacer web workbench.

The dashboard's ~1700 lines of inline JS have no other test coverage; unit
tests exercise the JSON API but never execute the page. This test loads the
page in Chromium, clicks through every panel a user touches in the first five
minutes, and fails on any JS error — exactly the class of breakage users hit
that the rest of the suite cannot see.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from visual_agent.dashboard import _bind_dashboard_server  # noqa: E402
from visual_agent.chief_plans_store import append_worker_record  # noqa: E402
from visual_agent.missions import create_mission, default_budget_policy  # noqa: E402
from visual_agent.workspace import init_workspace  # noqa: E402


@pytest.fixture()
def dashboard_url(tmp_path):
    workspace = tmp_path / ".agent-workspace"
    (workspace / "missions").mkdir(parents=True)
    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.browser
def test_workbench_page_loads_and_panels_open_without_js_errors(dashboard_url) -> None:
    js_errors: list[str] = []
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: js_errors.append(msg.text) if msg.type == "error" else None,
        )

        page.goto(dashboard_url)
        # First data poll populates the header; wait for it.
        page.wait_for_selector("#projPath", state="attached")
        page.wait_for_timeout(1500)

        assert "Pacer" in page.title()
        assert page.locator("header .logo").inner_text().startswith("Pacer")
        assert page.locator(".sidebar").is_visible()
        assert page.locator(".side-nav").is_visible()
        assert page.locator("#kRunning").inner_text() != "undefined"

        # 对话 panel opens and closes.
        page.click("text=对话")
        assert page.locator("#chatPanel").is_visible()
        page.click("#chatPanel .close-btn")

        # 设置 panel opens, form fields render, and closes.
        page.click("text=设置")
        assert page.locator("#settingsPanel").is_visible()
        for field in (
            "cfgSupabaseUrl",
            "cfgGoogleOauthConfigured",
            "cfgStripePublishableKey",
            "cfgStripePriceId",
            "commercialCfgStatus",
            "cfgSmtpHost",
            "cfgRecipient",
            "cfgStatus",
        ):
            assert page.locator(f"#{field}").count() == 1
        page.click("#settingsPanel .close-btn")

        # 切换项目 modal opens.
        page.click("text=切换项目")
        page.wait_for_timeout(800)
        assert page.locator("#projModal").is_visible()
        page.click("#projModal .close-btn")

        # New-mission form exists with its core fields.
        for field in ("fGoal", "fTest", "fRepo", "fAgent"):
            assert page.locator(f"#{field}").count() == 1, f"missing form field #{field}"
        assert page.locator("#fTest").is_visible()
        assert page.locator("#traceStream").is_hidden()
        page.click(".side-group button:text('任务列表')")
        page.wait_for_selector("#missionList", state="visible", timeout=5000)
        assert page.locator("#missionList").is_visible()
        assert page.locator("#fGoal").is_hidden()
        page.click(".side-group button:text('流式工作任务')")
        assert page.locator("#traceStream").is_visible()
        assert page.locator("#missionList").is_hidden()
        page.click(".side-group button:text('Pacer 可观测性')")
        assert page.locator("#obsLoadState").is_visible()
        page.wait_for_function(
            "() => !document.querySelector('#obsStatus')?.textContent.includes('加载')",
            timeout=5000,
        )
        assert "暂无" in page.locator("#obsLoadState").inner_text()
        page.click(".side-group button:text('中转站')")
        assert page.locator("#relayBaseUrl").is_visible()
        assert page.locator("#traceStream").is_hidden()
        page.click(".side-group button:text('产品可用度')")
        assert page.locator("#coreReadinessScore").is_visible()
        assert page.locator("#coreReadinessChecks").is_visible()
        page.click(".side-group button:text('推广就绪')")
        assert page.locator("#readinessScore").is_visible()
        assert page.locator("#readinessChecks").is_visible()

        browser.close()

    assert not js_errors, f"页面产生了 JS 错误: {js_errors}"


@pytest.mark.browser
def test_workbench_renders_worker_exit_code_without_js_error(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    ws = init_workspace(workspace, with_demo=False)
    create_mission(
        workspace_root=ws.root,
        objective="展示带 worker exit code 的任务",
        repo_root=repo,
        plan_id="plan-worker-exit",
        budget_policy=default_budget_policy(),
        mission_id="mission-worker-exit",
        status="verified",
    )
    append_worker_record(
        ws.root,
        "plan-worker-exit",
        {
            "status": "completed",
            "exit_code": 0,
            "elapsed_seconds": 1.0,
            "backend": {"name": "mimo", "model": "mimo-v2.5-pro"},
        },
    )
    mission_dir = ws.root / "missions" / "mission-worker-exit"
    (mission_dir / "rounds.jsonl").write_text(
        json.dumps(
            {
                "round": 1,
                "type": "verification",
                "status": "pass",
                "recorded_at": "2026-07-07T08:09:10+00:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    server = _bind_dashboard_server("127.0.0.1", 0, ws.root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        js_errors: list[str] = []
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda exc: js_errors.append(str(exc)))
            page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            page.click(".side-group button:text('任务列表')")
            page.wait_for_function(
                "() => document.querySelector('#missionList')?.innerText.includes('展示带 worker exit code 的任务')",
                timeout=5000,
            )
            card_details = page.locator("#missionList .tcard-details")
            assert card_details.count() == 1
            card_details.locator("summary").click()
            assert "exit 0" in page.locator("#missionList").inner_text()
            board_text = page.locator("#missionList").inner_text()
            trace_text = page.locator("#traceStream").inner_text()
            assert "展示带 worker exit code 的任务" in trace_text
            assert page.locator("#traceCount").inner_text() != "0"
            timestamp_pattern = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
            assert re.search(timestamp_pattern, board_text)
            assert re.search(timestamp_pattern, trace_text)

            page.click("#missionList .mission-row")
            page.wait_for_selector("#drawer", state="visible")
            drawer_text = page.locator("#drawerBody").inner_text()
            assert "创建：" in drawer_text
            assert "更新：" in drawer_text
            assert "Round 1" in drawer_text
            assert re.search(timestamp_pattern, drawer_text)
            browser.close()

        assert not js_errors, f"页面产生了 JS 错误: {js_errors}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.browser
def test_task_intake_dialog_refines_goal_without_backend_chat(dashboard_url) -> None:
    js_errors: list[str] = []
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

        page.goto(dashboard_url)
        page.wait_for_selector("#fGoal", state="attached")
        page.fill("#fGoal", "把已经做好的元思轻语app通过数据线传输到我手机上")
        page.click("#btnClarify")
        page.wait_for_selector("#mainIntakeActions", state="visible")
        assert "Android" in page.locator("#mainChatMsgs").inner_text()
        page.click("#mainIntakeActions button.primary")
        page.wait_for_function("() => document.querySelector('#fGoal')?.value.includes('adb install -r')")

        goal = page.locator("#fGoal").input_value()
        assert "执行步骤" in goal
        assert "adb install -r" in goal
        assert page.locator("#fRepo").input_value() == "yuansi_app"
        assert page.locator("#fTest").input_value() == "flutter build apk --release"
        browser.close()

    assert not js_errors, f"页面产生了 JS 错误: {js_errors}"


@pytest.mark.browser
def test_legacy_intake_entry_uses_main_chat_not_side_panel(dashboard_url) -> None:
    js_errors: list[str] = []
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

        page.goto(dashboard_url)
        page.wait_for_selector("#fGoal", state="attached")
        page.evaluate("() => openTaskIntake('把元思轻语 app 通过数据线安装到手机上')")

        assert not page.locator("#chatPanel").is_visible()
        page.wait_for_selector("#mainIntakeActions", state="visible")
        assert "主对话区" in page.locator("#mainChatMsgs").inner_text()
        assert "adb install -r" in page.locator("#mainChatMsgs").inner_text()
        browser.close()

    assert not js_errors, f"页面产生了 JS 错误: {js_errors}"


@pytest.mark.browser
def test_livekit_manual_verification_uses_field_acceptance_plan(dashboard_url) -> None:
    js_errors: list[str] = []
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("pageerror", lambda exc: js_errors.append(str(exc)))
        page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

        page.goto(dashboard_url)
        page.wait_for_selector("#fGoal", state="attached")
        page.fill("#fGoal", "完成LiveKit真机验证：真实手机弱网和户外噪声语音通话验收")
        page.click("#btnClarify")

        page.wait_for_selector("#mainIntakeActions", state="visible")
        text = page.locator("#mainChatMsgs").inner_text()
        assert "弱网" in text
        assert "户外噪声" in text
        assert "flutter build apk" not in text
        assert "adb install" not in text
        page.click("#mainIntakeActions button.primary")
        goal = page.locator("#fGoal").input_value()
        assert "验收方案" in goal
        assert "切换生产主链路" in goal
        browser.close()

    assert not js_errors, f"页面产生了 JS 错误: {js_errors}"


@pytest.mark.browser
def test_diagnostic_endpoint_serves_bundle(dashboard_url) -> None:
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(dashboard_url)
        payload = page.evaluate(
            "() => fetch('/api/diagnostic').then(r => r.json())"
        )
        browser.close()
    assert payload["product"] == "Pacer"
    assert "agents" in payload
    assert "error_log_tail" in payload


@pytest.mark.browser
def test_workbench_frontend_creates_mission_and_updates_board(tmp_path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is required for this test")

    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    ws = init_workspace(workspace, with_demo=False)
    ws.workflows_dir.joinpath("checkout.yaml").write_text(
        "schema_version: 1\n"
        "name: checkout\n"
        "version: 1\n"
        "affects:\n"
        "  - src/payment/\n"
        "tags:\n"
        "  - verification\n"
        "steps:\n"
        "  - id: observe\n"
        "    action: observe_ocr\n"
        "    mock_text: ready\n",
        encoding="utf-8",
    )

    target = repo / "src" / "payment" / "checkout.py"
    target.parent.mkdir(parents=True)
    target.write_text("def total():\n    return 100\n", encoding="utf-8")
    git(repo, "init")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", "src/payment/checkout.py")
    git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "initial")
    target.write_text("def total():\n    return 128\n", encoding="utf-8")

    server = _bind_dashboard_server("127.0.0.1", 0, workspace)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        js_errors: list[str] = []
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda exc: js_errors.append(str(exc)))
            page.on(
                "console",
                lambda msg: js_errors.append(msg.text) if msg.type == "error" else None,
            )

            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            page.wait_for_selector("#fGoal", state="attached")
            page.evaluate("(value) => { document.querySelector('#fRepo').value = value; }", str(repo))
            page.fill("#fGoal", "Fix checkout total display")
            page.click("#btnPreview")
            page.wait_for_function(
                "() => document.querySelector('#launchMsg')?.textContent.includes('预览')",
                timeout=10000,
            )
            page.wait_for_function(
                "() => document.querySelector('#missionList')?.textContent.includes('Fix checkout total display')",
                timeout=30000,
            )

            assert page.locator("#missionList").inner_text().__contains__("Fix checkout total display")
            browser.close()

        assert not js_errors, f"页面产生了 JS 错误: {js_errors}"

        state_path = wait_for_single_state(workspace)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["current_state"] == "REVIEW"
        assert state["context"]["spec"]["scope"][0]["repo_root"] == str(repo)
        assert state["context"]["spec"]["risk"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.browser
def test_delete_button_hides_review_mission_from_board(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = repo / ".agent-workspace"
    ws = init_workspace(workspace, with_demo=False)
    create_mission(
        workspace_root=ws.root,
        objective="清理待验收任务",
        repo_root=repo,
        plan_id="plan-delete",
        budget_policy=default_budget_policy(),
        mission_id="mission-delete",
        status="stopped",
    )

    server = _bind_dashboard_server("127.0.0.1", 0, ws.root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        js_errors: list[str] = []
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on("pageerror", lambda exc: js_errors.append(str(exc)))
            page.on("console", lambda msg: js_errors.append(msg.text) if msg.type == "error" else None)

            page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
            page.click(".side-group button:text('任务列表')")
            page.wait_for_selector("#missionList", state="attached")
            page.wait_for_function(
                "() => document.querySelector('#missionList')?.innerText.includes('清理待验收任务')",
                timeout=5000,
            )
            page.on("dialog", lambda dialog: dialog.accept())
            page.click("text=删除")
            page.wait_for_timeout(300)
            page.wait_for_function(
                "() => !document.querySelector('#missionList')?.innerText.includes('清理待验收任务')",
                timeout=5000,
            )
            assert "清理待验收任务" not in page.locator("#missionList").inner_text()
            browser.close()

        assert not js_errors, f"页面产生了 JS 错误: {js_errors}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_for_single_state(workspace: Path, timeout: float = 20.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        states = sorted((workspace / "missions").glob("*/state.json"))
        if states:
            return states[0]
        time.sleep(0.2)
    raise AssertionError("mission state.json was not written")


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)
