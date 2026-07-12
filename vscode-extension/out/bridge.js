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
exports.getWorkspaceRoot = getWorkspaceRoot;
exports.workspacePath = workspacePath;
exports.runCli = runCli;
exports.getSessionSnapshot = getSessionSnapshot;
exports.getVisualStatus = getVisualStatus;
exports.runCodexCheck = runCodexCheck;
exports.runVerificationAll = runVerificationAll;
exports.runVerificationAllWithProfile = runVerificationAllWithProfile;
exports.buildVerifyImplementationArgs = buildVerifyImplementationArgs;
exports.verifyImplementationFromDiff = verifyImplementationFromDiff;
exports.showLatestFailure = showLatestFailure;
exports.renderLatestFailureMarkdown = renderLatestFailureMarkdown;
exports.autoRepairFailure = autoRepairFailure;
const cp = __importStar(require("child_process"));
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
function getPythonPath() {
    return vscode.workspace
        .getConfiguration("visualAgent")
        .get("pythonPath", "python");
}
function getWorkspaceRoot() {
    return vscode.workspace
        .getConfiguration("visualAgent")
        .get("workspaceRoot", ".agent-workspace");
}
function getCwd() {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();
}
function withWorkspaceRoot(args) {
    if (args.includes("--workspace-root")) {
        return args;
    }
    return [...args, "--workspace-root", getWorkspaceRoot()];
}
function workspacePath(relativePath) {
    if (path.isAbsolute(relativePath)) {
        return relativePath;
    }
    return path.join(getCwd(), relativePath);
}
function runCli(args, options = { workspaceRoot: true, progressHint: true }) {
    return new Promise((resolve) => {
        const python = getPythonPath();
        const cliArgs = options.workspaceRoot === false ? args : withWorkspaceRoot(args);
        const allArgs = ["-m", "visual_agent.cli", ...cliArgs];
        const proc = cp.spawn(python, allArgs, {
            cwd: getCwd(),
            windowsHide: true
        });
        let output = "";
        let completed = false;
        const progressTimer = options.progressHint === false
            ? undefined
            : setTimeout(() => {
                if (!completed) {
                    vscode.window.showInformationMessage("Checkpoint: CLI is still running. Long workflow runs may take more than 30 seconds.");
                }
            }, 30000);
        proc.stdout.on("data", (chunk) => {
            output += chunk.toString();
        });
        proc.stderr.on("data", (chunk) => {
            output += chunk.toString();
        });
        proc.on("error", (err) => {
            completed = true;
            if (progressTimer) {
                clearTimeout(progressTimer);
            }
            resolve({ code: 1, output: String(err.message || err) });
        });
        proc.on("close", (code) => {
            completed = true;
            if (progressTimer) {
                clearTimeout(progressTimer);
            }
            resolve({ code: code ?? 1, output });
        });
    });
}
async function getSessionSnapshot() {
    const { code, output } = await runCli(["context-snapshot", "--format", "json"]);
    if (code !== 0) {
        return null;
    }
    try {
        const data = JSON.parse(output.trim());
        return normalizeSnapshot(data);
    }
    catch {
        return {
            passingWorkflows: [],
            failingWorkflows: [],
            nextAction: "Could not parse context-snapshot output.",
            rawSnapshot: output.trim()
        };
    }
}
async function getVisualStatus() {
    const { code, output } = await runCli(["show-status", "--format", "json"]);
    if (code !== 0) {
        return null;
    }
    try {
        const data = JSON.parse(output.trim());
        return normalizeVisualStatus(data);
    }
    catch {
        return null;
    }
}
async function runCodexCheck(includeSlow = false, tags, runProfile = "dry-run") {
    const args = ["codex-check", "--format", "json"];
    args.push("--run-profile", runProfile);
    if (includeSlow) {
        args.push("--include-slow");
    }
    if (tags && tags.length > 0) {
        args.push("--tags", tags.join(","));
    }
    return runCli(args);
}
async function runVerificationAll() {
    return runVerificationAllWithProfile("dry-run");
}
async function runVerificationAllWithProfile(runProfile = "dry-run") {
    return runCli(["verify", "--format", "json", "--include-slow", "--run-profile", runProfile]);
}
function buildVerifyImplementationArgs(options) {
    const args = [
        "verify-impl",
        "--task-description",
        options.taskDescription
    ];
    if (options.baseUrl) {
        args.push("--base-url", options.baseUrl);
    }
    args.push("--run-profile", options.runProfile || "dry-run", "--format", "markdown");
    if (typeof options.minQualityScore === "number") {
        args.push("--min-quality-score", String(options.minQualityScore));
    }
    if (options.frameworkHint) {
        args.push("--framework-hint", options.frameworkHint);
    }
    if (options.noUntracked) {
        args.push("--no-untracked");
    }
    if (options.runNegative) {
        args.push("--run-negative");
    }
    return args;
}
async function verifyImplementationFromDiff(options) {
    return runCli(buildVerifyImplementationArgs(options));
}
async function showLatestFailure() {
    return runCli(["summarize-latest-failure", "--format", "json"]);
}
function renderLatestFailureMarkdown(output) {
    const parsed = parseLatestFailurePayload(output);
    if (!parsed) {
        return output.trim() || "No latest failure.";
    }
    const lines = ["# Checkpoint: Latest Failure", ""];
    if (parsed.workflow) {
        lines.push(`- Workflow: \`${parsed.workflow}\``);
    }
    if (parsed.run_id) {
        lines.push(`- Run ID: \`${parsed.run_id}\``);
    }
    if (parsed.failed_step?.id || parsed.failed_step?.action) {
        const step = parsed.failed_step.id || parsed.failed_step.action || "unknown";
        lines.push(`- Failed step: \`${step}\``);
    }
    if (parsed.expected) {
        lines.push(`- Expected: ${parsed.expected}`);
    }
    if (parsed.actual) {
        lines.push(`- Actual: ${parsed.actual}`);
    }
    if (parsed.hint) {
        lines.push(`- Hint: ${parsed.hint}`);
    }
    const structured = parsed.structured_failure;
    if (structured) {
        lines.push("", "## Structured Failure", "");
        if (structured.root_cause) {
            lines.push(`- Root cause: \`${structured.root_cause}\``);
        }
        if (typeof structured.confidence === "number") {
            lines.push(`- Confidence: \`${structured.confidence.toFixed(2)}\``);
        }
        if (structured.page_state) {
            lines.push(`- Page state: \`${structured.page_state}\``);
        }
        if (structured.page_url) {
            lines.push(`- Page URL: \`${structured.page_url}\``);
        }
        if (structured.screenshot_path) {
            lines.push(`- Screenshot: \`${structured.screenshot_path}\``);
        }
        if (structured.suggested_fix) {
            lines.push(`- Suggested fix: ${structured.suggested_fix}`);
        }
        if (structured.related_files && structured.related_files.length > 0) {
            lines.push("- Related files:");
            for (const file of structured.related_files) {
                lines.push(`  - \`${file}\``);
            }
        }
    }
    if (parsed.artifacts) {
        lines.push("", `- Artifacts: \`${parsed.artifacts}\``);
    }
    return lines.join("\n").trim() + "\n";
}
async function autoRepairFailure(dryRun = false, promoteRegression = false, runRegression = false) {
    const args = ["auto-repair", "--format", "markdown"];
    if (dryRun) {
        args.push("--dry-run");
    }
    if (promoteRegression) {
        args.push("--promote-regression");
    }
    if (runRegression) {
        args.push("--run-regression");
    }
    return runCli(args);
}
function normalizeSnapshot(data) {
    const rawSnapshot = String(data?.snapshot || "");
    const session = data?.session || data;
    const passingWorkflows = stringList(session?.passingWorkflows || session?.passing_workflows);
    const failingWorkflows = stringList(session?.failingWorkflows || session?.failing_workflows);
    const nextAction = String(session?.nextAction || session?.next_action || extractLine(rawSnapshot, "Next action:") || "Run a workflow to update Checkpoint status.");
    const latestFailure = normalizeFailure(session?.latestFailure || session?.latest_failure, rawSnapshot);
    return {
        passingWorkflows,
        failingWorkflows,
        latestFailure,
        nextAction,
        rawSnapshot
    };
}
function normalizeVisualStatus(data) {
    return {
        status: String(data?.status || "UNKNOWN"),
        passing: stringList(data?.passing),
        failing: Array.isArray(data?.failing)
            ? data.failing.map((item) => ({
                workflow: stringOrUndefined(item?.workflow),
                step: stringOrUndefined(item?.step),
                detail: stringOrUndefined(item?.detail),
                raw: stringOrUndefined(item?.raw)
            }))
            : [],
        activeTask: stringOrUndefined(data?.active_task || data?.activeTask),
        lastRunMinutesAgo: typeof data?.last_run_minutes_ago === "number"
            ? data.last_run_minutes_ago
            : typeof data?.lastRunMinutesAgo === "number"
                ? data.lastRunMinutesAgo
                : undefined,
        environment: stringOrUndefined(data?.environment),
        path: stringOrUndefined(data?.path)
    };
}
function normalizeFailure(value, rawSnapshot) {
    if (value && typeof value === "object") {
        const structuredFailure = normalizeStructuredFailure(value.structured_failure || value.structuredFailure);
        return {
            workflow: stringOrUndefined(value.workflow),
            stepId: stringOrUndefined(value.stepId || value.step_id),
            action: stringOrUndefined(value.action),
            expected: stringOrUndefined(value.expected),
            actual: stringOrUndefined(value.actual),
            hint: stringOrUndefined(value.hint),
            artifactDir: stringOrUndefined(value.artifactDir || value.artifact_dir),
            structuredFailure
        };
    }
    const workflow = extractLine(rawSnapshot, "Workflow:");
    if (!workflow) {
        return undefined;
    }
    return {
        workflow,
        stepId: extractLine(rawSnapshot, "Step:"),
        expected: extractLine(rawSnapshot, "Expected:"),
        actual: extractLine(rawSnapshot, "Actual:"),
        hint: extractLine(rawSnapshot, "Hint:"),
        artifactDir: extractLine(rawSnapshot, "Artifact:")
    };
}
function parseLatestFailurePayload(output) {
    try {
        const data = JSON.parse(output.trim());
        if (data && typeof data === "object") {
            return data;
        }
    }
    catch {
        return undefined;
    }
    return undefined;
}
function normalizeStructuredFailure(value) {
    if (!value || typeof value !== "object") {
        return undefined;
    }
    const relatedFiles = Array.isArray(value.related_files || value.relatedFiles)
        ? (value.related_files || value.relatedFiles).map((item) => String(item)).filter(Boolean)
        : undefined;
    const actualVisible = Array.isArray(value.actual_visible || value.actualVisible)
        ? (value.actual_visible || value.actualVisible).map((item) => String(item)).filter(Boolean)
        : undefined;
    return {
        stepId: stringOrUndefined(value.stepId || value.step_id),
        action: stringOrUndefined(value.action),
        expected: stringOrUndefined(value.expected),
        actualVisible,
        pageUrl: stringOrUndefined(value.pageUrl || value.page_url),
        pageState: stringOrUndefined(value.pageState || value.page_state),
        screenshotPath: stringOrUndefined(value.screenshotPath || value.screenshot_path),
        rootCause: stringOrUndefined(value.rootCause || value.root_cause),
        confidence: typeof value.confidence === "number" ? value.confidence : undefined,
        suggestedFix: stringOrUndefined(value.suggestedFix || value.suggested_fix),
        relatedFiles
    };
}
function stringList(value) {
    return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}
function stringOrUndefined(value) {
    return value === undefined || value === null || value === "" ? undefined : String(value);
}
function extractLine(text, prefix) {
    const line = text
        .split(/\r?\n/)
        .map((item) => item.trim())
        .find((item) => item.startsWith(prefix));
    return line ? line.slice(prefix.length).trim() : undefined;
}
//# sourceMappingURL=bridge.js.map