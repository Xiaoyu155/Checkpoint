from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

from .audit import RunAudit
from .capture import ScreenCapture, Screenshot
from .dispatcher import ActionDispatchContext, ActionDispatcher
from .models import ActionResult, LocationEvidence, Observation, Point, ProviderKind, ResolvedTarget, Target
from .providers import ProviderContext, default_provider_registry
from .selector import OCRSelectorStrategy, SelectorResolver
from .uia import find_uia_window_match, minimize_window
from .workflow import close_context_resources
from .workflow_types import WorkflowContext


class VisualSession:
    """Programmable Visual Agent session for Python scripts and tests."""

    def __init__(
        self,
        workspace: str | Path = ".agent-workspace",
        *,
        dry_run: bool = False,
        run_profile: str | None = None,
        synthetic_on_capture_fail: bool = False,
    ) -> None:
        self._workspace = Path(workspace)
        self._dry_run = dry_run
        self._run_profile = run_profile
        self._synthetic = synthetic_on_capture_fail
        self._context: WorkflowContext | None = None
        self._results: list[ActionResult] = []
        self._dispatcher = ActionDispatcher()
        self._providers = default_provider_registry()
        self._resolver = SelectorResolver()

    def __enter__(self) -> "VisualSession":
        audit = RunAudit(self._workspace / "runs")
        run_id, run_dir = audit.create_run_dir()
        self._context = WorkflowContext(run_id=run_id, run_dir=run_dir)
        return self

    def __exit__(self, *args: object) -> None:
        if self._context is not None:
            close_context_resources(self._context)
        self._context = None

    def bring_to_front(self, title_contains: str) -> None:
        find_uia_window_match(
            {"title_contains": title_contains, "bring_to_front": True},
            max_depth=3,
            max_elements=800,
        )

    def minimize(self, title_contains: str) -> bool:
        match = find_uia_window_match({"title_contains": title_contains}, max_depth=3, max_elements=800)
        return bool(match.native_window_handle and minimize_window(int(match.native_window_handle)))

    def observe_uia(self, **params: Any) -> Observation:
        return self._observe("observe_uia", params)

    def observe_ocr(self, *, engine: str = "auto", **params: Any) -> Observation:
        return self._observe("observe_ocr", {"engine": engine, **params})

    def observe_screen(self, **params: Any) -> Observation:
        return self._observe("observe_screen", params, cache=False)

    def observe_browser(self, *, url: str | None = None, reuse_page: bool = False, **params: Any) -> Observation:
        merged = {**params, "reuse_page": reuse_page}
        if url is not None:
            merged["url"] = url
        return self._observe("observe_browser", merged, cache=False)

    def observe_dom(self, *, url: str, **params: Any) -> Observation:
        return self._observe("observe_dom", {"url": url, **params})

    def click_text(
        self,
        text: str,
        *,
        engine: str = "auto",
        timeout_seconds: float = 5.0,
        dry_run: bool | None = None,
        **params: Any,
    ) -> ActionResult:
        return self._dispatch(
            "click_text",
            self._null_resolved(text),
            {"text": text, "engine": engine, "timeout_seconds": timeout_seconds, **params},
            self._effective_dry_run(dry_run),
        )

    def wait_for_text(
        self,
        text: str,
        *,
        timeout: float = 10.0,
        poll: float = 1.0,
        engine: str = "auto",
        **params: Any,
    ) -> ActionResult:
        return self._dispatch(
            "wait_for_text",
            self._null_resolved(text),
            {
                "text": text,
                "timeout_seconds": timeout,
                "poll_seconds": poll,
                "engine": engine,
                **params,
            },
            self._effective_dry_run(None),
        )

    def press_key(self, keys: str | list[str], *, dry_run: bool | None = None) -> ActionResult:
        return self._dispatch(
            "press_key",
            self._null_resolved("_key_"),
            {"keys": keys},
            self._effective_dry_run(dry_run),
        )

    def click(
        self,
        *,
        text: str | None = None,
        contains_text: str | None = None,
        role: str | None = None,
        label: str | None = None,
        selector: str | None = None,
        observation: Observation | None = None,
        dry_run: bool | None = None,
    ) -> ActionResult:
        resolved = self._resolve_target(
            Target(text=text, contains_text=contains_text, role=role, label=label, selector=selector),
            observation=observation,
        )
        return self._dispatch("click", resolved, {}, self._effective_dry_run(dry_run))

    def type_text(
        self,
        value: str,
        *,
        text: str | None = None,
        role: str | None = None,
        label: str | None = None,
        selector: str | None = None,
        observation: Observation | None = None,
        sensitive: bool = False,
        dry_run: bool | None = None,
    ) -> ActionResult:
        resolved = self._resolve_target(
            Target(text=text, role=role, label=label, selector=selector),
            observation=observation,
        )
        return self._dispatch(
            "type",
            resolved,
            {"value": value, "sensitive": sensitive},
            self._effective_dry_run(dry_run),
        )

    def assert_text_visible(
        self,
        text: str,
        *,
        engine: str = "auto",
        message: str | None = None,
        **params: Any,
    ) -> None:
        observation = self.observe_ocr(engine=engine, **params)
        evidence = OCRSelectorStrategy().locate(Target(text=text, contains_text=text), observation)
        if evidence is None:
            raise AssertionError(message or f"Text not visible on screen: '{text}'")

    def screenshot(self, label: str = "screenshot") -> Screenshot:
        context = self._require_context()
        capture = ScreenCapture(output_dir=context.run_dir)
        screenshot = capture.capture_primary()
        if label != "screenshot":
            renamed = context.run_dir / f"{label}.png"
            screenshot.path.replace(renamed)
            return Screenshot(image=screenshot.image, path=renamed, width=screenshot.width, height=screenshot.height)
        return screenshot

    @property
    def run_dir(self) -> Path:
        return self._require_context().run_dir

    @property
    def results(self) -> list[ActionResult]:
        return list(self._results)

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def _require_context(self) -> WorkflowContext:
        if self._context is None:
            raise RuntimeError("VisualSession must be used as a context manager: `with VisualSession() as s:`")
        return self._context

    def _provider_context(self) -> ProviderContext:
        context = self._require_context()
        return ProviderContext(
            run_dir=context.run_dir,
            synthetic_on_capture_fail=self._synthetic,
            resources=context.resources,
        )

    def _effective_dry_run(self, override: bool | None) -> bool:
        return self._dry_run if override is None else override

    def _observe(self, action: str, params: dict[str, Any], *, cache: bool = True) -> Observation:
        context = self._require_context()
        cache_key = context.observation_cache_key(action, params) if cache else ""
        if cache:
            cached = context.get_cached_observation(cache_key)
            if cached is not None:
                return cached
        observation = self._providers.observe(action, params, self._provider_context())
        if cache:
            context.set_cached_observation(cache_key, observation)
        context.observations[f"{action.replace('observe_', '')}-{uuid4().hex[:6]}"] = observation
        return observation

    def _resolve_target(self, target: Target, *, observation: Observation | None = None) -> ResolvedTarget:
        context = self._require_context()
        return self._resolver.resolve(target, observation or context.latest_observation)

    def _dispatch(self, action: str, resolved: ResolvedTarget, params: dict[str, Any], dry_run: bool) -> ActionResult:
        context = self._require_context()
        result = self._dispatcher.execute(
            action,
            resolved,
            params,
            ActionDispatchContext(workflow_context=context, dry_run=dry_run),
        )
        self._results.append(result)
        context.actions[f"{action}-{len(self._results)}"] = result
        context.invalidate_observation_cache()
        return result

    def _null_resolved(self, label: str = "_sdk_") -> ResolvedTarget:
        return ResolvedTarget(
            target=Target(text=label),
            evidence=LocationEvidence(
                provider=ProviderKind.MOCK,
                confidence=1.0,
                reason="SDK self-contained action",
                point=Point(x=0, y=0),
            ),
        )
