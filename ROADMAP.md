# Visual Agent 产品路线图 Phase 4+

> 版本：2026-06-05
> 前置：MASTER_DEV_PLAN.md 第一至第三阶段已完成
> 本文档描述第四阶段起的产品开发计划，是交付给 Codex 的执行文档。

---

## 产品共识（开发前必读）

### 核心定位

Visual Agent 是 AI 编程助手（Codex / Claude Code / Cursor / VS Code）的**验证层 + 执行记忆**。
AI 写代码，Visual Agent 看结果、记历史、诊断失败。

### 护城河来源

1. **Workflow 资产**：用户积累的 workflow 是最难迁移的资产，是产品核心
2. **生态位置**：深度集成进 VS Code / Cursor / Codex，成为 AI 编程工具链标准配置
3. **内容库**：用户公开分享的 workflow 形成平台内容护城河

### 开发原则

- 所有面向 AI 的输出遵守 token 预算（见 MASTER_DEV_PLAN.md 原则 1）
- 收费功能现在不激活，但接口必须预留
- 能复用现有代码的不新建文件
- Workflow YAML schema 改动必须向后兼容

---

## 阶段优先级总览

| 阶段 | 内容 | 优先级 | 预估工期 |
|---|---|---|---|
| Phase 4 | VS Code 扩展 | P0 最高 | 3 周 |
| Phase 5 | AI 生成 Workflow | P0 最高 | 2 周 |
| Phase 6 | 货币化基础设施 | P1 | 1 周 |
| Phase 7 | 上下文连续性（窗口切换记忆） | P1 | 1 周 |
| Phase 8 | Workflow 市场基础 | P2 | 2 周 |

---

## Phase 4：VS Code 扩展

**目标：进入 VS Code Marketplace 和 Cursor 扩展市场，建立生态位置**

Cursor 基于 VS Code 内核，VS Code 扩展自动兼容 Cursor。发布一次，两个市场都覆盖。

### 目录结构

```
vscode-extension/
├── package.json          # 扩展元数据、贡献点声明
├── tsconfig.json
├── src/
│   ├── extension.ts      # 扩展入口
│   ├── sidebar.ts        # 侧边栏 TreeView
│   ├── statusBar.ts      # 状态栏
│   ├── commands.ts       # 命令注册
│   └── bridge.ts         # 与 Python CLI 通信
├── icons/
│   └── visual-agent.png  # 128x128 图标
└── README.md
```

### 4-1：package.json

```json
{
  "name": "visual-agent",
  "displayName": "Visual Agent",
  "description": "UI verification co-pilot for AI coding assistants. Run workflows, see results, never lose context when switching windows.",
  "version": "0.1.0",
  "publisher": "visual-agent",
  "engines": { "vscode": "^1.85.0" },
  "categories": ["Testing", "Other"],
  "activationEvents": ["onStartupFinished"],
  "main": "./out/extension.js",
  "contributes": {
    "viewsContainers": {
      "activitybar": [{
        "id": "visual-agent",
        "title": "Visual Agent",
        "icon": "icons/visual-agent.png"
      }]
    },
    "views": {
      "visual-agent": [{
        "id": "visualAgentWorkflows",
        "name": "Workflows"
      }]
    },
    "commands": [
      {
        "command": "visualAgent.runAll",
        "title": "Visual Agent: Run All Workflows"
      },
      {
        "command": "visualAgent.runAffected",
        "title": "Visual Agent: Run Affected Workflows"
      },
      {
        "command": "visualAgent.showLatestFailure",
        "title": "Visual Agent: Show Latest Failure"
      },
      {
        "command": "visualAgent.generateWorkflow",
        "title": "Visual Agent: Generate Workflow from Description"
      },
      {
        "command": "visualAgent.connectCloud",
        "title": "Visual Agent: Connect to Cloud (Pro)"
      }
    ],
    "configuration": {
      "title": "Visual Agent",
      "properties": {
        "visualAgent.workspaceRoot": {
          "type": "string",
          "default": ".agent-workspace",
          "description": "Workspace root for Visual Agent data."
        },
        "visualAgent.runOnSave": {
          "type": "boolean",
          "default": false,
          "description": "Run affected workflows on file save."
        },
        "visualAgent.autoRunTags": {
          "type": "array",
          "default": ["fast"],
          "description": "Tags to include in auto-run on save."
        },
        "visualAgent.pythonPath": {
          "type": "string",
          "default": "python",
          "description": "Path to Python interpreter."
        }
      }
    }
  },
  "scripts": {
    "compile": "tsc -p ./",
    "watch": "tsc -watch -p ./"
  },
  "devDependencies": {
    "@types/vscode": "^1.85.0",
    "@types/node": "^20.0.0",
    "typescript": "^5.0.0"
  }
}
```

