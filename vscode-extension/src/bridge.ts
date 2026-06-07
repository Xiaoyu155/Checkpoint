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
}

export interface SessionSnapshot {
  passingWorkflows: string[];
  failingWorkflows: string[];
  latestFailure?: LatestFailure;
  nextAction: string;
  rawSnapshot: string;
}

export interface CliResult {
  code: number;
  output: string;
}

export interface VerifyImplementationOptions {
  taskDescription: string;
  baseUrl: string;
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

export function runCli(args: string[], options: { workspaceRoot?: boolean } = { workspaceRoot: true }): Promise<CliResult> {
  return new Promise((resolve) => {
    const python = getPythonPath();
    const cliArgs = options.workspaceRoot === false ? args : withWorkspaceRoot(args);
    const allArgs = ["-m", "visual_agent.cli", ...cliArgs];
    const proc = cp.spawn(python, allArgs, {
      cwd: getCwd(),
      windowsHide: true
    });

    let output = "";
    proc.stdout.on("data", (chunk) => {
      output += chunk.toString();
    });
    proc.stderr.on("data", (chunk) => {
      output += chunk.toString();
    });
    proc.on("error", (err) => {
      resolve({ code: 1, output: String(err.message || err) });
    });
    proc.on("close", (code) => {
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
    options.taskDescription,
    "--base-url",
    options.baseUrl,
    "--run-profile",
    options.runProfile || "dry-run",
    "--format",
    "markdown"
  ];
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
  const nextAction = String(session?.nextAction || session?.next_action || extractLine(rawSnapshot, "Next action:") || "Run a workflow to update Visual Agent status.");
  const latestFailure = normalizeFailure(session?.latestFailure || session?.latest_failure, rawSnapshot);
  return {
    passingWorkflows,
    failingWorkflows,
    latestFailure,
    nextAction,
    rawSnapshot
  };
}

function normalizeFailure(value: any, rawSnapshot: string): LatestFailure | undefined {
  if (value && typeof value === "object") {
    return {
      workflow: stringOrUndefined(value.workflow),
      stepId: stringOrUndefined(value.stepId || value.step_id),
      action: stringOrUndefined(value.action),
      expected: stringOrUndefined(value.expected),
      actual: stringOrUndefined(value.actual),
      hint: stringOrUndefined(value.hint),
      artifactDir: stringOrUndefined(value.artifactDir || value.artifact_dir)
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
