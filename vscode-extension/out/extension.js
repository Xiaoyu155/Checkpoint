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
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const agentStatus_1 = require("./agentStatus");
const commands_1 = require("./commands");
const bridge_1 = require("./bridge");
const sidebar_1 = require("./sidebar");
const statusBar_1 = require("./statusBar");
function activate(context) {
    const treeProvider = new sidebar_1.WorkflowTreeProvider(context);
    context.subscriptions.push(vscode.window.registerTreeDataProvider("visualAgentWorkflows", treeProvider));
    (0, statusBar_1.initStatusBar)(context);
    (0, commands_1.registerCommands)(context, treeProvider);
    (0, commands_1.registerRunOnSave)(context, treeProvider);
    (0, agentStatus_1.watchAgentStatus)(context, async () => {
        await treeProvider.refresh();
        await (0, statusBar_1.refreshStatusBar)();
    });
    void treeProvider.refresh();
    void checkPythonEnvironment();
}
function deactivate() { }
async function checkPythonEnvironment() {
    const result = await (0, bridge_1.runCli)(["--help"], { workspaceRoot: false, progressHint: false });
    if (result.code === 0) {
        return;
    }
    const action = await vscode.window.showWarningMessage("Checkpoint could not run the Python CLI. Install the visual-agent package or set visualAgent.pythonPath.", "Open Settings", "Show Install Command");
    if (action === "Open Settings") {
        await vscode.commands.executeCommand("workbench.action.openSettings", "visualAgent.pythonPath");
    }
    else if (action === "Show Install Command") {
        vscode.window.showInformationMessage("Install from the repo root: pip install -e .[web,mcp,desktop]");
    }
}
//# sourceMappingURL=extension.js.map