### 4-2：bridge.ts（Python CLI 通信层）

```typescript
import * as cp from "child_process";
import * as vscode from "vscode";

export interface WorkflowStatus {
  name: string;
  status: "passed" | "failed" | "unknown";
  elapsed?: number;
  failedStep?: string;
  hint?: string;
}

export interface SessionSnapshot {
  passingWorkflows: string[];
  failingWorkflows: string[];
  latestFailure?: {
    workflow: string;
    stepId: string;
    action: string;
    expected: string;
    actual: string;
    hint: string;
    artifactDir: string;
  };
  nextAction: string;
}

function getPythonPath(): string {
  return vscode.workspace
    .getConfiguration("visualAgent")
    .get("pythonPath", "python");
}

function getWorkspaceRoot(): string {
  return vscode.workspace
    .getConfiguration("visualAgent")
    .get("workspaceRoot", ".agent-workspace");
}

export function runCli(args: string[]): Promise<{ code: number; output: string }> {
  return new Promise((resolve) => {
    const python = getPythonPath();
    const wsRoot = getWorkspaceRoot();
    const cwd = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();

    const allArgs = ["-m", "visual_agent.cli", ...args, "--workspace-root", wsRoot];
    const proc = cp.spawn(python, allArgs, { cwd });

    let output = "";
    proc.stdout.on("data", (d) => (output += d.toString()));
    proc.stderr.on("data", (d) => (output += d.toString()));
    proc.on("close", (code) => resolve({ code: code ?? 1, output }));
  });
}

export async function getSessionSnapshot(): Promise<SessionSnapshot | null> {
  const { code, output } = await runCli(["context-snapshot", "--format", "json"]);
  if (code !== 0) return null;
  try {
    const data = JSON.parse(output.trim());
    return data as SessionSnapshot;
  } catch {
    return null;
  }
}

export async function runCodexCheck(includeSlow = false): Promise<string> {
  const args = ["codex-check", "--format", "json"];
  if (includeSlow) args.push("--include-slow");
  const { output } = await runCli(args);
  return output;
}
```

### 4-3：sidebar.ts（侧边栏 TreeView）

```typescript
import * as vscode from "vscode";
import { getSessionSnapshot, SessionSnapshot } from "./bridge";

export class WorkflowTreeProvider implements vscode.TreeDataProvider<WorkflowItem> {
  private _onDidChangeTreeData = new vscode.EventEmitter<WorkflowItem | undefined>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  private snapshot: SessionSnapshot | null = null;

  refresh(): void {
    getSessionSnapshot().then((s) => {
      this.snapshot = s;
      this._onDidChangeTreeData.fire(undefined);
    });
  }

  getTreeItem(element: WorkflowItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: WorkflowItem): WorkflowItem[] {
    if (element) return [];
    if (!this.snapshot) {
      return [new WorkflowItem("No session yet. Run a workflow first.", "info")];
    }

    const items: WorkflowItem[] = [];
    for (const name of this.snapshot.failingWorkflows) {
      items.push(new WorkflowItem(name, "failed"));
    }
    for (const name of this.snapshot.passingWorkflows) {
      items.push(new WorkflowItem(name, "passed"));
    }
    return items;
  }
}

class WorkflowItem extends vscode.TreeItem {
  constructor(label: string, kind: "passed" | "failed" | "info") {
    super(label, vscode.TreeItemCollapsibleState.None);
    if (kind === "passed") {
      this.iconPath = new vscode.ThemeIcon("check", new vscode.ThemeColor("testing.iconPassed"));
    } else if (kind === "failed") {
      this.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed"));
    } else {
      this.iconPath = new vscode.ThemeIcon("info");
    }
  }
}
```

