# Pacer 关机交接存档（2026-07-20）

记录时间：2026-07-20 06:59（Asia/Shanghai）

## 当前结论

Pacer 托管、失败记忆兜底、五阶段任务闭环和微信 Native 额度购买已经完成本轮收口，
可以继续做小范围种子用户推广。当前工作区包含大量尚未提交的用户改动和本轮改动，
重启后不要执行 `git reset --hard`、`git checkout -- .` 或清理未跟踪文件。

邮箱账户本地能力已经落地为 SQLite 账户、验证码、会话和密码重置；生产邮箱服务、
域名 DNS 与合规资料仍是上线前外部事项，不能直接把现有 `pacer_sk_*` 客户 Key
机械拼成登录系统。

## 本轮已完成

### 长托管与本地记忆

- `pacer host status`、`doctor` 和预览不再触发 auto-resume；只有活跃 host tick 会恢复孤儿任务。
- Codex 探测确认账号恢复后会自动清理 `codex/openai` 配额失败缓存，并支持
  `pacer host doctor --clear-quota-cache` 手动清理。
- `aborted` 已纳入 background 终态；race settlement 改为 host tick 异步轮询，不再阻塞主循环。
- 同一个 mission 恢复前会刷新项目记忆，把停止原因、失败签名、历史证据和变更范围交给下一位 worker。
- host 仪表增加 `memory_fallback`，显示记忆选中数和是否已注入 worker。
- 五阶段证据链已落地：路由选择 -> 本地记忆 -> 托管执行 -> 强验收 -> 交付/Dogfood。

关键文件：

- `src/visual_agent/pacer_host.py`
- `src/visual_agent/provider_liveness.py`
- `src/visual_agent/chief_background.py`
- `src/visual_agent/chief_dispatch.py`
- `src/visual_agent/mission_journey.py`
- `docs/PACER_HOST.md`

### 微信 Native 额度购买

- 新增微信 API v3 RSA 请求签名、响应/回调验签、API v3 解密和平台证书刷新。
- 已实现 Native 下单、查单、关单、二维码、套餐、租户隔离、金额精确校验、原子入账和回调幂等。
- `/billing` 客户额度中心支持 API Key 连接、套餐选择、二维码、轮询到账、订单恢复和移动端布局。
- Docker 通过只读 `/run/secrets` 挂载商户密钥；Git 和 Docker build context 均排除真实密钥。
- 真实联调发现订单刚创建时关单接口可能瞬时返回 `ORDER_NOT_EXIST`；现在只对
  `ORDER_NOT_EXIST`、`SYSTEM_ERROR`、`FREQUENCY_LIMITED` 做最多三次短退避重试。
- 最终真实商户探针：1 分钱订单创建成功，主动查询为 `NOTPAY`，随后关单成功；未真实付款。

关键文件：

- `cloud_api/wechat_native.py`
- `cloud_api/gateway_routes.py`
- `cloud_api/gateway_store.py`
- `cloud_api/gateway_static/billing.html`
- `cloud_api/gateway_static/billing.css`
- `cloud_api/gateway_static/billing.js`
- `cloud_api/billing_demo.py`
- `examples/workflows/checkout/pacer_gateway_billing_acceptance.yaml`
- `docs/中转站_商业网关.md`

### MCP 与浏览器组合链路

- 修复 `run_browser_smoke` / `run_browser_smoke_suite` 在 MCP asyncio 事件循环中直接调用
  Playwright Sync API 的问题：两个处理器现在通过 `asyncio.to_thread` 执行。
- 修复“等待文本”在页面存在多个相同可见文本时触发 strict-mode 假失败的问题；等待语义改为
  至少一个匹配可见，点击和填充仍要求明确目标。
- 当前已经运行的旧 MCP 长进程可能缓存旧模块；关机重启后会自然加载新代码。

### 关机后继续补的运营闭环

- 新增管理员 `POST /api/gateway/admin/wechat-orders/{order_id}/reconcile`：主动查单并复用
  回调的严格校验和幂等入账，回调延迟时可恢复，不能由运营手填金额。
- Gateway 管理控制台新增“支付订单”视图和“查单恢复”按钮。
- 上线准备检查新增微信支付 readiness：不阻塞 API 网关本身，但会明确显示微信密钥/套餐是否可收款。
- 管理查单恢复与终态幂等回归已通过；支付专项当前为 `10 passed`。

关键文件：

- `src/visual_agent/mcp_server.py`
- `src/visual_agent/browser_smoke.py`
- `tests/test_mcp_server.py`
- `tests/test_browser_smoke.py`

运营补丁关键文件：

- `cloud_api/gateway_routes.py`
- `cloud_api/gateway_static/index.html`
- `cloud_api/gateway_static/gateway.js`
- `tests/test_cloud_gateway_billing.py`
- `docs/中转站_商业网关.md`

## 验证证据

