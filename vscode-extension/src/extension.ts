import * as vscode from "vscode";
import { watchAgentStatus } from "./agentStatus";
import { registerCommands, registerRunOnSave } from "./commands";
import { runCli } from "./bridge";
import { WorkflowTreeProvider } from "./sidebar";
import { initStatusBar, refreshStatusBar } from "./statusBar";

export function activate(context: vscode.ExtensionContext): void {
  const treeProvider = new WorkflowTreeProvider(context);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("visualAgentWorkflows", treeProvider)
  );

  initStatusBar(context);
  registerCommands(context, treeProvider);
  registerRunOnSave(context, treeProvider);
  watchAgentStatus(context, async () => {
    await treeProvider.refresh();
    await refreshStatusBar();
  });

  void treeProvider.refresh();
  void checkPythonEnvironment();
}

export function deactivate(): void {}

async function checkPythonEnvironment(): Promise<void> {
  const result = await runCli(["--help"], { workspaceRoot: false, progressHint: false });
  if (result.code === 0) {
    return;
  }
  const action = await vscode.window.showWarningMessage(
    "Checkpoint could not run the Python CLI. Install the visual-agent package or set visualAgent.pythonPath.",
    "Open Settings",
    "Show Install Command"
  );
  if (action === "Open Settings") {
    await vscode.commands.executeCommand("workbench.action.openSettings", "visualAgent.pythonPath");
  } else if (action === "Show Install Command") {
    vscode.window.showInformationMessage("Install from the repo root: pip install -e .[web,mcp,desktop]");
  }
}