### 4-4：statusBar.ts

```typescript
import * as vscode from "vscode";
import { getSessionSnapshot } from "./bridge";

let statusBarItem: vscode.StatusBarItem;

export function initStatusBar(context: vscode.ExtensionContext): void {
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = "visualAgent.showLatestFailure";
  context.subscriptions.push(statusBarItem);
  refreshStatusBar();
}

export async function refreshStatusBar(): Promise<void> {
  const snapshot = await getSessionSnapshot();
  if (!snapshot) {
    statusBarItem.text = "$(eye) Visual Agent";
    statusBarItem.tooltip = "No session yet.";
  } else {
    const failing = snapshot.failingWorkflows.length;
    const passing = snapshot.passingWorkflows.length;
    const total = failing + passing;
    if (failing === 0) {
      statusBarItem.text = `$(check) Visual Agent: ${passing}/${total}`;
      statusBarItem.backgroundColor = undefined;
    } else {
      statusBarItem.text = `$(error) Visual Agent: ${passing}/${total}`;
      statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    }
    statusBarItem.tooltip = snapshot.nextAction;
  }
  statusBarItem.show();
}
```

### 4-5：extension.ts（入口）

```typescript
import * as vscode from "vscode";
import { WorkflowTreeProvider } from "./sidebar";
import { initStatusBar, refreshStatusBar } from "./statusBar";
import { runCli, runCodexCheck } from "./bridge";

export function activate(context: vscode.ExtensionContext) {
  const treeProvider = new WorkflowTreeProvider();

  vscode.window.registerTreeDataProvider("visualAgentWorkflows", treeProvider);
  initStatusBar(context);

  // 命令：运行所有 workflow
  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.runAll", async () => {
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Running all workflows..." },
        () => runCli(["codex-check", "--format", "json"])
      );
      treeProvider.refresh();
      refreshStatusBar();
      vscode.window.showInformationMessage(result.output.slice(0, 200));
    })
  );

  // 命令：运行受影响的 workflow
  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.runAffected", async () => {
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Running affected workflows..." },
        () => runCodexCheck()
      );
      treeProvider.refresh();
      refreshStatusBar();
    })
  );

  // 命令：显示最新失败
  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.showLatestFailure", async () => {
      const { output } = await runCli(["summarize-latest-failure", "--format", "json"]);
      const panel = vscode.window.createWebviewPanel(
        "visualAgentFailure",
        "Visual Agent: Latest Failure",
        vscode.ViewColumn.Beside,
        {}
      );
      panel.webview.html = `<pre style="font-family:monospace;padding:16px">${output}</pre>`;
    })
  );

  // 命令：AI 生成 workflow（Phase 5 实现后激活）
  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.generateWorkflow", async () => {
      const description = await vscode.window.showInputBox({
        prompt: "描述你想验证的流程（例如：验证用户登录后首页显示用户名）",
        placeHolder: "用自然语言描述...",
      });
      if (!description) return;
      const { code, output } = await runCli(["generate-workflow", "--description", description]);
      if (code !== 0) {
        vscode.window.showErrorMessage("生成失败：" + output.slice(0, 200));
        return;
      }
      // 打开生成的 workflow 文件
      const match = output.match(/Saved to: (.+\.yaml)/);
      if (match) {
        const doc = await vscode.workspace.openTextDocument(match[1]);
        vscode.window.showTextDocument(doc);
      }
    })
  );

  // 命令：连接云端（收费功能占位）
  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.connectCloud", () => {
      vscode.window.showInformationMessage(
        "Cloud runs are coming in Pro plan. Star us on GitHub to stay updated.",
        "Open GitHub"
      ).then((action) => {
        if (action === "Open GitHub") {
          vscode.env.openExternal(vscode.Uri.parse("https://github.com/your-org/visual-agent"));
        }
      });
    })
  );

  // 保存时自动运行（可配置）
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      const config = vscode.workspace.getConfiguration("visualAgent");
      if (!config.get("runOnSave", false)) return;
      const tags = config.get<string[]>("autoRunTags", ["fast"]);
      await runCli(["codex-check", "--tags", ...tags]);
      treeProvider.refresh();
      refreshStatusBar();
    })
  );

  // 启动时刷新一次
  treeProvider.refresh();
}

export function deactivate() {}
```

