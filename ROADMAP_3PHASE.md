# 三阶段开发路线图

> **状态：已归档。**
>
> 本文件是早期路线图，当前权威计划与进度以 `MASTER_DEV_PLAN.md` 为准。
> 截至 2026-06-03，三阶段核心开发、MCP token 预算硬化、安全清单和文档同步均已完成。

> 目标：让 Visual Agent 成为 Coding Agent 的本地验证层和执行记忆
>
> 核心路线：E2E 测试稳固 → 失败摘要压缩 → 完整验收闭环

---

## 资源需求说明

**结论：三个阶段全部只需要开发时间，不需要额外投入。**

| 资源 | 是否需要 | 说明 |
|---|---|---|
| 资金 | 否 | 开发、测试、发布 GitHub 全部免费 |
| 额外设备 | 否 | 当前 Windows 机器完全够用 |
| 服务器 | 否 | 产品本地运行，不需要云服务器 |
| GPU | 否 | VLM 用云端 API，不需要本地 GPU |
| 云 VLM API Key | 可选 | 测试视觉兜底时需要，约 $5-10 人民币级别 |
| 域名 | 可选 | 以后做文档站用，现在不需要 |
| CI/CD | 否 | GitHub Actions 免费额度足够 |

**唯一可能花钱的地方：**
- 如果要测试云端 VLM（OpenAI / Qwen），需要一个 API key
- 按调用次数计费，开发测试阶段花费极低（几十元以内）
- 不影响核心功能开发，可以完全用 mock 先跑通

---

## 第一阶段：E2E 测试稳固

**时间：当前 → 2 周内**
**目标：每个核心能力有端到端证明，不是靠 mock 蒙混**
**验收：`pytest tests/e2e/` 全绿，CI 自动跑**

### E2E-IMPL-01 — 创建 e2e 目录结构

```
tests/e2e/
  __init__.py
  conftest.py          # 共享 fixture：workspace 路径、Python 路径、跳过条件
  test_e2e_install.py
  test_e2e_local_form.py
  test_e2e_mcp.py
  test_e2e_failure_diagnosis.py
  test_e2e_queue.py
  test_e2e_browser.py  # 需要 Playwright，自动 skip
```

**conftest.py 内容：**
```python
import pytest
from pathlib import Path
import sys

ROOT = Path(__file__).parent.parent.parent
WORKSPACE = ROOT / ".agent-workspace"
PYTHON = Path(sys.executable)

@pytest.fixture(scope="session", autouse=True)
def ensure_workspace():
    if not WORKSPACE.exists():
        import subprocess
        subprocess.run([str(PYTHON), "-m", "visual_agent.cli",
                       "init-workspace", "--root", str(WORKSPACE)], cwd=ROOT)
```

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/ --collect-only -q
```

---

### E2E-IMPL-02 — test_e2e_install.py

参照 `E2E_TEST_PLAN.md` 中 E2E-01 的测试代码实现，包含：
- `test_doctor_perception_dom_ready`
- `test_doctor_warnings_are_actionable`
- `test_workspace_dashboard_loads`

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_install.py -v
```
验收标准：3 个测试全绿

---

### E2E-IMPL-03 — test_e2e_local_form.py

参照 E2E_TEST_PLAN.md E2E-02，包含：
- `test_local_form_all_steps_succeed`
- `test_local_form_run_id_generated`
- `test_local_form_run_dir_exists`
- `test_local_form_sensitive_input_not_in_report`
- `test_local_form_report_has_step_details`

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_local_form.py -v
```
验收标准：5 个测试全绿

---

### E2E-IMPL-04 — test_e2e_mcp.py（最重要）

参照 E2E_TEST_PLAN.md E2E-04，包含：
- list_workflows 工具
- validate_workflow 工具
- run_workflow 工具（含 dry-run 默认、approved 拒绝、secret 不泄露）
- get_run_report 工具
- list_run_artifacts 工具
- 路径穿越攻击防护

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_mcp.py -v
```
验收标准：全部绿，尤其是 secret 不泄露和路径穿越两个安全测试

---

