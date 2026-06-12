from __future__ import annotations

import json
from typing import Any, Callable

from .models import to_jsonable
from .recorder import (
    BrowserRecordingError,
    record_browser_session,
    recorded_result_ok,
    recorded_result_to_dict,
    recorded_result_to_markdown,
    recording_failure_to_markdown,
)
from .workspace import open_workspace


WORKSPACE_RECORD_COMMANDS = {"workspace-record-browser"}


def handle_workspace_record_command(args: Any, *, recorder: Callable[..., Any] = record_browser_session) -> int:
    try:
        result = recorder(
            open_workspace(args.root),
            url=args.url,
            save_as=args.save_as,
            timeout_seconds=args.timeout_seconds,
            headed=not args.headless,
            assert_text=args.assert_text,
            auto_assert=not args.no_auto_assert,
            save_auth_state=args.save_auth_state,
            check=not args.no_check,
            preview_run=args.preview_run,
            overwrite=args.overwrite,
            queue_run=args.queue,
            queue_priority=args.queue_priority,
            queue_max_retries=args.queue_max_retries,
        )
    except BrowserRecordingError as exc:
        if args.format == "markdown":
            print(recording_failure_to_markdown(to_jsonable(exc.failure_report)))
        else:
            print(json.dumps(to_jsonable(exc.failure_report), ensure_ascii=False, indent=2))
        return 1
    payload = recorded_result_to_dict(result)
    if args.format == "markdown":
        print(recorded_result_to_markdown(to_jsonable(payload)))
    else:
        print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
    return 0 if recorded_result_ok(payload) else 1
