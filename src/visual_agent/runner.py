from __future__ import annotations

from pathlib import Path

from .actions import DesktopActions
from .audit import RunAudit
from .capture import ScreenCapture
from .models import Observation, ProviderKind, RunResult, Target
from .selector import SelectorResolver


class VisualAgentRunner:
    def __init__(self, output_dir: str | Path = ".runs") -> None:
        self.audit = RunAudit(output_dir)
        self.actions = DesktopActions()
        self.resolver = SelectorResolver()

    def click_target(
        self,
        target: str | Target,
        provider: str = "mock",
        *,
        dry_run: bool = False,
        synthetic_on_capture_fail: bool = False,
    ) -> RunResult:
        target_model = Target.from_text(target) if isinstance(target, str) else target
        run_id, run_dir = self.audit.create_run_dir()
        capture = ScreenCapture(output_dir=run_dir)

        try:
            screenshot = capture.capture_primary()
        except Exception:
            if not synthetic_on_capture_fail:
                raise
            screenshot = capture.capture_synthetic()

        observation = Observation(
            provider=ProviderKind.SCREEN,
            source="primary-screen",
            screenshot_path=screenshot.path,
            width=screenshot.width,
            height=screenshot.height,
            metadata={"requested_provider": provider},
        )
        resolved = self.resolver.resolve(target_model, observation)
        action = self.actions.click(
            resolved.click_point,
            target_model,
            provider=resolved.evidence.provider,
            dry_run=dry_run,
        )
        result = RunResult(
            run_id=run_id,
            run_dir=run_dir,
            observation=observation,
            resolved_target=resolved,
            action=action,
        )
        self.audit.write_result(result)
        return result
