from __future__ import annotations

from typing import Any

from .mcp_common import require_str, require_workspace


def run_browser_smoke_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .browser_smoke import run_browser_smoke

    workspace = require_workspace(args)
    return {
        "workspace": str(workspace.root),
        **run_browser_smoke(
            url=require_str(args, "url"),
            output_dir=workspace.root / "browser-smoke-runs",
            headed=bool(args.get("headed", False)),
            timeout_ms=int(args.get("timeout_ms") or 10_000),
            min_text_length=int(args.get("min_text_length") or 1),
            min_interactive=int(args.get("min_interactive") or 0),
            expect_text=[str(item) for item in args.get("expect_text", []) if str(item)],
            expect_url_contains=[str(item) for item in args.get("expect_url_contains", []) if str(item)],
            fill=[str(item) for item in args.get("fill", []) if str(item)],
            fill_selector=[str(item) for item in args.get("fill_selector", []) if str(item)],
            click_text=str(args.get("click_text") or "") or None,
            click_selector=str(args.get("click_selector") or "") or None,
            require_change_after_click=bool(args.get("require_change_after_click", False)),
            wait_for_text_after=[str(item) for item in args.get("wait_for_text_after", []) if str(item)],
            wait_for_url_contains_after=[str(item) for item in args.get("wait_for_url_contains_after", []) if str(item)],
            wait_timeout_seconds=float(args.get("wait_timeout_seconds") or 5.0),
            expect_text_after=[str(item) for item in args.get("expect_text_after", []) if str(item)],
            expect_url_contains_after=[str(item) for item in args.get("expect_url_contains_after", []) if str(item)],
            save_workflow=(workspace.root / str(args.get("save_workflow"))).resolve() if str(args.get("save_workflow") or "").strip() else None,
            overwrite_workflow=bool(args.get("overwrite_workflow", False)),
        ),
    }


def run_browser_smoke_suite_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .browser_smoke_suite import run_browser_smoke_suite

    workspace = require_workspace(args)
    suite_file = (workspace.root / require_str(args, "suite_file")).resolve()
    return {
        "workspace": str(workspace.root),
        **run_browser_smoke_suite(
            suite_file,
            output_dir=workspace.root / "browser-smoke-suite-runs",
            headed=True if bool(args.get("headed", False)) else None,
        ),
    }
