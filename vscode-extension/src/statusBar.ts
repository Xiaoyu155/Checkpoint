import * as vscode from "vscode";
import { agentStatusLabel, agentStatusSeverity, readAgentStatus } from "./agentStatus";
import { getSessionSnapshot } from "./bridge";

let statusBarItem: vscode.StatusBarItem | undefined;

export function initStatusBar(context: vscode.ExtensionContext): void {
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = "visualAgent.showLatestFailure";
  context.subscriptions.push(statusBarItem);
  void refreshStatusBar();
}

export async function refreshStatusBar(): Promise<void> {
  if (!statusBarItem) {
    return;
  }
  const agentStatus = await readAgentStatus();
  if (agentStatus) {
    statusBarItem.command = "visualAgent.showLastVerification";
    const severity = agentStatusSeverity(agentStatus);
    if (severity === "passed") {
      statusBarItem.text = `$(check) Visual Agent: ${qualityText(agentStatus.qualityScore)}`;
      statusBarItem.backgroundColor = undefined;
    } else if (severity === "failed") {
      statusBarItem.text = `$(error) Visual Agent: ${qualityText(agentStatus.qualityScore)}`;
      statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    } else if (severity === "warning") {
      statusBarItem.text = `$(warning) Visual Agent: ${qualityText(agentStatus.qualityScore)}`;
      statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
    } else {
      statusBarItem.text = "$(eye) Visual Agent";
      statusBarItem.backgroundColor = undefined;
    }
    statusBarItem.tooltip = agentStatusLabel(agentStatus);
    statusBarItem.show();
    return;
  }
  statusBarItem.command = "visualAgent.showLatestFailure";
  const snapshot = await getSessionSnapshot();
  if (!snapshot) {
    statusBarItem.text = "$(eye) Visual Agent";
    statusBarItem.tooltip = "No session yet.";
    statusBarItem.backgroundColor = undefined;
    statusBarItem.show();
    return;
  }

  const failing = snapshot.failingWorkflows.length;
  const passing = snapshot.passingWorkflows.length;
  const total = failing + passing;
  if (total === 0) {
    statusBarItem.text = "$(eye) Visual Agent";
    statusBarItem.backgroundColor = undefined;
  } else if (failing === 0) {
    statusBarItem.text = `$(check) Visual Agent: ${passing}/${total}`;
    statusBarItem.backgroundColor = undefined;
  } else {
    statusBarItem.text = `$(error) Visual Agent: ${passing}/${total}`;
    statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
  }
  statusBarItem.tooltip = snapshot.nextAction;
  statusBarItem.show();
}

function qualityText(value: number | undefined): string {
  return typeof value === "number" ? value.toFixed(2) : "AI verification";
}
