# 技术汇报：多源自动化 Agent 平台

## 当前结论

当前项目已经从“截图点按钮 demo”推进为一个可扩展的自动化 Agent 内核。

核心能力已经形成：

```text
Workspace
  -> Template Catalog
  -> Workflow Runtime
  -> ProviderRegistry
  -> SelectorResolver
  -> ActionDispatcher
  -> Validation
  -> Audit
  -> Reports
  -> Capability Doctor
  -> Atomic Capability Manifest
  -> Planner Draft Check
  -> Failure Diagnosis
  -> RunLock
  -> RunQueue
  -> Queue Scheduler
  -> Workspace Dashboard
  -> Workspace GUI
  -> Workspace Report Detail
  -> Workspace Report Export
  -> Report Index
  -> Failure Sample Tagging
  -> Regression Fixture Export
  -> Regression Promotion
  -> Regression Test Runner
  -> CI Integration Profile
  -> CI Config Templates
  -> Quality Gate Report Index
```

当前所有核心功能都有测试覆盖，近期的关键回归和文档收口验证已通过。

## 关键技术路线

已按“结构化优先，视觉兜底”的路线实现。

当前 Provider：

- `observe_html`: 本地 HTML 结构化观测，用于稳定 demo 和测试。
- `observe_fixture`: 回放 Observation，用于失败样本回归。
- `observe_dom`: Playwright DOM Provider，用于一次性网页 DOM 观测。
- `observe_browser`: Playwright 持久浏览器页面，用于 DOM 原生 `locator.click/fill` 执行闭环。
- `observe_uia`: Windows UI Automation Provider，预留桌面软件路线。
- `observe_ocr`: OCR Provider，从截图或图片提取文本框；支持 mock OCR 做确定性测试。
- `observe_vision`: VLM Provider，从截图或图片生成视觉状态解释；支持 mock VLM 做确定性测试。
- `observe_screen`: 屏幕截图 Provider，作为兜底感知。

当前 Action：

- `click`
- `type`
- `paste`

当前 Workflow 能力：

- `assert_text`
- `assert_response`
- `expect_download`
- `assert_file_exists`
- `save_storage_state`
- `wait_for text`
- `wait_for target`
- `retry`
- `timeout_seconds`
- `value_from: input.xxx`
- 默认 dry-run
- 审计落盘
- `state.json` checkpoint
- `resume_from` 恢复执行
- 敏感输入 hash 审计
- Playwright DOM 原生点击/填写，不经过坐标
- Playwright 网络事件审计和响应断言
- Playwright 下载保存和文件断言
- Playwright 登录态保存和恢复
- 表格行定位：按行文本定位行内按钮/链接
- 表格列名定位：按列名定位单元格或行内控件
- Planner 可见原子能力清单：输入 schema、输出 schema、风险等级、dry-run 支持
- Planner 安全 Workspace 上下文：workflow、inputs 文件、fixtures、runs、capabilities
- Planner 草案安全校验：能力白名单、风险拦截、workspace 路径边界、dry-run 强制
- 失败诊断：失败 step 自动记录预期、实际观测、截图 artifact、恢复建议和模型反思 prompt
- 失败诊断 known_issue 标注：已知框架噪音可在结构化输出中明确标记，避免被误判成普通回归
- OCR 感知闭环：`observe_ocr` + `OCRSelectorStrategy` + mock OCR workflow
- 失败诊断 OCR 二次观测：截图 artifact 存在时自动补充 `failure_diagnosis.evidence.ocr`
- VLM 感知闭环：`observe_vision` + `VisionSelectorStrategy` + mock VLM workflow
- 失败诊断 VLM 二次观测：截图 artifact 存在时自动补充 `failure_diagnosis.evidence.vision`
- Run Report 2.0：schema-versioned JSON / Markdown 报告，包含步骤、耗时、artifact、下载、失败诊断
- Workflow Contract Tests：examples/templates 自动校验，核心 demo dry-run 或预期失败回归
- Strict Validation Mode：生产前检查 observation、verification assertion、敏感字段、高风险动作
- Workflow Schema Versioning：`schema_version` / `min_runtime_version` / run result 版本落盘
- Runtime Preflight：运行前自动执行 validation + capability availability 检查
- Production Run Profile：`dry-run` / `supervised` / `approved` 三档统一控制真实动作和高风险动作
- RunLock：默认防止多个 workflow 同时操作同一桌面/浏览器资源，支持 TTL 替换陈旧锁
- RunQueue：锁被占用时可显式等待，排队耗时和尝试次数写入审计
- Queue Scheduler：`queue/tasks.json` 持久化任务队列，支持优先级、取消、重试和运行历史
- Workspace Dashboard：汇总 workspace health、runs、reports、quality gates、regression tests 和 queue
- Workspace GUI：tkinter 桌面窗口，展示摘要卡片、报告列表、报告详情、artifact/auth-state 列表、external sample readiness 和受控操作按钮
- Workspace Report Detail：按 run_id 展开 steps、artifacts、downloads、annotation 和 failure diagnosis
- Workspace Report Export：workspace-run 自动导出 JSON/Markdown 报告到 `reports/`
- Report Index：`reports/index.json` 汇总报告状态、失败步骤、耗时和报告路径
- Failure Sample Tagging：`reports/tags.json` 保存人工复盘状态、标签、备注和回归候选标记
- Regression Fixture Export：从失败报告导出 observation fixture、pytest 草案和 manifest
- Regression Promotion：把回归草案转正到 workspace `regression_tests/` 并生成索引
- Regression Test Runner：CLI 一键执行 workspace regression tests 并生成 JSON/Markdown 报告
- CI Integration Profile：`quality-gate` 定义 local/CI 发布门禁，支持 dry-run 和报告落盘
- CI Config Templates：生成 GitHub Actions workflow、本地 PowerShell 和 bat 发布门禁脚本
- Quality Gate Report Index：`reports/quality_gates/index.json` 汇总门禁历史和最近状态

## 目录与模块

核心代码：

- `src/visual_agent/models.py`: 核心数据模型。
- `src/visual_agent/providers.py`: Provider 注册表。
- `src/visual_agent/selector.py`: 目标定位策略。
- `src/visual_agent/dispatcher.py`: Action 调度器。
- `src/visual_agent/workflow.py`: 工作流运行时。
- `src/visual_agent/workspace.py`: 工程层。
- `src/visual_agent/templates.py`: 模板目录。
- `src/visual_agent/validation.py`: Workflow 静态校验。
- `src/visual_agent/reports.py`: 运行摘要。
- `src/visual_agent/capabilities.py`: 能力清单和依赖诊断。
- `src/visual_agent/planner.py`: Planner 草案校验和安全闸门。
- `src/visual_agent/diagnostics.py`: 失败诊断和视觉反思输入构造。
- `src/visual_agent/ocr.py`: OCR Provider，可选接入 Tesseract，支持 mock 模式回归测试。
- `src/visual_agent/vlm.py`: VLM Provider，可选接入本地视觉模型，支持 mock 模式回归测试。
- `src/visual_agent/state.py`: checkpoint 状态存储和恢复上下文水合。
- `src/visual_agent/security.py`: 敏感字段审计策略。
- `src/visual_agent/locks.py`: workflow 运行锁，防止并发执行抢占同一资源。
- `src/visual_agent/quality.py`: 本地/CI 质量门禁计划、执行和报告。
- `src/visual_agent/ci_templates.py`: CI 和本地质量门禁脚本模板生成。

