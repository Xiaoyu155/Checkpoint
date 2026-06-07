# Visual Agent 开发日志与新窗口交接

> 更新时间：2026-06-07  
> 仓库地址：https://github.com/Xiaoyu155/visual-agent  
> 当前分支：`main`  
> 当前工作区状态：Phase 1 dogfooding 改动已测试并推送
> 最新代码提交：`37f678d Sync semi-auto profile across MCP verification`

## 一句话定位

Visual Agent 是面向 Codex、Claude Code、Cursor 等 AI 编程助手的本地产品验收系统。它让 AI 不只是写代码和跑后端测试，还能观察界面、真实点击输入、读取页面状态、检查产品合同、验证业务流程，并输出 AI 可继续处理的失败诊断。

## 新窗口启动方式

在新 Codex 窗口里先执行：

```powershell
cd "D:\longxia agent"
git pull
pip install -e .[web,mcp,desktop]
python -m pytest tests/ -q --tb=short
```

上次全量测试结果：

```text
890 passed, 6 skipped
```

如果只是确认 SDK 和 CLI：

```powershell
python -c "from visual_agent import VisualSession, VisualLock, Target; print('OK')"
visual-agent --help
python -m visual_agent --help
```

## 最近关键提交

```text
7770102 Document context audit progress
93b7498 Add context parse audit log
00c82e7 Update development handoff after release validation
37f678d Sync semi-auto profile across MCP verification
2d9fa5c Add semi-auto workflow profile and workspace auto-detect
51961fe V2 code-context verification complete: workflow synthesis, quality gate, negative testing, e2e samples
061e6c1 Add Python SDK and plugin entrypoints
392de95 Improve real interaction guidance and window capture defaults
159c23f Add product state contracts and issue reporting
32b7655 Complete Codex check and compact report test coverage
958a165 Add Codex diff-aware checks and platform connect
ca2996b Add post-action observation and slow workflow filtering
972a656 Cache repeated workflow observations
3be0498 Add OCR text click and wait actions
```

## 2026-06-07 Phase 1 dogfooding 进展

已完成：

- `init-workspace --auto-detect --repo-root <path>`：扫描 `package.json` / `requirements.txt` / 典型源码文件，自动识别 `nextjs`、`react`、`vue`、`remix`、`django`、`fastapi`、`flask`、`html`，并在 workspace manifest/status 中写入 `framework_hint`。
- 自动检测到框架时，生成对应 `fixtures/<framework>_demo.html` 和 `workflows/<framework>_verification.yaml`，让新 workspace 直接有可运行示例。
- `generate-from-diff --format markdown`：非 JSON 模式下输出 framework/confidence/fields/quality 摘要，并在有 parse warnings 时打印醒目的 warning 列表；JSON 结构保持兼容。
- `verify-impl --format markdown`：parse warnings 改为多行列表，便于 Codex/用户直接读取。
- 新增 `semi-auto` run profile：observe/assert 自动执行，只在 mutating action 真实执行前暂停确认；CLI、MCP schema、repair verify、external sample profile 校验已同步。
- `verify-impl` / `.vscode-agent-status.json` 的 `next_action` 增强：覆盖 `fail`、`timeout`、`needs_workflow_improvement`，失败时按 `assert_text` / `wait_for_text` / `assert_browser_ready` 等动作给出具体修复建议。

本轮定向验证：

```text
python -m pytest tests/test_mcp_server.py tests/test_verification_status.py tests/test_workflow.py::test_run_profile_semi_auto_policy_allows_medium_risk_actions tests/test_workflow.py::test_semi_auto_prompts_before_mutating_action
83 passed
python -m pytest
890 passed, 6 skipped
npm test --prefix vscode-extension
passed
visual-agent mcp-smoke
success
```

## 2026-06-07 Phase 2 真实样本审计进展

已完成：

- `generate-from-diff --audit-log <path>`：每次生成后追加一行 JSONL 审计记录，用于后续定位 parser 误判。
- 新增 `src/visual_agent/context_audit.py`，审计字段包含 `task`、`framework`、`confidence`、`method`、`fields`、`submit_actions`、`success_states`、`unmatched_data_displays`、`warnings`、`quality_score`、`workflow_name`、`workflow_path`、`changed_files`。
- 审计日志父目录自动创建；连续多次运行会追加多行有效 JSON，不影响原 CLI JSON/YAML/markdown 输出结构。

