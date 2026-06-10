import * as cp from "child_process";
import * as path from "path";
import * as vscode from "vscode";

export interface WorkflowStatus {
  name: string;
  status: "passed" | "failed" | "unknown";
  elapsed?: number;
  failedStep?: string;
  hint?: string;
}

export interface LatestFailure {
  workflow?: string;
  stepId?: string;
  action?: string;
  expected?: string;
  actual?: string;
  hint?: string;
  artifactDir?: string;
  structuredFailure?: StructuredFailureReport;
}

export interface StructuredFailureReport {
  stepId?: string;
  action?: string;
  expected?: string;
  actualVisible?: string[];
  pageUrl?: string;
  pageState?: string;
  screenshotPath?: string;
  rootCause?: string;
  confidence?: number;
  suggestedFix?: string;
  relatedFiles?: string[];
}

export interface SessionSnapshot {
  passingWorkflows: string[];
  failingWorkflows: string[];
  latestFailure?: LatestFailure;
  nextAction: string;
  rawSnapshot: string;
}

export interface VisualStatus {
  status: string;
  passing: string[];
  failing: Array<{ workflow?: string; step?: string; detail?: string; raw?: string }>;
  activeTask?: string;
  lastRunMinutesAgo?: number;
  environment?: string;
  path?: string;
}

export interface CliResult {
  code: number;
  output: string;
}

export interface VerifyImplementationOptions {
  taskDescription: string;
  baseUrl?: string;
  runProfile?: "dry-run" | "supervised" | "approved";
  minQualityScore?: number;
  frameworkHint?: string;
  noUntracked?: boolean;
  runNegative?: boolean;
}

function getPythonPath(): string {
  return vscode.workspace
    .getConfiguration("visualAgent")
    .get("pythonPath", "python");
}

export function getWorkspaceRoot(): string {
  return vscode.workspace
    .getConfiguration("visualAgent")
    .get("workspaceRoot", ".agent-workspace");
}

function getCwd(): string {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.cwd();
}

function withWorkspaceRoot(args: string[]): string[] {
  if (args.includes("--workspace-root")) {
    return args;
  }
  return [...args, "--workspace-root", getWorkspaceRoot()];
}

export function workspacePath(relativePath: string): string {
  if (path.isAbsolute(relativePath)) {
    return relativePath;
  }
  return path.join(getCwd(), relativePath);
}

export function runCli(args: string[], options: { workspaceRoot?: boolean; progressHint?: boolean } = { workspaceRoot: true, progressHint: true }): Promise<CliResult> {
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
        }, 30_000);
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

export async function getSessionSnapshot(): Promise<SessionSnapshot | null> {
  const { code, output } = await runCli(["context-snapshot", "--format", "json"]);
  if (code !== 0) {
    return null;
  }
  try {
    const data = JSON.parse(output.trim());
    return normalizeSnapshot(data);
  } catch {
    return {
      passingWorkflows: [],
      failingWorkflows: [],
      nextAction: "Could not parse context-snapshot output.",
      rawSnapshot: output.trim()
    };
  }
}

export async function getVisualStatus(): Promise<VisualStatus | null> {
  const { code, output } = await runCli(["show-status", "--format", "json"]);
  if (code !== 0) {
    return null;
  }
  try {
    const data = JSON.parse(output.trim());
    return normalizeVisualStatus(data);
  } catch {
    return null;
  }
}

export async function runCodexCheck(includeSlow = false, tags?: string[], runProfile = "dry-run"): Promise<CliResult> {
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

export async function runVerificationAll(): Promise<CliResult> {
  return runVerificationAllWithProfile("dry-run");
}

export async function runVerificationAllWithProfile(runProfile = "dry-run"): Promise<CliResult> {
  return runCli(["verify", "--format", "json", "--include-slow", "--run-profile", runProfile]);
}

export function buildVerifyImplementationArgs(options: VerifyImplementationOptions): string[] {
  const args = [
    "verify-impl",
    "--task-description",
    options.taskDescription
  ];
  if (options.baseUrl) {
    args.push("--base-url", options.baseUrl);
  }
  args.push(
    "--run-profile",
    options.runProfile || "dry-run",
    "--format",
    "markdown"
  );
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

export async function verifyImplementationFromDiff(options: VerifyImplementationOptions): Promise<CliResult> {
  return runCli(buildVerifyImplementationArgs(options));
}

export async function showLatestFailure(): Promise<CliResult> {
  return runCli(["summarize-latest-failure", "--format", "json"]);
}

export function renderLatestFailureMarkdown(output: string): string {
  const parsed = parseLatestFailurePayload(output);
  if (!parsed) {
    return output.trim() || "No latest failure.";
  }
  const lines: string[] = ["# Checkpoint: Latest Failure", ""];
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

export async function autoRepairFailure(dryRun = false, promoteRegression = false, runRegression = false): Promise<CliResult> {
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

function normalizeSnapshot(data: any): SessionSnapshot {
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

function normalizeVisualStatus(data: any): VisualStatus {
  return {
    status: String(data?.status || "UNKNOWN"),
    passing: stringList(data?.passing),
    failing: Array.isArray(data?.failing)
      ? data.failing.map((item: any) => ({
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

function normalizeFailure(value: any, rawSnapshot: string): LatestFailure | undefined {
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

function parseLatestFailurePayload(output: string): any | undefined {
  try {
    const data = JSON.parse(output.trim());
    if (data && typeof data === "object") {
      return data;
    }
  } catch {
    return undefined;
  }
  return undefined;
}

function normalizeStructuredFailure(value: any): StructuredFailureReport | undefined {
  if (!value || typeof value !== "object") {
    return undefined;
  }
  const relatedFiles = Array.isArray(value.related_files || value.relatedFiles)
    ? (value.related_files || value.relatedFiles).map((item: any) => String(item)).filter(Boolean)
    : undefined;
  const actualVisible = Array.isArray(value.actual_visible || value.actualVisible)
    ? (value.actual_visible || value.actualVisible).map((item: any) => String(item)).filter(Boolean)
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

function stringList(value: any): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function stringOrUndefined(value: any): string | undefined {
  return value === undefined || value === null || value === "" ? undefined : String(value);
}

function extractLine(text: string, prefix: string): string | undefined {
  const line = text
    .split(/\r?\n/)
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix));
  return line ? line.slice(prefix.length).trim() : undefined;
}

