# Visual Agent 下一阶段开发交接

> 日期：2026-06-07  
> 目的：在上下文过长时，给新窗口/新会话一个可直接接手的开发计划。  
> 相关主计划：`TECHNICAL_PLAN_V2.md`

## 当前状态

V2 主线已经完成了大部分“代码上下文生成 → workflow 合成 → 质量评分 → verify_implementation → VS Code 状态展示”的闭环。

最近已验证结果：

```text
python -m pytest
921 passed, 6 skipped

npm test  # vscode-extension
passed
```

本轮开始前已按用户要求先提交所有 dirty 代码，稳定基线：

```text
51961fe V2 code-context verification complete: workflow synthesis, quality gate, negative testing, e2e samples
```

不要用 `git reset --hard` 或 `git checkout --` 清理用户未明确要求回滚的内容。

## 已完成的 V2 能力

已完成模块：

- `src/visual_agent/context_ingestion.py`
- `src/visual_agent/workflow_synthesis.py`
- `src/visual_agent/workflow_quality.py`
- `src/visual_agent/git_diff.py`
- `src/visual_agent/verification_status.py`
- VS Code 扩展状态展示：`vscode-extension/src/agentStatus.ts`

已完成工具/命令：

- MCP `generate_workflow_from_context`
- MCP `verify_implementation`
- CLI `generate-from-diff`
- CLI `verify-impl`

已完成语义摄取：

- HTML 表单：label/input/button/form action
- React/JSX：input、button、navigate/router.push、成功/错误文本、模板变量展示
- Vue：template 表单、router.push、成功/错误文本
- Django/FastAPI/Flask：route、redirect、messages/json 成功文本
- 混合前后端 diff：前端字段 + 后端 success state 合并
- 基础验证规则：required、email、min/max、min/max length、pattern

已完成合成/质量能力：

- 静态置信度 `>= 0.5` 走确定性 workflow 合成
- 低置信度优先 LLM 兜底，无 SDK/配置时静态 fallback
- 成功路径断言：URL/text wait/assert
- 已知错误文案防护：`assert_text_contract forbidden_any`
- 动态数据展示：非敏感字段同名模板变量生成 `assert_text text_from: input.<field>`
- 敏感字段不会被回显断言验证
- 质量评分识别 success/error/data display/business assertions
- 质量低于默认阈值 `0.6` 时 `verify_implementation` 返回 `needs_workflow_improvement`
- `timeout_seconds` 超时返回 `timeout`

已完成 inputs 能力：

