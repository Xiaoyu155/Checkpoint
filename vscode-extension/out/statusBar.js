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
exports.initStatusBar = initStatusBar;
exports.refreshStatusBar = refreshStatusBar;
const vscode = __importStar(require("vscode"));
const agentStatus_1 = require("./agentStatus");
const bridge_1 = require("./bridge");
let statusBarItem;
function initStatusBar(context) {
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBarItem.command = "visualAgent.quickActions";
    context.subscriptions.push(statusBarItem);
    void refreshStatusBar();
}
async function refreshStatusBar() {
    if (!statusBarItem) {
        return;
    }
    const agentStatus = await (0, agentStatus_1.readAgentStatus)();
    if (agentStatus) {
        statusBarItem.command = "visualAgent.quickActions";
        const severity = (0, agentStatus_1.agentStatusSeverity)(agentStatus);
        if (severity === "passed") {
            statusBarItem.text = `$(check) Checkpoint: ${qualityText(agentStatus.qualityScore)}`;
            statusBarItem.backgroundColor = undefined;
        }
        else if (severity === "failed") {
            statusBarItem.text = `$(error) Checkpoint: ${qualityText(agentStatus.qualityScore)}`;
            statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
        }
        else if (severity === "warning") {
            statusBarItem.text = `$(warning) Checkpoint: ${qualityText(agentStatus.qualityScore)}`;
            statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
        }
        else {
            statusBarItem.text = "$(eye) Checkpoint";
            statusBarItem.backgroundColor = undefined;
        }
        statusBarItem.tooltip = (0, agentStatus_1.agentStatusLabel)(agentStatus);
        statusBarItem.show();
        return;
    }
    statusBarItem.command = "visualAgent.quickActions";
    const visualStatus = await (0, bridge_1.getVisualStatus)();
    if (visualStatus && visualStatus.status !== "not_found") {
        const failing = visualStatus.failing.length;
        const passing = visualStatus.passing.length;
        if (failing > 0 || visualStatus.status.toUpperCase() === "FAILING") {
            statusBarItem.text = `$(error) Checkpoint: ${passing}/${passing + failing}`;
            statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
        }
        else if (passing > 0 || visualStatus.status.toUpperCase() === "PASSING") {
            statusBarItem.text = passing > 0 ? `$(check) Checkpoint: ${passing}/${passing + failing}` : "$(check) Checkpoint";
            statusBarItem.backgroundColor = undefined;
        }
        else {
            statusBarItem.text = "$(eye) Checkpoint";
            statusBarItem.backgroundColor = undefined;
        }
        statusBarItem.tooltip = visualStatus.activeTask || visualStatus.environment || visualStatus.path || "Checkpoint status";
        statusBarItem.show();
        return;
    }
    const snapshot = await (0, bridge_1.getSessionSnapshot)();
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
    }
    else if (failing === 0) {
        statusBarItem.text = `$(check) Checkpoint: ${passing}/${total}`;
        statusBarItem.backgroundColor = undefined;
    }
    else {
        statusBarItem.text = `$(error) Checkpoint: ${passing}/${total}`;
        statusBarItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    }
    statusBarItem.tooltip = snapshot.nextAction;
    statusBarItem.show();
}
function qualityText(value) {
    return typeof value === "number" ? value.toFixed(2) : "AI verification";
}
//# sourceMappingURL=statusBar.js.map