业务内容：

- `templates/login_form`: 网页登录表单模板。
- `templates/order_entry`: ERP 订单录入模板。
- `templates/ecommerce_download`: 电商订单下载模板。
- `templates/external_readonly_probe`: 外部 HTTPS URL 只读 observe/assert 探测模板。

文档：

- `docs/framework.md`: 框架说明。
- `docs/technical_report.md`: 当前技术汇报。
- `宏大蓝图开发计划.md`: 长期计划和进度。

## 已验证命令

测试：

```powershell
.\.venv\Scripts\python.exe -m pytest
```

初始化 workspace：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli init --root .agent-workspace --overwrite
```

安装模板：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli install-template --root .agent-workspace --template order_entry --overwrite
.\.venv\Scripts\python.exe -m visual_agent.cli install-template --root .agent-workspace --template ecommerce_download --overwrite
.\.venv\Scripts\python.exe -m visual_agent.cli install-template --root .agent-workspace --template external_readonly_probe --overwrite
```

运行模板：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow order_entry --inputs-file order_entry_inputs.json
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow ecommerce_download --inputs-file ecommerce_download_inputs.json
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run --root .agent-workspace --workflow external_readonly_probe --inputs-file external_readonly_probe_inputs.json
```

查看工程：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli show-status --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-runs --root .agent-workspace --limit 5
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-context --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-check-plan --root .agent-workspace --file workflows/order_entry.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --run --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --run --save-as generated_login_check --preview-save --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --run --save-as generated_login_check --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-reports --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-index --root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-index --root .agent-workspace --failed-only
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-detail --root .agent-workspace --run-id <run-id> --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-tag-report --root .agent-workspace --run-id <run-id> --review-status needs_fix --tag selector --note "需要调整定位"
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-tags --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-export-regression-fixture --root .agent-workspace --run-id <failed-run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-promote-regression --root .agent-workspace --run-id <failed-run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-regression-tests --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run-regression-tests --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-queue-submit --root .agent-workspace --workflow local_html_form_workflow --inputs-file demo_login.json
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-queue-run-next --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-dashboard --root .agent-workspace --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-gui --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_business_backend_workflow.yaml --run-profile supervised
.\.venv\Scripts\python.exe -m visual_agent.cli external-samples-readiness --workspace-root .
.\.venv\Scripts\python.exe -m visual_agent.cli external-samples-readiness --workspace-root . --require-live-auth
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-run-plan --workspace-root .agent-workspace --sample-id external_ecommerce_orders_readonly
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-run-plan --workspace-root .agent-workspace --sample-id external_ecommerce_orders_readonly --require-live-auth
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-run --workspace-root .agent-workspace --sample-id external_ecommerce_orders_readonly --run-profile dry-run
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-batch-report --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-batch-report-index --workspace-root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli external-sample-batch-rerun-submit --workspace-root .agent-workspace --report-id external-samples-...
.\.venv\Scripts\python.exe -m visual_agent.cli auth-state-import --source path\to\storage_state.json --name seller-sandbox-state --workspace-root .
.\.venv\Scripts\python.exe -m visual_agent.cli auth-state-inspect --path .agent-auth\seller-sandbox-state.json
.\.venv\Scripts\python.exe -m visual_agent.cli auth-state-probe --path .agent-auth\seller-sandbox-state.json --url https://seller.sandbox.example.com/probe --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli model-credentials-inspect --source model_api_keys.txt --preferred openai --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli model-api-probe-plan --source model_api_keys.txt --preferred openai --base-url https://api.example.test --endpoint /v1/models --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli model-api-probe-plan --source model_api_keys.txt --preferred openai --run --timeout-seconds 20 --max-completion-tokens 64 --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile local --workspace-root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-index --workspace-root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-index --workspace-root .agent-workspace --strict-policy-failed true
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate-index --workspace-root .agent-workspace --strict-policy-failed true --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli install-ci-templates --root . --workspace-root .agent-workspace --overwrite
.\scripts\quality_gate.ps1 -Profile local
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/failure_diagnosis_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/ocr_mock_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/ocr_failure_diagnosis_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/vision_mock_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli report-run --run-dir .runs\<run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli report-run --run-dir .runs\<run-id> --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli validate-workflow --file examples/minimal_testable_workflow.yaml --strict
.\.venv\Scripts\python.exe -m visual_agent.cli preflight-workflow --file examples/minimal_testable_workflow.yaml --strict
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --run-profile dry-run
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --lock-ttl-seconds 600
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --queue-when-locked --lock-wait-seconds 60
```

能力诊断：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli capabilities
.\.venv\Scripts\python.exe -m visual_agent.cli atomic-capabilities
.\.venv\Scripts\python.exe -m visual_agent.cli doctor
```

## 原子能力清单

已完成 CapabilityManifest 升级，为后续 LLM Planner 提供稳定 API 面：

- 每个 capability 包含 `name`、`kind`、`available`、`input_schema`、`output_schema`、`dry_run_supported`、`risk_level`、`planner_visible`。
- 新增 `atomic-capabilities` CLI，只输出 Planner 可见能力。
- 当前原子能力覆盖 `observe_browser`、`click`、`type`、`paste`、`assert_text`、`assert_response`、`expect_download`、`assert_file_exists`、`save_storage_state`、`locate_table_cell` 等。
- `doctor` 继续保留依赖诊断；缺少 `uiautomation` 只作为可选能力，不阻塞网页主线。

## Planner Workspace Context

已完成 Planner 读取 Workspace 状态的确定性入口：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-context --root .agent-workspace
```

输出内容：

- workspace 根目录和标准目录。
- workflow 引用、相对路径、校验状态。
- inputs 文件元数据：文件名、路径、扩展名、大小。
- fixtures 文件元数据。
- recent runs 摘要。
- planner-visible atomic capabilities。

安全边界：

- 不读取 inputs JSON 内容。
- 不输出密码、token、客户字段等业务值。
- Planner 只能看到输入文件存在和大小，不能看到具体字段值。

## Planner Draft Check

