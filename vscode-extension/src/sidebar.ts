import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import { AgentVerificationStatus, agentStatusLabel, agentStatusSeverity, readAgentStatus } from "./agentStatus";
import { getSessionSnapshot, SessionSnapshot } from "./bridge";

export class WorkflowTreeProvider implements vscode.TreeDataProvider<WorkflowItem> {
  private readonly onDidChangeTreeDataEmitter = new vscode.EventEmitter<WorkflowItem | undefined>();
  readonly onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;

  private snapshot: SessionSnapshot | null = null;
  private agentStatus: AgentVerificationStatus | null = null;
  private showExampleLibrary = false;

  constructor(private readonly context: vscode.ExtensionContext) {}

  async refresh(): Promise<void> {
    this.agentStatus = await readAgentStatus();
    this.snapshot = await getSessionSnapshot();
    this.onDidChangeTreeDataEmitter.fire(undefined);
  }

  async showExamples(): Promise<void> {
    this.showExampleLibrary = true;
    this.agentStatus = null;
    this.snapshot = null;
    this.onDidChangeTreeDataEmitter.fire(undefined);
  }

  getTreeItem(element: WorkflowItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: WorkflowItem): WorkflowItem[] {
    if (element) {
      return [];
    }
    if (this.showExampleLibrary) {
      return exampleItems(this.context);
    }
    if (this.agentStatus) {
      return agentStatusItems(this.agentStatus);
    }
    if (!this.snapshot) {
      return [
        new WorkflowItem("Create your first workflow", "info", {
          command: "visualAgent.generateWorkflow",
          title: "Create your first workflow"
        }),
        new WorkflowItem("Browse example workflows", "info", {
          command: "visualAgent.openExamples",
          title: "Browse example workflows"
        })
      ];
    }

    const items: WorkflowItem[] = [];
    for (const name of this.snapshot.failingWorkflows) {
      items.push(new WorkflowItem(name, "failed"));
    }
    for (const name of this.snapshot.passingWorkflows) {
      items.push(new WorkflowItem(name, "passed"));
    }
    if (items.length === 0) {
      items.push(new WorkflowItem("Create your first workflow", "info", {
        command: "visualAgent.generateWorkflow",
        title: "Create your first workflow"
      }));
      items.push(new WorkflowItem(this.snapshot.nextAction, "info"));
    }
    return items;
  }
}

function exampleItems(context: vscode.ExtensionContext): WorkflowItem[] {
  const root = examplesRoot(context);
  if (!fs.existsSync(root)) {
    return [new WorkflowItem("No example workflows found in this checkout.", "warning")];
  }
  const items: WorkflowItem[] = [new WorkflowItem("Workflow Library", "info")];
  for (const category of fs.readdirSync(root).sort()) {
    const categoryDir = path.join(root, category);
    if (!fs.statSync(categoryDir).isDirectory()) {
      continue;
    }
    for (const file of fs.readdirSync(categoryDir).filter((name) => name.endsWith(".yaml")).sort()) {
      const sourcePath = path.join(categoryDir, file);
      items.push(new WorkflowItem(`${category}: ${path.basename(file, ".yaml")}`, "info", {
        command: "visualAgent.copyExampleWorkflow",
        title: "Copy Example Workflow",
        arguments: [sourcePath]
      }));
    }
  }
  return items.length > 1 ? items : [new WorkflowItem("No example workflows found in this checkout.", "warning")];
}

export function examplesRoot(context: vscode.ExtensionContext): string {
  const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const workspaceExamples = workspaceRoot ? path.join(workspaceRoot, "workflows", "examples") : "";
  if (workspaceExamples && fs.existsSync(workspaceExamples)) {
    return workspaceExamples;
  }
  return path.resolve(context.extensionPath, "..", "workflows", "examples");
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
  constructor(label: string, kind: "passed" | "failed" | "warning" | "info", command?: vscode.Command) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.tooltip = label;
    this.command = command;
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
