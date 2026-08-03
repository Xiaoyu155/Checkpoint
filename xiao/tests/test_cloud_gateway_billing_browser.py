from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from cloud_api.billing_demo import build_demo_app  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def billing_demo(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PACER_GATEWAY_DB", str(tmp_path / "placeholder.db"))
    monkeypatch.setenv("PACER_WECHAT_CREDIT_PACKAGES_JSON", "[]")
    app = build_demo_app(tmp_path / "billing-demo")
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 8
    while not server.started and thread.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        pytest.fail("Billing demo server did not start.")
    try:
        yield {
            "url": f"http://127.0.0.1:{port}/billing",
            "api_key": app.state.billing_demo_api_key,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.mark.browser
def test_billing_checkout_is_one_resumable_responsive_journey(
    billing_demo, tmp_path: Path
) -> None:
    artifact_dir = Path(
        os.environ.get("PACER_BILLING_E2E_ARTIFACTS") or tmp_path / "screenshots"
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    browser_errors: list[str] = []

    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda exc: browser_errors.append(str(exc)))
        page.on(
            "console",
            lambda message: browser_errors.append(message.text)
            if message.type == "error"
            else None,
        )

        page.goto(billing_demo["url"], wait_until="networkidle")
        assert page.title() == "Pacer 额度中心"
        assert page.locator("#authView").is_visible()
        assert page.locator("#billingView").is_hidden()
        page.screenshot(path=artifact_dir / "01-connect-desktop.png", full_page=True)

        page.fill("#apiKey", billing_demo["api_key"])
        page.click("#authForm button[type='submit']")
        page.locator("#billingView").wait_for(state="visible", timeout=5000)
        assert page.locator("#accountTitle").inner_text() == "Pacer 演示账户"
        assert page.locator(".package-item").count() == 2
        assert page.locator("#providerState").inner_text() == "可支付"
        page.screenshot(path=artifact_dir / "02-packages-desktop.png", full_page=True)

        page.click("button[data-package-id='starter']")
        page.locator("#paymentQr").wait_for(state="visible", timeout=5000)
        assert page.locator("#orderStatus").inner_text() == "待支付"
        assert page.locator("#paymentQr").get_attribute("src").startswith(
            "data:image/png;base64,"
        )
        qr_pixels = page.evaluate(
            """() => {
                const image = document.querySelector('#paymentQr');
                const canvas = document.createElement('canvas');
                canvas.width = image.naturalWidth;
                canvas.height = image.naturalHeight;
                const context = canvas.getContext('2d');
                context.drawImage(image, 0, 0);
                const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
                let dark = 0;
                let light = 0;
                for (let index = 0; index < pixels.length; index += 16) {
                    const value = pixels[index] + pixels[index + 1] + pixels[index + 2];
                    if (value < 180) dark += 1;
                    if (value > 720) light += 1;
                }
                return { width: image.naturalWidth, height: image.naturalHeight, dark, light };
            }"""
        )
        assert qr_pixels["width"] >= 200
        assert qr_pixels["height"] >= 200
        assert qr_pixels["dark"] > 100
        assert qr_pixels["light"] > 100
        page.screenshot(path=artifact_dir / "03-pending-desktop.png", full_page=True)

        page.locator("#orderStatus").wait_for(state="visible")
        playwright_api.expect(page.locator("#orderStatus")).to_have_text(
            "已到账", timeout=16000
        )
        assert page.locator("#accountBalance").inner_text() == "US$1.00"
        assert page.locator("#resultTitle").inner_text() == "额度已到账"
        assert "已到账" in page.locator("#orderRows").inner_text()
        page.screenshot(path=artifact_dir / "04-paid-desktop.png", full_page=True)

        page.reload(wait_until="networkidle")
        page.locator("#billingView").wait_for(state="visible", timeout=5000)
        assert page.locator("#accountBalance").inner_text() == "US$1.00"
        assert "已到账" in page.locator("#orderRows").inner_text()

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload(wait_until="networkidle")
        page.locator("#billingView").wait_for(state="visible", timeout=5000)
        mobile_layout = page.evaluate(
            """() => {
                const packages = document.querySelector('.packages-section').getBoundingClientRect();
                const payment = document.querySelector('.payment-tool').getBoundingClientRect();
                const buttonsFit = [...document.querySelectorAll('button')]
                    .filter((button) => button.offsetParent !== null)
                    .every((button) => button.scrollWidth <= button.clientWidth + 1);
                return {
                    viewport: document.documentElement.clientWidth,
                    pageWidth: document.documentElement.scrollWidth,
                    paymentAfterPackages: payment.top >= packages.bottom,
                    buttonsFit,
                };
            }"""
        )
        assert mobile_layout["pageWidth"] <= mobile_layout["viewport"] + 1
        assert mobile_layout["paymentAfterPackages"] is True
        assert mobile_layout["buttonsFit"] is True
        page.screenshot(path=artifact_dir / "05-paid-mobile.png", full_page=True)
        browser.close()

    assert not browser_errors, f"Billing page produced browser errors: {browser_errors}"

