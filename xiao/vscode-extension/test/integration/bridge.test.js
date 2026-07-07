const assert = require("assert");
const EventEmitter = require("events");
const Module = require("module");
const cp = require("child_process");

const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") {
    return {
      workspace: {
        workspaceFolders: [{ uri: { fsPath: process.cwd() } }],
        getConfiguration: () => ({
          get: (key, defaultValue) => {
            if (key === "pythonPath") {
              return "fake-python";
            }
            if (key === "workspaceRoot") {
              return ".agent-workspace";
            }
            return defaultValue;
          }
        })
      },
      window: {
        showInformationMessage: () => undefined
      }
    };
  }
  return originalLoad(request, parent, isMain);
};

const originalSpawn = cp.spawn;
let captured;
cp.spawn = function fakeSpawn(command, args, options) {
  captured = { command, args, options };
  const proc = new EventEmitter();
  proc.stdout = new EventEmitter();
  proc.stderr = new EventEmitter();
  process.nextTick(() => {
    proc.stdout.emit("data", Buffer.from(JSON.stringify({ status: "success", saved_to: "workflows/demo.yaml" })));
    proc.emit("close", 0);
  });
  return proc;
};

const { runCli, renderLatestFailureMarkdown } = require("../../out/bridge");

async function testRunCliInvokesVisualAgentModule() {
  const result = await runCli(["generate-workflow", "--description", "Verify login"], { progressHint: false });

  assert.strictEqual(result.code, 0);
  assert.strictEqual(captured.command, "fake-python");
  assert.deepStrictEqual(captured.args.slice(0, 3), ["-m", "visual_agent.cli", "generate-workflow"]);
  assert(captured.args.includes("--workspace-root"));
  assert.strictEqual(JSON.parse(result.output).status, "success");
}

testRunCliInvokesVisualAgentModule()
  .then(() => {
    cp.spawn = originalSpawn;
    console.log("bridge integration tests passed");
  })
  .catch((err) => {
    cp.spawn = originalSpawn;
    console.error(err);
    process.exit(1);
  });

function testRenderLatestFailureMarkdownIncludesStructuredFields() {
  const markdown = renderLatestFailureMarkdown(
    JSON.stringify({
      status: "found",
      workflow: "checkout",
      run_id: "run-1",
      failed_step: { id: "assert_total", action: "assert_text" },
      expected: "Total shown",
      actual: "Total missing",
      hint: "Check the totals block.",
      artifacts: "runs/run-1",
      structured_failure: {
        step_id: "assert_total",
        action: "assert_text",
        root_cause: "assertion_wrong",
        confidence: 0.92,
        page_url: "http://localhost:3000/checkout",
        page_state: "authenticated",
        screenshot_path: "runs/run-1/assert_total.png",
        suggested_fix: "Update the totals assertion.",
        related_files: ["src/pages/Checkout.tsx", "workflows/checkout.yaml"]
      }
    })
  );

  assert(markdown.includes("## Structured Failure"));
  assert(markdown.includes("assertion_wrong"));
  assert(markdown.includes("Update the totals assertion."));
  assert(markdown.includes("src/pages/Checkout.tsx"));
}

testRenderLatestFailureMarkdownIncludesStructuredFields();
