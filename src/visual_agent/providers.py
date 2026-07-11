from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .capture import capture_visual_region
from .dom import DomProvider, INTERACTIVE_SELECTOR, _COLLECT_ELEMENTS_SCRIPT
from .fixtures import load_observation_fixture
from .html_provider import HtmlFileProvider
from .models import Observation, ProviderKind
from .ocr import observe_ocr as observe_ocr_image
from .playwright_env import ensure_playwright_browsers_path
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
    from .plugins import load_provider_plugins

    load_provider_plugins(registry)
    return registry


def observe_screen(params: dict[str, Any], context: ProviderContext) -> Observation:
    image, path, region_metadata = capture_visual_region(
        params,
        output_dir=context.run_dir,
        label="screen-region",
        synthetic_on_capture_fail=context.synthetic_on_capture_fail,
    )

    return Observation(
        provider=ProviderKind.SCREEN,
        source="primary-screen",
        screenshot_path=path,
        width=image.width,
        height=image.height,
        metadata=region_metadata,
    )


def observe_dom(params: dict[str, Any], context: ProviderContext) -> Observation:
    ensure_playwright_browsers_path(context.run_dir, (context.resources or {}).get("workspace_root"))
    url = normalize_url(require_param(params, "url"), run_dir=context.run_dir)
    return DomProvider(headless=not bool(params.get("headed", False))).observe_url(url)


def observe_browser(params: dict[str, Any], context: ProviderContext) -> Observation:
    ensure_playwright_browsers_path(context.run_dir, (context.resources or {}).get("workspace_root"))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install -e .[web]") from exc

    if params.get("reuse_page") is True:
        page = context.resources.get("playwright_page") if context.resources is not None else None
        if page is None:
            raise RuntimeError("observe_browser reuse_page requires an existing browser page.")
        if bool(params.get("refresh", False)):
            page.reload(wait_until=str(params.get("wait_until") or "domcontentloaded"), timeout=int(params.get("timeout_ms", 10_000)))
        return browser_page_observation(
            page,
            storage_state_loaded=bool(context.resources.get("storage_state") if context.resources else False),
            run_dir=context.run_dir,
            label=str(params.get("screenshot_label") or "browser-reuse"),
            network_events=context.resources.get("network_events", []) if context.resources else [],
            console_events=context.resources.get("console_events", []) if context.resources else [],
            page_errors=context.resources.get("page_errors", []) if context.resources else [],
        )

    url = normalize_url(require_param(params, "url"), run_dir=context.run_dir)
    existing_page = context.resources.get("playwright_page") if context.resources is not None else None
    if existing_page is not None and not existing_page.is_closed():
        # Reuse the live browser session: starting a second sync_playwright in the
        # same thread fails with "Sync API inside the asyncio loop".
        existing_page.goto(url, wait_until=str(params.get("wait_until") or "domcontentloaded"), timeout=int(params.get("timeout_ms", 10_000)))
        try:
            # SPA hash navigation returns before the framework re-renders; wait for
            # visible content, then give the router a short settle window.
            existing_page.wait_for_function(
                "() => !!document.body && document.body.innerText.trim().length > 0",
                timeout=int(params.get("timeout_ms", 10_000)),
            )
        except Exception:
            pass
        settle_seconds = float(params.get("wait_after_seconds", 0.3) or 0)
        if settle_seconds > 0:
            existing_page.wait_for_timeout(int(settle_seconds * 1000))
        return browser_page_observation(
            existing_page,
            storage_state_loaded=bool(context.resources.get("storage_state") if context.resources else False),
            run_dir=context.run_dir,
            label=str(params.get("screenshot_label") or "browser"),
            network_events=context.resources.get("network_events", []) if context.resources else [],
            console_events=context.resources.get("console_events", []) if context.resources else [],
            page_errors=context.resources.get("page_errors", []) if context.resources else [],
        )
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
        trace_path = None
        if params.get("record_trace", True) is not False:
            trace_dir = context.run_dir / "traces"
            trace_dir.mkdir(parents=True, exist_ok=True)
            trace_path = trace_dir / f"{safe_artifact_label(str(params.get('trace_label') or 'browser'))}.zip"
            try:
                browser_context.tracing.start(screenshots=True, snapshots=True, sources=True)
            except Exception as exc:
                trace_path = None
                if context.resources is not None:
                    context.resources["playwright_trace_error"] = str(exc)[:500]
        page = browser_context.new_page()
        network_events: list[dict[str, Any]] = []
        console_events: list[dict[str, Any]] = []
        page_errors: list[dict[str, Any]] = []
        page.on("response", lambda response: network_events.append(response_event(response)))
        page.on("requestfailed", lambda request: network_events.append(request_failed_event(request)))
        page.on("console", lambda message: console_events.append(console_event(message)))
        page.on("pageerror", lambda exc: page_errors.append(page_error_event(exc)))
        for route in params.get("routes", []) or []:
            if not isinstance(route, dict):
                continue
            page.route(str(route.get("url") or "**/*"), route_handler(route, context.run_dir))
        page.goto(url, wait_until=str(params.get("wait_until") or "domcontentloaded"), timeout=int(params.get("timeout_ms", 10_000)))
        observation = browser_page_observation(
            page,
            storage_state_loaded=bool(storage_state),
            run_dir=context.run_dir,
            label=str(params.get("screenshot_label") or "browser"),
            network_events=network_events,
            console_events=console_events,
            page_errors=page_errors,
        )
        if context.resources is not None:
            context.resources["playwright"] = playwright
            context.resources["playwright_browser"] = browser
            context.resources["playwright_context"] = browser_context
            context.resources["playwright_page"] = page
            context.resources["playwright_trace_path"] = str(trace_path) if trace_path is not None else ""
            context.resources["playwright_trace_started"] = trace_path is not None
            context.resources["network_events"] = network_events
            context.resources["console_events"] = console_events
            context.resources["page_errors"] = page_errors
        return observation
    except Exception:
        if browser_context is not None:
            browser_context.close()
        if browser is not None:
            browser.close()
        playwright.stop()
        raise


