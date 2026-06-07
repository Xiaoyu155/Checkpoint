import * as vscode from "vscode";
import { AgentVerificationStatus, agentStatusLabel, agentStatusSeverity, readAgentStatus } from "./agentStatus";
import { getSessionSnapshot, SessionSnapshot } from "./bridge";

export class WorkflowTreeProvider implements vscode.TreeDataProvider<WorkflowItem> {
  private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<WorkflowItem | undefined>();
  readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;

  private snapshot: SessionSnapshot | null = null;
  private agentStatus: AgentVerificationStatus | null = null;

  async refresh(): Promise<void> {
    this.agentStatus = await readAgentStatus();
    this.snapshot = await getSessionSnapshot();
    this.onDidChangeTreeDataEmitter.fire(undefined);
  }

  getTreeItem(element: WorkflowItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: WorkflowItem): WorkflowItem[] {
    if (element) {
      return [];
    }
    if (this.agentStatus) {
      return agentStatusItems(this.agentStatus);
    }
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
    if (items.length === 0) {
      items.push(new WorkflowItem(this.snapshot.nextAction, "info"));
    }
    return items;
  }
}

function agentStatusItems(status: AgentVerificationStatus): WorkflowItem[] {
  const severity = agentStatusSeverity(status);
  const items = [new WorkflowItem(agentStatusLabel(status), severity)];
  if (status.quality?.gaps.length) {
    for (const gap of status.quality.gaps.slice(0, 3)) {
      items.push(new WorkflowItem(gap, "warning"));
    }
  }
  if (status.quality?.recommendation) {
    items.push(new WorkflowItem(status.quality.recommendation, "info"));
  }
  if (status.nextAction && status.nextAction !== status.quality?.recommendation) {
    items.push(new WorkflowItem(status.nextAction, severity === "failed" ? "failed" : severity));
  }
  if (status.negativeVerification) {
    const negative = status.negativeVerification;
    const negativeKind = negative.status === "fail" ? "failed" : negative.status === "timeout" || negative.status === "skipped" ? "warning" : "info";
    const reason = negative.reason ? ` (${negative.reason})` : "";
    items.push(new WorkflowItem(`Negative: ${negative.status || "unknown"}${reason}`, negativeKind));
    if (negative.resetStrategy) {
      items.push(new WorkflowItem(`Negative reset: ${negative.resetStrategy}`, "info"));
    }
    if (negative.reportHint) {
      items.push(new WorkflowItem(negative.reportHint, "info"));
    }
    if (negative.nextAction) {
      items.push(new WorkflowItem(negative.nextAction, negativeKind));
    }
  }
  if (status.failedStep) {
    items.push(new WorkflowItem(`Step: ${status.failedStep.id || status.failedStep.action || "unknown"}`, "failed"));
    if (status.failedStep.fixHint) {
      items.push(new WorkflowItem(status.failedStep.fixHint, "info"));
    }
  } else if (status.message) {
    items.push(new WorkflowItem(status.message, "info"));
  }
  return items;
}

class WorkflowItem extends vscode.TreeItem {
  constructor(label: string, kind: "passed" | "failed" | "warning" | "info") {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.tooltip = label;
    if (kind === "passed") {
      this.iconPath = new vscode.ThemeIcon("check", new vscode.ThemeColor("testing.iconPassed"));
    } else if (kind === "failed") {
      this.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed"));
      this.contextValue = "failedWorkflow";
    } else if (kind === "warning") {
      this.iconPath = new vscode.ThemeIcon("warning", new vscode.ThemeColor("testing.iconQueued"));
    } else {
      this.iconPath = new vscode.ThemeIcon("info");
    }
  }
}
