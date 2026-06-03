# Visual Agent 主开发计划

> 版本：2026-06-03
> 状态：第一至第三阶段已实现
> 本文档取代之前所有计划文档，是唯一权威的开发指南。

---

## 一、产品定位

### 一句话

> Visual Agent 是 Coding Agent 的本地验证层、执行记忆和安全边界。

### 解释

| Coding Agent（Codex/Claude Code/Cursor）做什么 | Visual Agent 做什么 |
|---|---|
| 理解需求，生成代码，分析问题 | 操作浏览器和桌面，执行 workflow |
| 推理下一步怎么做 | 记住每次执行的结果、截图、失败诊断 |
| 消耗 token 读日志 | 把日志压缩成 AI-ready 摘要，省 token |
| 每次重新理解业务流程 | 持久化 workflow 资产，永久复用 |
| 无法控制执行风险 | 提供 dry-run/supervised/approved 安全边界 |

### 1+1>2 的核心机制

```
Codex 修代码
    ↓
Visual Agent 执行验证 workflow
    ↓
失败 → 生成 300 token 诊断摘要 → 注入 Codex 上下文
    ↓
Codex 继续修，不需要读几千行日志
    ↓
Visual Agent 再次验证
    ↓
通过 → workflow 转为回归测试资产
```

---

## 二、核心架构原则

### 原则 1：Token 预算约束（最重要）

Visual Agent 解决的一个核心痛点是：Codex 上下文太长被迫开新窗口。
**如果 Visual Agent 的输出本身也很长，问题没有解决，只是转移了。**

所有面向 AI 的输出必须遵守 token 预算：

| 输出类型 | token 上限 | 超出处理 |
|---|---|---|
| context-snapshot（会话快照） | 500 token | 强制截断，超出内容只留路径 |
| summarize_latest_failure | 400 token | 只保留最关键的 1 个失败步骤 |
| run_verification 结果 | 800 token | 通过的 workflow 只显示名字 |
| get_session_context MCP 工具 | 600 token | 分层，细节按需拉取 |
| MCP 工具单次响应 | 2000 token | 超出返回摘要+路径 |

**懒加载原则：快照是目录，不是内容。细节通过 MCP 工具按需获取。**

```
✗ 错误方式：把完整 run report 塞进快照
✓ 正确方式：快照只有 "last_failure: checkout_flow > step3"
            需要细节时调用 get_run_report(run_id)
```

### 原则 2：结构化优先，视觉兜底

感知顺序：DOM → UIA → OCR → VLM

VLM 仅在前三者都无法定位目标时才调用。
新增 provider 时遵循相同优先级。

### 原则 3：默认安全

- 所有执行默认 dry-run
- approved 必须在 workspace.json 白名单
- MCP 响应不含 secret/cookie/token
- artifact 路径限制在 workspace 内
- context-snapshot 不包含输入值、密码、session token

### 原则 4：可审计

每次 workflow 执行、每次 MCP 调用、每次 GUI 操作都写入审计日志。
失败必须有截图+诊断，不允许静默失败。

---

## 三、第一阶段：E2E 测试基础

**目标：证明每个核心能力真实可用，不靠 mock 蒙混**
**时间：2 周**
**完成标准：`pytest tests/e2e/ -m "not browser"` 全绿**

---

### E2E-IMPL-01：创建 tests/e2e/ 目录结构

**新增文件：`tests/e2e/__init__.py`（空文件）**

**新增文件：`tests/e2e/conftest.py`**

```python
"""
E2E 测试共享 fixture。
所有 E2E 测试通过此文件获取路径、workspace、运行工具函数。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WORKSPACE = ROOT / ".agent-workspace"
PYTHON = Path(sys.executable)
EXAMPLES = ROOT / "examples"


def run_cli(*args, cwd=None, timeout=60) -> tuple[int, str]:
    """运行 CLI 命令，返回 (exit_code, combined_output)。"""
    result = subprocess.run(
        [str(PYTHON), "-m", "visual_agent.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd or ROOT,
        timeout=timeout,
    )
    return result.returncode, result.stdout + result.stderr


def parse_json_output(output: str) -> dict:
    """解析 CLI JSON 输出，处理 UTF-8 BOM。"""
    return json.loads(output.lstrip("﻿"))


def playwright_available() -> bool:
    try:
        result = subprocess.run(
            [str(PYTHON), "-c", "from playwright.sync_api import sync_playwright"],
            capture_output=True,
            cwd=ROOT,
        )
        return result.returncode == 0
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def ensure_workspace():
    """确保 workspace 存在，不存在则初始化。"""
    if not (WORKSPACE / "workspace.json").exists():
        run_cli("init-workspace", "--root", str(WORKSPACE))
    return WORKSPACE


@pytest.fixture
def fresh_run_id(tmp_path):
    """运行一个 minimal workflow，返回 run_id 供后续使用。"""
    code, output = run_cli(
        "run-workflow",
        "--file", str(EXAMPLES / "minimal_testable_workflow.yaml"),
        "--run-profile", "dry-run",
        "--synthetic-on-capture-fail",
    )
    assert code == 0, f"setup run failed: {output}"
    data = parse_json_output(output)
    return data["run_id"]
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/ --collect-only -q
```

**验收标准：** 能收集到测试，0 个 error

---

### E2E-IMPL-02：test_e2e_install.py

**新增文件：`tests/e2e/test_e2e_install.py`**

