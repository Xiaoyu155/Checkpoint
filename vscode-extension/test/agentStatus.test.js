const assert = require("assert");
const Module = require("module");

const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") {
    return {
      workspace: {
        createFileSystemWatcher: () => ({
          onDidCreate: () => undefined,
          onDidChange: () => undefined,
          onDidDelete: () => undefined
        }),
        getConfiguration: () => ({ get: (_key, defaultValue) => defaultValue })
      },
      RelativePattern: class RelativePattern {
        constructor(base, pattern) {
          this.base = base;
          this.pattern = pattern;
        }
      }
    };
  }
  return originalLoad(request, parent, isMain);
};

const {
  agentStatusLabel,
  agentStatusMarkdown,
  agentStatusSeverity,
  normalizeAgentStatus,
  normalizeResult
} = require("../out/agentStatus");
const { buildVerifyImplementationArgs } = require("../out/bridge");

function testNormalizeSnakeCasePayload() {
  const status = normalizeAgentStatus({
    result: "needs_workflow_improvement",
    workflow_name: "login_verification",
    quality_score: 0.52,
    quality: {
      gaps: ["no success state assertion"],
      data_display_assertions: 1,
      forbidden_error_assertions: 1,
      text_from_input_references: 1,
      invalid_text_from_references: ["input.timezone"],
      recommendation: "Add assert_text after submit."
    },
    semantic_summary: {
      framework: "html",
      confidence: 0.8,
      generation_method: "static",
      field_count: 1,
      required_field_count: 1,
      validation_rule_count: 2,
      success_state_count: 0,
      data_display_count: 1,
      negative_input_case_count: 2,
      data_displays: ["profile.displayName"],
      matched_data_displays: ["profile.displayName"],
      unmatched_data_displays: ["profile.unused"],
      warnings: ["no parser warning"]
    },
    report_path: "reports/run-1.json",
    inputs_path: "inputs/login_inputs.json",
    inputs_source: "generated_template",
    negative_verification: {
      requested: true,
      status: "skipped",
      reason: "no_negative_oracle",
      workflow_name: "login_verification_negative_draft",
      workflow_path: "workflows/login_negative_draft.yaml",
      reset_strategy: "fresh_observe_per_case",
      oracles: [],
      next_action: "Add parsed validation error text before enabling negative verification."
    },
    generation_trace: ["field email -> paste input.email"],
    next_action: "Add assert_text after submit."
  });

  assert.strictEqual(status.result, "needs_workflow_improvement");
  assert.strictEqual(status.workflowName, "login_verification");
  assert.strictEqual(status.qualityScore, 0.52);
  assert.strictEqual(status.semanticSummary.framework, "html");
  assert.strictEqual(status.semanticSummary.fieldCount, 1);
  assert.strictEqual(status.semanticSummary.requiredFieldCount, 1);
  assert.strictEqual(status.semanticSummary.validationRuleCount, 2);
  assert.strictEqual(status.semanticSummary.dataDisplayCount, 1);
  assert.strictEqual(status.semanticSummary.negativeInputCaseCount, 2);
  assert.deepStrictEqual(status.semanticSummary.dataDisplays, ["profile.displayName"]);
  assert.deepStrictEqual(status.semanticSummary.matchedDataDisplays, ["profile.displayName"]);
  assert.deepStrictEqual(status.semanticSummary.unmatchedDataDisplays, ["profile.unused"]);
  assert.deepStrictEqual(status.semanticSummary.warnings, ["no parser warning"]);
  assert.deepStrictEqual(status.quality.gaps, ["no success state assertion"]);
  assert.strictEqual(status.quality.dataDisplayAssertions, 1);
  assert.strictEqual(status.quality.forbiddenErrorAssertions, 1);
  assert.strictEqual(status.quality.textFromInputReferences, 1);
  assert.deepStrictEqual(status.quality.invalidTextFromReferences, ["input.timezone"]);
  assert.strictEqual(status.reportPath, "reports/run-1.json");
  assert.strictEqual(status.inputsPath, "inputs/login_inputs.json");
  assert.strictEqual(status.inputsSource, "generated_template");
  assert.strictEqual(status.negativeVerification.status, "skipped");
  assert.strictEqual(status.negativeVerification.reason, "no_negative_oracle");
  assert.strictEqual(status.negativeVerification.workflowName, "login_verification_negative_draft");
  assert.strictEqual(status.negativeVerification.resetStrategy, "fresh_observe_per_case");
  assert.deepStrictEqual(status.negativeVerification.oracles, []);
  assert.deepStrictEqual(status.generationTrace, ["field email -> paste input.email"]);
  assert.strictEqual(agentStatusSeverity(status), "warning");
  assert(agentStatusLabel(status).startsWith("Needs workflow improvement"));
  assert(agentStatusMarkdown(status).includes("Quality:"));
  assert(agentStatusMarkdown(status).includes("data display assertions: 1"));
  assert(agentStatusMarkdown(status).includes("forbidden error assertions: 1"));
  assert(agentStatusMarkdown(status).includes("text_from input references: 1"));
  assert(agentStatusMarkdown(status).includes("invalid text_from: input.timezone"));
  assert(agentStatusMarkdown(status).includes("Semantics:"));
  assert(agentStatusMarkdown(status).includes("matched display: profile.displayName"));
  assert(agentStatusMarkdown(status).includes("unmatched display: profile.unused"));
  assert(agentStatusMarkdown(status).includes("negative input cases: 2"));
  assert(agentStatusMarkdown(status).includes("Generation Trace:"));
  assert(agentStatusMarkdown(status).includes("field email -> paste input.email"));
  assert(agentStatusMarkdown(status).includes("Inputs Source: generated_template"));
  assert(agentStatusMarkdown(status).includes("Negative Verification:"));
  assert(agentStatusMarkdown(status).includes("status: skipped"));
  assert(agentStatusMarkdown(status).includes("reason: no_negative_oracle"));
  assert(agentStatusMarkdown(status).includes("reset strategy: fresh_observe_per_case"));
  assert(agentStatusMarkdown(status).includes("oracle count: 0"));
  assert(agentStatusMarkdown(status).includes("Add parsed validation error text before enabling negative verification."));
}