已完成 Planner 草案执行前的确定性安全闸门：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-check-plan --root .agent-workspace --file workflows/order_entry.yaml
```

校验内容：

- 复用 `validate_workflow` 做基础结构校验。
- 检查每个 step 是否属于 Planner 可见原子能力。
- `save_storage_state` 等 high-risk capability 默认返回 error。
- `click`、`expect_download` 等 medium-risk capability 返回 dry-run warning。
- 草案缺少 observation 或 verification assertion 时返回 warning。
- `observe_html` / `observe_fixture` 的路径必须留在 workspace 内。
- 返回 `allowed_to_execute: false` 和 `dry_run_required: true`，避免模型草案绕过人工确认直接执行。

## Planner Draft Generation

已完成 openai 驱动的 Planner 草案生成入口：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --run --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --run --save-as generated_login_check --preview-save --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-planner-draft --root .agent-workspace --instruction "检查登录页是否出现登录文本" --source model_api_keys.txt --preferred openai --run --save-as generated_login_check --format markdown
```

安全边界：

- 默认 plan-only，不联网、不发送密钥。
- 只有显式 `--run` 才调用模型 API。
- Prompt 只包含 Planner-safe workspace context，不读取 inputs 内容。
- 默认生成结果只作为草案返回，不写入文件、不执行 workflow。
- 生成后立即调用 `workspace-check-plan` 同一套安全闸门。
- 常见模型输出的 `steps[].params`、`steps[].input` 和 `name` step 形状会先归一化为内部 workflow DSL，再进入校验。
- 只有显式 `--save-as`、草案 valid、目标路径留在 `workflows/` 内时才会落盘；覆盖现有文件必须再显式加 `--overwrite`。
- `--preview-save` 复用同样的 valid/path 检查，只输出 unified diff，不写 workflow 文件。
- GUI/控制台已接入 Preview Draft action，可对当前选中 workflow 走同一套 planner check 和 diff 预览，不调用模型、不写文件。
- GUI/控制台已接入 Generate Draft action，可调用 openai 真实生成草案，随后只做 planner check 和 diff preview，不保存、不执行。

## Failure Diagnosis

已完成失败出口的结构化诊断：

- `WorkflowRuntime._run_step` 在所有重试失败后调用 `diagnose_failure`。
- 浏览器上下文存在时保存 `<step-id>_failure.png`。
- 非浏览器场景复用最近 observation 的截图路径。
- `metadata.failure_diagnosis.expected` 记录原始预期。
- `metadata.failure_diagnosis.actual` 记录当前 provider、source、元素数量、可见文本、最新网络事件。
- `metadata.failure_diagnosis.recovery_suggestions` 给出确定性恢复建议。
- `metadata.failure_diagnosis.evidence.ocr` 在截图存在时执行一次 best-effort OCR。
- `metadata.failure_diagnosis.evidence.vision` 在截图存在时执行一次 best-effort VLM 状态解释。
- `metadata.failure_diagnosis.model_prompt` 保留未来接入 OCR/VLM/LLM 的反思提问：

```text
原本预期 [Target]，现在实际看到 [Actual]。请给出恢复建议，并优先选择结构化信息、DOM/UIA/API，其次才使用视觉。
```

示例命令：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/failure_diagnosis_workflow.yaml
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/ocr_failure_diagnosis_workflow.yaml
```

## OCR Provider

已完成 OCR 感知的最小可测试闭环：

- 新增 `observe_ocr` Provider。
- 支持 `path` 读取图片，或无 path 时截取当前屏幕。
- 支持 `mock_text` / `mock_bounds`，用于不依赖 Tesseract 的稳定测试。
- 如果安装 `pytesseract` 和系统 Tesseract binary，可走真实 OCR。
- 新增 `OCRSelectorStrategy`，支持 `text`、`label`、`contains_text`、`text_regex`。
- 新增 `examples/ocr_mock_workflow.yaml`，跑通 OCR observation、文本断言、目标解析、click dry-run。

示例命令：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/ocr_mock_workflow.yaml
```

## VLM Provider

已完成 VLM 感知的最小可测试闭环：

- 新增 `observe_vision` Provider。
- 支持 `path` 读取图片，或无 path 时截取当前屏幕。
- 支持 `mock_description` / `mock_status` / `mock_bounds`，用于不依赖真实模型的稳定测试。
- 新增 `VisionSelectorStrategy`，支持按视觉描述中的 `text`、`contains_text`、`text_regex` 定位。
- 新增 `examples/vision_mock_workflow.yaml`，跑通视觉 observation、文本断言、目标解析、click dry-run。
- 失败诊断中新增 `evidence.vision`，截图存在时尝试执行一次视觉状态解释；真实模型未接入时返回 `engine_available=false` 和安装提示。

示例命令：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/vision_mock_workflow.yaml
```

## 当前工程状态

已建立 `.agent-workspace`，包含：

```text
.agent-workspace/
  workspace.json
  workflows/
    local_html_form_workflow.yaml
    order_entry.yaml
    ecommerce_download.yaml
  inputs/
    demo_login.json
    order_entry_inputs.json
    ecommerce_download_inputs.json
  fixtures/
    login_demo.html
    erp_order_form.html
    ecommerce_orders.html
  runs/
```

已在 workspace 中 dry-run 跑通：

- `local_html_form_workflow`
- `order_entry`
- `ecommerce_download`

## Run Report 2.0

已完成面向 GUI/Planner/人工复盘的稳定报告结构：

- `schema_version: 1`
- run 级信息：run id、workflow、状态、总步骤、成功步骤、dry-run 动作、失败步骤、总耗时。
- step 级信息：id、action、status、message、elapsed、attempts、provider、target。
- artifacts：workflow result、state、step JSON、截图路径。
- downloads：下载文件名、扩展名、大小、路径。
- failure diagnosis：完整保留 expected / actual / OCR evidence / VLM evidence / recovery suggestions。
- Markdown 导出：便于用户或客户快速复盘。

命令：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli report-run --run-dir .runs\<run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli report-run --run-dir .runs\<run-id> --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-reports --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-index --root .agent-workspace --rebuild
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-report-detail --root .agent-workspace --run-id <run-id> --format markdown
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-tag-report --root .agent-workspace --run-id <run-id> --review-status regression_ready --regression-candidate
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-export-regression-fixture --root .agent-workspace --run-id <failed-run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-promote-regression --root .agent-workspace --run-id <failed-run-id>
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-run-regression-tests --root .agent-workspace
.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root .agent-workspace --run
```

Workspace 导出：