```python
"""
E2E-01：安装后的基础可用性验证。
验证 doctor 输出正确、dashboard 不崩溃、感知层状态准确。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.conftest import ROOT, WORKSPACE, run_cli, parse_json_output


class TestDoctorOutput:

    def test_exits_zero(self):
        """doctor 命令在所有 optional 依赖缺失时也应返回 0。"""
        code, output = run_cli("doctor")
        assert code == 0, f"doctor 非零退出: {output}"

    def test_has_perception_section(self):
        """doctor 输出必须包含 perception 字段。"""
        _, output = run_cli("doctor")
        data = parse_json_output(output)
        assert "perception" in data, f"缺少 perception 字段: {list(data.keys())}"

    def test_dom_browser_available(self):
        """Playwright 安装后 DOM provider 必须可用。"""
        _, output = run_cli("doctor")
        data = parse_json_output(output)
        perception = data["perception"]
        assert perception.get("dom_browser") is True, (
            "DOM browser 不可用。运行: pip install -e .[web] && "
            "python -m playwright install chromium"
        )

    def test_ready_for_dom_workflows(self):
        """DOM workflow 必须报告就绪。"""
        _, output = run_cli("doctor")
        data = parse_json_output(output)
        assert data["perception"].get("ready_for_dom_workflows") is True

    def test_warnings_are_actionable(self):
        """每条 warning 必须包含可操作的修复指引（含动词）。"""
        _, output = run_cli("doctor")
        data = parse_json_output(output)
        action_words = {"install", "set", "configure", "run", "add", "pip", "download"}
        for warning in data["perception"].get("warnings", []):
            has_action = any(w in warning.lower() for w in action_words)
            assert has_action, f"Warning 缺少修复指引: {warning}"

    def test_vlm_not_configured_shows_warning(self):
        """没有配置 VLM 时必须有 warning，不能静默 ok。"""
        _, output = run_cli("doctor")
        data = parse_json_output(output)
        if not data["perception"].get("vlm"):
            warnings = data["perception"].get("warnings", [])
            vlm_warnings = [w for w in warnings if "vlm" in w.lower() or "visual" in w.lower()]
            assert vlm_warnings, "VLM 不可用但没有 warning，会误导用户"


class TestWorkspaceDashboard:

    def test_dashboard_exits_zero(self):
        code, output = run_cli(
            "workspace-dashboard",
            "--root", str(WORKSPACE),
            "--format", "markdown",
        )
        assert code == 0, f"dashboard 崩溃: {output}"

    def test_dashboard_has_required_sections(self):
        _, output = run_cli(
            "workspace-dashboard",
            "--root", str(WORKSPACE),
            "--format", "markdown",
        )
        for section in ("Workspace Dashboard", "Workflows", "Queue"):
            assert section in output, f"dashboard 缺少 {section} 段落"

    def test_dashboard_no_traceback(self):
        _, output = run_cli(
            "workspace-dashboard",
            "--root", str(WORKSPACE),
            "--format", "markdown",
        )
        assert "Traceback" not in output, f"dashboard 输出包含 Python traceback"
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_install.py -v --tb=short
```

**验收标准：** 全部通过

---

### E2E-IMPL-03：test_e2e_local_form.py

**新增文件：`tests/e2e/test_e2e_local_form.py`**

```python
"""
E2E-02：本地 HTML 表单填写端到端验证。
不使用 fixture mock，使用真实 HTML 文件和真实输入。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.conftest import ROOT, WORKSPACE, EXAMPLES, run_cli, parse_json_output

WORKFLOW = EXAMPLES / "local_html_form_workflow.yaml"
INPUTS = EXAMPLES / "inputs" / "demo_login.json"


def run_form_workflow(run_profile="dry-run") -> dict:
    code, output = run_cli(
        "run-workflow",
        "--file", str(WORKFLOW),
        "--inputs-file", str(INPUTS),
        "--run-profile", run_profile,
    )
    assert code == 0, f"workflow 运行失败 (exit={code}):\n{output}"
    return parse_json_output(output)


class TestLocalFormExecution:

    def test_all_steps_not_failed(self):
        """所有步骤状态必须是 success 或 dry_run，不能是 failed。"""
        data = run_form_workflow()
        failed = [
            {"id": s["id"], "action": s["action"], "message": s.get("message", "")}
            for s in data["steps"]
            if s["status"] == "failed"
        ]
        assert not failed, f"以下步骤失败:\n{json.dumps(failed, indent=2, ensure_ascii=False)}"

    def test_unique_run_ids(self):
        """连续两次运行必须产生不同 run_id（审计隔离）。"""
        r1 = run_form_workflow()
        r2 = run_form_workflow()
        assert r1["run_id"] != r2["run_id"], "两次运行 run_id 相同，审计隔离失效"

    def test_run_dir_created(self):
        """运行后必须在 .runs/ 下创建对应目录。"""
        data = run_form_workflow()
        run_dir = ROOT / ".runs" / data["run_id"]
        assert run_dir.exists(), f"审计目录未创建: {run_dir}"

    def test_no_password_in_report(self):
        """报告不能包含密码明文 demo123。"""
        data = run_form_workflow()
        report_str = json.dumps(data, ensure_ascii=False)
        assert "demo123" not in report_str, "密码明文泄露到报告中，安全漏洞！"

    def test_each_step_has_required_fields(self):
        """每个步骤必须有 id、action、status 字段。"""
        data = run_form_workflow()
        for step in data["steps"]:
            for field in ("id", "action", "status"):
                assert field in step, f"步骤缺少 {field}: {step}"

    def test_run_profile_recorded(self):
        """报告必须记录 run_profile。"""
        data = run_form_workflow()
        assert data.get("run_profile") == "dry-run"

    def test_dry_run_click_not_executed(self):
        """dry-run 模式下 click 步骤不能真实执行。"""
        data = run_form_workflow("dry-run")
        for step in data["steps"]:
            if step["action"] == "click":
                assert step["status"] in ("dry_run", "skipped"), (
                    f"dry-run 下 click 不应真实执行: {step['id']} -> {step['status']}"
                )
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_local_form.py -v --tb=short
```

**验收标准：** 7 个测试全绿

---

### E2E-IMPL-04：test_e2e_mcp.py（最重要）

**新增文件：`tests/e2e/test_e2e_mcp.py`**

