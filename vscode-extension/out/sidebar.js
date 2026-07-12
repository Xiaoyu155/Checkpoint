"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.WorkflowTreeProvider = void 0;
exports.examplesRoot = examplesRoot;
const vscode = __importStar(require("vscode"));
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const agentStatus_1 = require("./agentStatus");
const bridge_1 = require("./bridge");
class WorkflowTreeProvider {
    constructor(context) {
        this.context = context;
        this.onDidChangeTreeDataEmitter = new vscode.EventEmitter();
        this.onDidChangeTreeData = this.onDidChangeTreeDataEmitter.event;
        this.snapshot = null;
        this.agentStatus = null;
        this.showExampleLibrary = false;
    }
    async refresh() {
        this.agentStatus = await (0, agentStatus_1.readAgentStatus)();
        this.snapshot = await (0, bridge_1.getSessionSnapshot)();
        this.onDidChangeTreeDataEmitter.fire(undefined);
    }
    async showExamples() {
        this.showExampleLibrary = true;
        this.agentStatus = null;
        this.snapshot = null;
        this.onDidChangeTreeDataEmitter.fire(undefined);
    }
    getTreeItem(element) {
        return element;
    }
    getChildren(element) {
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
        const items = [];
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
exports.WorkflowTreeProvider = WorkflowTreeProvider;
function exampleItems(context) {
    const root = examplesRoot(context);
    if (!fs.existsSync(root)) {
        return [new WorkflowItem("No example workflows found in this checkout.", "warning")];
    }
    const items = [new WorkflowItem("Workflow Library", "info")];
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
function examplesRoot(context) {
    const workspaceRoot = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const workspaceExamples = workspaceRoot ? path.join(workspaceRoot, "workflows", "examples") : "";
    if (workspaceExamples && fs.existsSync(workspaceExamples)) {
        return workspaceExamples;
    }
    return path.resolve(context.extensionPath, "..", "workflows", "examples");
}
function agentStatusItems(status) {
    const severity = (0, agentStatus_1.agentStatusSeverity)(status);
    const items = [new WorkflowItem((0, agentStatus_1.agentStatusLabel)(status), severity)];
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
    }
    else if (status.message) {
        items.push(new WorkflowItem(status.message, "info"));
    }
    return items;
}
class WorkflowItem extends vscode.TreeItem {
    constructor(label, kind, command) {
        super(label, vscode.TreeItemCollapsibleState.None);
        this.tooltip = label;
        this.command = command;
        if (kind === "passed") {
            this.iconPath = new vscode.ThemeIcon("check", new vscode.ThemeColor("testing.iconPassed"));
        }
        else if (kind === "failed") {
            this.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("testing.iconFailed"));
            this.contextValue = "failedWorkflow";
        }
        else if (kind === "warning") {
            this.iconPath = new vscode.ThemeIcon("warning", new vscode.ThemeColor("testing.iconQueued"));
        }
        else {
            this.iconPath = new vscode.ThemeIcon("info");
        }
    }
}
//# sourceMappingURL=sidebar.js.map