- `workspace-run` 默认导出 `reports/<run-id>.json`。
- `workspace-run` 默认导出 `reports/<run-id>.md`。
- `workspace-run` 默认刷新 `reports/index.json`。
- `show-status` 输出 workspace 状态、最近报告文件和当前 failure 线索。
- `workspace-planner-context` 输出报告索引条目，供 GUI/Planner 读取，不读取 inputs 内容。
- `workspace-report-index` 支持 `--status` / `--workflow` / `--failed-only` 过滤。
- `workspace-report-detail` 按 run_id 输出报告详情，包含步骤表、失败诊断、下载、artifact 和人工标注。
- `workspace-tag-report` 把人工复盘状态、标签、备注和回归候选标记写入 `reports/tags.json`。
- 报告索引会合并标注，但不改写原始 run report，保留审计不可变性。
- `workspace-export-regression-fixture` 从失败报告读取原始 `workflow_result.json`，导出最后一次 observation 到 `fixtures/regression/`。
- 同时生成 `reports/regression/<run-id>_manifest.json` 和 `test_<run-id>_draft.py`，作为人工转正测试前的草案。
- 导出后自动把报告标记为 `regression_ready` 和 `regression_candidate=true`。
- `workspace-promote-regression` 把草案转正为 `regression_tests/test_<run-id>.py`。
- `workspace-regression-tests` 输出 `regression_tests/index.json`，供 GUI 和 CI 读取。
- `workspace-run-regression-tests` 调用 pytest 执行 `regression_tests/`，报告写入 `reports/regression_runs/`。
- `workspace-queue-submit/list/cancel/retry/run-next` 管理 workspace 持久化任务队列，按优先级和创建时间调度。
- `workspace-dashboard` 汇总控制台只读视图，支持 JSON/Markdown，供 GUI 复用。
- `workspace-gui` 打开桌面控制台，复用 dashboard 和 report detail 数据层，并通过 action plan 执行 Run Dry、Run Next、Cancel、Retry。
- `workspace-report-detail` 汇总单个报告详情，支持 JSON/Markdown，供 GUI 详情页复用。
- `observe_browser reuse_page` 复用当前 Playwright 页面重新采集 DOM，支持 SPA 点击后的状态验证。
- `browser_business_backend_workflow` 覆盖真实浏览器业务后台组合场景：表格行列定位、响应断言、异常弹窗、分页和下载断言。
- `external-samples-readiness` 输出真实外部账号联调前置检查，包含允许域名、登录态文件、下载策略和 blockers。
- `external-samples-readiness --require-live-auth` 会检查 Playwright `storage_state` 的脱敏元数据：allowed domain 是否匹配、是否为空会话、cookie-only 会话是否已全部过期；不输出 cookie/token/localStorage 明文。
- `model-credentials-inspect` 已支持本地模型 API 密钥组脱敏检查；默认优先 `openai`，未发现时返回 missing 而不是自动使用其他模型 key。
- `model-api-probe-plan` 已支持生成/执行 openai 优先的只读 API 联调；无 `--run` 不发送 secret，显式 `--run` 会发起一次有 timeout/token limit 的 OpenAI-compatible chat health check。
- `examples/external_samples` 已扩展到订单、客服工单、库存补货、财务对账 4 类 sandbox 样本，并配套本地 HTML fixture。
- external sample workflow 已接入 Playwright route，本地 fixture 可 fulfill sandbox HTTPS 页面；财务样本包含 CSV download mock。
- `external-sample-run-plan/run` 提供外部样本受控联调入口：readiness blocked 禁止运行，只允许 `dry-run` / `supervised`。
- `external-sample-batch-plan/submit` 支持全 catalog 批量规划，并只把 ready 样本提交 workspace queue。
- `external-sample-summary` 按 sample_id 合并 readiness、queue task 和 latest report 状态。
- `external-sample-batch-report` 把 external sample summary 导出为 JSON/Markdown 批量复盘报告。
- `external-sample-batch-report-index` / `external-sample-batch-reports` 索引历史 batch reports，支持按 status/sample_id 筛选，并接入 GUI 列表。
- `external-sample-batch-failures` / `external-sample-batch-rerun-plan/submit` 从指定 batch report 聚合失败样本，并只对 ready 失败样本发起受控重跑。
- Batch Markdown report 已增强：直接输出 batch status、失败摘要、重跑命令提示、blocked 修复提示和 clean-batch review notes。
- `workspace-gui` 已支持 selected batch report Markdown 详情预览，切换 batch report 下拉项会刷新详情面板。
- `external-sample-rerun-plan/submit` 为 ready 的失败 external sample 提供受控重跑队列入口。
- external sample queue task 会保留 `metadata.external_sample`，队列执行完成后自动给报告注入 sample 元数据。
- external sample run 会把 `external_sample` 元数据写入 JSON/Markdown 报告、report index 和 GUI report detail。
- `auth-state-plan/import/inspect/probe` 把已有 Playwright storage_state 导入 `.agent-auth/`，只输出脱敏元数据；probe 会实际加载 browser context 并验证域名/会话状态。
- `workspace-gui` 已接入 artifact 和 auth-state 操作入口：可列出报告 artifact、检查登录态脱敏元数据，并通过 action plan 导入 storage_state。
- `workspace-gui` 已接入 external sample readiness 面板：可展示 ready/blocked、requirements、blockers、允许域名、下载策略和登录态文件存在状态。
- `quality-gate --profile local` 规划核心本地测试命令。
- `quality-gate --profile ci --run` 执行核心测试、workflow contract tests、workspace regression tests，并写入 `reports/quality_gates/`，同时刷新 `reports/quality_gates/index.json`。
- `quality-gate-index` 重建或筛选 quality gate 索引，供 GUI/Planner 判断最近门禁状态；支持 `--strict-policy-failed true|false` 筛选 strict policy gate 失败状态，并可用 `--format markdown` 输出 CI 可读表格。
- `--no-report-export` 可关闭导出，主要用于测试或调试。

## Workflow Contract Tests

已完成 examples/templates 合约测试：

- 所有 `examples/*_workflow.yaml`、`examples/screen_click_workflow.json`、`templates/*/*.yaml` 必须通过 `validate_workflow_file`。
- 核心可运行 demo 必须 dry-run 跑通。
- 失败诊断 demo 必须按预期失败，并包含 `failure_diagnosis`。
- 这能防止后续改内核时，示例和模板悄悄失效。

## Strict Validation Mode

已完成生产前严格校验模式：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli validate-workflow --file examples/minimal_testable_workflow.yaml --strict
.\.venv\Scripts\python.exe -m visual_agent.cli workspace-validate --root .agent-workspace --strict
```

严格规则：

- workflow 必须至少有一个 `observe_*` step。
- workflow 必须至少有一个验证断言：`assert_text` / `assert_response` / `assert_file_exists`。
- `password` / `token` / `secret` / `key` 等敏感输入必须标记 `sensitive: true`。
- `save_storage_state` 等高风险动作必须声明 `require_confirm: true`，或 CLI 显式 `--allow-high-risk`。
- step 内强行设置 `dry_run: false` 会给 warning，真实执行应由 CLI 授权控制。

默认 `validate-workflow` 保持兼容的宽松校验；只有显式 `--strict` 才启用生产规则。

## Workflow Schema Versioning

已完成 workflow DSL 版本合同：

```yaml
schema_version: 1
min_runtime_version: "0.1.0"
name: my_workflow
version: 1
```

规则：

- 当前支持 `schema_version: 1`。
- 缺少 `schema_version` 时，普通校验给 warning；strict 校验给 error。
- 高于当前 runtime 的 `min_runtime_version` 会被拒绝。
- 每次运行的 `workflow_result.json` 写入：
  - `workflow_schema_version`
  - `runtime_version`
- `report-run` 输出同样包含这两个字段。
- examples/templates 合约测试要求所有 workflow 显式声明 `schema_version: 1`。

## Runtime Preflight

已完成运行前检查：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli preflight-workflow --file examples/minimal_testable_workflow.yaml --strict
```

