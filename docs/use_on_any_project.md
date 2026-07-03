# 在任何项目上用 DevPacer

DevPacer 帮你省 Claude 额度、省时间、并且**用你项目自己的测试当验收**——不用先写任何工作流。核心思路：你只说要做什么，DevPacer 把便宜的活派给便宜模型、在隔离分支里干、跑你的测试确认真做好了才收工。AI 说"我改好了"不算数，测试过了才算。

## 最简单：桌面程序（推荐给不想记命令的人）

```powershell
checkpoint app
```

打开一个真正的窗口：**浏览…** 选你的项目文件夹 → 选编码 agent → 填目标 → 填验收命令（如 `pytest -q`）→
点 **预览**（不花钱）或 **开始执行**。窗口下方实时显示进度、停在哪、最终报告。
目标不知道怎么写？点 **帮我理清目标**，会用便宜的小模型帮你把模糊目标改写清楚、列出该先说清楚的点
（没配便宜模型时自动退回本地规则给建议，不联网也能用）。

> 账号说明：agent 用的账号 = 你在终端登录的那个。默认用 `codex`，先 `codex login` 登录你的 Codex 订阅；也可以切到 Claude Code（先 `claude` 登录）。

## 或者网页工作台

```powershell
cd 你的项目
checkpoint init --root .agent-workspace
checkpoint dashboard
```

浏览器会自动打开一个本地工作台，「新建任务」里同样填目标 → 验收命令 → 选 agent → 预览 / 执行。
（端口被占用会自动换一个，不会打不开。）

## 或者用命令行（3 条命令）

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

DevPacer 把编码工具当"打工人"来调度——默认 Codex，`--agent claude-code` 可切到 Claude Code。验收门是通用的：无论谁改的代码，都跑同一条 `--test-command` 确认。Cursor 等工具改完的代码，也能用 `checkpoint mission start --test-command ...`（不派新 worker、只验收）来把关。

## 给 Claude Code 教用户的一句话

> 想在这个项目上让 DevPacer 帮你省额度又保证质量：先 `checkpoint init --root .agent-workspace`，然后 `checkpoint mission start --goal "你要做的事" --test-command "你的测试命令" --execute`。它会在隔离分支里改代码、跑你的测试，过了才算完成。