- 托管、记忆、支付、MCP 合并回归：`367 passed`。
- MCP/浏览器完整模块：`210 passed`。
- 微信关单重试补丁后的支付专项：`15 passed`。
- Chromium 额度购买完整旅程：`1 passed`（连接、生成支付码、模拟到账、刷新恢复、移动端）。
- supervised Checkpoint：`Coverage: covered`，两个受影响工作流均为 L3 真实交互，`Verdict: PASS`。
- 运营控制台 `/gateway` 浏览器烟测：`success`，280 字可见内容、37 个交互控件。
- 邮箱账户专项：`3 passed`，覆盖验证码一次性消费、注册自动创建租户/API Key、HttpOnly 会话额度访问、退出和密码重置。
- 账户登录 UI 与旧 API Key 额度流程浏览器回归：`1 passed`；supervised Checkpoint 本轮仍为 `2/2` L3，`Verdict: PASS`。
- `compileall`、`node --check`、`docker compose config`、本轮相关 Ruff 均通过。

2026-07-26 收尾补跑：

- 全仓 `python -m pytest -q`：`2510 passed, 1 warning in 840.45s`。
- 全量 `python -m ruff check .`：`All checks passed!`。
- 商业支付、账户、MCP、browser smoke、provider liveness、host 定向回归：
  `263 passed in 120.25s`。
- `python scripts\release_matrix_preflight.py --format markdown`：`status=passed`，
  `release_ready=false`，15 个 managed sample 仍需真实 runner 执行。
- `docker compose -f docker-compose.gateway.yml config` 与 `python -m compileall cloud_api src scripts` 均通过。
- supervised Checkpoint：`Coverage: covered`，`pacer_gateway_billing_acceptance` 与
  `pacer_workbench_static_acceptance` 均为 L3 真实交互，`Verdict: PASS`。

最近的浏览器证据：

- `.agent-workspace/browser-smoke-runs/browser-smoke-20260719-181720-178284/screenshots/after-click.png`
- `.agent-workspace/billing-browser-evidence/01-connect-desktop.png`
- `.agent-workspace/billing-browser-evidence/02-packages-desktop.png`
- `.agent-workspace/billing-browser-evidence/03-pending-desktop.png`
- `.agent-workspace/billing-browser-evidence/04-paid-desktop.png`
- `.agent-workspace/billing-browser-evidence/05-paid-mobile.png`

## 敏感资料边界

- 真实微信配置位于 `D:\wechat-pay-native-organized-20260718`，目录包含真实密钥，禁止提交 Git、
  上传网盘、写进截图或复制到本存档。
- 本存档没有记录商户号、AppID、私钥、API v3 Key、平台公钥内容、支付链接、客户 API Key 或完整订单号。
- `cloud_api/secrets/` 只能放运行时 secret；除 README 外均已被 Git 忽略。

## 重启后第一步

```powershell
cd D:\助手codex\xiao
git status --short

# 定向回归
python -m pytest tests/test_wechat_native.py tests/test_cloud_gateway_billing.py `
  tests/test_cloud_gateway_billing_browser.py tests/test_browser_smoke.py `
  tests/test_mcp_server.py tests/test_provider_liveness.py tests/test_pacer_host.py -q

# 产品工作流验收
python -m visual_agent.cli codex-check `
  --workspace-root .agent-workspace `
  --repo-root . `
  --run-profile supervised
```

需要重新打开本地额度演示时：

```powershell
python -m cloud_api.billing_demo --port 8765
```

演示会生成新的本地客户 Key，只用于本机测试，不要把 Key 写入文档或对话。

## 仍需资料/后续事项

1. 邮箱注册已落地本地 SQLite 账户、验证码、会话和密码重置；上线前仍需产品域名、SMTP/邮件服务商、发信域名 DNS、验证码有效期、密码/MFA 策略、隐私条款、服务条款、注销与数据保留策略。生产环境不要开启 `PACER_ACCOUNT_DEV_CODES=1`。
2. 生产支付：部署域名与 HTTPS 回调、正式额度套餐 JSON、支付宝/Stripe（用户后续另行处理）、退款/冲销策略、日终账单对账负责人。
3. 中转站上游：具备 API 使用和转售许可的供应商账号、模型清单、价格、限额与故障切换合同。
4. 发布前：重启 MCP/Codex 进程，确认新进程加载本轮 `mcp_server.py`；再跑上述定向回归和 Checkpoint。
5. Git：本轮没有创建提交。重启后先查看 diff，按功能拆分提交，不要把真实密钥或运行时证据提交进去。

## 当前运行态（关机前）

- 本地额度演示此前运行在 `http://127.0.0.1:8765/billing`；关机后自然停止。
- 没有遗留 pytest 或 codex-check 进程。
- 有多个历史 MCP 长进程；关机后会结束，重启 Pacer/Codex 时会重新加载源码。