本轮定向验证：

```text
python -m pytest tests/test_cli.py tests/test_context_workflow_synthesis.py -q
51 passed
python -m pytest -q
890 passed, 6 skipped
npm test --prefix vscode-extension
passed
```

## 已完成能力

### 1. 真实点击与输入

已支持：

- `click`
- `type`
- `paste`
- `press_key`
- `click_text`
- `wait_for_text`

浏览器页面优先走 Playwright DOM 原生能力：

- `observe_browser`
- DOM 自动捕捉
- selector/label/text/role 定位
- Playwright `locator.click()`
- Playwright `locator.fill()`

已真实验证：

```powershell
python -m visual_agent.cli run-workflow --file examples/workflows/form-fill/browser_form_workflow.yaml --inputs-file examples/inputs/demo_login.json --run-profile supervised --wait-lock
```

结果中出现：

```text
playwright filled
playwright clicked
```

说明浏览器真实输入/点击路径可用。

### 2. 窗口捕捉与自动最小化

视觉捕捉现在支持这些顶层参数：

- `window_title_candidates`
- `window_title_contains`
- `title_contains`

只要识别到目标窗口，默认行为是：

- 置前目标窗口
- 截图/识别
- 自动最小化目标窗口
- 恢复之前的前台窗口

如果要保持窗口打开：

```yaml
window:
  title_contains: "Target App"
  bring_to_front: true
  post_capture: keep
```

### 3. 多窗口/多项目协作

设计原则：

- 一个项目一个 `.agent-workspace`
- 多个 Codex 窗口可以对应多个项目
- 视觉步骤通过全局 VisualLock 串行，避免抢屏
- workspace queue lock 已修正为先拿锁再 preflight/run

重要修复：

- `workspace-run` 的 queue 模式现在先拿 workspace lock，再做 preflight 和运行。
- `WorkflowRuntime.run()` 的 run lock 也前移，避免短锁测试和真实多窗口下出现竞态。

### 4. 产品状态与产品合同

新增模块：

- `src/visual_agent/product_state.py`
- `src/visual_agent/product_issues.py`

新增 workflow 动作：

- `observe_state`
- `assert_no_error`
- `assert_product_contract`
- `assert_ai_response_quality`
- `request_api`

用途：

- 把页面观察结果转成结构化状态：按钮、输入框、弹窗、错误、加载、空状态
- 检查页面是否有错误文案或失败接口
- 检查产品质量合同：必须有哪些模块/按钮，禁止哪些旧入口
- 检查 AI 回复是否空、模板化、重复、是否关联用户问题/上下文
- 支持 UI + API 联合验收

产品问题报告命令：

```powershell
python -m visual_agent.cli workspace-product-issues --root .agent-workspace --format markdown --write
```

### 5. Codex 集成

已支持：

- `codex-check`
- `connect codex`
- `coding-agent-brief`
- `context-snapshot`
- `summarize-latest-failure`
- `run_verification`
- compact report
- audit log

Codex 使用建议：

```text
这个任务涉及 UI，不能只跑后端测试。先 dry-run 校验 workflow，再用 --run-profile supervised 跑 Visual Agent 真实点击/输入。浏览器页面优先用 observe_browser，不要优先 OCR。
```

### 6. Python SDK

新增文件：

- `src/visual_agent/sdk.py`
- `src/visual_agent/dsl.py`
- `src/visual_agent/plugins.py`
- `src/visual_agent/pytest_plugin.py`
- `src/visual_agent/__main__.py`

### 7. V2：代码上下文生成与实现验证闭环

新增模块：

- `src/visual_agent/context_ingestion.py`
- `src/visual_agent/workflow_synthesis.py`
- `src/visual_agent/workflow_quality.py`

新增 MCP 工具：

- `generate_workflow_from_context`
- `verify_implementation`

新增 CLI：

```powershell
python -m visual_agent.cli generate-from-diff --workspace-root .agent-workspace --task-description "Verify login redirects" --base-url http://localhost:3000/login --dry-run
python -m visual_agent.cli verify-impl --workspace-root .agent-workspace --task-description "Verify login redirects" --base-url http://localhost:3000/login --run-profile dry-run
```

