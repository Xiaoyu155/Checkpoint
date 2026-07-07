import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path

from PIL import Image

from visual_agent.vision import MockVisionLocator, build_locator


def test_mock_locator_returns_image_center() -> None:
    image = Image.new("RGB", (800, 600))
    location = MockVisionLocator().locate(image, Path("screen.png"), "登录")

    assert location.x == 400
    assert location.y == 300
    assert location.target == "登录"


def test_build_locator_supports_mock() -> None:
    assert isinstance(build_locator("mock"), MockVisionLocator)


def test_build_locator_supports_omniparser_endpoint(monkeypatch) -> None:
    responses: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            responses.append(payload)
            body = json.dumps(
                {
                    "elements": [
                        {
                            "text": "Save button",
                            "bounds": {"left": 10, "top": 20, "width": 100, "height": 40},
                            "confidence": 0.92,
                            "role": "button",
                        }
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args, **_kwargs) -> None:  # noqa: D401
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("VISUAL_AGENT_OMNIPARSER_ENDPOINT", f"http://127.0.0.1:{server.server_address[1]}/detect")
    try:
        locator = build_locator("omniparser")
        image = Image.new("RGB", (200, 200))
        location = locator.locate(image, Path("screen.png"), "Save button")
        elements = locator.detect(image, Path("screen.png"), "Save button")
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert responses
    assert responses[0]["mode"] == "detect"
    assert responses[0]["target"] == "Save button"
    assert location.x == 60
    assert location.y == 40
    assert elements[0]["text"] == "Save button"