规则：

- `run-workflow` 和 `workspace-run` 默认执行 preflight。
- preflight 先跑 `validate_workflow`。
- 可选 `--strict-preflight` 使用生产严格校验。
- 检查 capability manifest 中必需能力是否可用。
- 如果 workflow 实际使用了不可用的可选 provider/action，例如未安装 UIA 时使用 `observe_uia`，会阻止运行。
- 可用 `--skip-preflight` 跳过，仅建议调试使用。

## Production Run Profile

已完成三档运行权限：

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/minimal_testable_workflow.yaml --run-profile dry-run
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_form_workflow.yaml --run-profile supervised
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples/browser_auth_save_workflow.yaml --run-profile approved
```

档位定义：

- `dry-run`: 默认档位，所有 mutating action 跳过真实执行；`save_storage_state` 也只记录 dry-run。
- `supervised`: 允许低/中风险真实动作，例如 DOM click/fill/download；阻止高风险动作。
- `approved`: 允许高风险动作，但高风险 step 必须声明 `require_confirm: true`。

兼容性：

- 旧参数 `--allow-click` 保留，等价于 `--run-profile approved`。
- `WorkflowRuntime.run(dry_run=True)` 仍等价于 `run_profile="dry-run"`。
- `WorkflowRuntime.run(dry_run=False)` 兼容映射为 `approved`。

## RunLock

已完成 workflow 运行锁和本地等待队列：

- `run-workflow` 和 `workspace-run` 默认在运行输出根目录创建 `workflow.lock`。
- 锁内容包含 owner、pid、created_at、host、cwd，便于排查是谁占用资源。
- 同一输出根目录有活跃锁时，第二个 workflow 会立即失败，不会并发操作桌面或浏览器。
- 超过 TTL 的陈旧锁会自动替换，避免异常退出后永久卡死。
- CLI 支持 `--lock-ttl-seconds <seconds>` 和 `--no-lock`。
- 每次成功运行的 `workflow_result.json` 写入 `run_lock` 元数据，并在运行结束释放锁。
- 显式增加 `--queue-when-locked` 后，锁占用时会在 `--lock-wait-seconds` 内等待。
- `run_queue` 审计字段记录是否启用队列、等待秒数、尝试次数、超时和轮询间隔。

验证：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_locks.py tests\test_workflow.py
```

## 真实浏览器验证

已完成本地 Playwright 真实浏览器闭环：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples\browser_form_workflow.yaml --inputs-file examples\inputs\demo_login.json --allow-click
```

验证结果：

- `observe_browser` 成功打开本地 HTML 页面。
- `fill_username` 使用 `page.locator("#username").fill(...)`。
- `fill_password` 使用 `page.locator("#password").fill(...)`。
- `click_login` 使用 `page.locator("#login").click()`。
- 三个动作均显示 `metadata.execution = playwright`。
- 三个动作均为 `point: null`，没有坐标点击/输入。
- 密码字段只记录 salted SHA-256，不记录长度和预览。

已完成本地 Playwright 网络响应闭环：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples\browser_network_workflow.yaml --allow-click
```

验证结果：

- `observe_browser` 支持 route mock，用于稳定复现业务接口。
- 点击 `export-orders` 后触发 `POST /api/orders/export`。
- `assert_response` 捕获响应：
  - `status: 201`
  - `ok: true`
  - `resource_type: fetch`
- 断言结果写入 step metadata，后续可用于审计报告。

已完成本地 Playwright 下载闭环：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples\browser_download_workflow.yaml --allow-click
```

验证结果：

- `observe_browser` 使用 `accept_downloads=True` 的 browser context。
- `expect_download` 等待下载事件、点击目标、保存文件。
- 文件保存到 `.runs/<run-id>/downloads/orders.csv`。
- `assert_file_exists` 校验扩展名 `.csv` 和最小字节数。
- 下载路径、文件名、扩展名、字节数写入审计 metadata。

已完成本地 Playwright 登录态闭环：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples\browser_auth_save_workflow.yaml --allow-click
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples\browser_auth_restore_workflow.yaml --allow-click
```

验证结果：

- `save_storage_state` 保存当前 browser context 到 `.agent-auth/demo-auth-state.json`。
- `observe_browser.storage_state` 能加载已有登录态。
- 第二次运行无需点击登录，页面直接显示 `已登录`。
- 审计只记录 storage state 文件路径、文件名、扩展名和大小，不记录 cookie/token 内容。
- `.agent-auth/` 已加入 `.gitignore`，避免会话文件误提交。