- 保存 workflow 时自动写 `inputs/<workflow>_inputs.json`
- 非敏感字段使用安全示例值
- password/token/secret/api key 等敏感字段保持空字符串
- 示例值会适配基础 validation rules
- `verify_implementation` 在未收到显式 `inputs` 时，自动使用本次生成的 inputs 模板
- 返回/状态文件包含 `inputs_path`、`inputs_source`
- validation rules 已生成 draft-only `negative_input_cases` 和独立 `negative_workflow_yaml` / `negative_workflow_path`
- 默认成功 workflow 和 `verify_implementation` 不执行 negative workflow 草案，敏感字段只使用空安全值
- `verify_implementation` / CLI `verify-impl` 已支持显式 `run_negative` / `--run-negative`，仅成功路径通过后运行 negative draft；无 parsed error oracle 时返回 `negative_verification.status=skipped`
- negative workflow 生成结果已包含 `negative_workflow_ready` / `negative_workflow_reason`；无 parsed error oracle 时在生成阶段标记 `no_negative_oracle`
- negative workflow 生成结果和执行报告已包含 `negative_workflow_reset_strategy=fresh_observe_per_case`，每个 negative case 从 fresh `observe_browser` entry URL 开始
- negative oracle 提取会忽略混入 success 关键词的常驻文本；无 oracle 的 skipped 报告会返回 `next_action`
- negative report 已补 `next_action` 和 run artifact hints：有 run_id 时返回 `report_path`、`report_markdown_path`、`report_hint`
- negative 生成结果和执行报告已返回 `negative_oracles` / `oracles`，包含 parsed error text 和 source，便于诊断 oracle 来源
- `negative_oracles` / `oracles` 的 text/source 已统一脱敏，避免错误文案携带 secret
- `.vscode-agent-status.json` 已保留 compact `negative_verification` 摘要，CLI markdown 也展示 negative reset/oracle/report/next action
- `normalize_verification_status()` 已支持类型化读取 `negative_verification`，包括 run artifact、reset strategy、steps 和脱敏 oracle text/source
- VS Code 扩展已读取 `negative_verification`，输出面板展示 status/reason/reset/oracles/report/next action，侧边栏展示 negative 摘要；negative fail/timeout 会提升扩展状态严重级别
- `.vscode-agent-status.json` 已写入主验证 `report_hint`，VS Code 和下一轮代理可直接从状态文件定位 `get_run_report` 用法
- 新增 `agent-status` CLI，可将 `.vscode-agent-status.json` 输出为 JSON 或 VS Code 同款 markdown
- 新增 `scripts/code_context_verify_demo.ps1`，一条命令演示 git diff -> `generate-from-diff --dry-run` -> `verify-impl --run-profile dry-run` -> status markdown
- VS Code 扩展新增 `Visual Agent: Verify Current Change` 命令，直接输入任务描述和 base URL/fixture 后调用 `verify-impl --format markdown`，并自动刷新状态/展示最新 verification
- React/JSX parser 已支持常见字段组件 `<TextField>`、`<Field>`、`<Form.Field>`、`<Select>`、`<Textarea>`，不再只识别原生 input 和 `*Input` 组件
- React/JSX parser 已将常见非 submit 动作按钮（delete/remove/archive/confirm/save/create/update）纳入 submit action 候选，并把 deleted/removed/archived 文案识别为成功态
- Workflow 合成已支持 destructive action + confirm action 的双点击确认序列，例如 `Delete Ada` 后继续点击 `Confirm Delete`
- 真实前端样例 e2e 已扩展到 Next.js、React 复杂组件/表格展示、React 列表行删除确认弹窗、Vue、Remix 五条 code-context verify dry-run 链路，覆盖 matched data display、无输入动作流、确认弹窗、生成 inputs、report artifacts 和状态文件落盘

已完成状态/诊断：

- `.vscode-agent-status.json` 写入 pass/fail/timeout/needs_workflow_improvement
- 包含 quality gaps/recommendation、failed_step、next_action、report paths、report hint
- 包含 `semantic_summary`
- `semantic_summary` 包含 framework、confidence、generation_method、field counts、validation counts、data display names、warnings
- VS Code 状态栏/侧边栏读取并展示状态，侧边栏和输出面板已展示 negative verification 摘要

## 当前未完全收口项

V2 代码上下文验证主线已经收口。本轮继续推进 Phase 1 dogfooding，并已完成：

- Task 1.1：`init-workspace --auto-detect`，自动识别项目框架并生成对应 demo fixture/workflow；`workspace_status` 返回 `framework_hint`。
- Task 1.4：`generate-from-diff --format markdown` 和 `verify-impl --format markdown` 在非 JSON 模式下打印 parse warnings，JSON payload 不变。
- Task 1.2：`verification_status.next_action` 覆盖 `fail` / `timeout` / `needs_workflow_improvement`，失败步骤给出可执行修复建议。
- Task 1.3：新增 `semi-auto` run profile，并同步 CLI、MCP schema、repair verify、external sample profile 校验。

本轮已通过：

```text
python -m pytest tests/test_mcp_server.py tests/test_verification_status.py tests/test_workflow.py::test_run_profile_semi_auto_policy_allows_medium_risk_actions tests/test_workflow.py::test_semi_auto_prompts_before_mutating_action
83 passed
python -m pytest
921 passed, 6 skipped
npm test --prefix vscode-extension
passed
visual-agent mcp-smoke
success
```

