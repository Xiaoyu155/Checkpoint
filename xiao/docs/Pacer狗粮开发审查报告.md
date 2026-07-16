# Pacer 狗粮开发审查报告

更新时间：2026-07-09

本文档给 Code / 代码审查者使用。目标是说明 Pacer 这个产品接下来应该重点审查哪些文件，以及基于最近一次真实 dogfood 托管开发，对 Pacer 当前能力、短板和修复方向做一次产品化分析。

## 一、Code 应重点审查的文件

### P0：托管状态与验收闭环

这些文件直接决定 Pacer 能不能把任务推进到 `verified`，以及用户看到的状态是否可信。

- `src/visual_agent/chief_run.py`
  - 审查重点：mission 生命周期、resume、最终状态折算、final report 生成、`verified` / `verified_blocked` / `stopped` 语义。
  - 重点问题：worker 失败但 command gate 通过时不能误判 verified；显式 `--test-command` 时报告不能继续误导为 workflow coverage gap。

- `src/visual_agent/chief_background.py`
  - 审查重点：后台 worker 启动、隐藏窗口、心跳、超时、orphan 识别、完成状态回写。
  - 重点问题：后台进程已退出时，mission status 不能继续显示 running。

- `src/visual_agent/chief_dispatch.py`
  - 审查重点：worktree 创建、worker 执行、repair loop、command gate、toolchain gate、merge 后复验。
  - 重点问题：验收通过但工具链违规时必须是 `verified_blocked`，不能让用户误以为可合并。

- `src/visual_agent/mission_progress.py`
  - 审查重点：progress 是否是 CLI / dashboard / report 的共同状态源；`activity`、`stage`、`blocker` 是否准确。
  - 重点问题：不能用旧 worker record 或旧 verification 污染当前运行；空白日志不能覆盖最后有意义的输出。

- `src/visual_agent/command_verification.py`
  - 审查重点：显式测试命令是否能作为可信 command gate；失败分类是否足够明确。
  - 重点问题：dry-run、测试被篡改、缺环境、超时、命令无效不能被包装成产品验收通过。

### P1：覆盖、diff 与运行产物过滤

这些文件决定 Pacer 是否能识别真实产品改动，而不是被 artifacts、缓存或报告文件误导。

- `src/visual_agent/codex_check.py`
  - 审查重点：runtime artifact 过滤、workflow coverage、strict product acceptance。
  - 重点问题：`artifacts/`、`.agent-workspace/`、嵌套 repo 前缀路径不能导致 COVERAGE GAP。

- `src/visual_agent/diff_summary.py`
  - 审查重点：tracked + untracked 文件统计、函数提取、diff summary 是否完整。
  - 重点问题：新增文件必须进入 diff summary，否则 verified 报告会漏掉关键改动。

- `src/visual_agent/chief_engineer.py`
  - 审查重点：plan status、coverage gap、agent selection、runtime artifact 过滤。
  - 重点问题：有显式 command gate 时，plan 层应该清楚说明 workflow coverage 被测试命令接管。

### P1：Windows 后台体验与进程树

这些文件决定 Pacer 是否会打扰用户，以及长托管任务是否可控。

- `src/visual_agent/subprocess_window.py`
  - 审查重点：Windows `CREATE_NO_WINDOW`、`SW_HIDE`、detached process group。
  - 重点问题：只能隐藏 Pacer 直接启动的进程，npm / Codex / cmd 的孙进程仍需要进程树级别追踪。

- `src/visual_agent/workflow.py`
  - 审查重点：workflow 中 `run_command` 的真实执行、dry-run 语义、隐藏窗口参数。

- `src/visual_agent/dashboard/api.py`
  - 审查重点：dashboard 启动 worker、读取状态、隐藏进程、状态刷新。

### P2：用户可见体验

这些文件决定用户是否能看懂“现在发生了什么”。

- `src/visual_agent/dashboard/data.py`
- `src/visual_agent/dashboard/static/app.js`
- `src/visual_agent/dashboard/static/style.css`
- `src/visual_agent/portfolio_worker.py`
- `src/visual_agent/workbench_app.py`

