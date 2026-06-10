import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import { agentStatusMarkdown, readAgentStatus } from "./agentStatus";
import {
  autoRepairFailure,
  getWorkspaceRoot,
  runCli,
  runCodexCheck,
  runVerificationAllWithProfile,
  showLatestFailure,
  renderLatestFailureMarkdown,
  verifyImplementationFromDiff,
  workspacePath
} from "./bridge";
import { refreshStatusBar } from "./statusBar";
import { examplesRoot, WorkflowTreeProvider } from "./sidebar";

export function registerCommands(context: vscode.ExtensionContext, treeProvider: WorkflowTreeProvider): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.refresh", async () => {
      await refresh(treeProvider);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.quickActions", async () => {
      const action = await vscode.window.showQuickPick(
        [
          { label: "Init Workspace", command: "visualAgent.initWorkspace" },
          { label: "Verify Implementation", command: "visualAgent.verifyImplementation" },
          { label: "Run All", command: "visualAgent.runAll" },
          { label: "Show Failure", command: "visualAgent.showLatestFailure" },
          { label: "Generate Workflow", command: "visualAgent.generateWorkflow" }
        ],
        { placeHolder: "Checkpoint quick actions" }
      );
      if (action) {
        await vscode.commands.executeCommand(action.command);
      }
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.initWorkspace", async () => {
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Checkpoint: initializing workspace..." },
        () => runCli(["init", "--root", getWorkspaceRoot()], { workspaceRoot: false })
      );
      await refresh(treeProvider);
      showOutputPanel("visualAgentInitWorkspace", "Checkpoint: Init Workspace", result.output || "No init output.");
      showCliResult("Init workspace", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.runAll", async () => {
      const mode = await vscode.window.showQuickPick(
        [
          {
            label: "Dry Run",
            description: "Open and observe pages, but skip real clicks and inputs.",
            runProfile: "dry-run"
          },
          {
            label: "Live Supervised",
            description: "Run real browser clicks, inputs, refreshes, downloads, and assertions.",
            runProfile: "supervised"
          }
        ],
        { placeHolder: "Choose Checkpoint run mode" }
      );
      if (!mode) {
        return;
      }
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Checkpoint: running all workflows (${mode.runProfile})...` },
        () => runVerificationAllWithProfile(mode.runProfile)
      );
      await refresh(treeProvider);
      showCliResult(`Run all workflows (${mode.runProfile})`, result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.runLiveSupervised", async () => {
      const confirm = await vscode.window.showWarningMessage(
        "Run live supervised workflows with real browser clicks, inputs, refreshes, downloads, and assertions?",
        { modal: false },
        "Run Live"
      );
      if (confirm !== "Run Live") {
        return;
      }
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Checkpoint: running live supervised workflows..." },
        () => runVerificationAllWithProfile("supervised")
      );
      await refresh(treeProvider);
      showCliResult("Run live supervised workflows", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.runAffected", async () => {
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Checkpoint: running affected workflows..." },
        () => runCodexCheck()
      );
      await refresh(treeProvider);
      showCliResult("Run affected workflows", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.verifyCurrentChange", async () => {
      await verifyImplementationCommand(treeProvider);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.verifyImplementation", async () => {
      await verifyImplementationCommand(treeProvider);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.showLastVerification", async () => {
      const status = await readAgentStatus();
      if (!status) {
        vscode.window.showInformationMessage("Checkpoint: no AI verification status yet.");
        return;
      }
      showOutputPanel("visualAgentLastVerification", "Checkpoint: Last AI Verification", agentStatusMarkdown(status));
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.showLatestFailure", async () => {
      const result = await showLatestFailure();
      showOutputPanel(
        "visualAgentFailure",
        "Checkpoint: Latest Failure",
        renderLatestFailureMarkdown(result.output || "No latest failure.")
      );
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.autoRepair", async () => {
      const preview = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Checkpoint: previewing auto repair..." },
        () => autoRepairFailure(true)
      );
      showOutputPanel("visualAgentAutoRepair", "Checkpoint: Auto Repair Preview", preview.output || "No auto-repair preview.");
      if (preview.code !== 0) {
        showCliResult("Auto repair preview", preview);
        return;
      }
      const confirm = await vscode.window.showWarningMessage(
        "Apply this deterministic workflow repair, verify it, and rollback on failure?",
        { modal: false },
        "Run Auto Repair",
        "Run and Promote Regression",
        "Run, Promote, and Test Regression"
      );
      if (
        confirm !== "Run Auto Repair" &&
        confirm !== "Run and Promote Regression" &&
        confirm !== "Run, Promote, and Test Regression"
      ) {
        return;
      }
      const promoteRegression = confirm === "Run and Promote Regression" || confirm === "Run, Promote, and Test Regression";
      const runRegression = confirm === "Run, Promote, and Test Regression";
      const result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: runRegression
            ? "Checkpoint: auto repairing, promoting, and testing regression..."
            : promoteRegression
              ? "Checkpoint: auto repairing and promoting regression..."
            : "Checkpoint: auto repairing latest failure..."
        },
        () => autoRepairFailure(false, promoteRegression, runRegression)
      );
      await refresh(treeProvider);
      showOutputPanel(
        "visualAgentAutoRepair",
        runRegression
          ? "Checkpoint: Auto Repair + Regression Test"
          : promoteRegression
            ? "Checkpoint: Auto Repair + Regression"
            : "Checkpoint: Auto Repair",
        result.output || "No auto-repair output."
      );
      showCliResult(
        runRegression
          ? "Auto repair, regression promotion, and regression test"
          : promoteRegression
            ? "Auto repair and regression promotion"
            : "Auto repair",
        result
      );
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.generateWorkflow", async () => {
      const description = await vscode.window.showInputBox({
        prompt: "Describe the workflow to verify.",
        placeHolder: "Verify the user can log in and see the dashboard."
      });
      if (!description) {
        return;
      }
      const result = await runCli(["generate-workflow", "--description", description]);
      if (result.code !== 0) {
        vscode.window.showErrorMessage("Checkpoint workflow generation failed: " + trimOutput(result.output));
        return;
      }

      const savedTo = parseSavedPath(result.output);
      if (savedTo) {
        const doc = await vscode.workspace.openTextDocument(savedTo);
        await vscode.window.showTextDocument(doc);
        return;
      }
      vscode.window.showInformationMessage("Workflow generated: " + trimOutput(result.output));
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.newWorkflow", async () => {
      const name = await vscode.window.showInputBox({
        prompt: "Workflow file name.",
        placeHolder: "login_flow",
        validateInput: (value) => /^[a-zA-Z0-9_-]+$/.test(value) ? undefined : "Use letters, numbers, underscores, or hyphens."
      });
      if (!name) {
        return;
      }
      const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
      if (!workspaceFolder) {
        vscode.window.showErrorMessage("Checkpoint: open a workspace folder before creating a workflow.");
        return;
      }
      const workflowsDir = path.join(workspaceFolder.uri.fsPath, "workflows");
      fs.mkdirSync(workflowsDir, { recursive: true });
      const filePath = path.join(workflowsDir, `${name.replace(/\.ya?ml$/i, "")}.yaml`);
      if (fs.existsSync(filePath)) {
        vscode.window.showWarningMessage("Checkpoint: workflow already exists: " + filePath);
        const doc = await vscode.workspace.openTextDocument(filePath);
        await vscode.window.showTextDocument(doc);
        return;
      }
      fs.writeFileSync(filePath, workflowTemplate(path.basename(filePath, ".yaml")), "utf8");
      const doc = await vscode.workspace.openTextDocument(filePath);
      await vscode.window.showTextDocument(doc);
      await refresh(treeProvider);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.openExamples", async () => {
      await treeProvider.showExamples();
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.copyExampleWorkflow", async (sourcePath?: string) => {
      const source = sourcePath || await pickExampleWorkflow(context);
      if (!source) {
        return;
      }
      const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
      if (!workspaceFolder) {
        vscode.window.showErrorMessage("Checkpoint: open a workspace folder before copying an example.");
        return;
      }
      const workflowsDir = path.join(workspaceFolder.uri.fsPath, "workflows");
      fs.mkdirSync(workflowsDir, { recursive: true });
      const destination = uniqueWorkflowPath(path.join(workflowsDir, path.basename(source)));
      fs.copyFileSync(source, destination);
      const doc = await vscode.workspace.openTextDocument(destination);
      await vscode.window.showTextDocument(doc);
      vscode.window.showInformationMessage("Checkpoint: copied example workflow to " + destination);
      await refresh(treeProvider);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.browserSmoke", async () => {
      const url = await vscode.window.showInputBox({
        prompt: "URL to inspect with a real browser.",
        placeHolder: "http://localhost:3000"
      });
      if (!url) {
        return;
      }
      const expectText = await vscode.window.showInputBox({
        prompt: "Required visible text (optional).",
        placeHolder: "Dashboard"
      });
      const expectUrl = await vscode.window.showInputBox({
        prompt: "Required URL fragment before click (optional).",
        placeHolder: "/login"
      });
      const fills = await vscode.window.showInputBox({
        prompt: "Inputs to fill before click, comma-separated label=value (optional).",
        placeHolder: "用户名=demo,密码=demo"
      });
      const clickText = await vscode.window.showInputBox({
        prompt: "Button/link text to click once (optional).",
        placeHolder: "Login"
      });
      const expectTextAfter = clickText
        ? await vscode.window.showInputBox({
            prompt: "Required visible text after click (optional).",
            placeHolder: "Welcome"
          })
        : undefined;
      const waitTextAfter = clickText
        ? await vscode.window.showInputBox({
            prompt: "Text to wait for after click before screenshot (optional).",
            placeHolder: expectTextAfter || "Loading complete"
          })
        : undefined;
      const expectUrlAfter = clickText
        ? await vscode.window.showInputBox({
            prompt: "Required URL fragment after click (optional).",
            placeHolder: "/dashboard"
          })
        : undefined;
      const saveWorkflow = await vscode.window.showInputBox({
        prompt: "Save reusable workflow YAML path (optional). Fill values are not stored.",
        placeHolder: "workflows/browser_smoke.yaml"
      });
      const args = [
        "browser-smoke",
        "--url",
        url,
        "--output-dir",
        workspacePath(getWorkspaceRoot() + "/browser-smoke-runs"),
        "--format",
        "markdown"
      ];
      if (expectText) {
        args.push("--expect-text", expectText);
      }
      if (expectUrl) {
        args.push("--expect-url-contains", expectUrl);
      }
      for (const fill of splitCsv(fills)) {
        args.push("--fill", fill);
      }
      if (clickText) {
        args.push("--click-text", clickText);
        args.push("--require-change-after-click");
      }
      if (waitTextAfter) {
        args.push("--wait-for-text-after", waitTextAfter);
      }
      if (expectUrlAfter) {
        args.push("--wait-for-url-contains-after", expectUrlAfter);
        args.push("--expect-url-contains-after", expectUrlAfter);
      }
      if (expectTextAfter) {
        args.push("--expect-text-after", expectTextAfter);
      }
      if (saveWorkflow) {
        args.push("--save-workflow", workspacePath(getWorkspaceRoot() + "/" + saveWorkflow));
      }
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Checkpoint: running browser smoke..." },
        () => runCli(args, { workspaceRoot: false })
      );
      showOutputPanel("visualAgentBrowserSmoke", "Checkpoint: Browser Smoke", result.output || "No browser smoke output.");
      showCliResult("Browser smoke", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.connectCloud", () => {
      vscode.window
        .showInformationMessage(
          "Cloud runs are planned for the Pro plan. Local Checkpoint workflows continue to run on this machine.",
          "Open GitHub"
        )
        .then((action) => {
          if (action === "Open GitHub") {
            void vscode.env.openExternal(vscode.Uri.parse("https://github.com/Xiaoyu155/visual-agent"));
          }
        });
    })
  );
}

async function verifyImplementationCommand(treeProvider: WorkflowTreeProvider): Promise<void> {
  const taskDescription = await vscode.window.showInputBox({
    prompt: "Describe the implementation change to verify.",
    placeHolder: "Verify profile form saves and displays the updated profile name."
  });
  if (!taskDescription) {
    return;
  }
  const baseUrl = await vscode.window.showInputBox({
    prompt: "App URL or workspace fixture path. Leave empty to infer from project config or workspace fixtures.",
    placeHolder: "http://localhost:3000/profile or fixtures/profile.html"
  });
  const mode = await vscode.window.showQuickPick(
    [
      {
        label: "Dry Run",
        description: "Generate, quality-gate, and run without real clicks or inputs.",
        runProfile: "dry-run" as const
      },
      {
        label: "Live Supervised",
        description: "Run real browser actions with supervised permissions.",
        runProfile: "supervised" as const
      }
    ],
    { placeHolder: "Choose implementation verification mode" }
  );
  if (!mode) {
    return;
  }
  const includeUntracked = await vscode.window.showQuickPick(
    [
      {
        label: "Include untracked files",
        description: "Use tracked changes and new files in git diff.",
        noUntracked: false
      },
      {
        label: "Tracked files only",
        description: "Ignore untracked files.",
        noUntracked: true
      }
    ],
    { placeHolder: "Choose git diff scope" }
  );
  if (!includeUntracked) {
    return;
  }
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Checkpoint: verifying implementation..." },
    () =>
      verifyImplementationFromDiff({
        taskDescription,
        baseUrl: baseUrl?.trim() || undefined,
        runProfile: mode.runProfile,
        noUntracked: includeUntracked.noUntracked
      })
  );
  await refresh(treeProvider);
  showOutputPanel(
    "visualAgentCurrentChangeVerification",
    "Checkpoint: Verify Implementation",
    result.output || "No verify-impl output."
  );
  const status = await readAgentStatus();
  if (status) {
    showOutputPanel("visualAgentLastVerification", "Checkpoint: Last AI Verification", agentStatusMarkdown(status));
  }
  showCliResult("Verify implementation", result);
}

async function pickExampleWorkflow(context: vscode.ExtensionContext): Promise<string | undefined> {
  const root = examplesRoot(context);
  if (!fs.existsSync(root)) {
    vscode.window.showWarningMessage("Checkpoint: no example workflows found.");
    return undefined;
  }
  const picks: Array<{ label: string; description: string; path: string }> = [];
  for (const category of fs.readdirSync(root).sort()) {
    const dir = path.join(root, category);
    if (!fs.statSync(dir).isDirectory()) {
      continue;
    }
    for (const file of fs.readdirSync(dir).filter((item) => item.endsWith(".yaml")).sort()) {
      picks.push({ label: path.basename(file, ".yaml"), description: category, path: path.join(dir, file) });
    }
  }
  const pick = await vscode.window.showQuickPick(picks, { placeHolder: "Copy an example workflow into this project" });
  return pick?.path;
}

function uniqueWorkflowPath(basePath: string): string {
  if (!fs.existsSync(basePath)) {
    return basePath;
  }
  const parsed = path.parse(basePath);
  for (let index = 2; index < 1000; index += 1) {
    const candidate = path.join(parsed.dir, `${parsed.name}_${index}${parsed.ext}`);
    if (!fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return path.join(parsed.dir, `${parsed.name}_${Date.now()}${parsed.ext}`);
}

function workflowTemplate(name: string): string {
  return `schema_version: 1
min_runtime_version: "0.1.0"
name: ${name.replace(/[^a-zA-Z0-9_]+/g, "_")}
version: 1
description: "Describe what this workflow verifies."
tags: [verification, fast]
visibility: private
author: ""
license: ""
steps:
  - id: observe_page
    action: observe_browser
    url: "http://localhost:3000"
  - id: browser_ready
    action: assert_browser_ready
    min_text_length: 1
    min_interactive: 1
  - id: verify_expected_text
    action: assert_text
    text: "Expected text here"
`;
}

export function registerRunOnSave(context: vscode.ExtensionContext, treeProvider: WorkflowTreeProvider): void {
  context.subscriptions.push(
    vscode.workspace.onDidSaveTextDocument(async (doc) => {
      if (doc.uri.scheme !== "file") {
        return;
      }
      const config = vscode.workspace.getConfiguration("visualAgent");
      if (!config.get("runOnSave", false)) {
        return;
      }
      const tags = config.get<string[]>("autoRunTags", ["fast"]);
      await runCodexCheck(false, tags, "dry-run");
      await refresh(treeProvider);
    })
  );
}

async function refresh(treeProvider: WorkflowTreeProvider): Promise<void> {
  await treeProvider.refresh();
  await refreshStatusBar();
}

function showCliResult(title: string, result: { code: number; output: string }): void {
  const text = trimOutput(result.output);
  if (result.code === 0) {
    vscode.window.showInformationMessage(`Checkpoint: ${title} completed. ${text}`);
  } else {
    vscode.window.showWarningMessage(`Checkpoint: ${title} finished with issues. ${text}`);
  }
}

function trimOutput(output: string): string {
  const maxChars = vscode.workspace
    .getConfiguration("visualAgent")
    .get("maxNotificationChars", 500);
  const normalized = output.replace(/\s+/g, " ").trim();
  return normalized.length > maxChars ? normalized.slice(0, maxChars - 3) + "..." : normalized;
}

function parseSavedPath(output: string): string | undefined {
  try {
    const data = JSON.parse(output.trim());
    const saved = data?.saved_to || data?.savedTo;
    return typeof saved === "string" && saved ? workspacePath(saved) : undefined;
  } catch {
    const match = output.match(/Saved to:\s*(.+\.ya?ml)/i);
    return match ? workspacePath(match[1].trim()) : undefined;
  }
}

function showOutputPanel(viewType: string, title: string, text: string): void {
  const panel = vscode.window.createWebviewPanel(
    viewType,
    title,
    vscode.ViewColumn.Beside,
    { enableScripts: false }
  );
  panel.webview.html = renderPre(text);
}

function renderPre(text: string): string {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { font-family: var(--vscode-font-family); padding: 16px; }
    pre { white-space: pre-wrap; word-break: break-word; }
  </style>
</head>
<body><pre>${escapeHtml(text)}</pre></body>
</html>`;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function splitCsv(value: string | undefined): string[] {
  if (!value) {
    return [];
  }
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