```python
"""
E2E-04：MCP 工具端到端验证。
直接调用 mcp_server handler 函数，使用真实 workspace，
验证 5 个工具全部返回有意义的内容。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.e2e.conftest import ROOT, WORKSPACE, run_cli

# 延迟导入避免 import 时 workspace 不存在
def get_handlers():
    from visual_agent.mcp_server import (
        _list_workflows,
        _validate_workflow,
        _run_workflow,
        _get_run_report,
        _list_run_artifacts,
    )
    return _list_workflows, _validate_workflow, _run_workflow, _get_run_report, _list_run_artifacts


def arun(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def parse_mcp(result) -> dict:
    return json.loads(result[0].text)


def ws() -> str:
    return str(WORKSPACE)


# ── list_workflows ──────────────────────────────────────────────────

class TestListWorkflows:

    def test_returns_workflow_list(self):
        _list_workflows, *_ = get_handlers()
        data = parse_mcp(arun(_list_workflows({"workspace_root": ws()})))
        assert "workflows" in data, f"缺少 workflows: {data}"
        assert isinstance(data["workflows"], list)

    def test_each_workflow_has_name_and_path(self):
        _list_workflows, *_ = get_handlers()
        data = parse_mcp(arun(_list_workflows({"workspace_root": ws()})))
        for wf in data["workflows"]:
            assert "name" in wf, f"workflow 条目缺少 name: {wf}"
            assert "path" in wf, f"workflow 条目缺少 path: {wf}"

    def test_path_traversal_rejected(self):
        _list_workflows, *_ = get_handlers()
        result = arun(_list_workflows({"workspace_root": "../../etc/passwd"}))
        data = json.loads(result[0].text)
        assert "error" in data, "路径穿越未被拦截"


# ── validate_workflow ───────────────────────────────────────────────

class TestValidateWorkflow:

    def test_valid_workflow_passes(self):
        _, _validate_workflow, *_ = get_handlers()
        data = parse_mcp(arun(_validate_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
        })))
        assert "error" not in data, f"合法 workflow 验证报错: {data}"
        assert data.get("valid") is True

    def test_nonexistent_workflow_returns_structured_error(self):
        _, _validate_workflow, *_ = get_handlers()
        data = parse_mcp(arun(_validate_workflow({
            "workspace_root": ws(),
            "workflow_name": "absolutely_does_not_exist_xyz",
        })))
        assert "error" in data
        assert "hint" in data, "error 响应必须包含 hint"


# ── run_workflow ────────────────────────────────────────────────────

class TestRunWorkflow:

    def test_dry_run_returns_run_id(self):
        _, _, _run_workflow, *_ = get_handlers()
        data = parse_mcp(arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "dry-run",
        })))
        assert "error" not in data, f"dry-run 失败: {data}"
        assert data.get("run_id"), "缺少 run_id"

    def test_default_profile_is_dry_run(self):
        """不传 run_profile 时默认必须 dry-run，不能真实执行。"""
        _, _, _run_workflow, *_ = get_handlers()
        data = parse_mcp(arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
        })))
        assert "error" not in data
        # dry-run 下 click 步骤状态是 dry_run
        failed = [s for s in data.get("failed_steps", []) if s.get("status") == "failed"]
        assert not failed, f"默认执行下有真实失败步骤: {failed}"

    def test_approved_without_whitelist_rejected(self):
        """未加白名单时 approved 必须被明确拒绝。"""
        _, _, _run_workflow, *_ = get_handlers()
        data = parse_mcp(arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "approved",
        })))
        assert "error" in data, "approved 未被拒绝"
        error_msg = data["error"].lower()
        assert "whitelist" in error_msg or "approved" in error_msg

    def test_no_secret_in_response(self):
        """MCP 响应不能包含密码明文。"""
        _, _, _run_workflow, *_ = get_handlers()
        result = arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "dry-run",
        }))
        raw = result[0].text
        assert "demo123" not in raw, "密码明文出现在 MCP 响应中"

    def test_nonexistent_workflow_returns_error(self):
        _, _, _run_workflow, *_ = get_handlers()
        data = parse_mcp(arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "does_not_exist_xyz",
            "run_profile": "dry-run",
        })))
        assert "error" in data


# ── get_run_report ──────────────────────────────────────────────────

class TestGetRunReport:

    def test_report_after_run(self):
        _, _, _run_workflow, _get_run_report, _ = get_handlers()
        run_data = parse_mcp(arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "dry-run",
        })))
        run_id = run_data.get("run_id")
        assert run_id

        report_result = arun(_get_run_report({
            "workspace_root": ws(),
            "run_id": run_id,
            "format": "markdown",
        }))
        text = report_result[0].text
        assert run_id in text or "local_html" in text

    def test_fake_run_id_returns_error(self):
        *_, _get_run_report, _ = get_handlers()
        data = parse_mcp(arun(_get_run_report({
            "workspace_root": ws(),
            "run_id": "fake-run-00000000",
        })))
        assert "error" in data

    def test_report_no_secret(self):
        _, _, _run_workflow, _get_run_report, _ = get_handlers()
        run_id = parse_mcp(arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "dry-run",
        }))).get("run_id")
        result = arun(_get_run_report({
            "workspace_root": ws(),
            "run_id": run_id,
            "format": "json",
        }))
        assert "demo123" not in result[0].text


# ── list_run_artifacts ──────────────────────────────────────────────

class TestListRunArtifacts:

    def test_artifacts_in_workspace(self):
        _, _, _run_workflow, _, _list_run_artifacts = get_handlers()
        run_id = parse_mcp(arun(_run_workflow({
            "workspace_root": ws(),
            "workflow_name": "local_html_form_workflow",
            "run_profile": "dry-run",
        }))).get("run_id")

        data = parse_mcp(arun(_list_run_artifacts({
            "workspace_root": ws(),
            "run_id": run_id,
        })))
        for artifact in data.get("artifacts", []):
            path_str = str(artifact["path"])
            assert str(WORKSPACE) in path_str or ".runs" in path_str, (
                f"artifact 路径在 workspace 外: {path_str}"
            )

    def test_fake_run_id_returns_empty(self):
        *_, _list_run_artifacts = get_handlers()
        data = parse_mcp(arun(_list_run_artifacts({
            "workspace_root": ws(),
            "run_id": "fake-run-00000000",
        })))
        artifacts = data.get("artifacts", [])
        assert isinstance(artifacts, list)
        assert len(artifacts) == 0
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_mcp.py -v --tb=short
```

**验收标准：** 全绿，尤其安全相关测试（secret、路径穿越、approved 拒绝）

---

