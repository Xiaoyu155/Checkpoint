from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .capture import ScreenCapture, apply_capture_region, finalize_capture_window, prepare_capture_window
from .dom import DomProvider, INTERACTIVE_SELECTOR, _COLLECT_ELEMENTS_SCRIPT
from .fixtures import load_observation_fixture
from .html_provider import HtmlFileProvider
from .models import Observation, ProviderKind
from .ocr import observe_ocr as observe_ocr_image
from .uia import UIAutomationProvider
from .vlm import observe_vision as observe_vision_image


ProviderHandler = Callable[[dict[str, Any], "ProviderContext"], Observation]


@dataclass(frozen=True)
class ProviderContext:
    run_dir: Path
    synthetic_on_capture_fail: bool = False
    resources: dict[str, Any] | None = None


class ProviderRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ProviderHandler] = {}

    def register(self, action: str, handler: ProviderHandler) -> None:
        self._handlers[action] = handler

    def observe(self, action: str, params: dict[str, Any], context: ProviderContext) -> Observation:
        if action not in self._handlers:
            raise ValueError(f"Unsupported provider action: {action}")
        return self._handlers[action](params, context)

    @property
    def actions(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


def default_provider_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register("observe_screen", observe_screen)
    registry.register("observe_dom", observe_dom)
    registry.register("observe_browser", observe_browser)
    registry.register("observe_uia", observe_uia)
    registry.register("observe_ocr", observe_ocr)
    registry.register("observe_vision", observe_vision)
    registry.register("observe_fixture", observe_fixture)
    registry.register("observe_html", observe_html)
    return registry


def observe_screen(params: dict[str, Any], context: ProviderContext) -> Observation:
    pre_capture_metadata = prepare_capture_window(params)
    capture = ScreenCapture(output_dir=context.run_dir)
    try:
        screenshot = capture.capture_primary()
    except Exception:
        if not context.synthetic_on_capture_fail:
            raise
        screenshot = capture.capture_synthetic()
    image, path, region_metadata = apply_capture_region(
        screenshot.image,
        screenshot.path,
        params,
        output_dir=context.run_dir,
        label="screen-region",
    )
    region_metadata = {**pre_capture_metadata, **region_metadata}
    region_metadata.update(finalize_capture_window(params, region_metadata))

    return Observation(
        provider=ProviderKind.SCREEN,
        source="primary-screen",
        screenshot_path=path,
        width=image.width,
        height=image.height,
        metadata=region_metadata,
    )


def observe_dom(params: dict[str, Any], context: ProviderContext) -> Observation:
    url = normalize_url(require_param(params, "url"), run_dir=context.run_dir)
    return DomProvider(headless=not bool(params.get("headed", False))).observe_url(url)


def observe_browser(params: dict[str, Any], context: ProviderContext) -> Observation:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install -e .[web]") from exc

    if params.get("reuse_page") is True:
        page = context.resources.get("playwright_page") if context.resources is not None else None
        if page is None:
            raise RuntimeError("observe_browser reuse_page requires an existing browser page.")
        return browser_page_observation(page, storage_state_loaded=bool(context.resources.get("storage_state") if context.resources else False))

    url = normalize_url(require_param(params, "url"), run_dir=context.run_dir)
    playwright = sync_playwright().start()
    browser = None
    browser_context = None
    try:
        browser = playwright.chromium.launch(headless=not bool(params.get("headed", False)))
        context_options: dict[str, Any] = {"accept_downloads": True}
        storage_state = params.get("storage_state")
        if storage_state:
            storage_state_path = resolve_context_path(storage_state, context.run_dir)
            if not storage_state_path.exists():
                raise FileNotFoundError(f"Storage state file not found: {storage_state_path}")
            context_options["storage_state"] = str(storage_state_path)
        browser_context = browser.new_context(**context_options)
        page = browser_context.new_page()
        network_events: list[dict[str, Any]] = []
        page.on("response", lambda response: network_events.append(response_event(response)))
        page.on("requestfailed", lambda request: network_events.append(request_failed_event(request)))
        for route in params.get("routes", []) or []:
            if not isinstance(route, dict):
                continue
            page.route(str(route.get("url") or "**/*"), route_handler(route, context.run_dir))
        page.goto(url, wait_until="domcontentloaded", timeout=int(params.get("timeout_ms", 10_000)))
        observation = browser_page_observation(page, storage_state_loaded=bool(storage_state))
        if context.resources is not None:
            context.resources["playwright"] = playwright
            context.resources["playwright_browser"] = browser
            context.resources["playwright_context"] = browser_context
            context.resources["playwright_page"] = page
            context.resources["network_events"] = network_events
        return observation
    except Exception:
        if browser_context is not None:
            browser_context.close()
        if browser is not None:
            browser.close()
        playwright.stop()
        raise


def browser_page_observation(page: Any, *, storage_state_loaded: bool = False) -> Observation:
    elements = tuple(page.evaluate(_COLLECT_ELEMENTS_SCRIPT, INTERACTIVE_SELECTOR))
    return Observation(
        provider=ProviderKind.DOM,
        source=page.url,
        width=page.viewport_size["width"] if page.viewport_size else None,
        height=page.viewport_size["height"] if page.viewport_size else None,
        elements=elements,
        metadata={
            "title": page.title(),
            "url": page.url,
            "provider": "playwright_browser",
            "storage_state_loaded": storage_state_loaded,
        },
    )


def observe_uia(params: dict[str, Any], context: ProviderContext) -> Observation:
    return UIAutomationProvider(max_depth=int(params.get("max_depth", 4))).observe_desktop()


def observe_ocr(params: dict[str, Any], context: ProviderContext) -> Observation:
    return observe_ocr_image(
        params,
        context.run_dir,
        synthetic_on_capture_fail=context.synthetic_on_capture_fail,
    )


def observe_vision(params: dict[str, Any], context: ProviderContext) -> Observation:
    return observe_vision_image(
        params,
        context.run_dir,
        synthetic_on_capture_fail=context.synthetic_on_capture_fail,
    )


def observe_fixture(params: dict[str, Any], context: ProviderContext) -> Observation:
    return load_observation_fixture(require_param(params, "path"))


def observe_html(params: dict[str, Any], context: ProviderContext) -> Observation:
    return HtmlFileProvider().observe_file(require_param(params, "path"))


def require_param(params: dict[str, Any], name: str) -> Any:
    if name not in params or params[name] in (None, ""):
        raise ValueError(f"Missing required parameter: {name}")
    return params[name]


def normalize_url(value: Any, *, run_dir: Path | None = None) -> str:
    text = str(value)
    if "://" in text:
        return text
    path = Path(text)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        base = run_dir if run_dir is not None else Path.cwd()
        resolved = (base / path).resolve()
        if not resolved.exists():
            cwd_candidate = (Path.cwd() / path).resolve()
            if cwd_candidate.exists():
                resolved = cwd_candidate
    if not is_allowed_local_path(resolved, run_dir=run_dir):
        raise ValueError(f"Path outside allowed directories: {resolved}")
    return resolved.as_uri()


def is_allowed_local_path(path: Path, *, run_dir: Path | None = None) -> bool:
    resolved = path.resolve()
    project_root = Path(__file__).resolve().parent.parent.parent
    roots = [Path.cwd().resolve(), project_root.resolve()]
    if run_dir is not None:
        roots.append(run_dir.resolve())
    return any(path_is_relative_to(resolved, root) for root in roots)


def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_context_path(value: Any, run_dir: Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    if str(value).startswith(".runs/") or str(value).startswith(".runs\\"):
        return path
    return path if path.exists() else run_dir / path


def response_event(response: Any) -> dict[str, Any]:
    request = response.request
    return {
        "type": "response",
        "url": response.url,
        "status": response.status,
        "ok": response.ok,
        "method": request.method,
        "resource_type": request.resource_type,
    }


def request_failed_event(request: Any) -> dict[str, Any]:
    failure = request.failure or ""
    return {
        "type": "request_failed",
        "url": request.url,
        "status": None,
        "ok": False,
        "method": request.method,
        "resource_type": request.resource_type,
        "failure": failure,
    }


def route_handler(route_config: dict[str, Any], run_dir: Path | None = None) -> Any:
    def handle(route: Any) -> None:
        headers = dict(route_config.get("headers") or {})
        content_type = str(route_config.get("content_type") or "")
        if content_type:
            headers.setdefault("content-type", content_type)
        body = str(route_config.get("body", ""))
        body_from_file = route_config.get("body_from_file")
        if body_from_file:
            body = _safe_fixture_path(str(body_from_file), run_dir or Path(".")).read_text(encoding="utf-8")
        route.fulfill(
            status=int(route_config.get("status", 200)),
            body=body,
            headers=headers,
        )

    return handle


def _safe_fixture_path(body_from_file: str, run_dir: Path) -> Path:
    raw = Path(str(body_from_file))
    project_root = Path(__file__).resolve().parent.parent.parent
    roots = [project_root.resolve(), Path.cwd().resolve()]
    workspace_root = run_dir.resolve().parent.parent if run_dir.resolve().parent.name == "runs" else None
    allowed_roots = [
        *(root / "examples" for root in roots),
        *(root / "fixtures" for root in roots),
        *(root / ".agent-workspace" / "fixtures" for root in roots),
        *((workspace_root / "fixtures",) if workspace_root is not None else ()),
        run_dir.resolve(),
    ]
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        candidates = [
            (run_dir / raw).resolve(),
            *(((workspace_root / raw).resolve(),) if workspace_root is not None else ()),
            (Path.cwd() / raw).resolve(),
        ]
        resolved = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
    if not any(path_is_relative_to(resolved, root) for root in allowed_roots):
        raise ValueError(f"body_from_file must stay inside allowed fixture directories: {raw}")
    if not resolved.exists():
        raise FileNotFoundError(f"body_from_file not found: {raw}")
    return resolved