审查重点：dashboard 是否展示 `stage`、`activity_label`、产品改动数、verification verdict、blocker、下一步操作，而不是只展示泛化 running。

### 必跑回归测试

- `tests/test_mission_progress.py`
- `tests/test_chief_run.py`
- `tests/test_chief_background.py`
- `tests/test_chief_dispatch.py`
- `tests/test_codex_check.py`
- `tests/test_diff_summary.py`
- `tests/test_command_verification.py`
- `tests/test_dashboard.py`

建议最小审查命令：

```powershell
python -m pytest tests/test_mission_progress.py tests/test_chief_run.py tests/test_chief_background.py tests/test_chief_dispatch.py tests/test_codex_check.py tests/test_diff_summary.py tests/test_command_verification.py -q
python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root . --format markdown
```

## 二、最近 Pacer dogfood 开发结果

最近一次真实托管任务：

- 目标项目：`D:\宠物小程序\backend`
- mission：`20260709-043012-586fa9`
- worktree：`D:\宠物小程序\backend.checkpoint-worktrees\20260709-043012-586fa9\track-1-codex`
- 最终状态：`verified`
- 产品改动：
  - `src/services/caseAtlasMatcher.js`
  - `test/caseAtlasMatcher.test.js`
- diff 规模：2 个文件，新增 53 行
- 验收命令：

```powershell
cmd /d /s /c if not exist node_modules\express\package.json npm ci --cache .npm-cache --prefer-offline ^&^& node --import ./test/setup.js --test test/diagnosisRisk.test.js test/caseAtlasMatcher.test.js
```

验收结果：`7/7 pass`。

实际产品改进：病例图谱匹配结果新增面向宠物主的相似度提示、就医前准备、红旗症状解释和分诊免责声明，避免把相似病例包装成确诊或治疗方案。

## 三、这次 dogfood 暴露的问题

### 1. 状态语义原本不够可信

观察到 worker 已经完成，但 status 仍显示 `worker_running`。这会误导用户，以为代码 worker 还在写，而实际已经进入验收阶段。

已修复方向：

- 增加 `verification_running` 阶段。
- worker record 为 completed 且后台仍活着时，进度显示为验收中。
- `mission status` 顶层 message 改为根据 progress 输出下一步。

### 2. Pacer 原本不知道“正在做什么”

之前只显示 `worker_running`，用户不知道是在读文件、安装依赖、跑测试还是卡住。

已修复方向：

- 增加 `activity` / `activity_label`。
- 可显示 `Installing dependencies`、`Running tests`、`Running verification` 等。
- 空白输出不再覆盖最后有意义的日志 tail。

还需要继续做：

- 将 activity 接入 dashboard。
- 记录当前命令、开始时间、持续时长。
- 对超过阈值的 dependency install / tests running 给出风险提示。

### 3. 显式 command gate 与 workflow coverage 的关系仍需收口

这次有明确 `--test-command`，Pacer 实际用 command gate 验收，但旧报告仍可能显示 `Plan status: needs_workflow_coverage`，容易让用户误解为验收闭环不完整。

已修复方向：

- final report 中 command gate 存在时显示 `Status: command_gate`。
- 原 workflow coverage 状态只作为说明：被显式测试命令接管。

还需要继续做：

- plan JSON 本身也应增加 `verification_mode: command`。
- dashboard 不应把 command-gate 任务渲染成 coverage risk。

### 4. 依赖安装 preflight 不足

这次 `npm ci` 真实耗时约 9 分钟。Pacer 能最终 verified，但托管过程里依赖安装占了大量时间。

问题本质：

- 派发前没有判断 `node_modules` 是否完整。
- 没有预估 npm install 是否会联网、是否有本地 cache、是否有 native dependency。
- Pacer 只能在验收阶段被动等待。

建议修复：

- 增加 `dependency_preflight`：
  - 检查 package manager。
  - 检查 lockfile。
  - 检查关键依赖是否存在。
  - 检查 cache 是否可用。
  - 标出 native install 风险。