### E2E-IMPL-05 — test_e2e_failure_diagnosis.py

参照 E2E_TEST_PLAN.md E2E-06，包含：
- `test_failure_workflow_produces_diagnosis`
- `test_failure_diagnosis_has_expected_and_actual`
- `test_failure_diagnosis_has_recovery_suggestions`

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_failure_diagnosis.py -v
```
验收标准：3 个测试全绿，recovery_suggestions 有实际内容

---

### E2E-IMPL-06 — test_e2e_queue.py

参照 E2E_TEST_PLAN.md E2E-07，包含：
- `test_queue_submit_and_run`

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_queue.py -v
```

---

### E2E-IMPL-07 — 把 E2E 测试加入 CI

在 `.github/workflows/visual-agent-quality-gate.yml` 中补充步骤：

```yaml
- name: Run E2E tests (no browser required)
  run: |
    python -m pytest tests/e2e/ -v --tb=short -m "not browser" -q
```

验收标准：push 后 GitHub Actions 通过

---

### 第一阶段完成标准

```powershell
# 以下命令全部绿色通过
.\.venv\Scripts\python.exe -m pytest tests/e2e/ -v --tb=short -m "not browser"
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

---

## 第二阶段：失败摘要压缩

**时间：1 个月内**
**目标：让 Codex/Cursor 读 100 token 就能理解一次 UI 失败，不需要读几千行日志**
**验收：MCP 工具 `summarize_latest_failure` 在 Claude Code 里真实调用返回有意义结果**

### 核心价值

```
没有这个工具：
  Codex 读完整失败日志 → 5000-50000 token → 慢且贵

有这个工具：
  Codex 调用 summarize_latest_failure → 300-500 token → 快且准
```

---

### P2-01 — 设计输出格式

新增文件：`docs/failure_summary_schema.md`

定义 AI-friendly 失败摘要的标准格式：

```json
{
  "workflow": "checkout_flow",
  "run_id": "20260603-xxx",
  "run_profile": "dry-run",
  "overall_status": "failed",
  "failed_step": {
    "id": "assert_success_banner",
    "action": "assert_text",
    "expected": "Order completed",
    "actual_text": ["Payment method required", "Add payment"],
    "probable_cause": "payment_not_initialized",
    "confidence": "high"
  },
  "artifacts": {
    "screenshot": ".agent-workspace/runs/xxx/assert_success_banner_failure.png",
    "run_dir": ".agent-workspace/runs/xxx"
  },
  "suggested_next_prompt": "The checkout fails at assert_success_banner. Expected 'Order completed' but saw 'Payment method required'. Check the payment method initialization in CheckoutForm.",
  "token_count": 287
}
```

验收标准：文档清晰，格式可被 AI 直接使用

---

### P2-02 — 实现 `build_failure_summary()` 函数

新增文件：`src/visual_agent/failure_summary.py`

核心逻辑：
1. 读取最近一次失败的 run report
2. 找到第一个 failed step 和其 failure_diagnosis
3. 压缩 visible_text（只保留最相关的几条）
4. 推断 probable_cause（基于 action 类型和 actual 内容的规则映射）
5. 生成 suggested_next_prompt（一句话，直接可粘贴给 AI）
6. 计算 token 估算值

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_failure_summary.py -v
```

---

### P2-03 — 实现 `summarize_latest_failure` CLI 命令

```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli summarize-latest-failure \
  --workspace-root .agent-workspace \
  --format markdown
```

输出示例：
```
## Latest Failure Summary

**Workflow:** checkout_flow
**Failed Step:** assert_success_banner
**Expected:** "Order completed"
**Actual:** "Payment method required"
**Probable Cause:** payment method not initialized before submit

**Suggested prompt for Codex/Cursor:**
> The checkout workflow fails at step `assert_success_banner`.
> Expected "Order completed" but the page shows "Payment method required".
> Check payment initialization in CheckoutForm — the default payment method may not be set before form submission.

**Artifacts:** `.agent-workspace/runs/20260603-xxx/`
```

验收命令：
```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli summarize-latest-failure \
  --workspace-root .agent-workspace --format markdown
```
验收标准：输出清晰，一个不懂代码的人也能读懂

