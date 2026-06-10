import * as vscode from "vscode";
import { agentStatusLabel, agentStatusSeverity, readAgentStatus } from "./agentStatus";
import { getSessionSnapshot, getVisualStatus } from "./bridge";

let statusBarItem: vscode.StatusBarItem | undefined;

export function initStatusBar(context: vscode.ExtensionContext): void {
  statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  statusBarItem.command = "visualAgent.quickActions";
  context.subscriptions.push(statusBarItem);
  void refreshStatusBar();
}

export async function refreshStatusBar(): Promise<void> {
  if (!statusBarItem) {
    return;
  }
  const agentStatus = await readAgentStatus();
  if (agentStatus) {
    statusBarItem.command = "visualAgent.quickActions";
    const severity = agentStatusSeverity(agentStatus);
    if (severity === "passed") {
      statusBarItem.text = `$(check) Checkpoint: ${qualityText(agentStatus.qualityScore)}`;
      statusBarItem.backgroundColor = undefined;
    } else if (severity === "failed") {
      statusBarItem.text = `$(error) Checkpoint: ${qualityText(agentStatus.qualityScore)}`;
      statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    } else if (severity === "warning") {
      statusBarItem.text = `$(warning) Checkpoint: ${qualityText(agentStatus.qualityScore)}`;
      statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
    } else {
      statusBarItem.text = "$(eye) Checkpoint";
      statusBarItem.backgroundColor = undefined;
    }
    statusBarItem.tooltip = agentStatusLabel(agentStatus);
    statusBarItem.show();
    return;
  }
  statusBarItem.command = "visualAgent.quickActions";
  const visualStatus = await getVisualStatus();
  if (visualStatus && visualStatus.status !== "not_found") {
    const failing = visualStatus.failing.length;
    const passing = visualStatus.passing.length;
    if (failing > 0 || visualStatus.status.toUpperCase() === "FAILING") {
      statusBarItem.text = `$(error) Checkpoint: ${passing}/${passing + failing}`;
      statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    } else if (passing > 0 || visualStatus.status.toUpperCase() === "PASSING") {
      statusBarItem.text = passing > 0 ? `$(check) Checkpoint: ${passing}/${passing + failing}` : "$(check) Checkpoint";
      statusBarItem.backgroundColor = undefined;
    } else {
      statusBarItem.text = "$(eye) Checkpoint";
      statusBarItem.backgroundColor = undefined;
    }
    statusBarItem.tooltip = visualStatus.activeTask || visualStatus.environment || visualStatus.path || "Checkpoint status";
    statusBarItem.show();
    return;
  }
  const snapshot = await getSessionSnapshot();
  if (!snapshot) {
    statusBarItem.text = "$(eye) Checkpoint";
    statusBarItem.tooltip = "No session yet.";
    statusBarItem.backgroundColor = undefined;
    statusBarItem.show();
    return;
  }

  const failing = snapshot.failingWorkflows.length;
  const passing = snapshot.passingWorkflows.length;
  const total = failing + passing;
  if (total === 0) {
    statusBarItem.text = "$(eye) Checkpoint";
    statusBarItem.backgroundColor = undefined;
  } else if (failing === 0) {
    statusBarItem.text = `$(check) Checkpoint: ${passing}/${total}`;
    statusBarItem.backgroundColor = undefined;
  } else {
    statusBarItem.text = `$(error) Checkpoint: ${passing}/${total}`;
    statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
  }
  statusBarItem.tooltip = snapshot.nextAction;
  statusBarItem.show();
}

function qualityText(value: number | undefined): string {
  return typeof value === "number" ? value.toFixed(2) : "AI verification";
}

