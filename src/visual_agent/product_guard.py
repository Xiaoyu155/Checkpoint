from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PRODUCT_GUARD_STEP_ID = "product_guard"
PRODUCT_GUARD_OPT_OUT_TAG = "skip-product-guard"
BLOCKING_HTTP_STATUS = 500
MAX_RECORDED_VIOLATIONS = 20

# Aborted navigations are routine browser behavior (e.g. Playwright aborts the
# request when a navigation turns into a download), not product defects.
IGNORED_REQUEST_FAILURES = frozenset({"net::ERR_ABORTED"})

# The browser echoes every failed resource load as a console error. 4xx echoes
# follow the same policy as 4xx network events: recorded, not blocking.
_CLIENT_ERROR_CONSOLE_ECHO = re.compile(r"Failed to load resource:.*status of 4\d\d")


@dataclass(frozen=True)
class ProductGuardReport:
    console_errors: tuple[dict[str, Any], ...] = ()
    page_errors: tuple[dict[str, Any], ...] = ()
    server_errors: tuple[dict[str, Any], ...] = ()
    failed_requests: tuple[dict[str, Any], ...] = ()
    client_error_count: int = 0

    @property
    def violation_count(self) -> int:
        return (
            len(self.console_errors)
            + len(self.page_errors)
            + len(self.server_errors)
            + len(self.failed_requests)
        )

    @property
    def passed(self) -> bool:
        return self.violation_count == 0

    def summary(self) -> str:
        parts = []
        if self.console_errors:
            parts.append(f"{len(self.console_errors)} console error(s)")
        if self.page_errors:
            parts.append(f"{len(self.page_errors)} page error(s)")
        if self.server_errors:
            parts.append(f"{len(self.server_errors)} HTTP 5xx response(s)")
        if self.failed_requests:
            parts.append(f"{len(self.failed_requests)} failed request(s)")
        if not parts:
            return "no blocking product issues detected"
        return "product guard blocked the run: " + ", ".join(parts)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "violation_count": self.violation_count,
            "console_errors": list(self.console_errors),
            "page_errors": list(self.page_errors),
            "server_errors": list(self.server_errors),
            "failed_requests": list(self.failed_requests),
            "client_error_count": self.client_error_count,
        }


def evaluate_product_guard(resources: dict[str, Any] | None) -> ProductGuardReport:
    resources = resources or {}
    console_events = _event_list(resources.get("console_events"))
    page_errors = _event_list(resources.get("page_errors"))
    network_events = _event_list(resources.get("network_events"))

    console_errors = [
        event
        for event in console_events
        if str(event.get("type") or "") in {"error", "assert"}
        and not _CLIENT_ERROR_CONSOLE_ECHO.search(str(event.get("text") or ""))
    ]
    server_errors = []
    failed_requests = []
    client_error_count = 0
    for event in network_events:
        if str(event.get("type") or "") == "request_failed":
            if str(event.get("failure") or "") in IGNORED_REQUEST_FAILURES:
                continue
            failed_requests.append(event)
            continue
        status = _event_status(event)
        if status >= BLOCKING_HTTP_STATUS:
            server_errors.append(event)
        elif status >= 400:
            client_error_count += 1

    return ProductGuardReport(
        console_errors=tuple(console_errors[:MAX_RECORDED_VIOLATIONS]),
        page_errors=tuple(page_errors[:MAX_RECORDED_VIOLATIONS]),
        server_errors=tuple(server_errors[:MAX_RECORDED_VIOLATIONS]),
        failed_requests=tuple(failed_requests[:MAX_RECORDED_VIOLATIONS]),
        client_error_count=client_error_count,
    )


def _event_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _event_status(event: dict[str, Any]) -> int:
    try:
        return int(event.get("status") or 0)
    except (TypeError, ValueError):
        return 0