已完成本地 Playwright 表格行/列定位闭环：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH='D:\longxia agent\.pw-browsers'
.\.venv\Scripts\python.exe -m visual_agent.cli run-workflow --file examples\browser_table_row_workflow.yaml --allow-click
```

验证结果：

- DOM 观测为行内按钮记录 `row_text`、`row_index`、`row_selector`。
- DOM 观测为行内按钮记录 `column_header`、`column_index`。
- Selector 支持 `row_text`、`row_contains_text`、`row_text_regex`。
- Selector 支持 `column_header`、`column_contains_text`、`column_text_regex`。
- Selector 支持 `near_text`、`near_contains_text`、`near_text_regex`，用于 label 附近输入框和相邻按钮。
- Selector 支持 `scope_role`、`scope_text`、`scope_contains_text`，用于弹窗/dialog 内目标定位。
- `local_business_backend_workflow` 用本地业务后台 fixture 组合验证 SPA 状态、复杂表单、分页、表格行列和异常弹窗。
- `browser_business_backend_workflow` 用路由拦截的 Playwright 页面组合验证 SPA 重新观测、网络响应、异常弹窗关闭、分页和下载。
- `windows_notepad_demo_workflow` 用 UIA fixture 组合验证 Windows 软件 5 步流程：窗口断言、主题输入、内容输入、保存点击。
- OCR 真实引擎验证已完成：`detect_tesseract()` 检查 `pytesseract`、Tesseract 二进制、版本和运行错误；`observe_ocr` 输出 `engine_status`。
- 本地 VLM 后端验证已完成：`detect_vlm_backend()` 检查 `qwen2-vl` / `moondream` 依赖和 `model_path`；`observe_vision` 输出 `engine_status`。
- 外部业务后台样本验证框架已完成：`external-samples-check` 校验 catalog、外部 HTTPS 观测、断言、敏感值、dry-run/confirm 保护、允许域名、登录态策略和下载策略。
- 真实外部账号环境联调前置检查已完成：`external-samples-readiness` 会在 required storage_state 文件缺失时输出 `missing_storage_state_file` blocker。
- 外部业务仿真样本已扩展：订单、工单、库存、财务 4 类样本覆盖 optional/required storage_state、download forbidden、confirm-required download 和 confirm-gated action。
- 外部样本本地路由运行闭环已完成：workflow 通过 route 使用本地 HTML fixture 和下载 mock，并修复 Playwright launch 失败时的资源清理。
- 外部样本运行报告与 GUI 复盘闭环已完成：报告详情可查看 sample_id、readiness、requirements、blockers 和策略元数据。
- 外部样本批量运行与队列集成已完成：ready 样本可批量提交 queue，blocked 样本保留 blockers。
- 外部样本批量运行结果汇总面板已完成：sample_id 维度可查看 readiness、queue 和 latest report。
- 外部样本失败重跑入口已完成：失败样本可按 readiness gate 重新提交 queue。
- 外部样本队列执行器保留 sample metadata 已完成：queue run 报告会自动获得 external_sample 注解。
- 外部样本队列执行结果批量汇总报告已完成：`reports/external_samples/` 下生成 JSON/Markdown batch artifact。
- 外部样本批量报告历史索引与 GUI 列表已完成：batch reports 支持 index、CLI 筛选和 GUI 打开。
- 外部样本批次失败摘要与一键重跑入口已完成：指定 batch 可提取失败样本、规划 ready failures、提交队列重跑。
- external sample batch report 详情 Markdown 增强已完成：报告正文包含 failure summary、rerun commands 和 blocked remediation hints。
- external sample batch report GUI 详情预览已完成：GUI model 输出 selected batch Markdown，桌面窗口支持下拉切换预览。
- 登录态导入与实测入口已完成：`auth-state-import` 写入 `.agent-auth/<name>.json` 和脱敏 manifest，`auth-state-inspect` 不打印 cookie/token 值，`auth-state-probe` 实际加载 storage_state 到 browser context 并验证域名/会话状态。
- 外部样本联调保护层已完成：`external-sample-run-plan/run` 在 catalog/readiness gate 通过前拒绝运行，并拒绝 `approved` profile。
- 两行订单、两列操作按钮使用相同 `data-testid=row-action`。
- workflow 通过 `row_contains_text: A1002` 和 `column_header: 查看` 定位到第二行“查看”按钮。
- 点击后触发 `POST /api/orders/A1002/view`。
- `assert_response` 确认返回 `202 ok=true`，证明没有误点 A1001 行或 A1002 的“下载”列。

## 当前边界

真实网页执行已完成本地文件验证，但还没有完成外部业务网站验证：

- 已验证本地下载文件闭环；未验证外部业务网站下载。
- 已验证本地登录态保存和恢复；未验证外部业务网站登录态。
- 已验证本地表格行/列定位；未验证外部复杂表格和虚拟滚动表格。
- 未验证跨域跳转、复杂 SPA。
- 已接入浏览器网络响应审计，但还没有验证外部真实网站流量。
- 未做真实客户 ERP/电商后台样本。

还没有完成 Windows 控件真实执行：

- `uiautomation` 依赖未安装。
- UIA Provider 和 Selector 已有代码和测试，但还没对真实桌面软件做验证。

OCR/Vision 已有 mock 和可选真实后端入口：

- `observe_ocr` 支持 mock 和 Tesseract 适配。
- `observe_vision` 支持 mock 和本地 VLM 适配入口。
- 真实 OCR/VLM 还未完成本机模型级验收。

真实点击/输入默认关闭：

- 所有 workflow 默认 dry-run。
- 真实动作必须显式加 `--allow-click`。

## 已根据第一性原理审查修正

### 状态机持久化

已实现：

- 每个 run 目录写入 `state.json`。
- 每步完成后更新 completed steps。
- 失败时记录 failed step。
- `resume_from` 支持从已有 run 目录恢复。
- 恢复时会从已完成 step JSON 水合 Observation / ResolvedTarget 上下文。

相关文件：

- `src/visual_agent/state.py`
- `src/visual_agent/workflow.py`

### 敏感字段审计

已实现：

- step 支持 `sensitive: true`。
- CLI 支持 `--sensitive-fields password,customer.id`。
- 敏感字段不记录长度。
- 敏感字段不记录预览。
- 只记录 salted SHA-256。

示例：

```yaml
- id: fill_password
  action: paste
  target:
    label: 请输入密码
    role: input
  value_from: input.password
  sensitive: true