已支持的上下文摄取：

- HTML 表单：label/input/button/form action、required/email/min/max/pattern 等基础验证规则
- React/JSX：input、button、navigate/router.push、成功/错误文本、模板变量展示、基础验证规则
- Vue：template 表单、router.push、成功/错误文本
- Django/FastAPI/Flask：route、redirect、messages/json 成功文本
- 混合前后端 diff：前端字段 + 后端 success state 合并

验证闭环行为：

- `code_changes` 可由调用方传入，也可省略后从 git diff 自动采集。
- 静态语义置信度 `>= 0.5` 时走确定性 workflow 合成。
- 静态语义置信度 `< 0.5` 时优先走 LLM 兜底；无 SDK/无配置时静态回退并返回 warning。
- 当模板变量和非敏感表单字段同名时，静态合成会追加 `assert_text text_from: input.<field>`，验证提交值被展示出来；敏感字段不会生成回显断言。
- 解析到已知错误文案时，静态合成会在成功路径后追加 `assert_text_contract forbidden_any`，确保成功态没有混入错误提示。
- 自动生成 inputs 模板时，会根据基础验证规则生成更贴近约束的示例值；敏感字段仍保持空字符串。
- `verify_implementation` 未收到显式 inputs 时，会自动使用本次生成的 inputs 模板，并在结果中返回 `inputs_source`。
- 解析到 validation rules 时，会生成 draft-only `negative_input_cases` 和独立 `negative_workflow_yaml` / `negative_workflow_path`；默认成功 workflow 和 `verify_implementation` 不执行该草案，敏感字段只使用空安全值。
- `verify_implementation` / CLI `verify-impl` 已支持显式 `run_negative` / `--run-negative`，仅成功路径通过后运行 negative draft；无 parsed error oracle 时返回 `negative_verification.status=skipped`。
- negative workflow 生成结果已包含 `negative_workflow_ready` / `negative_workflow_reason`；无 parsed error oracle 时在生成阶段标记 `no_negative_oracle`。
- negative workflow 生成结果和执行报告已包含 `negative_workflow_reset_strategy=fresh_observe_per_case`，每个 negative case 从 fresh `observe_browser` entry URL 开始。
- negative oracle 提取会忽略混入 success 关键词的常驻文本；无 oracle 的 skipped 报告会返回 `next_action`。
- negative report 已补 `next_action` 和 run artifact hints：有 run_id 时返回 `report_path`、`report_markdown_path`、`report_hint`。
- negative 生成结果和执行报告已返回 `negative_oracles` / `oracles`，包含 parsed error text 和 source，便于诊断 oracle 来源。
- `negative_oracles` / `oracles` 的 text/source 已统一脱敏，避免错误文案携带 secret。
- `.vscode-agent-status.json` 已保留 compact `negative_verification` 摘要，CLI markdown 也展示 negative reset/oracle/report/next action。
- `normalize_verification_status()` 已支持类型化读取 `negative_verification`，包括 run artifact、reset strategy、steps 和脱敏 oracle text/source。
- VS Code 扩展已读取 `negative_verification`，输出面板展示 status/reason/reset/oracles/report/next action，侧边栏展示 negative 摘要；negative fail/timeout 会提升扩展状态严重级别。
- `.vscode-agent-status.json` 已写入主验证 `report_hint`，VS Code 和下一轮代理可直接从状态文件定位 `get_run_report` 用法。
- 新增 `agent-status` CLI，可将 `.vscode-agent-status.json` 输出为 JSON 或 VS Code 同款 markdown。
- 新增 `scripts/code_context_verify_demo.ps1`，一条命令演示 git diff -> `generate-from-diff --dry-run` -> `verify-impl --run-profile dry-run` -> status markdown。
- VS Code 扩展新增 `Visual Agent: Verify Current Change` 命令，直接输入任务描述和 base URL/fixture 后调用 `verify-impl --format markdown`，并自动刷新状态/展示最新 verification。
- React/JSX parser 已支持常见字段组件 `<TextField>`、`<Field>`、`<Form.Field>`、`<Select>`、`<Textarea>`，不再只识别原生 input 和 `*Input` 组件。
- React/JSX parser 已将常见非 submit 动作按钮（delete/remove/archive/confirm/save/create/update）纳入 submit action 候选，并把 deleted/removed/archived 文案识别为成功态。
- Workflow 合成已支持 destructive action + confirm action 的双点击确认序列，例如 `Delete Ada` 后继续点击 `Confirm Delete`。
- 真实前端样例 e2e 已扩展到 Next.js、React 复杂组件/表格展示、React 列表行删除确认弹窗、Vue、Remix 五条 code-context verify dry-run 链路，覆盖 matched data display、无输入动作流、确认弹窗、生成 inputs、report artifacts 和状态文件落盘。
- `verify_implementation` 默认要求生成 workflow 质量分 `>= 0.6`，否则返回 `needs_workflow_improvement`，不运行弱验证。
- `verify_implementation` 支持 `timeout_seconds`，超时返回 `result: timeout`。
- MCP/CLI 响应会返回 `semantic_summary`，暴露解析框架、置信度、生成路径、字段/required/敏感字段/验证规则/成功状态/动态展示变量和 parse warnings。
- 每次 `verify_implementation` 都会写 `.vscode-agent-status.json`，供 VS Code 扩展刷新状态栏/侧边栏，并保留语义摘要用于诊断解析盲区。
- 已新增真实 CLI 子进程 e2e 回归，覆盖 git diff 生成 workflow、`verify-impl --run-profile dry-run`、自动 inputs 模板、report artifacts 和 `.vscode-agent-status.json` 落盘。