### 4 验收标准

```
- [ ] vsce package 能打出 .vsix 文件无报错
- [ ] 安装到 VS Code 后侧边栏出现 Visual Agent 图标
- [ ] 有 session 数据时侧边栏显示通过/失败列表
- [ ] 状态栏显示通过/失败计数，失败时红色背景
- [ ] 命令面板能搜索到所有 5 个命令
- [ ] runOnSave=true 时保存 .ts/.py 文件触发 codex-check
- [ ] "Connect to Cloud" 命令显示 Pro 提示而不崩溃
```

---

## Phase 5：AI 生成 Workflow

**目标：用自然语言描述验证场景，AI 自动生成可运行的 workflow YAML**

这是把产品用户从"只有开发者"扩展到"所有人"的关键功能。

### 5-1：新增 CLI 命令 `generate-workflow`

**修改文件：`src/visual_agent/cli.py`**

在 subparsers 区块添加：

```python
gen_workflow = subparsers.add_parser(
    "generate-workflow",
    help="Generate a workflow YAML from a natural language description."
)
gen_workflow.add_argument("--description", required=True, help="Natural language description of the workflow.")
gen_workflow.add_argument("--output", default=None, help="Output YAML file path. Default: auto-named in workflows/.")
gen_workflow.add_argument("--workspace-root", default=".agent-workspace")
gen_workflow.add_argument("--model", default="claude-haiku-4-5-20251001", help="LLM model to use for generation.")
gen_workflow.add_argument("--dry-run", action="store_true", help="Print generated YAML without saving.")
```

在命令分发区块添加：

```python
if args.command == "generate-workflow":
    from .workflow_generator import generate_workflow_yaml
    result = generate_workflow_yaml(
        description=args.description,
        workspace_root=Path(args.workspace_root).resolve(),
        output_path=Path(args.output) if args.output else None,
        model=args.model,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1
```

### 5-2：新增 `src/visual_agent/workflow_generator.py`

```python
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

WORKFLOW_SYSTEM_PROMPT = """You are a workflow YAML generator for Visual Agent, a UI verification tool.

Generate a valid workflow YAML based on the user's description.

YAML schema:
```yaml
name: snake_case_name          # required, unique identifier
description: "Human readable"  # required
tags: [verification, fast]     # required, always include "verification"
visibility: private             # required: private | public
author: ""                      # leave empty, filled by platform
steps:
  - id: step_id                 # snake_case, unique within workflow
    action: navigate            # see actions below
    url: "https://..."          # for navigate
  - id: step2
    action: click_text
    text: "Button Label"
  - id: step3
    action: type
    selector: "input[name=email]"
    text: "test@example.com"
  - id: step4
    action: assert_text
    text: "Expected text on page"
  - id: step5
    action: wait_for_text
    text: "Loading complete"
    timeout: 5000