- 对 Node 项目默认生成更稳的验收命令：

```powershell
cmd /d /s /c if not exist node_modules\express\package.json npm ci --cache .npm-cache --prefer-offline ^&^& node --import ./test/setup.js --test ...
```

### 5. Windows 子进程窗口控制仍不彻底

Pacer 直接启动的后台 worker 已经使用隐藏窗口参数，但 Codex CLI、npm、node-gyp、cmd shim 仍可能拉起子进程或 conhost。

观察结果：

- Pacer 后台 worker 没有残留。
- 正式验收期间出现过 cmd / node / conhost 子进程，来源是 npm install 链。

建议修复：

- 给 background record 增加完整 process tree snapshot。
- 每次 status 输出子进程树摘要。
- 超时或取消时清理整棵进程树。
- Windows 下尽量使用 `shell=False` + 参数数组；必须 `cmd /c` 时标记为 shell command。
- 报告里说明“Pacer 直接启动进程已隐藏，孙进程由 npm/Codex 拉起”。

### 6. verified 后的合并闭环还不够产品化

Pacer 为安全选择隔离 worktree，但 verified 后代码还没有自动进入主工作区。正常 Codex 直接改当前目录，用户更容易看到结果；Pacer 更安全，但要更明确告诉用户下一步。

建议修复：

- final report 固定展示：
  - verified: true / false
  - merged: true / false
  - worktree path
  - changed product files
  - recommended merge command
  - post-merge verification command
- 支持 `--merge --post-merge-verify`，只在 verified 且无冲突时合并。

## 四、我认为的解决方案路线

### 第一阶段：让状态可信

目标：用户不看日志也知道任务处于哪个真实阶段。

要做：

- 统一所有 CLI、dashboard、final report 从 `mission_progress.py` 读状态。
- 固定阶段：
  - `dispatch_ready`
  - `worker_running`
  - `dependency_install`
  - `tests_running`
  - `verification_running`
  - `verified`
  - `verified_blocked`
  - `blocked`
  - `worker_activity_stale`
- 状态字段必须包含：
  - `stage`
  - `activity`
  - `last_activity_at`
  - `changed_product_file_count`
  - `verification_verdict`
  - `blocker`

### 第二阶段：派发前阻断明显浪费时间的问题

目标：不要等托管半小时后才知道依赖或工具链不行。

要做：

- Node / Python / Flutter / Dart 工具链 preflight。
- dependency cache preflight。
- command launch preflight。
- worker toolchain policy 派发前提示。
- 对缺环境、缺 key、缺依赖的任务直接给 `preflight_blocked`。

### 第三阶段：验收与合并闭环

目标：Pacer 输出不是“看起来好了”，而是“已验收、可合并、或明确下一步”。

要做：

- command gate 与 workflow gate 统一抽象为 acceptance gate。
- dry-run 永远不能 verified。
- no product changes 永远不能 verified。
- worker failed 即使测试命令偶然 pass 也不能 verified。
- verified 后支持可选自动 merge。
- merge 后必须 post-merge verification。

### 第四阶段：托管体验产品化

目标：Pacer 能真正像托管产品，而不是后台日志聚合器。

要做：

- dashboard 展示实时 activity。
- 显示当前命令和耗时。
- 显示“卡住风险”，例如 dependency install 超过 10 分钟。
- 支持暂停/取消/清理进程树。
- final report 改成三段：
  - 结论
  - 证据
  - 下一步

## 五、总体判断

Pacer 相比正常 Codex 开发的核心价值是隔离、托管、证据链和验收闭环。最近这次 dogfood 已经证明：Pacer 能让 Codex 在隔离 worktree 中完成真实产品改动，并通过明确 command gate 到达 verified。

但 Pacer 的短板也很清楚：状态语义、进度可读性、依赖预检、Windows 进程树、verified 后合并闭环。只要把这些产品化收口做好，Pacer 才能从“能跑任务”升级到“可信托管任务”。
