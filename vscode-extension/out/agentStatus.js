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
exports.getAgentStatusPath = getAgentStatusPath;
exports.readAgentStatus = readAgentStatus;
exports.watchAgentStatus = watchAgentStatus;
exports.agentStatusLabel = agentStatusLabel;
exports.agentStatusMarkdown = agentStatusMarkdown;
exports.agentStatusSeverity = agentStatusSeverity;
exports.normalizeAgentStatus = normalizeAgentStatus;
exports.normalizeResult = normalizeResult;
const fs = __importStar(require("fs"));
const path = __importStar(require("path"));
const vscode = __importStar(require("vscode"));
const bridge_1 = require("./bridge");
function getAgentStatusPath() {
    return (0, bridge_1.workspacePath)(path.join((0, bridge_1.getWorkspaceRoot)(), ".vscode-agent-status.json"));
}
async function readAgentStatus() {
    const statusPath = getAgentStatusPath();
    try {
        const text = await fs.promises.readFile(statusPath, "utf8");
        const data = JSON.parse(text);
        return normalizeAgentStatus(data);
    }
    catch {
        return null;
    }
}
function watchAgentStatus(context, onChange) {
    const statusPath = getAgentStatusPath();
    const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(path.dirname(statusPath), path.basename(statusPath)));
    const fire = () => {
        void onChange();
    };
    watcher.onDidCreate(fire, undefined, context.subscriptions);
    watcher.onDidChange(fire, undefined, context.subscriptions);
    watcher.onDidDelete(fire, undefined, context.subscriptions);
    context.subscriptions.push(watcher);
}
function agentStatusLabel(status) {
    const workflow = status.workflowName || "implementation";
    const quality = typeof status.qualityScore === "number" ? ` q=${status.qualityScore.toFixed(2)}` : "";
    if (status.result === "pass") {
        return `Verified: ${workflow}${quality}`;
    }
    if (status.result === "fail") {
        return `Failed: ${workflow}${quality}`;
    }
    if (status.result === "needs_workflow_improvement") {
        return `Needs workflow improvement: ${workflow}${quality}`;
    }
    if (status.result === "timeout") {
        return `Timed out: ${workflow}${quality}`;
    }
    if (status.result) {
        return `${status.result}: ${workflow}${quality}`;
    }
    return `Verification: ${workflow}${quality}`;
}
function agentStatusMarkdown(status) {
    const lines = [
        `Result: ${status.result || "unknown"}`,
        `Workflow: ${status.workflowName || ""}`,
        typeof status.qualityScore === "number" ? `Quality: ${status.qualityScore.toFixed(2)}` : "",
        status.runId ? `Run: ${status.runId}` : "",
        typeof status.timeoutSeconds === "number" ? `Timeout: ${status.timeoutSeconds}s` : "",
        status.reportPath ? `Report: ${status.reportPath}` : "",
        status.reportMarkdownPath ? `Report Markdown: ${status.reportMarkdownPath}` : "",
        status.reportHint ? `Report Hint: ${status.reportHint}` : "",
        status.inputsPath ? `Inputs: ${status.inputsPath}` : "",
        status.inputsSource ? `Inputs Source: ${status.inputsSource}` : "",
        status.message ? `Message: ${status.message}` : ""
    ].filter(Boolean);
    if (status.negativeVerification) {
        const negative = status.negativeVerification;
        lines.push("");
        lines.push("Negative Verification:");
        lines.push(`- status: ${negative.status || ""}`);
        if (negative.reason) {
            lines.push(`- reason: ${negative.reason}`);
        }
        if (negative.workflowName) {
            lines.push(`- workflow: ${negative.workflowName}`);
        }
        if (negative.resetStrategy) {
            lines.push(`- reset strategy: ${negative.resetStrategy}`);
        }
        lines.push(`- oracle count: ${(negative.oracles || []).length}`);
        if (typeof negative.stepsPassed === "number" || typeof negative.stepsTotal === "number") {
            lines.push(`- steps: ${negative.stepsPassed || 0}/${negative.stepsTotal || 0}`);
        }
        if (negative.runId) {
            lines.push(`- run: ${negative.runId}`);
        }
        if (negative.reportPath) {
            lines.push(`- report: ${negative.reportPath}`);
        }
        if (negative.reportMarkdownPath) {
            lines.push(`- report markdown: ${negative.reportMarkdownPath}`);
        }
        if (negative.reportHint) {
            lines.push(`- report hint: ${negative.reportHint}`);
        }
        for (const oracle of negative.oracles || []) {
            const source = oracle.source ? ` (${oracle.source})` : "";
            lines.push(`- oracle: ${oracle.text || ""}${source}`);
        }
        if (negative.nextAction) {
            lines.push(`- next action: ${negative.nextAction}`);
        }
    }
    if (status.semanticSummary) {
        const semantic = status.semanticSummary;
        lines.push("");
        lines.push("Semantics:");
        lines.push(`- framework: ${semantic.framework || ""}`);
        if (typeof semantic.confidence === "number") {
            lines.push(`- confidence: ${semantic.confidence.toFixed(2)}`);
        }
        if (semantic.generationMethod) {
            lines.push(`- generation method: ${semantic.generationMethod}`);
        }
        lines.push(`- fields: ${semantic.fieldCount || 0}`);
        lines.push(`- required fields: ${semantic.requiredFieldCount || 0}`);
        lines.push(`- validation rules: ${semantic.validationRuleCount || 0}`);
        lines.push(`- success states: ${semantic.successStateCount || 0}`);
        lines.push(`- data displays: ${semantic.dataDisplayCount || 0}`);
        lines.push(`- negative input cases: ${semantic.negativeInputCaseCount || 0}`);
        for (const display of semantic.dataDisplays || []) {
            lines.push(`- display: ${display}`);
        }
        for (const display of semantic.matchedDataDisplays || []) {
            lines.push(`- matched display: ${display}`);
        }
        for (const display of semantic.unmatchedDataDisplays || []) {
            lines.push(`- unmatched display: ${display}`);
        }
        for (const warning of semantic.warnings || []) {
            lines.push(`- warning: ${warning}`);
        }
    }
    if (status.generationTrace && status.generationTrace.length > 0) {
        lines.push("");
        lines.push("Generation Trace:");
        for (const item of status.generationTrace) {
            lines.push(`- ${item}`);
        }
    }
    if (status.quality && (status.quality.gaps.length > 0 || status.quality.recommendation)) {
        lines.push("");
        lines.push("Quality:");
        for (const gap of status.quality.gaps) {
            lines.push(`- gap: ${gap}`);
        }
        if (typeof status.quality.dataDisplayAssertions === "number") {
            lines.push(`- data display assertions: ${status.quality.dataDisplayAssertions}`);
        }
        if (typeof status.quality.forbiddenErrorAssertions === "number") {
            lines.push(`- forbidden error assertions: ${status.quality.forbiddenErrorAssertions}`);
        }
        if (typeof status.quality.textFromInputReferences === "number") {
            lines.push(`- text_from input references: ${status.quality.textFromInputReferences}`);
        }
        for (const reference of status.quality.invalidTextFromReferences || []) {
            lines.push(`- invalid text_from: ${reference}`);
        }
        if (status.quality.recommendation) {
            lines.push(`- recommendation: ${status.quality.recommendation}`);
        }
    }
    if (status.failedStep) {
        lines.push("");
        lines.push("Failed step:");
        lines.push(`- id: ${status.failedStep.id || ""}`);
        lines.push(`- action: ${status.failedStep.action || ""}`);
        if (status.failedStep.expected) {
            lines.push(`- expected: ${status.failedStep.expected}`);
        }
        if (status.failedStep.actual) {
            lines.push(`- actual: ${status.failedStep.actual}`);
        }
        if (status.failedStep.fixHint) {
            lines.push(`- fix hint: ${status.failedStep.fixHint}`);
        }
    }
    return lines.join("\n");
}
function agentStatusSeverity(status) {
    if (status.negativeVerification?.status === "fail") {
        return "failed";
    }
    if (status.negativeVerification?.status === "timeout") {
        return "warning";
    }
    if (status.result === "pass") {
        return "passed";
    }
    if (status.result === "fail") {
        return "failed";
    }
    if (status.result === "needs_workflow_improvement" || status.result === "timeout") {
        return "warning";
    }
    return "info";
}
function normalizeAgentStatus(data) {
    const failedStep = data?.failed_step || data?.failedStep;
    const quality = data?.quality;
    const semantic = data?.semantic_summary || data?.semanticSummary;
    const negative = data?.negative_verification || data?.negativeVerification;
    const gaps = Array.isArray(quality?.gaps) ? quality.gaps.map((item) => String(item)).filter(Boolean) : [];
    const invalidTextFromReferences = Array.isArray(quality?.invalid_text_from_references || quality?.invalidTextFromReferences)
        ? (quality.invalid_text_from_references || quality.invalidTextFromReferences).map((item) => String(item)).filter(Boolean)
        : [];
    const semanticWarnings = Array.isArray(semantic?.warnings)
        ? semantic.warnings.map((item) => String(item)).filter(Boolean)
        : [];
    const semanticDisplays = Array.isArray(semantic?.data_displays || semantic?.dataDisplays)
        ? (semantic.data_displays || semantic.dataDisplays).map((item) => String(item)).filter(Boolean)
        : [];
    const matchedSemanticDisplays = Array.isArray(semantic?.matched_data_displays || semantic?.matchedDataDisplays)
        ? (semantic.matched_data_displays || semantic.matchedDataDisplays).map((item) => String(item)).filter(Boolean)
        : [];
    const unmatchedSemanticDisplays = Array.isArray(semantic?.unmatched_data_displays || semantic?.unmatchedDataDisplays)
        ? (semantic.unmatched_data_displays || semantic.unmatchedDataDisplays).map((item) => String(item)).filter(Boolean)
        : [];
    const rawResult = stringOrUndefined(data?.result);
    const generationTrace = Array.isArray(data?.generation_trace || data?.generationTrace)
        ? (data.generation_trace || data.generationTrace).map((item) => String(item)).filter(Boolean)
        : [];
    return {
        updatedAt: numberOrUndefined(data?.updated_at || data?.updatedAt),
        result: normalizeResult(rawResult),
        workflowName: stringOrUndefined(data?.workflow_name || data?.workflowName),
        qualityScore: numberOrUndefined(data?.quality_score || data?.qualityScore),
        failedStep: failedStep
            ? {
                id: stringOrUndefined(failedStep.id),
                action: stringOrUndefined(failedStep.action),
                expected: stringOrUndefined(failedStep.expected),
                actual: stringOrUndefined(failedStep.actual),
                fixHint: stringOrUndefined(failedStep.fix_hint || failedStep.fixHint)
            }
            : undefined,
        message: stringOrUndefined(data?.message),
        nextAction: stringOrUndefined(data?.next_action || data?.nextAction),
        runId: stringOrUndefined(data?.run_id || data?.runId),
        reportPath: stringOrUndefined(data?.report_path || data?.reportPath),
        reportMarkdownPath: stringOrUndefined(data?.report_markdown_path || data?.reportMarkdownPath),
        reportHint: stringOrUndefined(data?.report_hint || data?.reportHint),
        inputsPath: stringOrUndefined(data?.inputs_path || data?.inputsPath),
        inputsSource: stringOrUndefined(data?.inputs_source || data?.inputsSource),
        timeoutSeconds: numberOrUndefined(data?.timeout_seconds || data?.timeoutSeconds),
        negativeVerification: negative
            ? {
                requested: Boolean(negative.requested),
                status: stringOrUndefined(negative.status),
                reason: stringOrUndefined(negative.reason),
                workflowName: stringOrUndefined(negative.workflow_name || negative.workflowName),
                workflowPath: stringOrUndefined(negative.workflow_path || negative.workflowPath),
                runId: stringOrUndefined(negative.run_id || negative.runId),
                runProfile: stringOrUndefined(negative.run_profile || negative.runProfile),
                resetStrategy: stringOrUndefined(negative.reset_strategy || negative.resetStrategy),
                oracles: normalizeNegativeOracles(negative.oracles),
                reportPath: stringOrUndefined(negative.report_path || negative.reportPath),
                reportMarkdownPath: stringOrUndefined(negative.report_markdown_path || negative.reportMarkdownPath),
                reportHint: stringOrUndefined(negative.report_hint || negative.reportHint),
                nextAction: stringOrUndefined(negative.next_action || negative.nextAction),
                stepsPassed: numberOrUndefined(negative.steps_passed ?? negative.stepsPassed),
                stepsTotal: numberOrUndefined(negative.steps_total ?? negative.stepsTotal)
            }
            : undefined,
        semanticSummary: semantic
            ? {
                framework: stringOrUndefined(semantic.framework),
                confidence: numberOrUndefined(semantic.confidence),
                generationMethod: stringOrUndefined(semantic.generation_method || semantic.generationMethod),
                fieldCount: numberOrUndefined(semantic.field_count || semantic.fieldCount),
                requiredFieldCount: numberOrUndefined(semantic.required_field_count || semantic.requiredFieldCount),
                sensitiveFieldCount: numberOrUndefined(semantic.sensitive_field_count || semantic.sensitiveFieldCount),
                validationRuleCount: numberOrUndefined(semantic.validation_rule_count || semantic.validationRuleCount),
                submitActionCount: numberOrUndefined(semantic.submit_action_count || semantic.submitActionCount),
                successStateCount: numberOrUndefined(semantic.success_state_count || semantic.successStateCount),
                errorStateCount: numberOrUndefined(semantic.error_state_count || semantic.errorStateCount),
                dataDisplayCount: numberOrUndefined(semantic.data_display_count || semantic.dataDisplayCount),
                negativeInputCaseCount: numberOrUndefined(semantic.negative_input_case_count || semantic.negativeInputCaseCount),
                dataDisplays: semanticDisplays,
                matchedDataDisplays: matchedSemanticDisplays,
                unmatchedDataDisplays: unmatchedSemanticDisplays,
                warnings: semanticWarnings
            }
            : undefined,
        generationTrace,
        quality: quality
            ? {
                gaps,
                recommendation: stringOrUndefined(quality.recommendation),
                dataDisplayAssertions: numberOrUndefined(quality.data_display_assertions || quality.dataDisplayAssertions),
                forbiddenErrorAssertions: numberOrUndefined(quality.forbidden_error_assertions || quality.forbiddenErrorAssertions),
                textFromInputReferences: numberOrUndefined(quality.text_from_input_references || quality.textFromInputReferences),
                invalidTextFromReferences
            }
            : undefined
    };
}
function normalizeResult(value) {
    if (value === "pass" || value === "fail" || value === "needs_workflow_improvement" || value === "timeout") {
        return value;
    }
    return "unknown";
}
function stringOrUndefined(value) {
    return value === undefined || value === null || value === "" ? undefined : String(value);
}
function numberOrUndefined(value) {
    const numberValue = Number(value);
    return Number.isFinite(numberValue) ? numberValue : undefined;
}
function normalizeNegativeOracles(value) {
    if (!Array.isArray(value)) {
        return [];
    }
    return value
        .filter((item) => item && typeof item === "object")
        .map((item) => ({
        text: stringOrUndefined(item.text),
        source: stringOrUndefined(item.source)
    }))
        .filter((item) => item.text || item.source);
}
//# sourceMappingURL=agentStatus.js.map