```

Available actions:
- navigate: url (required)
- click_text: text (required), timeout (optional, ms)
- click: selector (required)
- type: selector (required), text (required)
- assert_text: text (required) — FAILS if text not found, use this to verify outcomes
- assert_no_error: checks for error messages on page
- wait_for_text: text (required), timeout (optional)
- press_key: key (required, e.g. "Enter", "Tab")

Rules:
1. Always start with a navigate step
2. End with assert_text steps to verify the expected outcome
3. Use descriptive step ids (e.g. fill_email, click_submit, verify_success)
4. tags must include "verification"; add "fast" if workflow takes < 10 seconds
5. name must be snake_case, descriptive, under 50 chars

Return ONLY the YAML, no explanation, no markdown fences."""


def generate_workflow_yaml(
    description: str,
    workspace_root: Path,
    output_path: Path | None = None,
    model: str = "claude-haiku-4-5-20251001",
    dry_run: bool = False,
) -> dict[str, Any]:
    try:
        import anthropic
    except ImportError:
        return _template_fallback(description, workspace_root, output_path, dry_run)

    client = anthropic.Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=1024,
        system=WORKFLOW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": description}],
    )
    yaml_text = message.content[0].text.strip()
    yaml_text = _strip_markdown_fences(yaml_text)

    return _save_or_return(yaml_text, description, workspace_root, output_path, dry_run)


def _template_fallback(
    description: str,
    workspace_root: Path,
    output_path: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    """当 anthropic SDK 不可用时，返回带占位符的模板。"""
    name = _description_to_name(description)
    yaml_text = f"""name: {name}
description: "{description}"
tags: [verification, fast]
visibility: private
author: ""
steps:
  - id: navigate_to_target
    action: navigate
    url: "https://example.com"
  - id: verify_outcome
    action: assert_text
    text: "Expected text here"
"""
    return _save_or_return(yaml_text, description, workspace_root, output_path, dry_run)


def _save_or_return(
    yaml_text: str,
    description: str,
    workspace_root: Path,
    output_path: Path | None,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {"status": "success", "yaml": yaml_text, "saved_to": None}

    if output_path is None:
        name = _extract_name_from_yaml(yaml_text) or _description_to_name(description)
        workflows_dir = workspace_root.parent / "workflows"
        workflows_dir.mkdir(parents=True, exist_ok=True)
        output_path = workflows_dir / f"{name}.yaml"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml_text, encoding="utf-8")
    return {
        "status": "success",
        "yaml": yaml_text,
        "saved_to": str(output_path),
        "message": f"Saved to: {output_path}",
    }


def _description_to_name(description: str) -> str:
    words = re.sub(r"[^\w\s]", "", description.lower()).split()
    return "_".join(words[:6]) or "generated_workflow"


def _extract_name_from_yaml(yaml_text: str) -> str | None:
    match = re.search(r"^name:\s*(\S+)", yaml_text, re.MULTILINE)
    return match.group(1) if match else None


def _strip_markdown_fences(text: str) -> str:
    text = re.sub(r"^```(?:yaml)?\n?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n?```$", "", text, flags=re.MULTILINE)
    return text.strip()
```

### 5-3：新增 MCP tool `generate_workflow`

**修改文件：`src/visual_agent/mcp_server.py`**

在 tools 列表中添加：

```python
Tool(
    name="generate_workflow",
    description=(
        "Generate a workflow YAML from a natural language description. "
        "The AI coding assistant can call this after writing new UI features "
        "to create a verification workflow automatically."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Natural language description of what to verify. E.g. 'Verify user can log in and see their dashboard'",
            },
            "workspace_root": {"type": "string"},
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "If true, return YAML without saving to disk.",
            },
        },
        "required": ["description"],
    },
),
```

添加处理函数：

```python
def generate_workflow_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .workflow_generator import generate_workflow_yaml

    description = str(args.get("description", "")).strip()
    if not description:
        return {"status": "error", "message": "description is required"}

    workspace = require_workspace(args)
    dry_run = bool(args.get("dry_run", False))

    return generate_workflow_yaml(
        description=description,
        workspace_root=workspace.root,
        dry_run=dry_run,
    )
```

### 5-4：Workflow YAML schema 新增字段

**修改所有现有 example workflow YAML，添加以下字段（向后兼容，缺省时用默认值）：**

```yaml
visibility: private    # private | public | unlisted
author: ""             # 平台填充，用户留空
license: ""            # public 时建议填 cc-by-4.0
```

**修改文件：`src/visual_agent/workspace.py`**

在 `WorkflowRef` dataclass 添加字段：

```python
@dataclass(frozen=True)
class WorkflowRef:
    name: str
    path: Path
    tags: list[str]
    visibility: str = "private"   # 新增
    author: str = ""               # 新增
    description: str = ""          # 新增
```

在 `discover_workflows` 函数中解析新字段：

```python
visibility = str(data.get("visibility", "private"))
author = str(data.get("author", ""))
description = str(data.get("description", ""))
```

### 5 验收标准

```
- [ ] `python -m visual_agent.cli generate-workflow --description "验证用户登录" --dry-run` 输出合法 YAML
- [ ] 安装 anthropic SDK 后生成的 YAML 包含真实步骤（非模板占位符）
- [ ] 未安装 anthropic SDK 时降级到模板，不崩溃
- [ ] MCP tool generate_workflow 在 Claude Code 里可调用
- [ ] 生成的 YAML 能被 `run-workflow` 命令正常执行
- [ ] VS Code 扩展里 "Generate Workflow" 命令调用后打开生成的文件
```

