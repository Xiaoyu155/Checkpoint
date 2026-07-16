from __future__ import annotations

from visual_agent.dashboard_static_acceptance import build_dashboard_static_acceptance


def test_dashboard_static_acceptance_passes_current_assets() -> None:
    payload = build_dashboard_static_acceptance(run_node_check=False)

    assert payload["status"] == "success"
    ids = {item["id"] for item in payload["checks"]}
    assert "app_contract:requirement_contract" in ids
    assert "app_contract:_requirementContractBlock" in ids
    assert "app_contract:_intakePolicyLabel" in ids
    assert "app_contract:PANEL_STATE_PREFIX" in ids
    assert "style_contract:flex-direction:column" in ids
    assert "index_text:后端节省" in ids
    assert "index_text:托管控制台" in ids
    assert "index_text:功能导航" in ids
    assert "index_text:套餐额度" in ids
    assert "index_text:中转站" in ids
    assert "index_text:产品可用度" in ids
    assert "index_text:登录与付费" in ids
    assert "index_text:Supabase Auth" in ids
    assert "index_text:Google OAuth" in ids
    assert "index_text:Stripe Billing" in ids
    assert "index_text:Pacer 可观测性" in ids
    assert "index_text:Raw ledger" in ids
    assert "index_text:Agent 父子树" in ids
    assert "app_contract:saveRelayConfig" in ids
    assert "app_contract:saveCommercialConfig" in ids
    assert "app_contract:renderCoreReadinessPanel" in ids
    assert "app_contract:/api/commercial-config" in ids
    assert "app_contract:renderSubscriptionQuotaPanel" in ids
    assert "app_contract:loadObservabilityLaunches" in ids
    assert "app_contract:_loadObservabilityTimeline" in ids
    assert "style_contract:.cockpit-layout" in ids
    assert "style_contract:.ops-rail" in ids
    assert "style_contract:.commercial-grid" in ids
    assert "style_contract:.core-readiness-card" in ids
    assert "style_contract:.detail-contract" in ids
    assert "style_contract:.observability-workbench" in ids
    assert "style_contract:.obs-token-stack" in ids


def test_dashboard_static_acceptance_reports_missing_contract_text(tmp_path) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("工作台总览 后端节省 套餐额度 新建任务 任务列表 工作痕迹 派发执行", encoding="utf-8")
    (static_dir / "app.js").write_text("function startMission(){} // /api/goal/refine", encoding="utf-8")
    (static_dir / "style.css").write_text(".detail-contract{}", encoding="utf-8")

    payload = build_dashboard_static_acceptance(static_dir=static_dir, run_node_check=False)

    assert payload["status"] == "failed"
    failed = [item for item in payload["checks"] if item["status"] == "failed"]
    assert any("requirement_contract" in item["message"] for item in failed)
    assert any(".detail-contract-row" in item["message"] for item in failed)
