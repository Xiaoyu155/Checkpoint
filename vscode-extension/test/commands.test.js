const assert = require("assert");
const EventEmitter = require("events");
const Module = require("module");
const cp = require("child_process");

const registered = new Map();
const quickPickQueue = [];
const panels = [];
const messages = [];

const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === "vscode") {
    class TreeItem {
      constructor(label, collapsibleState) {
        this.label = label;
        this.collapsibleState = collapsibleState;
      }
    }
    return {
      commands: {
        registerCommand: (name, callback) => {
          registered.set(name, callback);
          return { dispose: () => undefined };
        },
        executeCommand: async (name, ...args) => {
          const callback = registered.get(name);
          if (!callback) {
            throw new Error("Command not registered: " + name);
          }
          return callback(...args);
        }
      },
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
        }),
        onDidSaveTextDocument: () => ({ dispose: () => undefined })
      },
      window: {
        showQuickPick: async () => quickPickQueue.shift(),
        withProgress: async (_options, task) => task(),
        showInformationMessage: (message) => {
          messages.push({ kind: "info", message });
          return undefined;
        },
        showWarningMessage: (message) => {
          messages.push({ kind: "warning", message });
          return undefined;
        },
        createWebviewPanel: (viewType, title) => {
          const panel = { viewType, title, webview: { html: "" } };
          panels.push(panel);
          return panel;
        }
      },
      ProgressLocation: { Notification: 15 },
      ViewColumn: { Beside: 2 },
      TreeItem,
      TreeItemCollapsibleState: { None: 0 },
      ThemeIcon: class ThemeIcon {
        constructor(id, color) {
          this.id = id;
          this.color = color;
        }
      },
      ThemeColor: class ThemeColor {
        constructor(id) {
          this.id = id;
        }
      },
      EventEmitter: class VsEventEmitter {
        constructor() {
          this.event = () => undefined;
        }
        fire() {}
      },
      Uri: { parse: (value) => ({ value }) },
      env: { openExternal: async () => undefined }
    };
  }
  return originalLoad(request, parent, isMain);
};

const originalSpawn = cp.spawn;
const spawned = [];
cp.spawn = function fakeSpawn(command, args, options) {
  spawned.push({ command, args, options });
  const proc = new EventEmitter();
  proc.stdout = new EventEmitter();
  proc.stderr = new EventEmitter();
  process.nextTick(() => {
    proc.stdout.emit("data", Buffer.from("ok"));
    proc.emit("close", 0);
  });
  return proc;
};

async function main() {
  const { registerCommands } = require("../out/commands");
  const treeProvider = { refresh: async () => undefined };
  const context = { subscriptions: [], extensionPath: process.cwd() };

  registerCommands(context, treeProvider);

  assert(registered.has("visualAgent.verifyNow"));
  assert(registered.has("visualAgent.showProductIssues"));

  quickPickQueue.push({ label: "Dry Run", live: false });
  await registered.get("visualAgent.verifyNow")();
  const verifyCall = spawned.at(-1);
  assert.deepStrictEqual(verifyCall.args.slice(0, 4), ["-m", "visual_agent.cli", "verify-now", "--format"]);
  assert(verifyCall.args.includes("--workspace-root"));
  assert(!verifyCall.args.includes("--live"));
  assert.strictEqual(panels.at(-1).title, "Checkpoint: Verify Now");

  await registered.get("visualAgent.showProductIssues")();
  const issuesCall = spawned.at(-1);
  assert.deepStrictEqual(
    issuesCall.args.slice(0, 7),
    ["-m", "visual_agent.cli", "workspace-product-issues", "--root", ".agent-workspace", "--format", "markdown"]
  );
  assert.strictEqual(panels.at(-1).title, "Checkpoint: Product Issues");

  console.log("commands tests passed");
}

main()
  .catch((err) => {
    console.error(err);
    process.exitCode = 1;
  })
  .finally(() => {
    cp.spawn = originalSpawn;
    Module._load = originalLoad;
  });