### E2E-IMPL-05：test_e2e_failure_diagnosis.py

**新增文件：`tests/e2e/test_e2e_failure_diagnosis.py`**

```python
"""
E2E-06：失败诊断可读性验证。
故意触发失败，验证报告提供足够信息帮助 AI/人 修复。
"""
from __future__ import annotations

import json

import pytest

from tests.e2e.conftest import ROOT, EXAMPLES, run_cli, parse_json_output

FAILURE_WORKFLOW = EXAMPLES / "failure_diagnosis_workflow.yaml"


def run_failure_workflow() -> dict:
    _, output = run_cli(
        "run-workflow",
        "--file", str(FAILURE_WORKFLOW),
        "--run-profile", "dry-run",
        "--synthetic-on-capture-fail",
    )
    return parse_json_output(output)


class TestFailureDiagnosis:

    def test_failed_steps_exist(self):
        """failure_diagnosis_workflow 必须产生失败步骤。"""
        data = run_failure_workflow()
        failed = [s for s in data["steps"] if s["status"] == "failed"]
        assert failed, "预期有失败步骤，全部通过了（workflow 可能有问题）"

    def test_every_failed_step_has_diagnosis(self):
        """每个失败步骤必须有 failure_diagnosis 块。"""
        data = run_failure_workflow()
        for step in data["steps"]:
            if step["status"] != "failed":
                continue
            diag = step.get("metadata", {}).get("failure_diagnosis")
            assert diag, (
                f"失败步骤 {step['id']} 没有 failure_diagnosis，"
                "AI 无法理解失败原因"
            )

    def test_diagnosis_has_expected_and_actual(self):
        """诊断必须说清楚：预期什么、实际看到什么。"""
        data = run_failure_workflow()
        for step in data["steps"]:
            if step["status"] != "failed":
                continue
            diag = step["metadata"]["failure_diagnosis"]
            assert diag.get("expected"), f"{step['id']}: diagnosis 缺少 expected"
            assert diag.get("actual"), f"{step['id']}: diagnosis 缺少 actual"

    def test_diagnosis_has_recovery_suggestions(self):
        """诊断必须有至少 1 条有意义的恢复建议（> 10 字符）。"""
        data = run_failure_workflow()
        for step in data["steps"]:
            if step["status"] != "failed":
                continue
            suggestions = step["metadata"]["failure_diagnosis"].get("recovery_suggestions", [])
            assert suggestions, f"{step['id']}: 没有 recovery_suggestions"
            for s in suggestions:
                assert len(str(s)) > 10, f"恢复建议太短，没有价值: {s!r}"

    def test_diagnosis_not_generic(self):
        """恢复建议不能是通用废话（如'请重试'）。"""
        data = run_failure_workflow()
        generic_phrases = {"请重试", "try again", "unknown error", "error occurred"}
        for step in data["steps"]:
            if step["status"] != "failed":
                continue
            suggestions = step["metadata"]["failure_diagnosis"].get("recovery_suggestions", [])
            for s in suggestions:
                for phrase in generic_phrases:
                    assert phrase.lower() not in str(s).lower(), (
                        f"恢复建议是通用废话: {s!r}"
                    )
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_failure_diagnosis.py -v --tb=short
```

---

### E2E-IMPL-06：test_e2e_queue.py

**新增文件：`tests/e2e/test_e2e_queue.py`**

```python
"""
E2E-07：队列提交 → 执行 → 报告链路。
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from tests.e2e.conftest import ROOT, WORKSPACE, run_cli, parse_json_output


class TestQueue:

    def test_submit_and_run_produces_report(self):
        """提交到队列，run-next 后必须产生报告。"""
        before_count = len(list((WORKSPACE / "reports").glob("*.json")))

        code, out = run_cli(
            "workspace-queue-submit",
            "--root", str(WORKSPACE),
            "--workflow", "local_html_form_workflow",
            "--run-profile", "dry-run",
        )
        assert code == 0, f"队列提交失败: {out}"

        code, out = run_cli(
            "workspace-queue-run-next",
            "--root", str(WORKSPACE),
        )
        assert code == 0, f"队列执行失败: {out}"

        after_count = len(list((WORKSPACE / "reports").glob("*.json")))
        assert after_count > before_count, "执行后报告数量没有增加"

    def test_queue_status_updates(self):
        """执行完成后队列中不应有 running 状态任务残留。"""
        run_cli(
            "workspace-queue-submit",
            "--root", str(WORKSPACE),
            "--workflow", "local_html_form_workflow",
            "--run-profile", "dry-run",
        )
        run_cli("workspace-queue-run-next", "--root", str(WORKSPACE))

        code, out = run_cli("workspace-queue-list", "--root", str(WORKSPACE))
        assert code == 0
        data = parse_json_output(out)
        running = [t for t in data.get("tasks", []) if t.get("status") == "running"]
        assert not running, f"执行完后仍有 running 状态任务: {running}"
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_queue.py -v --tb=short
```

---

### E2E-IMPL-07：将 E2E 加入 CI

**修改文件：`.github/workflows/visual-agent-quality-gate.yml`**

在现有 pytest 步骤之后增加：

```yaml
- name: Run E2E tests (no browser required)
  run: python -m pytest tests/e2e/ -v --tb=short -m "not browser" -q
```

**验收标准：** push 后 GitHub Actions 通过

---

### 第一阶段完成验收

```powershell
# 全部 E2E 测试（无浏览器）
.\.venv\Scripts\python.exe -m pytest tests/e2e/ -v -m "not browser" --tb=short

# 全量测试无回归
.\.venv\Scripts\python.exe -m pytest tests/ -q --tb=short
```

**两条命令全绿，第一阶段完成。**

---

## 四、第二阶段：AI 上下文层

**目标：解决 Codex 上下文过长的痛点，省 token，减少开新窗口频率**
**时间：1 个月内**
**核心设计约束：所有 AI-facing 输出严格遵守 token 预算**

---

### 设计：Token 预算分配

**context-snapshot 标准结构（≤ 500 token）：**

