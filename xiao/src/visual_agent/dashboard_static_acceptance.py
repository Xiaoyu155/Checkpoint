from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


REQUIRED_INDEX_TEXT = (
    "工作台总览",
    "托管控制台",
    "功能导航",
    "开发入口",
    "后端节省",
    "套餐额度",
    "订阅额度",
    "中转站",
    "流式工作任务",
    "自己的付费模型网关",
    "产品可用度",
    "推广就绪",
    "邮箱身份",
    "登录与付费",
    "Supabase Auth",
    "Google OAuth",
    "Stripe Billing",
    "Stripe Customer Portal",
    "月预算 USD",
    "新建任务",
    "任务列表",
    "工作痕迹",
    "派发执行",
    "Pacer 可观测性",
    "Actual added",
    "Raw ledger",
    "逐轮 token",
    "Agent 父子树",
    "Tool / MCP 时间线",
    "闭环证据",
)

REQUIRED_APP_TEXT = (
    "requirement_contract",
    "_requirementContractBlock",
    "_requirementContractDetail",
    "_intakePolicyLabel",
    "focusWorkbenchPanel",
    "switchWorkbenchView",
    "需求合同",
    "收口方式",
    "PANEL_STATE_PREFIX",
    "/api/goal/refine",
    "startMission",
    "saveRelayConfig",
    "refreshRelayPanel",
    "renderSubscriptionQuotaPanel",
    "renderCoreReadinessPanel",
    "renderPromotionReadinessPanel",
    "saveProfileConfig",
    "saveCommercialConfig",
    "_commercialConfigBody",
    "/api/commercial-config",
    "connectEventStream",
    "OBSERVABILITY_API",
    "loadObservabilityLaunches",
    "_loadObservabilityDetail",
    "_loadObservabilityTimeline",
    "Reasoning 已包含于 Output",
)

REQUIRED_STYLE_TEXT = (
    ".detail-contract",
    ".detail-contract-row",
    ".readiness-check",
    ".cockpit-layout",
    ".ops-rail",
    ".commercial-grid",
    ".core-readiness-card",
    ".workbench-view:not(.is-active-view)",
    "flex-direction:column",
    "width:min(100%,1080px)",
    ".observability-workbench",
    ".obs-token-stack",
    ".obs-agent-tree",
    ".obs-timeline",
    ".obs-evidence-grid",
)


def build_dashboard_static_acceptance(*, static_dir: str | Path | None = None, run_node_check: bool = True) -> dict[str, Any]:
    root = Path(static_dir).expanduser().resolve() if static_dir else Path(__file__).resolve().parent / "dashboard" / "static"
    index_html = root / "index.html"
    app_js = root / "app.js"
    style_css = root / "style.css"
    checks: list[dict[str, Any]] = []

    index_text = _read_text(index_html, checks, check_id="index_exists")
    app_text = _read_text(app_js, checks, check_id="app_exists")
    style_text = _read_text(style_css, checks, check_id="style_exists")

    checks.extend(_contains_checks("index_text", index_text, REQUIRED_INDEX_TEXT))
    checks.extend(_contains_checks("app_contract", app_text, REQUIRED_APP_TEXT))
    checks.extend(_contains_checks("style_contract", style_text, REQUIRED_STYLE_TEXT))
    if run_node_check:
        checks.append(_node_check(app_js))

    failed = [item for item in checks if item["status"] == "failed"]
    warnings = [item for item in checks if item["status"] == "warning"]
    return {
        "schema_version": 1,
        "status": "failed" if failed else "success",
        "static_dir": str(root),
        "summary": {
            "checks": len(checks),
            "failed": len(failed),
            "warnings": len(warnings),
        },
        "checks": checks,
    }


def _read_text(path: Path, checks: list[dict[str, Any]], *, check_id: str) -> str:
    if not path.exists():
        checks.append({"id": check_id, "status": "failed", "message": f"Missing file: {path}"})
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        checks.append({"id": check_id, "status": "failed", "message": f"Could not read {path}: {exc}"})
        return ""
    checks.append({"id": check_id, "status": "success", "message": str(path)})
    return text


def _contains_checks(prefix: str, text: str, required: tuple[str, ...]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for needle in required:
        ok = needle in text
        checks.append(
            {
                "id": f"{prefix}:{needle}",
                "status": "success" if ok else "failed",
                "message": f"Found required text: {needle}" if ok else f"Missing required text: {needle}",
            }
        )
    return checks


def _node_check(app_js: Path) -> dict[str, Any]:
    node = shutil.which("node")
    if not node:
        return {
            "id": "app_js_syntax",
            "status": "warning",
            "message": "node is not installed; skipped app.js syntax check.",
        }
    completed = subprocess.run(
        [node, "--check", str(app_js)],
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode == 0:
        return {"id": "app_js_syntax", "status": "success", "message": "node --check app.js passed."}
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    return {
        "id": "app_js_syntax",
        "status": "failed",
        "message": detail[-1] if detail else f"node --check exited {completed.returncode}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Pacer dashboard static workbench acceptance signals.")
    parser.add_argument("--static-dir", default=None, help="Override dashboard static directory.")
    parser.add_argument("--no-node-check", action="store_true", help="Skip node --check app.js.")
    args = parser.parse_args(argv)
    payload = build_dashboard_static_acceptance(static_dir=args.static_dir, run_node_check=not args.no_node_check)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
