import * as vscode from "vscode";
import { agentStatusMarkdown, readAgentStatus } from "./agentStatus";
import {
  autoRepairFailure,
  getWorkspaceRoot,
  runCli,
  runCodexCheck,
  runVerificationAllWithProfile,
  showLatestFailure,
  verifyImplementationFromDiff,
  workspacePath
} from "./bridge";
import { refreshStatusBar } from "./statusBar";
import { WorkflowTreeProvider } from "./sidebar";

export function registerCommands(context: vscode.ExtensionContext, treeProvider: WorkflowTreeProvider): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.refresh", async () => {
      await refresh(treeProvider);
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
        { placeHolder: "Choose Visual Agent run mode" }
      );
      if (!mode) {
        return;
      }
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `Visual Agent: running all workflows (${mode.runProfile})...` },
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
        { location: vscode.ProgressLocation.Notification, title: "Visual Agent: running live supervised workflows..." },
        () => runVerificationAllWithProfile("supervised")
      );
      await refresh(treeProvider);
      showCliResult("Run live supervised workflows", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.runAffected", async () => {
      const result = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Visual Agent: running affected workflows..." },
        () => runCodexCheck()
      );
      await refresh(treeProvider);
      showCliResult("Run affected workflows", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.verifyCurrentChange", async () => {
      const taskDescription = await vscode.window.showInputBox({
        prompt: "Describe the implementation change to verify.",
        placeHolder: "Verify profile form saves and displays the updated profile name."
      });
      if (!taskDescription) {
        return;
      }
      const baseUrl = await vscode.window.showInputBox({
        prompt: "App URL or workspace fixture path used as the workflow entry point.",
        placeHolder: "http://localhost:3000/profile or fixtures/profile.html"
      });
      if (!baseUrl) {
        return;
      }
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
        { location: vscode.ProgressLocation.Notification, title: "Visual Agent: verifying current change..." },
        () =>
          verifyImplementationFromDiff({
            taskDescription,
            baseUrl,
            runProfile: mode.runProfile,
            noUntracked: includeUntracked.noUntracked
          })
      );
      await refresh(treeProvider);
      showOutputPanel(
        "visualAgentCurrentChangeVerification",
        "Visual Agent: Verify Current Change",
        result.output || "No verify-impl output."
      );
      const status = await readAgentStatus();
      if (status) {
        showOutputPanel("visualAgentLastVerification", "Visual Agent: Last AI Verification", agentStatusMarkdown(status));
      }
      showCliResult("Verify current change", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.showLastVerification", async () => {
      const status = await readAgentStatus();
      if (!status) {
        vscode.window.showInformationMessage("Visual Agent: no AI verification status yet.");
        return;
      }
      showOutputPanel("visualAgentLastVerification", "Visual Agent: Last AI Verification", agentStatusMarkdown(status));
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.showLatestFailure", async () => {
      const result = await showLatestFailure();
      showOutputPanel("visualAgentFailure", "Visual Agent: Latest Failure", result.output || "No latest failure.");
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.autoRepair", async () => {
      const preview = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Visual Agent: previewing auto repair..." },
        () => autoRepairFailure(true)
      );
      showOutputPanel("visualAgentAutoRepair", "Visual Agent: Auto Repair Preview", preview.output || "No auto-repair preview.");
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
            ? "Visual Agent: auto repairing, promoting, and testing regression..."
            : promoteRegression
              ? "Visual Agent: auto repairing and promoting regression..."
            : "Visual Agent: auto repairing latest failure..."
        },
        () => autoRepairFailure(false, promoteRegression, runRegression)
      );
      await refresh(treeProvider);
      showOutputPanel(
        "visualAgentAutoRepair",
        runRegression
          ? "Visual Agent: Auto Repair + Regression Test"
          : promoteRegression
            ? "Visual Agent: Auto Repair + Regression"
            : "Visual Agent: Auto Repair",
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
        vscode.window.showErrorMessage("Visual Agent workflow generation failed: " + trimOutput(result.output));
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
        { location: vscode.ProgressLocation.Notification, title: "Visual Agent: running browser smoke..." },
        () => runCli(args, { workspaceRoot: false })
      );
      showOutputPanel("visualAgentBrowserSmoke", "Visual Agent: Browser Smoke", result.output || "No browser smoke output.");
      showCliResult("Browser smoke", result);
    })
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("visualAgent.connectCloud", () => {
      vscode.window
        .showInformationMessage(
          "Cloud runs are planned for the Pro plan. Local Visual Agent workflows continue to run on this machine.",
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
    vscode.window.showInformationMessage(`Visual Agent: ${title} completed. ${text}`);
  } else {
    vscode.window.showWarningMessage(`Visual Agent: ${title} finished with issues. ${text}`);
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
