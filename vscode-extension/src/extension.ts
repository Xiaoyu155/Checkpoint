import * as vscode from "vscode";
import { watchAgentStatus } from "./agentStatus";
import { registerCommands, registerRunOnSave } from "./commands";
import { WorkflowTreeProvider } from "./sidebar";
import { initStatusBar, refreshStatusBar } from "./statusBar";

export function activate(context: vscode.ExtensionContext): void {
  const treeProvider = new WorkflowTreeProvider();

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
}

export function deactivate(): void {}