---

### P2-04 — 实现 `summarize_latest_failure` MCP 工具

在 `mcp_server.py` 中新增第 6 个工具：

```python
Tool(
    name="summarize_latest_failure",
    description=(
        "Get a token-efficient summary of the latest workflow failure. "
        "Returns the failed step, expected vs actual state, probable cause, "
        "and a ready-to-use suggested prompt for the coding agent. "
        "Designed to minimize context usage when debugging UI failures."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string"},
            "workflow_name": {
                "type": "string",
                "description": "Filter by workflow name (optional)"
            },
            "max_tokens": {
                "type": "integer",
                "description": "Max tokens for the summary (default: 500)",
                "default": 500
            }
        },
        "required": ["workspace_root"]
    }
)
```

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_mcp.py -k "failure" -v
```

---

### P2-05 — 业务场景 HTML fixture + workflow

新增两个真实业务仿真场景，为第三阶段 verify 命令提供素材：

**场景 A：电商订单导出**
- `examples/web/order_export_demo.html`（订单列表 + 导出按钮 + 下载触发）
- `examples/order_export_workflow.yaml`
- `tests/e2e/test_e2e_order_export.py`

**场景 B：后台数据核查**
- `examples/web/data_check_demo.html`（搜索 + 筛选 + 结果表格）
- `examples/data_check_workflow.yaml`
- `tests/e2e/test_e2e_data_check.py`

验收标准：两个场景 dry-run 全部通过

---

### 第二阶段完成标准

```powershell
# CLI 能输出可读的失败摘要
.\.venv\Scripts\python.exe -m visual_agent.cli summarize-latest-failure \
  --workspace-root .agent-workspace --format markdown

# MCP 工具测试通过
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_mcp.py -v

# 业务场景 E2E 测试通过
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_order_export.py -v
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_data_check.py -v
```

在 Claude Code / Cursor 里真实调用 `summarize_latest_failure`，AI 能根据返回内容直接给出修复建议。

---

## 第三阶段：visual-agent verify 完整验收闭环

**时间：2 个月内**
**目标：AI 修完代码后，一条命令自动验收，输出 AI 可直接消费的报告**
**验收：Codex 修完前端代码 → `visual-agent verify` → 报告告诉 Codex 是否通过**

---

### 核心设计

```powershell
visual-agent verify --for codex --workspace-root .agent-workspace
```

内部逻辑：
1. 读取 workspace 中所有 workflow
2. 按标签或命名规则筛选"验收类"workflow（tag: verification）
3. 依次 dry-run 或 supervised 执行
4. 汇总结果
5. 生成 AI-friendly 报告

输出示例：
```
## Verification Report

Ran 3 workflows:
✓ checkout_flow         — 6 steps passed
✓ login_flow            — 4 steps passed  
✗ order_export_flow     — FAILED at step assert_download_exists

### Failed: order_export_flow

Step: assert_download_exists
Expected: file orders_2026.csv (size > 0)
Actual: file not found

Probable cause: export button click did not trigger download
Screenshot: .agent-workspace/runs/xxx/assert_download_exists_failure.png

### Suggested next action for Codex:
The export button in OrderList.tsx is not triggering the download handler.
Check the onClick handler and ensure it calls triggerDownload() with correct params.
```

---

### P3-01 — workflow 标签系统

在 YAML workflow 中支持 `tags` 字段：

```yaml
schema_version: 1
name: checkout_flow
version: 1
tags:
  - verification
  - checkout
  - smoke
steps:
  ...
```

`verify` 命令默认运行 tag 为 `verification` 的 workflow。

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_workflow_tags.py -v
```

---

### P3-02 — verify 命令核心实现

新增文件：`src/visual_agent/verify.py`

核心函数：
```python
def run_verification(
    workspace: Workspace,
    *,
    tags: list[str] = ("verification",),
    run_profile: str = "dry-run",
    for_agent: str = "codex",
    max_workflows: int = 10,
) -> VerificationResult:
    ...
```