随后已完成 Phase 2 Task 2.1：

- `generate-from-diff --audit-log <path>` 会追加 JSONL parser 审计记录。
- 审计字段覆盖 framework/confidence/method、字段列表、submit actions、success states、unmatched data displays、warnings、quality score、workflow/changed files。
- 审计文件父目录自动创建，连续运行追加多行有效 JSON。

已通过：

```text
python -m pytest tests/test_cli.py tests/test_context_workflow_synthesis.py -q
51 passed
python -m pytest -q
921 passed, 6 skipped
npm test --prefix vscode-extension
passed
```

随后已完成 Phase 2 Task 2.3：

- 新增 `workflow-lint <workflow.yaml>` / `workflow-lint --file <workflow.yaml>`。
- 默认 markdown 输出 workflow 状态、quality score、validation issues、quality gaps、suggestions。
- `--format json` 输出结构化 `quality`、`validation.issues`、`suggestions`。
- validation 失败或 quality score 低于默认阈值 `0.6` 时退出码为 1。
- 新增 `workflow-add-step --workflow <path> --after <step_id> --action <action>`。
- 支持 `--text`、`--url-contains`、`--timeout-ms`、`--observation`、`--dry-run`、`--format json/markdown`。
- 会自动生成不冲突 step id，写回后运行 workflow validation；dry-run 模式只返回预览 YAML。

已通过：

```text
python -m pytest tests/test_cli.py tests/test_workflow_quality.py tests/test_validation.py -q
59 passed
python -m pytest tests/test_cli.py tests/test_validation.py tests/test_workflow_quality.py -q
62 passed
```

随后已完成 Phase 2 Task 2.2：

- React/JSX parser 支持组件库字段：`Select`、`DatePicker`、`InputNumber`、`Switch checked={...}`、`Upload`。
- 字段类型分别归一为 `select`、`date`、`number`、`boolean`、`file`。
- AntD Modal 的 `okText` / `confirmText` / `title` 会作为 confirm submit action 候选，用于 destructive + confirm 双点击合成。
- JSX data display 提取已过滤状态/事件类 prop binding，避免 `open={confirmOpen}` 这类状态变量进入 unmatched displays。
- 已新增 5 条组件字段单元参数化测试、1 条 Modal 单元测试、6 条 e2e 参数化样例。

已通过：

```text
python -m pytest tests/test_context_workflow_synthesis.py tests/e2e/test_e2e_context_verification.py tests/test_cli.py tests/test_workflow_quality.py -q
74 passed
```

V2 既有验证结果：

已通过：

```text
python -m pytest tests/e2e/test_e2e_context_verification.py tests/test_cli.py tests/test_mcp_server.py tests/test_verification_status.py tests/test_context_workflow_synthesis.py tests/test_workflow_quality.py -q
136 passed

python -m pytest tests/ -q --tb=short
921 passed, 6 skipped

npm test --prefix vscode-extension
passed
```

此前 Roadmap Phase 6 状态：