```
## Visual Agent Context                    [标题: 5 token]

Status: {N} failing / {N} passing         [状态行: 10 token]

Latest Failure:                            [最多 1 个失败: 100 token]
  Workflow: {name}
  Step: {id} ({action})
  Expected: {expected}
  Actual: {actual_summary}（最多 50 字符）
  Hint: {one_line_hint}
  Artifact: {run_dir_path}

Recent Passes: {name1}, {name2}, ...      [通过列表: 30 token]

Next action: {one_sentence}               [下一步: 20 token]
Context fetched: {timestamp}              [时间戳: 10 token]
```

**超出预算的处理规则：**
- actual 内容超 50 字符 → 截断加省略号
- 失败步骤超 1 个 → 只保留最新的
- 通过 workflow 超 5 个 → 只显示数量
- 所有路径只显示相对路径

---

### P2-01：agent_session.json 持久化

**目标：每次 workflow 运行后自动更新会话状态文件**

**新增文件：`src/visual_agent/session.py`**

```python
"""
持久化 agent 会话状态。
每次 workflow 运行后调用 update_agent_session()。
文件位置：workspace/agent_session.json
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from time import time
from typing import Any


SESSION_FILE = "agent_session.json"


@dataclass
class FailureSummary:
    workflow: str
    run_id: str
    step_id: str
    action: str
    expected: str
    actual: str           # 截断到 80 字符
    hint: str             # 单行恢复建议
    artifact_dir: str     # 相对路径


@dataclass
class AgentSession:
    updated_at: float
    passing_workflows: list[str]
    failing_workflows: list[str]
    latest_failure: FailureSummary | None
    next_action: str       # 给 AI 的一句话建议
    token_estimate: int    # 预估 token 数


def session_path(workspace: Path) -> Path:
    return workspace / SESSION_FILE


def update_agent_session(workspace: Path, run_result: Any) -> AgentSession:
    """
    workflow 运行完成后调用。
    run_result 是 WorkflowRunResult 对象。
    """
    existing = _load_session(workspace)
    session = _build_session(workspace, run_result, existing)
    _write_session(workspace, session)
    return session


def load_agent_session(workspace: Path) -> AgentSession | None:
    return _load_session(workspace)


def _load_session(workspace: Path) -> AgentSession | None:
    path = session_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        failure = data.get("latest_failure")
        return AgentSession(
            updated_at=data.get("updated_at", 0.0),
            passing_workflows=data.get("passing_workflows", []),
            failing_workflows=data.get("failing_workflows", []),
            latest_failure=FailureSummary(**failure) if failure else None,
            next_action=data.get("next_action", ""),
            token_estimate=data.get("token_estimate", 0),
        )
    except Exception:
        return None


def _build_session(
    workspace: Path,
    run_result: Any,
    existing: AgentSession | None,
) -> AgentSession:
    from .models import ActionStatus

    workflow_name = str(getattr(run_result, "workflow_name", "unknown"))
    steps = list(getattr(run_result, "steps", []))
    run_id = str(getattr(run_result, "run_id", ""))

    failed_steps = [s for s in steps if getattr(s, "status", None) == ActionStatus.FAILED]
    run_passed = len(failed_steps) == 0

    # 更新通过/失败列表
    passing = list(existing.passing_workflows) if existing else []
    failing = list(existing.failing_workflows) if existing else []

    if run_passed:
        if workflow_name not in passing:
            passing.append(workflow_name)
        if workflow_name in failing:
            failing.remove(workflow_name)
        latest_failure = existing.latest_failure if existing else None
    else:
        if workflow_name not in failing:
            failing.append(workflow_name)
        if workflow_name in passing:
            passing.remove(workflow_name)
        latest_failure = _extract_failure_summary(
            workflow_name, run_id, failed_steps[0], workspace
        )

    next_action = _suggest_next_action(run_passed, workflow_name, latest_failure)
    session = AgentSession(
        updated_at=time(),
        passing_workflows=passing[-10:],   # 最多保留 10 个
        failing_workflows=failing[-5:],    # 最多保留 5 个
        latest_failure=latest_failure,
        next_action=next_action,
        token_estimate=0,
    )
    session.token_estimate = _estimate_tokens(session)
    return session


def _extract_failure_summary(
    workflow: str,
    run_id: str,
    failed_step: Any,
    workspace: Path,
) -> FailureSummary:
    step_id = str(getattr(failed_step, "id", ""))
    action = str(getattr(failed_step, "action", ""))
    meta = dict(getattr(failed_step, "metadata", {}) or {})
    diag = meta.get("failure_diagnosis", {}) or {}

    expected = str(diag.get("expected", ""))[:100]
    actual_raw = str(diag.get("actual", ""))
    actual = actual_raw[:80] + ("..." if len(actual_raw) > 80 else "")

    suggestions = diag.get("recovery_suggestions", [])
    hint = str(suggestions[0]) if suggestions else "Review step parameters."
    hint = hint[:120]

    run_dir = str(workspace / "runs" / run_id)
    try:
        rel = str(Path(run_dir).relative_to(workspace.parent))
    except ValueError:
        rel = run_dir

    return FailureSummary(
        workflow=workflow,
        run_id=run_id,
        step_id=step_id,
        action=action,
        expected=expected,
        actual=actual,
        hint=hint,
        artifact_dir=rel,
    )


def _suggest_next_action(passed: bool, workflow: str, failure: FailureSummary | None) -> str:
    if passed:
        return f"{workflow} passed. Run verify to check all workflows."
    if failure:
        return (
            f"{failure.workflow} fails at {failure.step_id}. "
            f"{failure.hint} Then run run_verification to confirm fix."
        )
    return "Run run_verification to check current workflow status."


def _estimate_tokens(session: AgentSession) -> int:
    text = _session_to_snapshot_text(session)
    # 粗略估算：4 字符 ≈ 1 token
    return len(text) // 4


def _session_to_snapshot_text(session: AgentSession) -> str:
    lines = ["## Visual Agent Context\n"]
    n_fail = len(session.failing_workflows)
    n_pass = len(session.passing_workflows)
    lines.append(f"Status: {n_fail} failing / {n_pass} passing\n")

    if session.latest_failure:
        f = session.latest_failure
        lines.append(f"\nLatest Failure:")
        lines.append(f"  Workflow: {f.workflow}")
        lines.append(f"  Step: {f.step_id} ({f.action})")
        lines.append(f"  Expected: {f.expected}")
        lines.append(f"  Actual: {f.actual}")
        lines.append(f"  Hint: {f.hint}")
        lines.append(f"  Artifacts: {f.artifact_dir}")

    if session.passing_workflows:
        names = ", ".join(session.passing_workflows[:5])
        lines.append(f"\nRecent Passes: {names}")

    lines.append(f"\nNext: {session.next_action}")
    return "\n".join(lines)


def _write_session(workspace: Path, session: AgentSession) -> None:
    path = session_path(workspace)
    data = asdict(session)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
```