公开 API：

```python
from visual_agent import VisualSession

with VisualSession(dry_run=True) as s:
    s.press_key("escape")
    s.click_text("确认", mock_text="确认")
    print(s.run_dir)
```

SDK 支持：

- `observe_uia`
- `observe_ocr`
- `observe_screen`
- `observe_browser`
- `observe_dom`
- `click_text`
- `wait_for_text`
- `press_key`
- `click`
- `type_text`
- `assert_text_visible`
- `screenshot`
- `results`
- `run_dir`

### 8. Phase 6：本地 license 元数据

已支持：

- `get_license()` 默认返回 free tier
- `VISUAL_AGENT_LICENSE_TIER` / `VISUAL_AGENT_LICENSE_SEATS` / `VISUAL_AGENT_LICENSE_EXPIRES_AT` / `VISUAL_AGENT_LICENSE_KEY`
- `%USERPROFILE%\.visual-agent\license.json`
- `VISUAL_AGENT_HOME\license.json`
- `VISUAL_AGENT_LICENSE_FILE`
- 过期 license 在 `check_feature()` 中降级为 free
- `require_feature()` 仍保持非阻断占位，避免云端/收费能力未正式启用前影响本地功能
- `agent_session.json` 记录本月本地运行次数、云端运行占位次数和 reset month
- `context-snapshot` / MCP `get_session_context` 会展示 usage 摘要
- `usage-status` CLI 输出 usage、license tier 和 feature access，不输出 license key 或 inputs
- `run_remote_workflow()` 支持注入 remote client；只有返回 `status: success` 时才记录 `cloud_runs_used`，默认未实现/失败/异常均不计数
- `usage-status` 已展示远端配置 readiness：`VISUAL_AGENT_CLOUD_ENDPOINT`、`VISUAL_AGENT_CLOUD_API_KEY` present、`VISUAL_AGENT_CLOUD_ORG`、blockers、`network_probe: not_run`
- `build_remote_workflow_request()` 已提供远端请求 dry-run 结构，包含 workflow metadata、run profile、cloud readiness 和脱敏 inputs 摘要，不发网络请求
- `usage-status --format json` 已包含 `remote_request_preview`
- `remote_client_from_env()` 已提供可测试 adapter 草案；默认无 transport 时返回 blocked 诊断，不发网络请求
- `filter_remote_workflow_response()` 只保留 `status`、`run_id`、`report_url`、`message`，并脱敏 message
- `cloud-run-plan` CLI 已提供远端请求/adapter 诊断预览；不读取 inputs 文件内容，不发网络请求，不输出 cloud key
- `cloud-run` CLI 已加入显式执行开关；默认只 plan，`--execute` 在无内置 transport 时返回 blocked 且不计云端 usage，注入 fake transport 成功才记录 `cloud_runs_used`
- `cloud-run --execute --transport http` 已加入显式 HTTP transport 壳；endpoint/key 缺失时先 blocked，不发网络；HTTP 超时/失败返回 `failed` 且不计云端 usage
- HTTP transport 已覆盖 401/403 -> `blocked`、其他 4xx/5xx -> `failed`、非 JSON/非对象响应 -> `failed`，错误 body/message 走脱敏且不计 usage
- HTTP transport 已支持可配置 retry/backoff；仅 429 和 5xx 会重试，4xx 不重试，最终 success 才记录 `cloud_runs_used`
- 远端响应过滤已保留 `remote_schema_version`，并确认 queued/running/blocked/failed/unknown 不计 `cloud_runs_used`；未知 status 规范化为 `unknown`