- `src/visual_agent/licensing.py` 已从占位推进为可读取本地/env license 元数据。
- `agent_session.json` 已记录 `runs_this_month`、`cloud_runs_used`、`usage_reset_date`。
- `context-snapshot` / MCP `get_session_context` 已展示 usage 摘要。
- `usage-status` CLI 已输出 usage、license tier 和 feature access。
- `run_remote_workflow()` 已支持注入 remote client；只有返回 `status: success` 时才记录 `cloud_runs_used`。
- 默认云端入口仍是 `NotImplementedError`，失败/异常不增加 cloud usage。
- `usage-status` 已展示 remote config readiness：endpoint、org、api key present、blockers、`network_probe: not_run`。
- `build_remote_workflow_request()` 已提供 remote request dry-run payload，包含脱敏 inputs 摘要。
- `usage-status --format json` 已包含 `remote_request_preview`。
- `remote_client_from_env()` 已提供 adapter 草案；默认无 transport 时 blocked，不发网络请求。
- `filter_remote_workflow_response()` 已限制响应字段并脱敏 message。
- `cloud-run-plan` CLI 已输出 remote request 和 adapter diagnostic；不读取 inputs 文件内容，不发网络请求。
- `cloud-run` CLI 已提供显式 `--execute` 开关；默认仍只 plan，不发网络；未显式选择 transport 时即使 `--execute` 也返回 blocked，不记录 `cloud_runs_used`。
- `cloud-run --execute --transport http` 已提供显式 HTTP transport 壳；endpoint/key 缺失时先 blocked，不发网络；HTTP 超时/失败不记录 `cloud_runs_used`。
- HTTP transport 已覆盖 401/403 -> `blocked`、其他 4xx/5xx -> `failed`、非 JSON/非对象响应 -> `failed`，错误 body/message 走脱敏且不记录 `cloud_runs_used`。
- HTTP transport 已支持可配置 retry/backoff；仅 429 和 5xx 会重试，4xx 不重试，最终 success 才记录 `cloud_runs_used`。
- 远端响应过滤已保留 `remote_schema_version`，并确认 queued/running/blocked/failed/unknown 不计 `cloud_runs_used`；未知 status 规范化为 `unknown`。
- `require_feature()` 此前保持非阻断占位；Phase 3 Task 3.2 已对 `cloud_run` 启用 feature gate，并通过 workspace usage 实现 free 5 次/月 quota。
- 已补 `tests/test_licensing.py`、`tests/test_session.py`、`tests/test_cli.py` 覆盖 license/usage。

随后已完成 Phase 3 Task 3.1 最小 cloud-server：

- 新增 `src/visual_agent/cloud_server.py`，使用标准库 `ThreadingHTTPServer`。
- 新增 CLI：`python -m visual_agent.cli cloud-server --workspace-root .agent-workspace --host 127.0.0.1 --port 7890 --run-profile dry-run`。
- 端点：`GET /v1/health`、`POST /v1/run`、`GET /v1/runs`、`GET /v1/run/{run_id}`。
- `POST /v1/run` 支持现有 `cloud-run --execute --transport http` 发出的 `workflow_name` / `workspace` / `run_profile` 请求，也预留 `workflow_yaml` 请求。
- 本地服务端执行 workspace workflow 后返回 `status`、`run_id`、`report_url`、`steps_passed`、`steps_total`。
- `GET /v1/runs` 会返回 workspace report index 的 compact 列表，支持 `limit`、`offset`、`status`、`workflow`、`failed_only=true`，并返回 `next_offset` / `has_more`；`GET /v1/run/{run_id}` 会读取持久化 workspace report detail，server 重启后仍能查询已有报告。
- cloud-server 报告查询复用历史报告 tier gate：free tier 旧报告 detail 返回 HTTP 403 + `status: upgrade_required`，列表中过滤旧报告。
- cloud-server 已支持可选鉴权：配置 `--api-key` / `--api-key-env` 后，非 health 端点要求 `Authorization: Bearer <token>`；配置 `--required-org` 后还要求匹配 `X-Visual-Agent-Org`。启动输出只展示 auth/org 是否启用，不打印 token。
- `cloud-run --execute --transport http` 已能打通本地 cloud-server。
- Phase 3 Task 3.2 已完成：`cloud_run` 已启用真实 feature/quota gate；free tier 支持 5 次/月，超额后在发起 transport 前返回结构化 `status: upgrade_required` / `reason: quota_exceeded`，pro/team/enterprise 继续无限云端执行。
- `usage-status` 已展示 `cloud_run_quota`，markdown 输出 cloud run limit/remaining。
- 历史报告查询已启用 tier gate：free tier 只能查询最近 7 天 workspace reports；旧报告会在列表/索引中过滤，并在 detail/MCP artifact 查询时返回 `status: upgrade_required` / `reason: history_window_exceeded`。
- pro/team/enterprise tier 可查询无限历史报告；`workflow_history_unlimited` 已调整为 pro feature。
- `FeatureGatedError` 现在包含 `feature`、`required_tier`、`current_tier`；`run_remote_workflow()` 捕获该错误并返回可被 CLI/MCP 消费的升级提示，不影响本地执行和 `generate-from-diff`。

