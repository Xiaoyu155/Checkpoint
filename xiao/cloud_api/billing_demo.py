from __future__ import annotations

import argparse
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from .main import create_app


_DEMO_PACKAGES = (
    '[{"id":"starter","name":"入门额度包","description":"Pacer API 体验额度",'
    '"amount_fen":100,"credit_microusd":1000000},'
    '{"id":"standard","name":"标准额度包","description":"Pacer API 标准额度",'
    '"amount_fen":5000,"credit_microusd":55000000}]'
)


class DemoWechatNativeClient:
    """Local-only provider that completes an order after two provider queries."""

    def __init__(self) -> None:
        self.config = SimpleNamespace(
            app_id="wx0000000000000000", mch_id="1900000109"
        )
        self._orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_order(
        self,
        *,
        out_trade_no: str,
        description: str,
        amount_fen: int,
        expires_at: float,
    ) -> dict[str, str]:
        with self._lock:
            self._orders[out_trade_no] = {
                "amount_fen": int(amount_fen),
                "queries": 0,
                "trade_state": "NOTPAY",
            }
        return {"code_url": f"weixin://wxpay/bizpayurl?pr=demo-{out_trade_no}"}

    def query_order(self, out_trade_no: str) -> dict[str, Any]:
        with self._lock:
            order = self._orders[out_trade_no]
            if order["trade_state"] == "NOTPAY":
                order["queries"] += 1
                if order["queries"] >= 2:
                    order["trade_state"] = "SUCCESS"
            state = str(order["trade_state"])
            amount_fen = int(order["amount_fen"])
        payload: dict[str, Any] = {
            "appid": self.config.app_id,
            "mchid": self.config.mch_id,
            "out_trade_no": out_trade_no,
            "trade_type": "NATIVE",
            "trade_state": state,
            "amount": {"total": amount_fen, "currency": "CNY"},
        }
        if state == "SUCCESS":
            payload["transaction_id"] = f"4200000000{out_trade_no[-18:]}"
        return payload

    def close_order(self, out_trade_no: str) -> None:
        with self._lock:
            order = self._orders[out_trade_no]
            if order["trade_state"] == "SUCCESS":
                return
            order["trade_state"] = "CLOSED"


def build_demo_app(workspace_root: str | Path):
    root = Path(workspace_root).resolve()
    os.environ["PACER_GATEWAY_DB"] = str(root / "billing-demo.db")
    os.environ["PACER_WECHAT_CREDIT_PACKAGES_JSON"] = _DEMO_PACKAGES
    app = create_app(workspace_root=root, audit_log=root / "billing-demo-audit.jsonl")
    app.state.wechat_native_client = DemoWechatNativeClient()
    store = app.state.gateway_store
    tenant = store.create_tenant(name="Pacer 演示账户", initial_credit_microusd=0)
    api_key = store.create_api_key(
        tenant_id=str(tenant["id"]), name="Browser demo"
    )
    app.state.billing_demo_api_key = api_key["token"]
    return app


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local Pacer billing demo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace-root", default="")
    args = parser.parse_args(argv)
    root = (
        Path(args.workspace_root).expanduser().resolve()
        if args.workspace_root
        else Path(tempfile.mkdtemp(prefix="pacer-billing-demo-"))
    )
    app = build_demo_app(root)
    print(f"Billing demo: http://{args.host}:{args.port}/billing")
    print(f"Demo customer API key: {app.state.billing_demo_api_key}")
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