---

## Phase 6：货币化基础设施

**目标：预留收费接口，现在不激活，但接口设计好，未来随时可以打开**

### 6-1：新增 `src/visual_agent/licensing.py`

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TierName = Literal["free", "pro", "team", "enterprise"]

FREE_FEATURES = frozenset({
    "local_run",
    "mcp_server",
    "codex_check",
    "basic_report",
    "context_snapshot",
    "generate_workflow",
    "vscode_extension",
})

PRO_FEATURES = frozenset({
    "cloud_run",
    "ci_github_check",
    "workflow_history_30d",
    "priority_support",
})

TEAM_FEATURES = frozenset({
    "team_workspace",
    "shared_workflow_library",
    "workflow_history_unlimited",
    "audit_log_export",
})


@dataclass(frozen=True)
class License:
    tier: TierName
    seats: int = 1
    expires_at: float | None = None


def get_license() -> License:
    """
    现在永远返回 free tier。
    未来：从 ~/.visual-agent/license.json 或环境变量 VISUAL_AGENT_LICENSE_KEY 读取。
    """
    return License(tier="free")


def check_feature(feature: str) -> bool:
    """检查当前 license 是否包含某功能。"""
    lic = get_license()
    if feature in FREE_FEATURES:
        return True
    if lic.tier in ("pro", "team", "enterprise") and feature in PRO_FEATURES:
        return True
    if lic.tier in ("team", "enterprise") and feature in TEAM_FEATURES:
        return True
    return False


class FeatureGatedError(Exception):
    def __init__(self, feature: str):
        self.feature = feature
        super().__init__(
            f"Feature '{feature}' requires a paid plan. "
            f"Visit https://visualagent.dev/upgrade to unlock."
        )


def require_feature(feature: str) -> None:
    """在收费功能入口调用。现在不拦截，占位。"""
    if not check_feature(feature):
        raise FeatureGatedError(feature)
```

### 6-2：usage 计数器加入 AgentSession

**修改文件：`src/visual_agent/session.py`**

在 `AgentSession` dataclass 添加：

```python
@dataclass(frozen=True)
class AgentSession:
    updated_at: float
    passing_workflows: list[str]
    failing_workflows: list[str]
    latest_failure: FailureSummary | None
    next_action: str
    token_estimate: int
    runs_this_month: int = 0          # 新增：本月运行次数
    cloud_runs_used: int = 0          # 新增：云端运行次数（Pro 功能）
    usage_reset_date: str = ""        # 新增：格式 YYYY-MM
```

在 `_build_session` 中更新计数：

```python
from datetime import datetime

current_month = datetime.now().strftime("%Y-%m")
prev_month = existing.usage_reset_date if existing else ""
prev_runs = existing.runs_this_month if existing and prev_month == current_month else 0

# 在构建新 session 时
runs_this_month = prev_runs + 1
usage_reset_date = current_month
```

### 6-3：云端运行占位入口

**新增文件：`src/visual_agent/cloud.py`**

```python
from __future__ import annotations

from pathlib import Path

from .licensing import require_feature, FeatureGatedError


def run_remote_workflow(workflow_name: str, workspace_root: Path) -> dict:
    """
    云端运行 workflow 占位。
    Pro 功能：用户不需要本地安装 Playwright/浏览器，在云端执行。
    """
    require_feature("cloud_run")
    # 未来实现：调用云端 API，返回结果
    raise NotImplementedError(
        "Cloud runs are not yet available. "
        "Sign up at https://visualagent.dev to be notified."
    )
```

### 6 验收标准

```
- [ ] `from visual_agent.licensing import check_feature, get_license` 可以 import
- [ ] check_feature("local_run") 返回 True
- [ ] check_feature("cloud_run") 返回 False（free tier）
- [ ] AgentSession 序列化/反序列化包含 runs_this_month 字段
- [ ] cloud.py import 不崩溃，调用 run_remote_workflow 抛出 FeatureGatedError
```

---

## Phase 7：上下文连续性（窗口切换记忆）

**目标：解决 AI 编程助手切换窗口后丢失任务上下文的核心痛点**

这是产品最独特的价值点之一：AI 在切换前保存任务状态，回来后一条命令恢复。

### 7-1：AgentSession 添加 AI 任务上下文字段

**修改文件：`src/visual_agent/session.py`**

```python
@dataclass(frozen=True)
class AiTaskContext:
    task: str           # 当前任务描述
    analyzed_files: list[str]   # 已分析的文件
    root_cause: str     # 推断的根因
    plan: str           # 下一步计划
    tried: list[str]    # 已尝试的方案
    updated_at: float


