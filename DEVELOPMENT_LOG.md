# Visual Agent 开发日志与新窗口交接

> 更新时间：2026-06-05  
> 仓库地址：https://github.com/Xiaoyu155/visual-agent  
> 当前分支：`main`  
> 当前工作区状态：干净，无未提交改动  
> 最新提交：`061e6c1 Add Python SDK and plugin entrypoints`

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
651 passed, 6 skipped
```

如果只是确认 SDK 和 CLI：

```powershell
python -c "from visual_agent import VisualSession, VisualLock, Target; print('OK')"
visual-agent --help
python -m visual_agent --help
```

## 最近关键提交

```text
061e6c1 Add Python SDK and plugin entrypoints
392de95 Improve real interaction guidance and window capture defaults
159c23f Add product state contracts and issue reporting
32b7655 Complete Codex check and compact report test coverage
958a165 Add Codex diff-aware checks and platform connect
ca2996b Add post-action observation and slow workflow filtering
972a656 Cache repeated workflow observations
3be0498 Add OCR text click and wait actions
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

### 7. 插件系统

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

### 8. pytest 插件

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

### 9. Python DSL

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

