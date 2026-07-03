# 在任何项目上用 DevPacer（3 条命令）

DevPacer 帮你省 Claude 额度、省时间、并且**用你项目自己的测试当验收**——不用先写任何工作流。核心思路：你只说要做什么，DevPacer 把便宜的活派给便宜模型、在隔离分支里干、跑你的测试确认真做好了才收工。AI 说"我改好了"不算数，测试过了才算。

## 一次性准备（每个项目一次）

```powershell
cd 你的项目
checkpoint init --root .agent-workspace
```

## 干活：一条命令

```powershell
checkpoint mission start ^
  --goal "把 calc.py 里的 add 函数修好，让测试通过" ^
  --repo-root . ^
  --test-command "pytest -q" ^
  --execute --run-profile supervised
```

- `--goal`：说人话，要做什么。含糊的目标会被拦下来让你先说清楚（省一次白跑）。
- `--test-command`：**你项目自己的测试/构建命令**当验收门。Python 用 `pytest -q`，前端用 `npm test`，Rust 用 `cargo test`，构建用 `npm run build`……随便你项目怎么测。
- 不加 `--execute` 就是**预览**（只看它准备怎么干、派给哪个模型、预计怎么验收），不花钱、不改代码。

它会：在隔离 git worktree 里派一个 worker 改代码 → 跑你的 `--test-command` → **过了才 verified，不过就带着报错自动重修一轮**。你的主分支全程不动。

## 省额度是怎么发生的

- **便宜路由**：机械小活（改文案、重命名）自动派给便宜模型/便宜后端（如 MiMo），烧的不是你的 Claude 订阅；复杂活才上 Opus。
- **瘦上下文**：worker 每次是全新会话，只拿到这个任务需要的东西，不让它满项目乱翻、不重复读整个工程。
- **确定性验收门**：测试不过就打回，AI 不能用"我觉得好了"糊弄你，也就不会在死循环里烧额度。

## 看结果

```powershell
checkpoint mission list                 # 所有任务：verified / 停在哪 / 花了多少 / 省了多少
checkpoint dashboard                    # 网页看板：已验收交付、省钱路由、真实花费 vs 省下
checkpoint quota                        # Claude 订阅 5 小时 / 7 天额度用了多少（不是 API 花费）
checkpoint repo-map --goal "你的目标"    # 零 token 的项目架构地图（worker 派发时自动带上）
```

额度感知需要一次性配置：把 Claude Code 的 statusline 指到 `checkpoint quota-statusline`
（`~/.claude/settings.json` 里加 `{"statusLine": {"type": "command", "command": "checkpoint quota-statusline"}}`），
之后每次 Claude Code 刷新状态栏，DevPacer 就有最新的 5h/7d 用量，可在派发前预警。

## AI 不能糊弄验收

worker 改动测试文件会被机械检测到并直接判验收失败（测试是验收契约，不许动）；
任务本身就是要改测试时，显式加 `--allow-test-edits`。

## 适配 Codex / Cursor 等

DevPacer 把编码工具当"打工人"来调度——默认 Claude Code，`--agent codex` 换成 Codex。验收门是通用的：无论谁改的代码，都跑同一条 `--test-command` 确认。Cursor 等工具改完的代码，也能用 `checkpoint mission start --test-command ...`（不派新 worker、只验收）来把关。

## 给 Claude Code 教用户的一句话

> 想在这个项目上让 DevPacer 帮你省额度又保证质量：先 `checkpoint init --root .agent-workspace`，然后 `checkpoint mission start --goal "你要做的事" --test-command "你的测试命令" --execute`。它会在隔离分支里改代码、跑你的测试，过了才算完成。