@dataclass(frozen=True)
class AgentSession:
    # ... 现有字段 ...
    ai_task_context: AiTaskContext | None = None   # 新增
```

在 `load_agent_session` 中解析：

```python
task_ctx = data.get("ai_task_context")
ai_task_context = AiTaskContext(**task_ctx) if isinstance(task_ctx, dict) else None
```

在 `_session_to_snapshot_text` 中输出：

```python
if session.ai_task_context is not None:
    ctx = session.ai_task_context
    lines.extend([
        "",
        "AI Task Context (saved before window switch):",
        f"  Task: {ctx.task}",
        f"  Root cause: {ctx.root_cause}" if ctx.root_cause else "",
        f"  Plan: {ctx.plan}" if ctx.plan else "",
        f"  Files: {', '.join(ctx.analyzed_files[:5])}" if ctx.analyzed_files else "",
        f"  Tried: {', '.join(ctx.tried[:3])}" if ctx.tried else "",
    ])
```

### 7-2：新增 MCP tool `save_task_context`

**修改文件：`src/visual_agent/mcp_server.py`**

```python
Tool(
    name="save_task_context",
    description=(
        "Save the AI assistant's current task state before switching windows. "
        "Call this before running tests, opening a browser, or starting a new chat. "
        "The saved context is included in context-snapshot so you can resume exactly where you left off."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "What you are currently trying to accomplish.",
            },
            "analyzed_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File paths you have analyzed.",
            },
            "root_cause": {
                "type": "string",
                "description": "Your current hypothesis about the root cause.",
            },
            "plan": {
                "type": "string",
                "description": "Your plan for the next steps.",
            },
            "tried": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Approaches you have already tried.",
            },
            "workspace_root": {"type": "string"},
        },
        "required": ["task"],
    },
),
```

处理函数：

```python
def save_task_context_payload(args: dict[str, Any]) -> dict[str, Any]:
    from .session import load_agent_session, _write_session, AiTaskContext
    from time import time
    from dataclasses import replace

    workspace = require_workspace(args)
    existing = load_agent_session(workspace.root)

    ctx = AiTaskContext(
        task=str(args.get("task", "")),
        analyzed_files=[str(f) for f in args.get("analyzed_files", [])],
        root_cause=str(args.get("root_cause", "")),
        plan=str(args.get("plan", "")),
        tried=[str(t) for t in args.get("tried", [])],
        updated_at=time(),
    )

    if existing is None:
        from .session import AgentSession
        session = AgentSession(
            updated_at=time(),
            passing_workflows=[],
            failing_workflows=[],
            latest_failure=None,
            next_action="Task context saved. Run verification to check status.",
            token_estimate=0,
            ai_task_context=ctx,
        )
    else:
        session = replace(existing, ai_task_context=ctx)

    _write_session(workspace.root, session)
    return {
        "status": "saved",
        "task": ctx.task,
        "message": "Task context saved. Resume with: context-snapshot --format markdown",
    }