返回：
- 通过的 workflow 列表
- 失败的 workflow 列表
- 每个失败的摘要（复用 P2 的 failure_summary）
- 给指定 agent 的 suggested_prompt

验收命令：
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_verify.py -v
```

---

### P3-03 — verify CLI 命令

```powershell
# 基础用法
.\.venv\Scripts\python.exe -m visual_agent.cli verify \
  --workspace-root .agent-workspace \
  --for codex \
  --format markdown

# 只跑特定 tag
.\.venv\Scripts\python.exe -m visual_agent.cli verify \
  --workspace-root .agent-workspace \
  --tag checkout \
  --format markdown
```

验收标准：
- 有失败时，输出包含可操作的修复建议
- 全通过时，输出简洁的绿色确认
- `--for codex/cursor/claude` 输出格式针对不同 AI 优化

---

### P3-04 — `run_verification` MCP 工具

在 `mcp_server.py` 中新增第 7 个工具：

```python
Tool(
    name="run_verification",
    description=(
        "Run all verification workflows in the workspace and return an AI-friendly summary. "
        "Use this after making code changes to check if the UI still works correctly. "
        "Returns pass/fail for each workflow and a suggested fix prompt for any failures."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only run workflows with these tags. Default: ['verification']"
            },
            "run_profile": {
                "type": "string",
                "enum": ["dry-run", "supervised"],
                "default": "dry-run"
            }
        },
        "required": ["workspace_root"]
    }
)
```

---

### P3-05 — E2E 测试：完整验收循环

新增文件：`tests/e2e/test_e2e_verify.py`

测试场景：
1. workspace 中有 verification 标签 workflow → verify 命令运行并输出报告
2. 所有 workflow 通过 → 输出全绿
3. 某 workflow 故意失败 → 输出包含 suggested_prompt
4. MCP `run_verification` 调用 → 返回 AI-friendly 格式

---

### P3-06 — coding-agent-brief 增强

在现有 `coding-agent-brief` 命令中加入验证层说明：

```
## Available Verifications

Run `run_verification` to check if your code changes broke any UI workflows.
Currently registered verification workflows:
- checkout_flow (tags: verification, checkout)
- login_flow (tags: verification, smoke)
- order_export_flow (tags: verification, export)

After making changes, call:
  run_verification(workspace_root="...", tags=["verification"])
```

---

### 第三阶段完成标准

```powershell
# verify 命令端到端运行
.\.venv\Scripts\python.exe -m visual_agent.cli verify \
  --workspace-root .agent-workspace \
  --for codex \
  --format markdown

# MCP 工具完整测试
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_mcp.py -v
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_verify.py -v

# 全量测试
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

在 Claude Code 里对话：
> "帮我检查最新改动有没有破坏 checkout 流程"

Claude 调用 `run_verification`，返回结果，直接告诉用户哪里坏了以及怎么修。

---

## 三阶段总工时预估

| 阶段 | 任务数 | 工时 | 完成后的价值 |
|---|---|---|---|
| 第一阶段：E2E 稳固 | 7 个任务 | ~15h | 能证明产品真实可用 |
| 第二阶段：失败摘要 | 5 个任务 | ~12h | 让 Codex 调试 UI 省 90% token |
| 第三阶段：verify 闭环 | 6 个任务 | ~18h | Coding agent 改完代码自动验收 |
| **合计** | **18 个任务** | **~45h** | |

---

## 给 Codex 的执行规则

1. 按阶段顺序，不跨阶段
2. 每个任务完成后跑验收命令，绿了再下一个
3. 遇到阻塞标记 skipped，写原因，不卡队列
4. 第一阶段未全绿之前，不开始第二阶段
5. 每完成一个阶段，更新本文件中的状态

---

## 当前状态

- [x] 第一阶段：已完成，详见 `MASTER_DEV_PLAN.md`
- [x] 第二阶段：已完成，详见 `MASTER_DEV_PLAN.md`
- [x] 第三阶段：已完成，详见 `MASTER_DEV_PLAN.md`
- [x] 本路线图已归档，后续以 `MASTER_DEV_PLAN.md` 为唯一权威计划