**修改文件：`src/visual_agent/workspace.py`**

在 `run_workspace_workflow()` 函数末尾，报告导出之后，增加：

```python
# 更新 agent 会话状态
try:
    from .session import update_agent_session
    update_agent_session(workspace.root, result)
except Exception:
    pass  # session 更新失败不影响主流程
```

**新增测试文件：`tests/test_session.py`**

```python
from visual_agent.session import (
    AgentSession, FailureSummary,
    _session_to_snapshot_text, _estimate_tokens,
)

def test_snapshot_within_token_budget():
    """会话快照必须在 500 token 以内。"""
    session = AgentSession(
        updated_at=0.0,
        passing_workflows=["checkout_flow", "login_flow", "order_list"],
        failing_workflows=["order_export_flow"],
        latest_failure=FailureSummary(
            workflow="order_export_flow",
            run_id="20260603-xxx",
            step_id="assert_download_exists",
            action="assert_file_exists",
            expected="orders_2026.csv (size > 0)",
            actual="file not found after export button click",
            hint="Check onClick handler in OrderExport.tsx line ~45",
            artifact_dir=".agent-workspace/runs/20260603-xxx",
        ),
        next_action="order_export fails. Check onClick. Run verify after fix.",
        token_estimate=0,
    )
    text = _session_to_snapshot_text(session)
    tokens = len(text) // 4
    assert tokens <= 500, f"快照超出 token 预算: {tokens} token\n{text}"

def test_snapshot_no_secrets():
    """快照不能包含密码、cookie、token 字段值。"""
    session = AgentSession(
        updated_at=0.0,
        passing_workflows=[],
        failing_workflows=[],
        latest_failure=None,
        next_action="All passing.",
        token_estimate=0,
    )
    text = _session_to_snapshot_text(session)
    for keyword in ("password", "cookie", "Bearer ", "api_key"):
        assert keyword not in text
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_session.py -v
```

---

### P2-02：context-snapshot CLI 命令

**修改文件：`src/visual_agent/cli.py`**

新增 `context-snapshot` 命令，输出严格控制在 500 token 内：

```python
if args.command == "context-snapshot":
    from .session import load_agent_session, _session_to_snapshot_text
    from .workspace import Workspace
    from pathlib import Path

    ws = Workspace(root=Path(args.workspace_root))
    session = load_agent_session(ws.root)

    if session is None:
        print("No session data yet. Run a workflow first.")
        return 0

    text = _session_to_snapshot_text(session)
    token_est = len(text) // 4

    if token_est > 500:
        # 强制截断
        words = text.split()
        truncated = []
        count = 0
        for word in words:
            count += len(word) // 4 + 1
            if count > 450:
                truncated.append("...[truncated, use MCP tools for details]")
                break
            truncated.append(word)
        text = " ".join(truncated)

    if getattr(args, "format", "text") == "markdown":
        print(text)
    else:
        import json as _json
        print(_json.dumps({
            "snapshot": text,
            "token_estimate": token_est,
            "within_budget": token_est <= 500,
        }, ensure_ascii=False, indent=2))
    return 0
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli context-snapshot \
  --workspace-root .agent-workspace --format markdown
```

**验收标准：** 输出 ≤ 500 token（字符数 ≤ 2000），没有密码、token、cookie

---

### P2-03：summarize_latest_failure CLI + MCP

**新增文件：`src/visual_agent/failure_summary.py`**

```python
"""
生成面向 AI 的失败摘要，严格控制在 400 token 以内。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def build_failure_summary(workspace: Path, *, max_chars: int = 1600) -> dict[str, Any]:
    """
    读取最近一次失败的 run report，返回 AI-friendly 摘要。
    max_chars=1600 约对应 400 token。
    """
    from .session import load_agent_session

    session = load_agent_session(workspace)
    if session is None or session.latest_failure is None:
        return {"status": "no_failure", "message": "No recent failures found."}

    f = session.latest_failure
    prompt = (
        f"The workflow '{f.workflow}' fails at step '{f.step_id}' ({f.action}). "
        f"Expected: {f.expected}. "
        f"Actual: {f.actual}. "
        f"Suggested fix: {f.hint}"
    )

    return {
        "workflow": f.workflow,
        "run_id": f.run_id,
        "failed_step": {
            "id": f.step_id,
            "action": f.action,
        },
        "expected": f.expected,
        "actual": f.actual,
        "hint": f.hint,
        "artifacts": f.artifact_dir,
        "suggested_next_prompt": prompt,
        "token_estimate": len(prompt) // 4,
    }
```

**在 `mcp_server.py` 中新增第 6 个工具 `summarize_latest_failure`：**

```python
Tool(
    name="summarize_latest_failure",
    description=(
        "Get a token-efficient summary (≤400 tokens) of the latest workflow failure. "
        "Returns failed step, expected vs actual, probable cause, and a ready-to-use "
        "suggested prompt. Use this instead of reading full run reports to save tokens."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string"},
        },
        "required": ["workspace_root"],
    },
)
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_mcp.py -k "failure" -v
```

---

### P2-04：get_session_context MCP 工具

**在 `mcp_server.py` 中新增第 7 个工具：**

```python
Tool(
    name="get_session_context",
    description=(
        "Get a compact context snapshot (≤500 tokens) to resume work after opening "
        "a new chat window. Returns current pass/fail status, latest failure summary, "
        "and suggested next action. Call this at the start of a new session."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string"},
        },
        "required": ["workspace_root"],
    },
)
```