function testNormalizeFailedStepPayload() {
  const status = normalizeAgentStatus({
    result: "fail",
    workflowName: "checkout_verification",
    failedStep: {
      id: "assert_done",
      action: "assert_text",
      fixHint: "Render Done after checkout."
    }
  });

  assert.strictEqual(status.result, "fail");
  assert.strictEqual(status.failedStep.id, "assert_done");
  assert.strictEqual(status.failedStep.fixHint, "Render Done after checkout.");
  assert.strictEqual(agentStatusSeverity(status), "failed");
  assert(agentStatusMarkdown(status).includes("Failed step:"));
}

function testTimeoutPayload() {
  const status = normalizeAgentStatus({
    result: "timeout",
    workflow_name: "slow_verification",
    timeout_seconds: 30
  });

  assert.strictEqual(status.result, "timeout");
  assert.strictEqual(status.timeoutSeconds, 30);
  assert.strictEqual(agentStatusSeverity(status), "warning");
  assert(agentStatusLabel(status).startsWith("Timed out"));
}

function testNegativeFailureElevatesSeverity() {
  const status = normalizeAgentStatus({
    result: "pass",
    workflow_name: "signup_verification",
    negative_verification: {
      requested: true,
      status: "fail",
      reason: "oracle_not_found",
      workflow_name: "signup_verification_negative_draft",
      run_id: "run-negative",
      run_profile: "fast",
      reset_strategy: "fresh_observe_per_case",
      oracles: [{ text: "Invalid email", source: "html:text" }],
      report_path: "reports/run-negative.json",
      report_markdown_path: "reports/run-negative.md",
      report_hint: "Use get_run_report with run_id='run-negative' for full details.",
      next_action: "Keep invalid input rejected and visible.",
      steps_passed: 2,
      steps_total: 3
    }
  });

  assert.strictEqual(status.result, "pass");
  assert.strictEqual(status.negativeVerification.status, "fail");
  assert.strictEqual(status.negativeVerification.runId, "run-negative");
  assert.strictEqual(status.negativeVerification.runProfile, "fast");
  assert.strictEqual(status.negativeVerification.stepsPassed, 2);
  assert.strictEqual(status.negativeVerification.stepsTotal, 3);
  assert.strictEqual(status.negativeVerification.oracles[0].text, "Invalid email");
  assert.strictEqual(status.negativeVerification.oracles[0].source, "html:text");
  assert.strictEqual(agentStatusSeverity(status), "failed");
  assert(agentStatusMarkdown(status).includes("report hint: Use get_run_report with run_id='run-negative' for full details."));
  assert(agentStatusMarkdown(status).includes("oracle: Invalid email (html:text)"));
  assert(agentStatusMarkdown(status).includes("steps: 2/3"));
}

function testUnknownResult() {
  assert.strictEqual(normalizeResult("partial"), "unknown");
  const status = normalizeAgentStatus({ result: "partial" });
  assert.strictEqual(status.result, "unknown");
  assert.strictEqual(agentStatusSeverity(status), "info");
}

function testBuildVerifyImplementationArgs() {
  const args = buildVerifyImplementationArgs({
    taskDescription: "Verify profile saves",
    baseUrl: "fixtures/profile.html",
    runProfile: "dry-run",
    minQualityScore: 0.7,
    frameworkHint: "nextjs",
    noUntracked: true,
    runNegative: true
  });

  assert.deepStrictEqual(args, [
    "verify-impl",
    "--task-description",
    "Verify profile saves",
    "--base-url",
    "fixtures/profile.html",
    "--run-profile",
    "dry-run",
    "--format",
    "markdown",
    "--min-quality-score",
    "0.7",
    "--framework-hint",
    "nextjs",
    "--no-untracked",
    "--run-negative"
  ]);
}

testNormalizeSnakeCasePayload();
testNormalizeFailedStepPayload();
testTimeoutPayload();
testNegativeFailureElevatesSeverity();
testUnknownResult();
testBuildVerifyImplementationArgs();
console.log("agentStatus tests passed");
