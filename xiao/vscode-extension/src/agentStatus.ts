import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { getWorkspaceRoot, workspacePath } from "./bridge";

export interface AgentVerificationStatus {
  updatedAt?: number;
  result?: AgentVerificationResult;
  workflowName?: string;
  qualityScore?: number;
  failedStep?: {
    id?: string;
    action?: string;
    expected?: string;
    actual?: string;
    fixHint?: string;
  };
  message?: string;
  nextAction?: string;
  runId?: string;
  reportPath?: string;
  reportMarkdownPath?: string;
  reportHint?: string;
  inputsPath?: string;
  inputsSource?: string;
  timeoutSeconds?: number;
  negativeVerification?: {
    requested?: boolean;
    status?: string;
    reason?: string;
    workflowName?: string;
    workflowPath?: string;
    runId?: string;
    runProfile?: string;
    resetStrategy?: string;
    oracles: {
      text?: string;
      source?: string;
    }[];
    reportPath?: string;
    reportMarkdownPath?: string;
    reportHint?: string;
    nextAction?: string;
    stepsPassed?: number;
    stepsTotal?: number;
  };
  semanticSummary?: {
    framework?: string;
    confidence?: number;
    generationMethod?: string;
    fieldCount?: number;
    requiredFieldCount?: number;
    sensitiveFieldCount?: number;
    validationRuleCount?: number;
    submitActionCount?: number;
    successStateCount?: number;
    errorStateCount?: number;
    dataDisplayCount?: number;
    negativeInputCaseCount?: number;
    dataDisplays: string[];
    matchedDataDisplays: string[];
    unmatchedDataDisplays: string[];
    warnings: string[];
  };
  generationTrace?: string[];
  quality?: {
    gaps: string[];
    recommendation?: string;
    dataDisplayAssertions?: number;
    forbiddenErrorAssertions?: number;
    textFromInputReferences?: number;
    invalidTextFromReferences: string[];
  };
}

export type AgentVerificationResult = "pass" | "fail" | "needs_workflow_improvement" | "timeout" | "unknown";

export function getAgentStatusPath(): string {
  return workspacePath(path.join(getWorkspaceRoot(), ".vscode-agent-status.json"));
}

export async function readAgentStatus(): Promise<AgentVerificationStatus | null> {
  const statusPath = getAgentStatusPath();
  try {
    const text = await fs.promises.readFile(statusPath, "utf8");
    const data = JSON.parse(text);
    return normalizeAgentStatus(data);
  } catch {
    return null;
  }
}

export function watchAgentStatus(context: vscode.ExtensionContext, onChange: () => void): void {
  const statusPath = getAgentStatusPath();
  const watcher = vscode.workspace.createFileSystemWatcher(
    new vscode.RelativePattern(path.dirname(statusPath), path.basename(statusPath))
  );
  const fire = (): void => {
    void onChange();
  };
  watcher.onDidCreate(fire, undefined, context.subscriptions);
  watcher.onDidChange(fire, undefined, context.subscriptions);
  watcher.onDidDelete(fire, undefined, context.subscriptions);
  context.subscriptions.push(watcher);
}

export function agentStatusLabel(status: AgentVerificationStatus): string {
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

export function agentStatusMarkdown(status: AgentVerificationStatus): string {
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

export function agentStatusSeverity(status: AgentVerificationStatus): "passed" | "failed" | "warning" | "info" {
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

export function normalizeAgentStatus(data: any): AgentVerificationStatus {
  const failedStep = data?.failed_step || data?.failedStep;
  const quality = data?.quality;
  const semantic = data?.semantic_summary || data?.semanticSummary;
  const negative = data?.negative_verification || data?.negativeVerification;
  const gaps = Array.isArray(quality?.gaps) ? quality.gaps.map((item: any) => String(item)).filter(Boolean) : [];
  const invalidTextFromReferences = Array.isArray(quality?.invalid_text_from_references || quality?.invalidTextFromReferences)
    ? (quality.invalid_text_from_references || quality.invalidTextFromReferences).map((item: any) => String(item)).filter(Boolean)
    : [];
  const semanticWarnings = Array.isArray(semantic?.warnings)
    ? semantic.warnings.map((item: any) => String(item)).filter(Boolean)
    : [];
  const semanticDisplays = Array.isArray(semantic?.data_displays || semantic?.dataDisplays)
    ? (semantic.data_displays || semantic.dataDisplays).map((item: any) => String(item)).filter(Boolean)
    : [];
  const matchedSemanticDisplays = Array.isArray(semantic?.matched_data_displays || semantic?.matchedDataDisplays)
    ? (semantic.matched_data_displays || semantic.matchedDataDisplays).map((item: any) => String(item)).filter(Boolean)
    : [];
  const unmatchedSemanticDisplays = Array.isArray(semantic?.unmatched_data_displays || semantic?.unmatchedDataDisplays)
    ? (semantic.unmatched_data_displays || semantic.unmatchedDataDisplays).map((item: any) => String(item)).filter(Boolean)
    : [];
  const rawResult = stringOrUndefined(data?.result);
  const generationTrace = Array.isArray(data?.generation_trace || data?.generationTrace)
    ? (data.generation_trace || data.generationTrace).map((item: any) => String(item)).filter(Boolean)
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

export function normalizeResult(value: string | undefined): AgentVerificationResult {
  if (value === "pass" || value === "fail" || value === "needs_workflow_improvement" || value === "timeout") {
    return value;
  }
  return "unknown";
}

function stringOrUndefined(value: any): string | undefined {
  return value === undefined || value === null || value === "" ? undefined : String(value);
}

function numberOrUndefined(value: any): number | undefined {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : undefined;
}

function normalizeNegativeOracles(value: any): { text?: string; source?: string }[] {
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