已通过：

```text
python -m pytest tests/test_cloud_server.py tests/test_cli.py::test_cloud_run_cli_execute_http_calls_local_cloud_server tests/test_cli.py::test_cloud_run_cli_execute_http_without_config_blocks_without_network -q
5 passed
python -m pytest tests/test_licensing.py -q
40 passed
python -m pytest tests/test_cli.py tests/test_cloud_server.py tests/test_licensing.py tests/test_session.py -q
92 passed
python -m pytest tests/test_workspace.py tests/test_mcp_server.py tests/test_cli.py tests/test_licensing.py -q
172 passed
python -m pytest tests/test_cli.py tests/test_cloud_server.py tests/test_licensing.py tests/test_session.py tests/test_workspace.py tests/test_mcp_server.py -q
202 passed
```

下一位接手者建议先做：

```powershell
cd "D:\longxia agent"
python -m pytest tests/test_cli.py tests/test_cloud_server.py tests/test_licensing.py tests/test_session.py tests/test_context_workflow_synthesis.py tests/test_workflow_quality.py tests/test_validation.py tests/test_mcp_server.py tests/test_verification_status.py tests/test_workflow.py::test_run_profile_semi_auto_policy_allows_medium_risk_actions tests/test_workflow.py::test_semi_auto_prompts_before_mutating_action
npm test --prefix vscode-extension
python -m pytest
```

如果全量测试数量变化，更新 `DEVELOPMENT_LOG.md` 的测试计数。

## 下一阶段优先级

### 阶段性暂停点

V2 code-context verification 主线可以阶段性暂停。当前已覆盖：

- code diff -> workflow 生成 -> quality gate -> `verify-impl` -> report/status
- VS Code 状态展示和一键 verify 当前改动
- negative verification draft/显式执行入口/状态展示
- runnable demo script
- Next.js、React 复杂组件/表格展示、React 列表行删除确认弹窗、Vue、Remix 真实样例 e2e

后续候选：

- 多步骤 wizard 表单
- 更复杂组件库适配，例如更多 AntD/MUI 组件和真实项目误判样本
- 真实项目样本集和误判审计
- 云端执行/商业化链路

## 推荐下一步执行顺序

1. 跑全量 `python -m pytest` 和 `npm test --prefix vscode-extension`，确认 cloud-server 改动没有长尾回归。
2. dogfooding `cloud-server` + `cloud-run --execute --transport http`，确认 CI/另一进程触发本地浏览器执行的链路稳定；测试环境如需执行云端链路，设置 `VISUAL_AGENT_LICENSE_TIER=pro`。
3. 下一步可补 cloud-server 报告下载 endpoint，或把鉴权配置写入示例 CI 文档。
4. 每轮结束更新 `DEVELOPMENT_LOG.md` / `README_MCP.md` / `NEXT_DEVELOPMENT_HANDOFF.md`。

## 常用验证命令

```powershell
python -m pytest tests/test_context_workflow_synthesis.py tests/test_workflow_quality.py tests/test_mcp_server.py tests/test_cli.py tests/test_verification_status.py
npm test --prefix vscode-extension
python -m pytest
```

如果只改 VS Code 扩展：

```powershell
cd vscode-extension
npm test
```

## 交接注意事项

- 不要回滚不相关 dirty 文件。
- 不要把敏感字段写进 workflow YAML 或 inputs 示例。
- `verify_implementation` 默认质量阈值保持 `0.6`。
- MCP 响应要继续控制大小，长内容只给 summary/path/hint。
- 所有新增字段要同步三处：
  - MCP/CLI payload
  - `verification_status.py`
  - `vscode-extension/src/agentStatus.ts`