def browser_page_observation(
    page: Any,
    *,
    storage_state_loaded: bool = False,
    run_dir: Path | None = None,
    label: str = "browser",
    network_events: list[dict[str, Any]] | None = None,
    console_events: list[dict[str, Any]] | None = None,
    page_errors: list[dict[str, Any]] | None = None,
) -> Observation:
    elements = tuple(page.evaluate(_COLLECT_ELEMENTS_SCRIPT, INTERACTIVE_SELECTOR))
    visible_text = str(page.evaluate("() => document.body ? (document.body.innerText || document.body.textContent || '') : ''") or "")
    screenshot_path = None
    html_path = None
    visible_text_path = None
    if run_dir is not None:
        screenshots_dir = run_dir / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = screenshots_dir / f"{safe_artifact_label(label)}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        artifacts_dir = run_dir / "browser"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        html_path = artifacts_dir / f"{safe_artifact_label(label)}.html"
        visible_text_path = artifacts_dir / f"{safe_artifact_label(label)}.txt"
        content = page.content() if hasattr(page, "content") else ""
        html_path.write_text(str(content), encoding="utf-8")
        visible_text_path.write_text(visible_text, encoding="utf-8")
    network_events = network_events or []
    console_events = console_events or []
    page_errors = page_errors or []
    return Observation(
        provider=ProviderKind.DOM,
        source=page.url,
        screenshot_path=screenshot_path,
        width=page.viewport_size["width"] if page.viewport_size else None,
        height=page.viewport_size["height"] if page.viewport_size else None,
        elements=elements,
        metadata={
            "title": page.title(),
            "url": page.url,
            "provider": "playwright_browser",
            "storage_state_loaded": storage_state_loaded,
            "visible_text": visible_text[:10_000],
            "visible_text_length": len(visible_text.strip()),
            "interactive_count": len(elements),
            "network_event_count": len(network_events),
            "failed_request_count": sum(1 for event in network_events if is_failed_network_event(event)),
            "console_errors": tuple(event for event in console_events[-20:] if event.get("type") in {"error", "assert"}),
            "page_errors": tuple(page_errors[-20:]),
            "screenshot_path": str(screenshot_path) if screenshot_path is not None else None,
            "html_path": str(html_path) if html_path is not None else None,
            "visible_text_path": str(visible_text_path) if visible_text_path is not None else None,
        },
    )


def console_event(message: Any) -> dict[str, Any]:
    return {
        "type": str(getattr(message, "type", "") or ""),
        "text": str(getattr(message, "text", "") or "")[:1000],
    }


def page_error_event(exc: Any) -> dict[str, Any]:
    return {"type": "pageerror", "message": str(exc)[:1000]}


def is_failed_network_event(event: dict[str, Any]) -> bool:
    if event.get("type") == "request_failed":
        return True
    if event.get("ok") is False:
        return True
    try:
        return int(event.get("status") or 200) >= 400
    except (TypeError, ValueError):
        return False


def safe_artifact_label(value: str) -> str:
    text = value.strip() or "browser"
    safe = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in text)
    return safe[:80] or "browser"


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
    event = {
        "type": "response",
        "url": response.url,
        "status": response.status,
        "ok": response.ok,
        "method": request.method,
        "resource_type": request.resource_type,
    }
    # open convention: apps may label AI responses with x-ai-source so the
    # acceptance officer can tell real model output from degraded fallbacks
    try:
        ai_source = response.headers.get("x-ai-source")
    except Exception:
        ai_source = None
    if ai_source:
        event["ai_source"] = str(ai_source).strip().lower()
    return event


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