**验收测试（加入 test_e2e_mcp.py）：**

```python
def test_get_session_context_within_token_budget():
    """get_session_context 输出必须在 500 token 以内。"""
    from visual_agent.mcp_server import _get_session_context
    result = arun(_get_session_context({"workspace_root": ws()}))
    text = result[0].text
    token_est = len(text) // 4
    assert token_est <= 500, f"context 超出 token 预算: {token_est} token"

def test_get_session_context_no_secrets():
    from visual_agent.mcp_server import _get_session_context
    result = arun(_get_session_context({"workspace_root": ws()}))
    text = result[0].text
    for keyword in ("password", "cookie", "Bearer ", "demo123"):
        assert keyword not in text, f"快照包含敏感词: {keyword}"
```

---

### 第二阶段完成验收

```powershell
# 会话状态测试
.\.venv\Scripts\python.exe -m pytest tests/test_session.py -v

# context-snapshot 命令
.\.venv\Scripts\python.exe -m visual_agent.cli context-snapshot \
  --workspace-root .agent-workspace --format markdown

# MCP 工具测试（含新增工具）
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_e2e_mcp.py -v

# 全量测试
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

**人工验收：**
在 Claude Code 新窗口中输入：
> "调用 get_session_context 告诉我现在的状态"

Claude 返回内容能让你在 30 秒内理解当前工作进展。

---

## 五、第三阶段：visual-agent verify

**目标：AI 改完代码后，一条命令验收，输出 AI-ready 报告**
**时间：2 个月内**

---

### P3-01：workflow tags 支持

**修改文件：`src/visual_agent/workflow.py`**

在 `Workflow` dataclass 中增加 `tags` 字段：

```python
@dataclass(frozen=True)
class Workflow:
    name: str
    version: int
    steps: tuple[WorkflowStep, ...]
    schema_version: int | None = None
    min_runtime_version: str | None = None
    tags: tuple[str, ...] = ()    # 新增
```

在 `parse_workflow_file()` 中解析 tags：

```python
tags = tuple(str(t) for t in data.get("tags", []))
```

**业务 workflow 示例（加入 verification tag）：**

```yaml
# examples/checkout_verification_workflow.yaml
schema_version: 1
name: checkout_verification
version: 1
tags:
  - verification
  - checkout
steps:
  ...