### 9. 插件系统

支持 entry points：

```toml
[project.entry-points."visual_agent.actions"]
custom_action = "my_package.actions:handler"

[project.entry-points."visual_agent.providers"]
observe_custom = "my_package.providers:handler"
```

默认加载位置：

- `ActionDispatcher.__init__`
- `default_provider_registry()`

插件加载失败会 warn，不会让主程序崩溃。

### 10. pytest 插件

新增 fixtures：

- `visual_session`
- `visual_session_live`

注册：

```toml
[project.entry-points."pytest11"]
visual_agent = "visual_agent.pytest_plugin"
```

用法：

```python
def test_ui_contract(visual_session):
    result = visual_session.press_key("escape")
    assert result.status.value == "dry_run"
```

### 11. Python DSL

新增：

```python
from visual_agent.dsl import workflow, run_dsl_workflow

@workflow(name="checkout-verify", tags=["verification"])
def checkout(session):
    session.press_key("escape")

run_dsl_workflow("checkout-verify", dry_run=True)
```

## 常用命令

### 初始化工作区

```powershell
python -m visual_agent.cli init-workspace --root .agent-workspace --overwrite
```

### 查看工作区状态

```powershell
python -m visual_agent.cli workspace-status --root .agent-workspace
```

### 跑验证

```powershell
python -m visual_agent.cli verify --workspace-root .agent-workspace --tags verification --run-profile dry-run --format markdown
```

### Codex 智能检查

```powershell
python -m visual_agent.cli codex-check --workspace-root .agent-workspace
```

包含视觉/OCR 慢工作流：

```powershell
python -m visual_agent.cli codex-check --workspace-root .agent-workspace --include-slow
```

### 浏览器真实交互示例

```powershell
python -m visual_agent.cli run-workflow --file examples/workflows/form-fill/browser_form_workflow.yaml --inputs-file examples/inputs/demo_login.json --run-profile supervised --wait-lock
```

### 读取 AI 上下文

```powershell
python -m visual_agent.cli context-snapshot --workspace-root .agent-workspace --format markdown
```

### 读取产品问题

```powershell
python -m visual_agent.cli workspace-product-issues --root .agent-workspace --format markdown --write
```

## 注意事项

1. `dry-run` 不会真实点击/输入。要真实操作低/中风险动作，用 `--run-profile supervised`。
2. 浏览器界面优先用 `observe_browser`，不要优先 OCR。
3. 桌面、小程序、canvas 类界面才优先使用 OCR/UIA/VLM。
4. 多项目不要共用一个 `.agent-workspace`。
5. 视觉步骤会抢占屏幕，所以必须依赖 VisualLock 和自动最小化/恢复。
6. 如果 OCR 截错窗口，检查目标窗口是否被遮挡，以及 workflow 是否配置了 `window_title_candidates`。
7. `.pytest_cache` 在本机偶尔有权限 warning，不影响测试通过。

## 对新 Codex 窗口的直接指令

```text
你正在继续 Visual Agent 项目。先读 DEVELOPMENT_LOG.md、docs/codex.md、CODEX_SDK_SPEC.md。不要把工具做成只服务小程序的专用工具，必须保持通用产品方向。

当前目标是让 Visual Agent 成为 Codex/Claude Code/Cursor 的本地产品验收系统。它应该能看界面、跑流程、查接口、评估 AI 回复、输出产品问题，而不是只做截图或后端测试。

开发前先运行：
python -m pytest tests/ -q --tb=short

任何 UI 相关改动，不能只跑后端测试。必须至少 dry-run 一个 workflow；涉及真实交互时，再用 --run-profile supervised 跑一次。
```