```

### 恢复能力验证

已增加测试：

- 缺少密码输入时失败。
- 修复 inputs 后从原 run 目录 resume。
- 已完成步骤跳过。
- 上下文从 step JSON 恢复。
- 后续步骤继续执行到完成。

## 技术风险

1. 真实浏览器操作已经从“观测 DOM”升级到“DOM 原生执行”。
   - 已实现 `observe_browser` 持久 Playwright page。
   - 已实现 ActionDispatcher 的 Playwright `click/fill` 分支。
   - 已实现 Playwright 下载保存和文件断言。
   - 已实现 Playwright 登录态保存和恢复。
   - 下一步风险在外部网站泛化和异常弹窗。

2. 复杂页面需要更强 Selector。
   - 当前 Selector 已支持 text/label/role/selector/test_id/contains_text/text_regex。
   - 当前 Selector 已支持 row_text/row_contains_text/row_text_regex。
   - 当前 Selector 已支持 column_header/column_contains_text/column_text_regex。
   - 当前 Selector 已支持 near_text/near_contains_text/near_text_regex。
   - 当前 Selector 已支持 scope_role/scope_text/scope_contains_text。
   - 后续需要支持虚拟滚动表格。

3. 输入敏感信息需要继续扩展策略。
   - `sensitive: true` 已支持 hash-only。
   - 后续应支持字段级默认策略，例如所有 `password/token/id_card` 自动敏感。

4. Workspace 路径上下文已处理，单机并发保护已完成 RunLock / RunQueue / Queue Scheduler。
   - 后续如果多机器并发，需要 SQLite 状态后端或服务端 scheduler。

5. Web 业务后台本地组合验证已完成。
   - 后续如果要提高真实性，应补 Playwright 真实浏览器版 SPA workflow。

6. Windows 软件 5 步 demo 已完成。
   - 后续如果要提高真实性，应补真实 UIA 应用手动 smoke 或自动启动本地小程序。

7. OCR 真实引擎验证已完成。
   - 当前环境如果未安装 `pytesseract` / Tesseract，会作为可选缺失项出现在 `doctor`，不阻塞 mock OCR 和核心测试。

8. 本地 VLM 后端验证已完成。
   - 当前环境如果未安装 `torch` / `transformers` 或未配置 `model_path`，会作为可选缺失项/诊断信息出现，不阻塞 mock VLM 和核心测试。

9. 外部业务后台样本验证框架已完成。
   - 当前提供只读外部样本模板和安全校验，不包含真实账号或真实凭据。

5. checkpoint 当前是文件级状态。
   - 单机本地执行足够。
   - 并发和分布式执行需要 SQLite / Rust scheduler / file lock。

6. 原子能力清单已可供 Planner 使用，并已有受控 LLM 草案生成入口。
   - 当前 LLM 只负责生成 workflow 草案。
   - 草案生成后必须通过确定性 planner safety check。
   - 仍不会让模型直接写文件、执行 workflow 或绕过 dry-run 边界。

7. Planner 已能读取 Workspace 状态、生成草案并校验草案，但还不能自动修改 workspace。
   - 当前生成入口只返回草案和校验结果。
   - 后续如果要落盘，应增加人工确认、diff 预览和审计记录。

8. 失败诊断当前是确定性版本。
   - 已能输出结构化实际状态和恢复建议。
   - 已把失败截图自动送入 OCR 生成二次文本证据。
   - 已把失败截图自动送入 VLM Provider 生成二次视觉状态证据。

## 下一步建议

优先级 1：真实业务网页闭环

- 本地路由模拟的真实浏览器业务后台组合验证已完成。
- 下载、SPA 重新观测、异常弹窗、分页、表格行列和 `assert_response` 已串成一个 workflow。
- 登录态采集/导入向导已完成：导入已有 storage_state 后可解除 external readiness 的 `missing_storage_state_file` blocker。
- GUI artifact/auth-state 操作入口已完成：报告 artifact 和登录态文件可在控制台模型中列出，inspect/import 只输出脱敏元数据。
- GUI external sample readiness 面板已完成：真实账号联调 blockers 可以在控制台模型中查看。
- 真实外部账号 dry-run/supervised 联调保护层已完成。
- 外部业务仿真样本已扩展到订单、工单、库存、财务 4 类。
- 外部样本本地路由运行闭环已完成。
- 外部样本运行报告与 GUI 复盘闭环已完成。
- 外部样本批量运行与队列集成已完成。
- 外部样本批量运行结果汇总面板已完成。
- 外部样本失败重跑入口已完成。
- 外部样本队列执行器保留 sample metadata 已完成。
- 外部样本队列执行结果批量汇总报告已完成。
- 外部样本批量报告历史索引与 GUI 列表已完成。
- 外部样本批次失败摘要与一键重跑入口已完成。
- external sample batch report 详情 Markdown 增强已完成。
- external sample batch report GUI 详情预览已完成。
- external sample queue/batch 操作后的 GUI model refresh 统一入口已完成：写入类 GUI action 返回 `refreshed_model`，并保留刚生成的 run 或刚操作的 batch 选择态。
- GUI 桌面窗口消费 `refreshed_model` 已完成：action callback 后刷新 summary cards、report/queue/artifact/auth/readiness/batch combobox 和详情预览。
- GUI 按钮可用态与操作反馈 polish 已完成：按钮状态跟随 selected task/batch/readiness/rerun 状态，操作反馈统一走 message 或显式 payload。
- GUI readiness/batch 详情状态摘要 polish 已完成：readiness 详情突出 ready/blocked 和 remediation，batch 详情突出 failed、blocked、ready rerun candidates，并保留完整 artifact。
- GUI action 错误处理与恢复提示 polish 已完成：Tk callback 使用 safe wrapper，失败 action 返回结构化错误、recovery hint 和 refreshed model。
- GUI action 审计事件记录已完成：safe callback 成功/失败都会写入 `gui/actions.jsonl`，console model 暴露最近 `gui_action_events`。
- GUI action history 面板/过滤入口已完成：console model 暴露 history options，桌面窗口可查看事件详情，底层支持 action/status 过滤。
- GUI action history CLI/report 导出已完成：`workspace-gui-actions` 支持 JSON/Markdown 和 action/status/limit 过滤。
- GUI action history 汇总索引已完成：`workspace-gui-action-index` 输出最近错误率、常见失败 action、action/status 计数和 recent errors，console model 同步暴露 `gui_action_history_index`。
- GUI action history Planner/CI 消费入口已完成：`workspace-planner-context` 暴露 `gui_action_history` 风险摘要，`quality-gate` 输出 `risk_summary` 和 Markdown 风险提示；当前为 warning-only，不改变 gate pass/fail。
- GUI action history 风险阈值配置已完成：`workspace.json` 支持 `quality.gui_action_history` 默认策略和 `profiles.planner/local/ci` 覆盖，可配置 `error_rate_threshold`、`history_limit`、`failed_action_limit`。
- GUI action history 风险可视化已完成：`workspace-gui` summary cards 显示 GUI Action Risk，console model 暴露 `gui_action_history_risk_markdown`，详情包含 warnings、failed actions 和 recent errors。
- GUI action history 风险筛选/跳转已完成：console model 暴露 `gui_action_risk_event_options` 和 `selected_gui_action_risk_event`，桌面 Risk 按钮可直接打开最近失败 action event。
- GUI action history 风险恢复建议聚合已完成：`gui_action_history_remediation_items` 按 action/error_type/recovery_hint 去重计数，GUI Action Risk Markdown 顶部显示 remediation checklist。
- GUI action history 恢复建议导出已完成：`workspace-gui-action-index --risk` 支持 JSON/Markdown remediation checklist，`quality-gate` Markdown risk summary 同步输出 checklist。
- GUI action history 风险趋势摘要已完成：risk summary 新增 newest-vs-older trend window，输出 error rate delta、remediation count delta 和 improving/worsening/stable/mixed/insufficient_history。
- GUI action history 风险趋势索引已完成：`quality-gate-index` 的 report entry 暴露 risk level、warning count、remediation count、risk trend direction/deltas，并汇总 `risk_trends`。
- GUI action history 风险趋势 Dashboard 集成已完成：`workspace-dashboard` 暴露 quality risk warnings/latest risk trend，dashboard Markdown 和 GUI Quality Gates card 同步显示。
- GUI action history 风险趋势健康状态已完成：latest risk trend 为 `worsening` 时 dashboard health issues 增加 `gui_action_risk_worsening`，improving/stable/unknown 不触发。
- GUI action history 风险趋势健康策略配置已完成：`workspace.json` 支持 `quality.gui_action_history.health.attention_trend_directions`，默认只关注 `worsening`，workspace 可选择让 `mixed` 等方向触发 dashboard attention。
- workspace 风险策略模板导出已完成：`workspace-risk-policy-template` 输出可复制的 `workspace.json` quality fragment，覆盖 GUI action risk 阈值、planner/local/ci profile 和 dashboard health attention trend。
- workspace 风险策略应用校验已完成：`workspace-risk-policy-check --root <workspace>` 校验当前 `workspace.json` 风险策略，输出错误/提醒、路径、code 和修复建议，配置错误时返回非 0。
- workspace 风险策略校验 Dashboard/GUI 集成已完成：`workspace-dashboard` 暴露 `risk_policy_check`，错误配置会触发 `workspace_risk_policy_invalid` health issue，GUI summary cards 新增 Risk Policy 卡片。
- workspace 风险策略校验 Markdown 详情已完成：dashboard Markdown 和 GUI detail model 会渲染 Risk Policy Check issue table，包含 level/code/path/message/suggestion。
- workspace 风险策略应用向导已完成：`workspace-risk-policy-plan --root <workspace>` 生成可合并的 quality policy patch 预览，默认不写文件；`--apply` 显式写入，`--overwrite` 允许模板默认值覆盖现有风险策略值。
- workspace 风险策略应用 GUI action 已完成：`workspace-gui` 新增 Plan Policy / Apply Policy action，前者只预览 patch，后者显式写入并刷新 Risk Policy card/detail。
- workspace 风险策略 GUI action 反馈 Markdown 已完成：Plan/Apply action 反馈会渲染 apply 状态、changed paths、before/after validation table，不再要求人工阅读原始 JSON。
- workspace 风险策略审计事件增强已完成：GUI action history compact result 会记录 policy patch 的 applied/changed/changed paths 和 before/after validation 摘要，并避免写入完整 patch。
- workspace 风险策略质量门禁联动已完成：`quality-gate` risk summary/Markdown 输出 risk policy check 状态，策略错误会追加 `workspace_risk_policy_invalid` warning；`quality-gate-index` 汇总 policy error/warning count。
- workspace 风险策略 CI 模板提示已完成：`install-ci-templates` 生成的 GitHub Actions、PowerShell、bat 会在 `quality-gate` 前运行 `workspace-risk-policy-check`，安装结果也返回可复制的 policy check/plan 命令。
- workspace 风险策略严格门禁选项已完成：`quality-gate --fail-on-risk-policy-error` 会记录 strict policy gate 摘要，并在执行门禁时把 workspace policy error 转为 failed status；默认仍保持 warning-only 兼容行为。
- quality gate 严格策略索引增强已完成：`quality-gate-index` 汇总 strict policy gate enabled/failed/policy error 计数，latest entry 暴露 strict 状态，dashboard/GUI Quality Gates 摘要显示 strict failed。
- quality gate strict policy CLI 筛选已完成：`quality-gate-index` 与 `quality-gate-reports` 支持 `--strict-policy-failed true|false`，过滤条件会写入 index filters。
- quality gate 筛选结果 Markdown 导出已完成：`quality-gate-index --format markdown` 和 `quality-gate-reports --format markdown` 输出 filters、summary、strict failure count 和报告路径表格。
- quality gate 筛选结果 GUI/控制台入口已完成：`workspace-dashboard` 暴露 strict policy failed report list 和 Markdown，dashboard Markdown 增加 strict failure history，`workspace-gui` 新增 Strict Failures 按钮直接查看筛选结果。

优先级 2：Selector 增强

- 支持 `selector: "#id"`。
- 支持 `test_id`。
- 支持相邻文本定位。
- 支持弹窗范围定位。
- 支持 `contains_text`。
- 表格行/列定位和相邻文本定位已完成本地闭环。

优先级 3：运行报告

- Markdown/JSON 报告已完成。
- Workspace 自动报告导出已完成。
- 报告索引和失败筛选已完成。
- 失败样本标注和人工复盘备注已完成。
- 回归 fixture/test 草案导出已完成。
- 回归候选转正为 workspace 正式测试已完成。
- workspace regression tests 一键执行和报告已完成。
- CI Integration Profile 已完成。
- 报告详情数据层已完成。
- GUI 桌面窗口已完成。
- GUI 操作按钮已完成，workflow run 默认 dry-run。
- GUI / 控制台 open artifact 和 auth-state 操作入口已完成。
- GUI / 控制台 external sample readiness 面板已完成。
- 真实外部账号 dry-run/supervised 联调保护层已完成。
- 外部业务仿真样本扩展已完成。
- 外部样本本地路由运行闭环已完成。
- 外部样本运行报告与 GUI 复盘闭环已完成。
- 外部样本批量运行与队列集成已完成。
- 外部样本批量运行结果汇总面板已完成。
- 外部样本失败重跑入口已完成。
- 外部样本队列执行器保留 sample metadata 已完成。
- 外部样本队列执行结果批量汇总报告已完成。
- 外部样本批量报告历史索引与 GUI 列表已完成。
- 外部样本批次失败摘要与一键重跑入口已完成。
- external sample batch report 详情 Markdown 增强已完成。
- external sample batch report GUI 详情预览已完成。
- external sample queue/batch 操作后的 GUI model refresh 统一入口已完成。
- GUI 桌面窗口消费 `refreshed_model` 并更新控件状态已完成。
- GUI 按钮可用态与操作反馈 polish 已完成。
- GUI readiness/batch 详情状态摘要 polish 已完成。
- GUI action 错误处理与恢复提示 polish 已完成。
- GUI action 审计事件记录已完成。
- GUI action history 面板/过滤入口已完成。
- GUI action history CLI/report 导出已完成。
- GUI action history 汇总索引已完成。
- GUI action history Planner/CI 消费入口已完成。
- GUI action history 风险阈值配置已完成。
- GUI action history 风险可视化已完成。
- GUI action history 风险筛选/跳转已完成。
- GUI action history 风险恢复建议聚合已完成。
- GUI action history 恢复建议导出已完成。
- GUI action history 风险趋势摘要已完成。
- GUI action history 风险趋势索引已完成。
- GUI action history 风险趋势 Dashboard 集成已完成。
- GUI action history 风险趋势健康状态已完成。
- GUI action history 风险趋势健康策略配置已完成。
- workspace 风险策略模板导出已完成。
- workspace 风险策略应用校验已完成。
- workspace 风险策略校验 Dashboard/GUI 集成已完成。
- workspace 风险策略校验 Markdown 详情已完成。
- workspace 风险策略应用向导已完成。
- workspace 风险策略应用 GUI action 已完成。
- workspace 风险策略 GUI action 反馈 Markdown 已完成。
- workspace 风险策略审计事件增强已完成。
- workspace 风险策略质量门禁联动已完成。
- workspace 风险策略 CI 模板提示已完成。
- workspace 风险策略严格门禁选项已完成。
- quality gate 严格策略索引增强已完成。
- quality gate strict policy CLI 筛选已完成。
- quality gate 筛选结果 Markdown 导出已完成。
- 下一步应做 quality gate 筛选结果 GUI/控制台入口。

优先级 4：GUI/控制台

- 读取 Workspace。
- 读取 `workspace-dashboard`。
- 读取 `workspace-report-detail`。
- 展示 workflows、runs、capabilities。
- 展示 report 列表和详情。
- 提供 Run Dry、Run Next、Cancel、Retry，并保持 dry-run 默认模式。