```

### 7-3：新增 CLI 命令 `save-task-context`

**修改文件：`src/visual_agent/cli.py`**

```python
save_task = subparsers.add_parser(
    "save-task-context",
    help="Save AI task state before switching windows."
)
save_task.add_argument("--task", required=True)
save_task.add_argument("--files", nargs="*", default=[])
save_task.add_argument("--root-cause", default="")
save_task.add_argument("--plan", default="")
save_task.add_argument("--workspace-root", default=".agent-workspace")
```

### 7 验收标准

```
- [ ] MCP tool save_task_context 保存后，context-snapshot 输出包含任务上下文
- [ ] 连续调用 save_task_context 覆盖旧上下文
- [ ] ai_task_context 为 None 时 context-snapshot 正常输出（向后兼容）
- [ ] task 字段经过 scrub_secrets 处理
- [ ] token 预算：加入 task context 后 context-snapshot 不超过 600 token
```

---

## Phase 8：Workflow 市场基础

**目标：建立 workflow 分享的数据结构和入口，市场功能本期不上线但接口预留**

### 8-1：workflow index 文件

每次运行 workflow 后，自动维护 workspace 里的 workflow 索引：

**新增文件：`src/visual_agent/workflow_index.py`**

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

INDEX_FILE = "workflow_index.json"


def update_workflow_index(workspace: Path, workflow_ref: Any) -> None:
    """workflow 每次运行后更新索引。"""
    index_path = workspace / INDEX_FILE
    index = _load_index(index_path)

    entry = {
        "name": workflow_ref.name,
        "description": getattr(workflow_ref, "description", ""),
        "tags": list(getattr(workflow_ref, "tags", [])),
        "visibility": getattr(workflow_ref, "visibility", "private"),
        "author": getattr(workflow_ref, "author", ""),
        "path": str(workflow_ref.path),
    }
    index[workflow_ref.name] = entry
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def list_public_workflows(workspace: Path) -> list[dict[str, Any]]:
    """列出所有 visibility=public 的 workflow（未来市场使用）。"""
    index = _load_index(workspace / INDEX_FILE)
    return [v for v in index.values() if v.get("visibility") == "public"]


def _load_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
```

### 8-2：share-workflow 命令（占位）

**修改文件：`src/visual_agent/cli.py`**

```python
share_workflow = subparsers.add_parser(
    "share-workflow",
    help="Share a workflow to the Visual Agent marketplace (coming soon)."
)
share_workflow.add_argument("--name", required=True, help="Workflow name to share.")
share_workflow.add_argument("--workspace-root", default=".agent-workspace")
```

处理逻辑：

```python
if args.command == "share-workflow":
    from .licensing import require_feature, FeatureGatedError
    # 分享功能目前免费可用，但需要账号（未来）
    print(json.dumps({
        "status": "coming_soon",
        "message": (
            f"Sharing workflows to the marketplace is coming soon. "
            f"Workflow '{args.name}' has been marked as public in your local index. "
            f"Sign up at https://visualagent.dev to publish when the marketplace launches."
        ),
    }, ensure_ascii=False, indent=2))
    return 0
```

### 8 验收标准

```
- [ ] workflow_index.json 在 workflow 运行后自动创建/更新
- [ ] list_public_workflows 返回 visibility=public 的条目
- [ ] share-workflow 命令不崩溃，返回 coming_soon 消息
- [ ] workflow YAML 的 visibility/author/license 字段被正确读取和存储
```

---

## ToS 条款（需加入产品文档）

在 `docs/` 下新增 `terms.md`，核心条款：

```
用户保留其创建的所有 workflow 的所有权。

将 workflow 的 visibility 设置为 public 或通过 share-workflow 命令发布时，
用户授予 Visual Agent 平台全球性、免费、可转授权的许可，
用于托管、展示、分发和推广该 workflow。

用户可随时将 workflow 改回 private 以撤回公开授权。
平台不对用户创建的 workflow 内容承担责任。
```

---

## 总体交付顺序

```
Week 1-3:   Phase 4 VS Code 扩展开发 + 发布到 Marketplace
Week 4-5:   Phase 5 AI 生成 Workflow + anthropic SDK 集成
Week 6:     Phase 6 货币化基础设施（licensing.py + usage 计数）
Week 7:     Phase 7 上下文连续性（save_task_context MCP tool）
Week 8-9:   Phase 8 Workflow 市场基础（index + share 占位）
```

每个 Phase 完成后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -x -q
```

确保现有 651 个测试仍然全部通过。

---

## 给 Codex 的特别说明

1. **不要修改现有测试**，除非接口签名变化导致测试必须更新
2. **新增功能必须新增对应测试**，测试文件放在 `tests/test_<module>.py`
3. **向后兼容**：AgentSession、WorkflowRef 的新字段必须有默认值
4. **token 预算**：所有面向 AI 的输出不超过 MASTER_DEV_PLAN.md 中规定的上限
5. **收费功能**：`require_feature()` 调用现在不拦截，不要改变这个行为
6. VS Code 扩展在 `vscode-extension/` 目录开发，不影响 Python 包