```

**验收命令：**
```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_workflow_tags.py -v
```

---

### P3-02：verify 核心逻辑

**新增文件：`src/visual_agent/verify.py`**

```python
"""
verify 命令：运行所有 verification 标签的 workflow，
生成 AI-friendly 验收报告（严格 ≤ 800 token）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import Workspace, discover_workflows, run_workspace_workflow
from .failure_summary import build_failure_summary


@dataclass
class WorkflowVerifyResult:
    name: str
    passed: bool
    step_count: int
    failed_step: str | None
    hint: str | None
    run_id: str


@dataclass
class VerificationReport:
    total: int
    passed: int
    failed: int
    results: list[WorkflowVerifyResult]
    suggested_prompt: str        # 给 AI 的一句话
    token_estimate: int


def run_verify(
    workspace: Workspace,
    *,
    tags: tuple[str, ...] = ("verification",),
    run_profile: str = "dry-run",
) -> VerificationReport:
    workflows = [
        wf for wf in discover_workflows(workspace)
        if _has_tag(wf, tags, workspace)
    ]

    results: list[WorkflowVerifyResult] = []
    for wf_ref in workflows[:10]:  # 最多 10 个，避免超时
        try:
            result = run_workspace_workflow(
                workspace,
                wf_ref.name,
                run_profile=run_profile,
                export_report=True,
            )
            steps = list(result.steps)
            failed = next(
                (s for s in steps if str(getattr(s, "status", "")) == "failed"),
                None,
            )
            hint = None
            if failed:
                meta = dict(getattr(failed, "metadata", {}) or {})
                diag = meta.get("failure_diagnosis", {}) or {}
                suggestions = diag.get("recovery_suggestions", [])
                hint = str(suggestions[0])[:100] if suggestions else None

            results.append(WorkflowVerifyResult(
                name=wf_ref.name,
                passed=failed is None,
                step_count=len(steps),
                failed_step=str(getattr(failed, "id", "")) if failed else None,
                hint=hint,
                run_id=str(result.run_id),
            ))
        except Exception as exc:
            results.append(WorkflowVerifyResult(
                name=wf_ref.name,
                passed=False,
                step_count=0,
                failed_step="execution_error",
                hint=str(exc)[:100],
                run_id="",
            ))

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    prompt = _build_verify_prompt(results)

    report = VerificationReport(
        total=len(results),
        passed=passed,
        failed=failed,
        results=results,
        suggested_prompt=prompt,
        token_estimate=len(prompt) // 4,
    )
    return report


def verify_to_markdown(report: VerificationReport) -> str:
    """生成 AI-friendly Markdown，≤ 800 token。"""
    lines = [f"## Verification Report\n"]
    lines.append(f"Ran {report.total} workflows: {report.passed} passed, {report.failed} failed\n")

    failed = [r for r in report.results if not r.passed]
    passed = [r for r in report.results if r.passed]

    if failed:
        lines.append("### Failed")
        for r in failed:
            lines.append(f"✗ **{r.name}**")
            if r.failed_step:
                lines.append(f"  Step: {r.failed_step}")
            if r.hint:
                lines.append(f"  Fix: {r.hint}")
            lines.append("")

    if passed:
        names = ", ".join(r.name for r in passed)
        lines.append(f"### Passed\n✓ {names}\n")

    lines.append(f"### Suggested Action\n{report.suggested_prompt}")
    result = "\n".join(lines)

    # 强制截断到 800 token
    if len(result) > 3200:
        result = result[:3100] + "\n...[use get_run_report for full details]"

    return result


def _has_tag(wf_ref: Any, tags: tuple[str, ...], workspace: Workspace) -> bool:
    try:
        from .workflow import parse_workflow_file
        wf = parse_workflow_file(wf_ref.path)
        wf_tags = set(getattr(wf, "tags", ()))
        return bool(wf_tags & set(tags))
    except Exception:
        return False


def _build_verify_prompt(results: list[WorkflowVerifyResult]) -> str:
    failed = [r for r in results if not r.passed]
    if not failed:
        return "All verification workflows passed. Code changes look good."

    parts = []
    for r in failed[:2]:  # 最多展示 2 个失败
        part = f"{r.name} fails"
        if r.failed_step:
            part += f" at {r.failed_step}"
        if r.hint:
            part += f". {r.hint}"
        parts.append(part)

    return " ".join(parts) + " Fix these issues and run verify again."
```

---

### P3-03：verify CLI 命令 + run_verification MCP 工具

**CLI 命令：**
```powershell
.\.venv\Scripts\python.exe -m visual_agent.cli verify \
  --workspace-root .agent-workspace \
  --for codex \
  --format markdown
```

**MCP 工具（第 8 个）：**

```python
Tool(
    name="run_verification",
    description=(
        "Run all verification-tagged workflows and return AI-friendly report (≤800 tokens). "
        "Use after code changes to confirm UI still works. "
        "Returns pass/fail per workflow and actionable fix suggestions."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "workspace_root": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["verification"],
            },
            "run_profile": {
                "type": "string",
                "enum": ["dry-run", "supervised"],
                "default": "dry-run",
            },
        },
        "required": ["workspace_root"],
    },
)
```

---

### 第三阶段完成验收

```powershell
# verify 命令
.\.venv\Scripts\python.exe -m visual_agent.cli verify \
  --workspace-root .agent-workspace --format markdown

# 全量测试
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

**人工验收：**
在 Claude Code 里说：
> "我刚改完 checkout 相关代码，帮我验证一下"

Claude 调用 `run_verification`，30 秒内返回通过/失败和修复建议。

---

## 六、安全检查清单

每次发布前必须全部通过：

```
[x] MCP workspace_root 不接受 .. 路径
[x] MCP run_workflow 默认 dry-run
[x] MCP approved 必须在 workspace.json 白名单
[x] MCP get_run_report 经过 scrub_secrets
[x] MCP get_run_report 超预算时截断并保留完整报告路径提示
[x] MCP 单次响应超过 2000 token 时有通用兜底截断
[x] list_workflows / list_run_artifacts 大列表结构化截断并返回 omitted_count
[x] context-snapshot 不含 secret/cookie/password
[x] summarize_latest_failure 不含 secret
[x] artifact 路径全在 workspace 内
[x] 全量 pytest 通过
[x] test_e2e_mcp.py 中安全测试全通过
```

---

## 七、MCP 工具完整清单（最终版）

| # | 工具名 | 用途 | token 上限 |
|---|---|---|---|
| 1 | list_workflows | 列出可用 workflow | 无限制 |
| 2 | validate_workflow | 校验 workflow | 无限制 |
| 3 | run_workflow | 执行 workflow（默认 dry-run） | 无限制 |
| 4 | get_run_report | 获取详细报告 | 2000 |
| 5 | list_run_artifacts | 列出 artifact 路径 | 无限制 |
| 6 | summarize_latest_failure | AI-ready 失败摘要 | **400** |
| 7 | get_session_context | 会话快照（新窗口恢复用） | **500** |
| 8 | run_verification | 批量验收报告 | **800** |

工具 6、7、8 是 AI 上下文管理的核心，token 上限不可突破。

兼容保留工具：
- get_workspace_dashboard
- get_latest_failure

---

## 八、给 Codex 的执行规则

1. **按阶段严格顺序**，第一阶段未全绿不开始第二阶段
2. **每个任务只做一件事**，完成后跑验收命令
3. **不许 mock 核心路径**：E2E 测试必须调用真实函数
4. **token 预算是硬约束**：6/7/8 号工具超预算视为 bug
5. **遇到阻塞标记 skipped**，写明原因，继续推进其他任务
6. **不许改已有测试的断言**来让测试通过
7. **每阶段完成后更新本文档状态**

---

## 九、当前进度

### 第一阶段
- [x] E2E-IMPL-01 目录结构
- [x] E2E-IMPL-02 test_e2e_install.py
- [x] E2E-IMPL-03 test_e2e_local_form.py
- [x] E2E-IMPL-04 test_e2e_mcp.py ← 最重要
- [x] E2E-IMPL-05 test_e2e_failure_diagnosis.py
- [x] E2E-IMPL-06 test_e2e_queue.py
- [x] E2E-IMPL-07 加入 CI

### 第二阶段
- [x] P2-01 agent_session.json 持久化
- [x] P2-02 context-snapshot CLI
- [x] P2-03 summarize_latest_failure
- [x] P2-04 get_session_context MCP 工具

### 第三阶段
- [x] P3-01 workflow tags
- [x] P3-02 verify 核心逻辑
- [x] P3-03 verify CLI + run_verification MCP

### 最近验收
- [x] `.\.venv\Scripts\python.exe -m pip install -e ".[web,mcp]"`：success
- [x] `.\.venv\Scripts\python.exe -m visual_agent.cli doctor`：ok，DOM workflow ready；OCR/VLM 为可选缺失
- [x] `.\.venv\Scripts\python.exe -m visual_agent.cli quality-gate --profile ci --workspace-root <temp> --run --fail-on-secret-leak`：success
- [x] `.\.venv\Scripts\python.exe -m pytest tests/e2e/ -m "not browser" -q --tb=short`：14 passed, 6 deselected
- [x] `.\.venv\Scripts\python.exe -m pytest tests/ -q --tb=short`：564 passed

### 验证循环 Demo（2026-06-03）
- [x] checkout_verification_demo.html 创建并测试
- [x] checkout_verification workflow 创建并验证全流程
- [x] session.py Bug fix：latest_failure 在 workflow 转绿时清除
- [x] session.py Bug fix：actual 优先保留 visible_text 字段
- [x] quickstart.md 重写，移除本地绝对路径，加入验证循环 demo
- [x] demo 文件纳入 examples/ 并加入 init_workspace 初始化
