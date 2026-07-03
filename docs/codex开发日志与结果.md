# Codex 开发日志与结果

本文档是 DevPacer / Checkpoint 后续由 Codex 接手开发时的专属记录。每次 Codex 修改本仓库，都应在这里追加：目标、改动文件、验证命令、结果、已知风险和回滚线索，方便 Fable 或其他开发者审查。

## 2026-07-03 P0-P1 接手

### 目标

- 支持多任务队列中每个任务选择不同 coding worker，例如一个任务用 Claude Code，另一个任务用 Codex；后续可扩展到其他 coding agent。
- 将工作台默认策略从“执行即自动合并”改为“验收通过后等待用户确认”，自动合并只作为显式选项。
- 将验收命令、agent、合并策略、测试编辑策略等执行上下文持久化到队列，避免托管/恢复时丢失。
- 任务结束时自动写项目级强制测试记录文件，形成可审查、可回滚的交付轨迹。
- 改善失败分类，减少把环境/命令错误误判成代码失败导致额度浪费。

### 初始状态

- `.visual-agent-status.md` 当前状态：PASSING。
- 重点风险文件：
  - `src/visual_agent/chief_queue.py`
  - `src/visual_agent/chief_run.py`
  - `src/visual_agent/command_verification.py`
  - `src/visual_agent/dashboard.py`
  - `src/visual_agent/workbench_app.py`
  - `src/visual_agent/cli.py`

### 本轮验证记录

- `python -m pytest tests/test_chief_queue.py tests/test_dashboard_workbench.py tests/test_chief_run.py tests/test_command_verification.py tests/test_workbench_app.py -q`
  - 结果：33 passed。
- `python -m pytest tests/test_chief_queue.py tests/test_mission_plan_import.py tests/test_mission_facade.py tests/test_dashboard.py tests/test_dashboard_workbench.py tests/test_chief_background.py tests/test_chief_dispatch.py tests/test_command_verification.py tests/test_chief_run.py -q`
  - 结果：66 passed。
- `$env:PYTHONPATH='src'; python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root .`
  - 结果：命令退出 0，但 verdict 为 COVERAGE GAP。
  - 说明：当前 Checkpoint 只命中 `mission_import_acceptance`，且为 inspection-only；本轮 queue/workbench/verification 变更还缺精确产品 workflow 覆盖，不能把该结果当成完整产品验收通过。
- `python -m pytest -q`
  - 结果：120 秒超时；随后 300 秒重跑仍超时。
  - 说明：本轮不声明全仓测试通过；已验证范围是上面的 66 个受影响测试。后续需要拆分定位全量测试耗时/卡住的测试组。

### 本轮已完成

- 队列项新增并持久化：`agent`、`test_command`、`allow_test_edits`、`merge_policy`。
- 队列 worker 恢复 mission 时会传入对应 agent、验收命令、测试编辑策略和合并策略。
- 恢复已有 mission 时，如果队列项指定 agent，会覆盖落盘 plan 的 coding track，使不同队列任务可以分别使用 Codex / Claude Code / 后续 coding agent。
- Web 工作台和桌面工作台默认不自动合并；只有显式选择自动合并才会在验收通过后 merge。
- Dashboard 队列列表读取字段从错误的 `items` 修正为 `entries`。
- 每次 mission 结束自动向项目根目录 `强制测试记录.md` 追加记录；写入失败降级，不阻断 mission。
- 验收命令失败新增 `failure_kind`，可区分 `test_command_invalid`、`command_launch_error`、`command_timeout`、`command_failed`。

### 回滚线索

- 如果多任务队列恢复异常，优先回滚 `chief_queue.py` 和 `cli.py` 中新增的 queue context 字段。
- 如果工作台启动异常，优先检查 `dashboard.start_workbench_mission` 参数兼容性。
- 如果项目级记录写入影响任务结束，优先检查 `chief_run._append_mandatory_record`，该功能应降级失败，不能阻断 mission。

## 2026-07-03 P2-P5 继续开发

### 目标

- P2：自动识别项目验收命令，降低普通用户手填命令的门槛。
- P3：大任务计划导入创建/排队时继承 worker、验收命令、测试编辑策略和合并策略。
- P4：工作台展示托管队列状态和每个任务的 agent / merge 策略。
- P5：为 queue/workbench/verification 本轮改动补 Checkpoint workflow 覆盖，并记录验证结论。

### 初始状态

- `.visual-agent-status.md` 当前状态：PASSING。
- 当前未跟踪的 `assets/`、`build/`、打包脚本等不是本轮 Codex 产生，继续不触碰。

### 本轮已完成

- 新增 `verification_profiles.py`，可从 `package.json`、`pyproject.toml`、`pytest.ini`、`Cargo.toml`、`go.mod` 等识别默认验收命令。
- `run_chief_mission` 支持 `--test-command auto`，并会把自动识别结果写入 plan 的 `verification_profile`。
- Web/桌面工作台在验收命令留空时尝试自动识别测试命令。
- `mission import --create --queue` 现在会把 `agent`、`test_command`、`allow_test_edits`、`merge_policy` 透传到创建的 mission 和 queue item。
- Dashboard 新增“托管队列”区块，展示每个队列项的 agent、merge policy、attempts 和状态。
- 新增 Checkpoint workflow：`.agent-workspace/workflows/devpacer_queue_workbench_acceptance.yaml`，覆盖本轮 queue/workbench/verification profile 相关文件。
- 更新 `docs/use_on_any_project.md`，说明 `--test-command auto`、大任务导入排队、多 agent 队列和默认手动合并。

### 本轮验证记录

- `python -m pytest tests/test_verification_profiles.py tests/test_mission_plan_import.py tests/test_dashboard_workbench.py tests/test_chief_queue.py tests/test_chief_run.py tests/test_command_verification.py tests/test_workbench_app.py tests/test_dashboard.py tests/test_chief_dispatch.py -q`
  - 结果：71 passed。
- `$env:PYTHONPATH='src'; python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root .`
  - 结果：命令退出 0，但 verdict 仍为 COVERAGE GAP。
  - 说明：本轮新增 workflow 已命中 `devpacer_queue_workbench_acceptance`，但工作树存在未跟踪的 `assets/`、`build/`、打包脚本等非本轮文件，仍造成覆盖缺口；不能声明完整产品验收通过。

### 回滚线索

- 若自动验收命令误判，优先回滚 `src/visual_agent/verification_profiles.py` 以及 `chief_run.py` / `dashboard.py` 中调用 `resolve_test_command` 的改动。
- 若大任务导入排队异常，优先检查 `mission_plan_import.import_development_plan` 新增的 `test_command`、`allow_test_edits`、`merge_policy` 参数透传。
- 若 dashboard 页面异常，优先检查 `DASHBOARD_HTML` 中新增的 `queue` 区块渲染。

## 2026-07-03 Project Autopilot 设计计划（小时级额度托管）

### 目标修正

负责人明确：时间规划不是按周，而是按小时；核心约束是订阅制 coding worker 的 5 小时窗口。产品目标不是让普通用户承担验收，而是尽量从开发环节解放个人开发者：

- 用户给目标或开发计划。
- DevPacer 拆任务、排队、按小时规划、按额度调度。
- 尽量避免 5 小时窗口被一次烧穿。
- 强模型额度吃紧时，继续做低额度/零额度工作：计划整理、repo map、GitHub 技术方案检索、文档、轻量任务、便宜后端任务。
- 验收不再包装成“万能裁判”，而是作为阻止明显错误和生成交接证据的辅助层。

### 当前实现差距

已实现：

- Markdown 计划可拆成 mission drafts，并可 `--create --queue`。
- 队列项可保存 `agent`、`test_command`、`allow_test_edits`、`merge_policy`。
- 订阅额度已有 5h/7d snapshot，可显示 used percentage 和 reset 时间。
- mission 有 `max_wall_minutes`、`max_worker_minutes`、`max_rounds`。
- 项目记忆 v1 可从落盘 mission/round/final_report 聚合历史。

未实现：

- 没有 Project/Program 级对象，无法维护“整个项目今天要做哪些、已完成哪些、下一步是什么”。
- 没有小时级日程规划器，只有单 mission 超时。
- 没有 5 小时窗口内的额度分配算法。
- 没有持续开发策略：强模型额度紧张时不会自动切到低额度任务。
- 没有 GitHub 成熟技术案例检索与引用。
- 没有任务依赖图，队列只是优先级排序，不懂“先 A 后 B”。
- 没有普通用户友好的“今天能做什么、预计多久、何时暂停、何时恢复”的计划视图。

### 新产品层：Project Autopilot

新增一层 Project Autopilot，位于 DevPacer mission 之上：

```text
Project Autopilot
  ├─ Program：一个项目级开发计划
  ├─ Milestones：今天/当前 5h 窗口内的阶段目标
  ├─ Mission Graph：带依赖的任务图
  ├─ Hourly Budget Planner：小时级额度与时间规划
  ├─ Reference Research：成熟技术方案/GitHub 案例检索
  ├─ Scheduler：按额度、依赖、风险调度 mission queue
  └─ Daily Report：给普通用户看的进度、结果、下一步
```

底层仍复用现有：

- `mission import`：导入开发计划。
- `chief_run`：单任务执行。
- `chief_queue`：执行队列。
- `subscription_quota`：5h/7d 额度快照。
- `project_memory`：历史证据记忆。
- `强制测试记录.md`：项目级审查轨迹。

### 核心对象设计

#### Program

落盘路径：

```text
.agent-workspace/programs/<program_id>/program.json
.agent-workspace/programs/<program_id>/timeline.jsonl
.agent-workspace/programs/<program_id>/daily_plan.md
.agent-workspace/programs/<program_id>/reference_pack.md
```

字段：

```json
{
  "program_id": "20260703-yuansi",
  "repo_root": "D:/抖音快手支付宝",
  "objective": "继续开发元思轻语",
  "source_plan": "开发计划.md",
  "status": "planning|running|paused|blocked|done",
  "created_at": "...",
  "updated_at": "...",
  "quota_policy": {
    "window_minutes": 300,
    "strong_worker_budget_minutes": 180,
    "reserve_minutes": 45,
    "pause_at_used_percentage": 82,
    "resume_after_reset": true
  },
  "missions": [],
  "current_focus": "",
  "next_action": ""
}
```

#### Mission Graph

每个任务不再只是 flat queue item，要有：

```json
{
  "task_id": "task-003",
  "objective": "实现语音悬浮窗历史记录",
  "depends_on": ["task-001"],
  "risk": "low|medium|high|external",
  "worker_tier": "cheap|standard|strong",
  "agent": "codex",
  "estimated_minutes": 35,
  "estimated_strong_minutes": 25,
  "estimated_rounds": 1,
  "test_command": "auto",
  "acceptance_mode": "command|workflow|manual_review|best_effort",
  "status": "pending|queued|running|verified|needs_review|blocked"
}
```

### 小时级规划策略

5 小时窗口按 300 分钟看，但不能全烧完。默认规划：

```text
0-15 min     本地零额度准备：读计划、repo map、拆任务、识别验收命令
15-35 min    便宜模型/规则规划：任务排序、风险分类、生成今日计划
35-155 min   强 worker 主开发窗口 1：高价值任务 1-2 个
155-175 min  冷却/整理：记录、测试、文档、GitHub 方案检索、下一批 prompt 压缩
175-255 min  强 worker 主开发窗口 2：继续任务或修复
255-285 min  收尾窗口：只允许低风险修复、文档、记录、轻量测试
285-300 min  保护窗口：禁止启动新的强 worker，保存状态，生成恢复计划
```

关键原则：

- 永远保留 45 分钟强模型额度作为恢复/收尾缓冲。
- 超过 80%-85% 5h 使用率后，不再启动新的强 worker。
- 如果任务预计超过剩余可用强模型窗口，拆小或排到 reset 后。
- 额度紧张时继续做“不烧强额度”的工作，而不是停摆：
  - 本地 repo map。
  - 测试命令识别。
  - GitHub 技术方案检索。
  - 计划整理。
  - 文档更新。
  - cheap backend 小任务。
  - 已完成 diff 总结。

### 调度算法 v1

输入：

- 当前 5h quota snapshot。
- 当前时间。
- 任务图。
- 历史 mission 统计。
- 每类任务估算耗时。

输出：

- 今天接下来 1-5 小时的执行计划。
- 哪些任务现在跑，哪些等 reset。
- 哪些走 Codex/Claude Code，哪些走 cheap backend。
- 哪些只做 research/spec，不写代码。

伪代码：

```text
quota = load_quota_snapshot()
available_strong_minutes = estimate_remaining_5h_minutes(quota)
reserve = 45
usable = max(0, available_strong_minutes - reserve)

tasks = load_program_tasks()
ready = tasks whose dependencies are done

for task in ready:
    classify risk/tier
    estimate minutes
    if task.external_or_needs_secret:
        mark blocked_for_user_context, do not run worker
    elif task.estimated_strong_minutes <= usable and quota.used < 82%:
        schedule strong worker
        usable -= estimate
    elif task.can_run_cheap:
        schedule cheap backend
    else:
        schedule research/spec/doc only

if usable < 20:
    stop launching strong workers
    write resume plan after reset
```

### GitHub 技术案例引用

目标不是让 AI 上网乱抄，而是降低“凭空发明”的概率。

#### Reference Research 阶段

每个中高风险任务在派发前生成 `reference_pack.md`：

```text
任务：Flutter StatefulShellRoute 保活语音页

推荐关键词：
- Flutter StatefulShellRoute indexedStack bottom navigation preserve state
- go_router StatefulShellRoute voice session background

参考来源：
- 官方文档优先
- 高 star GitHub repo 其次
- StackOverflow/博客只能作为辅助

Worker 约束：
- 使用成熟库/官方模式
- 不引入无人维护依赖
- 不复制大段代码
- 只引用设计模式和 API 用法
```

实现顺序：

1. 先做“关键词生成 + 人工可打开链接”的 reference pack。
2. 再做 GitHub API / 搜索接入。
3. 最后做代码片段提炼，但必须限制版权风险，只给摘要和链接，不大段复制。

### 验收定位调整

对普通用户，不应承诺“全自动验收等于产品正确”。新的验收分层：

```text
Level 0: 编译/语法/测试命令通过
Level 1: 项目已有测试通过
Level 2: 工具自动生成/选择的 workflow 通过
Level 3: 运行截图/日志/关键路径证据完整
Level 4: 用户体验仍需人工最终确认
```

Autopilot 的承诺应该是：

- 不让明显坏的代码合并。
- 不让 worker 篡改测试糊弄通过。
- 不让环境错误被当成代码错误无限修。
- 给普通用户一份“你要看的最少内容”：
  - 改了什么。
  - 怎么运行。
  - 哪些地方最可能需要你点一下看看。
  - 如果你不看，风险是什么。

### 需要实现的命令

```powershell
checkpoint program create --file 开发计划.md --repo-root . --workspace-root .agent-workspace
checkpoint program plan --program <id> --hours 5 --prefer-continuous
checkpoint program start --program <id> --watch
checkpoint program status --program <id>
checkpoint program pause --program <id>
checkpoint program resume --program <id>
checkpoint program report --program <id>
```

普通用户路径：

```powershell
checkpoint autopilot --file 开发计划.md --hours 5 --agent codex --test-command auto
```

这个命令内部做：

1. 创建 Program。
2. 拆任务图。
3. 生成 5 小时内计划。
4. 排队可执行任务。
5. 开始 worker。
6. 快到额度限制时自动暂停强 worker。
7. 继续做 cheap/research/doc。
8. 写 daily report 和强制测试记录。

### 阶段计划

#### A1：Program 状态层

文件：

- `programs.py`
- `program_scheduler.py`
- `tests/test_programs.py`

完成标准：

- 能从 `mission import` 结果生成 Program。
- Program 保存任务图、依赖、状态。
- `program status` 能显示 pending/running/done/blocked。

#### A2：小时级 Budget Planner

文件：

- `hourly_budget.py`
- `tests/test_hourly_budget.py`

完成标准：

- 输入 quota snapshot + tasks，输出 5 小时计划。
- used >= 82% 时不再启动 strong worker。
- 保留 45 分钟 reserve。
- 强 worker、cheap worker、research/doc 任务分流。

#### A3：Program Scheduler 接队列

文件：

- `program_scheduler.py`
- `chief_queue.py`

完成标准：

- Scheduler 按依赖把 ready tasks 提交到 mission queue。
- 一个任务完成后自动解锁后续任务。
- blocked/external 任务不会被派给 coding worker。

#### A4：Reference Research v1

文件：

- `reference_research.py`
- `tests/test_reference_research.py`

完成标准：

- 对任务生成搜索关键词和 reference_pack.md。
- 先不强制联网，不依赖 GitHub API。
- Worker prompt 能引用 reference_pack 的约束和链接。

#### A5：普通用户 Autopilot 入口

文件：

- `cli.py`
- `dashboard.py`

完成标准：

- `checkpoint autopilot --file ... --hours 5` 可用。
- Dashboard 显示今日计划、剩余额度、正在做什么、暂停原因。
- 报告不要求用户懂测试，只给最少必要审查点。

### 风险与边界

- 无法保证真实产品体验完全正确，特别是 Flutter 真机、语音、支付、生产环境、外部凭据。
- 无法可靠预知 Codex/Claude 真实剩余额度，只能基于 snapshot 和历史用量估算。
- GitHub 技术案例只能作为参考约束，不能无审查复制代码。
- 如果任务需要账号、密钥、生产环境权限，Autopilot 必须识别为 blocked/external，不能假装完成。

### 本次设计结论

当前 DevPacer 已经具备单 mission 执行器和基础队列。要实现负责人说的“个体开发者从开发环节解放出来”，下一步不是继续强化单 mission 验收，而是新增 Project Autopilot：

- 用 Program 管项目。
- 用小时级 planner 管 5 小时额度。
- 用 scheduler 持续推进任务链。
- 用 reference research 降低 AI 乱写概率。
- 用 best-effort 验收和最小审查点服务普通用户。

这个方向比“全自动验收”更诚实，也更贴近用户痛点：用户不用懂工程细节，工具尽量持续推进开发；真正需要用户判断的地方，被压缩成少量明确问题。

## 2026-07-03 Project Autopilot 首版落地

### 本轮已完成

- 新增 `src/visual_agent/hourly_budget.py`：5 小时窗口的小时级额度规划器。
  - 从 quota snapshot 估算剩余 5h headroom。
  - 默认保留 45 分钟 reserve。
  - 5h used >= 82% 时停止启动新的 strong worker。
  - 将任务分为 strong_worker、cheap_worker、research_or_doc、deferred、blocked。
- 新增 `src/visual_agent/programs.py`：Project Autopilot 的 Program 状态层。
  - 从 Markdown 开发计划创建 Program。
  - 保存到 `.agent-workspace/programs/<program_id>/program.json`。
  - 生成任务图，支持顺序依赖。
  - 生成 `daily_plan.md` / `daily_plan.json`。
- 新增 `src/visual_agent/reference_research.py`：Reference Pack v1。
  - 不联网，先生成官方文档/GitHub 搜索关键词、来源策略、worker 约束。
  - 保存到 `.agent-workspace/programs/<program_id>/references/`。
- 新增 `src/visual_agent/program_scheduler.py`：Program Scheduler。
  - 按 hourly plan 选择 ready task。
  - 为任务生成 reference pack。
  - 创建 mission preview。
  - 提交到 mission queue。
  - 回写 task 的 `mission_id`、`queue_id`、`status`。
- CLI 新增：
  - `checkpoint program create`
  - `checkpoint program plan`
  - `checkpoint program start`
  - `checkpoint program status`
  - `checkpoint program list`
  - `checkpoint program report`
  - `checkpoint autopilot --file 开发计划.md --hours 5 --agent codex --test-command auto`
- Dashboard 新增“项目托管”区块，展示 Program 列表。
- `docs/use_on_any_project.md` 已补充 Autopilot 用法。
- `.agent-workspace/workflows/devpacer_queue_workbench_acceptance.yaml` 已纳入 Autopilot 新文件与测试文件。

### 仍未完成

- Scheduler 还没有自动监听 queue 完成并解锁后续依赖；目前需要再次运行 `program start` 或后续接 queue completion hook。
- Reference Research v1 只生成关键词和约束，不联网检索 GitHub/API。
- 还没有真实成本预测，只用 quota snapshot 和分钟估算做保守规划。
- Dashboard 只展示 Program 列表，还没有完整 daily plan 和暂停/恢复按钮。
- 没有实现 `program pause/resume` 命令。

### 本轮验证记录

- `python -m pytest tests/test_hourly_budget.py tests/test_programs.py tests/test_reference_research.py tests/test_program_scheduler.py tests/test_mission_plan_import.py tests/test_chief_queue.py tests/test_dashboard_workbench.py tests/test_verification_profiles.py tests/test_chief_run.py tests/test_command_verification.py -q`
  - 结果：49 passed。
- `python -m pytest tests/test_hourly_budget.py tests/test_programs.py tests/test_reference_research.py tests/test_program_scheduler.py tests/test_dashboard_workbench.py tests/test_dashboard.py tests/test_mission_plan_import.py tests/test_chief_queue.py tests/test_verification_profiles.py -q`
  - 结果：41 passed。
- `python -m pytest tests/test_hourly_budget.py tests/test_programs.py tests/test_reference_research.py tests/test_program_scheduler.py tests/test_mission_plan_import.py tests/test_chief_queue.py tests/test_dashboard_workbench.py tests/test_dashboard.py tests/test_verification_profiles.py tests/test_chief_run.py tests/test_command_verification.py tests/test_chief_dispatch.py -q`
  - 结果：75 passed。
- `$env:PYTHONPATH='src'; python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root .`
  - 结果：命令退出 0，但 verdict 仍为 COVERAGE GAP。
  - 说明：`devpacer_queue_workbench_acceptance` 已命中并 inspection-only 通过；覆盖缺口主要来自当前工作树中既有未跟踪的 `assets/`、`build/`、打包脚本等，不是本轮 Autopilot 核心代码。

### 回滚线索

- 若 Autopilot CLI 异常，优先回滚 `cli.py` 中 `program` / `autopilot` parser 和分发逻辑。
- 若 Program 状态异常，优先检查 `programs.py` 的 `create_program_from_plan` / `ready_program_tasks`。
- 若误排任务或额度策略不合理，优先检查 `hourly_budget.py`。
- 若入队失败，优先检查 `program_scheduler.py` 的 `start_program`。

## 2026-07-04 成熟度计划 Phase 0：安装自举

### 目标

- 按 `docs/成熟度开发计划_2026-07.md` 完成 Phase 0：本机 editable install、全局 `checkpoint` 命令、V0 三连、exe 打包与启动验证。

### 本轮已完成

- 系统 Python 执行 `python -m pip install -e .` 成功，`checkpoint` 命令已进入 PATH。
- `.venv` 同步执行 editable install，避免 exe 打包旧版本代码。
- 修复 `scripts/build_exe.ps1` 的仓库根目录解析问题。
- 修复 `scripts/checkpoint.spec` 的 PyInstaller 相对路径问题，入口、数据文件和图标改为绝对路径。
- 新增 `docs/验证记录.md`，记录 Phase 0 实测命令、结果和风险。

### 本轮验证记录

- `checkpoint quickstart`
  - 结果：通过。
- `checkpoint agents doctor`
  - 结果：通过；识别到 Claude Code `2.1.193`、Codex CLI `0.142.5`，Gemini 未安装。
- `checkpoint chief-run-demo --workspace-root .agent-workspace --format json`
  - 结果：通过；`status: verified`，最后一轮 `checkout_verification` 为 L4 product acceptance，真实交互数 1。
- `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1`
  - 结果：通过；生成 `dist\DevPacer\DevPacer.exe`。
- `Start-Process .\dist\DevPacer\DevPacer.exe`
  - 结果：通过；5 秒后进程仍存活，窗口标题为 `DevPacer 工作台`，关闭后退出码 0。
- `python -m pytest tests/test_cli.py -q`
  - 结果：56 passed。
- `python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root .`
  - 结果：命令退出 0，但 verdict 为 COVERAGE GAP。
  - 说明：命中了 `devpacer_queue_workbench_acceptance` 和 `mission_import_acceptance`，均为 inspection-only；当前工作树大量既有文档归档、脚本、资产和核心代码改动仍未被精确 workflow 覆盖，不能声明完整产品验收通过。

### 已知风险

- Phase 0 已过，但核心真实 AI worker 链路仍需进入 Phase 1 验证。
- 本轮没有清理既有工作树改动；所有非本轮改动保持原状。

## 2026-07-04 成熟度计划 Phase 1 / V1：真实 worker 小任务

### 目标

- 在隔离的最小 Python 项目中，用真实 Codex worker 完成一次“计划 -> 开发 -> pytest 验收 -> merge”。

### 暴露并修复的问题

- `mission start --test-command ...` 在 workspace 不存在时仍被 planning 阻断。
  - 修复：`run_chief_mission` 对 test-command 场景自动初始化 workspace。
- 自动 workspace init 追加 `.agent-workspace/*` 子路径到 `.gitignore`，导致 `.gitignore` 变脏并阻断 dispatch。
  - 修复：`_ensure_gitignored` 只写 `.agent-workspace/`，已有根忽略项时不再改文件。
- 目标明确要求“写测试”时，test tamper gate 仍禁止测试文件改动，导致自动修复删掉新增测试。
  - 修复：mission 层根据 objective 推断 test-edit policy；明确要求写/补/新增/更新测试时允许测试改动，普通任务仍禁止。

### 本轮验证记录

- `python -m pytest tests/test_chief_run.py tests/test_chief_dispatch.py tests/test_workbench_app.py -q`
  - 结果：38 passed。
- `python -m pytest tests/test_cli.py -q`
  - 结果：56 passed。
- V1 最终命令：
  - `checkpoint mission start --goal "给 utils 加一个 slugify 函数并写测试。要求 slugify(' Hello, World! ') == 'hello-world'，连续空白压成单个连字符，去掉首尾连字符。" --agent codex --test-command "python -m pytest -q" --execute --merge --allow-coverage-gap --workspace-root .agent-workspace --repo-root . --max-rounds 2 --max-worker-minutes 20 --timeout-seconds 1200 --format json`
  - 运行目录：`D:\助手codex\.agent-workspace\phase1_v1_slugdemo_tests`
  - 结果：`status: verified`，merge commit `7fbec326c50b1398a7e99dfcadf221d5f9a22524`。
  - gate：`python -m pytest -q`，结果 `4 passed in 0.01s`。
  - diff 摘要：`src/slugdemo/__init__.py`、`src/slugdemo/utils.py`、`tests/test_utils.py`；包含 `slugify` 和 3 个测试函数。
- 合并后人工复核：
  - `python -m pytest -q`：4 passed。
  - `$env:PYTHONPATH='src'; python -c "...slugify assertions..."`：通过，输出 `hello-world`。
- V1 后回归：
  - `python -m pytest tests/test_chief_run.py tests/test_chief_dispatch.py tests/test_workbench_app.py -q`：38 passed。
  - `python -m pytest tests/test_cli.py -q`：56 passed。
  - `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1`：通过，已重建 `dist\DevPacer\DevPacer.exe`。
  - `Start-Process .\dist\DevPacer\DevPacer.exe`：通过；5 秒后进程仍存活，窗口标题 `DevPacer 工作台`，关闭后退出码 0。
  - `python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root .`：命令退出 0，但 verdict 为 COVERAGE GAP；命中两个 inspection-only workflow，覆盖缺口包含既有大批文档归档/脚本/资产改动和本次 V1 隔离项目产物。

### 结论

- V1 通过。真实 Codex worker 在真实 git 项目上完成了实现、测试、命令验收和 merge。
- 仍需继续 V2-V5：吃狗粮、队列托管、中断恢复、空白项目冷启动。

## 2026-07-04 成熟度计划 Phase 1 / V2：狗粮开发 DevPacer 功能

### 目标

- 用 DevPacer 给 DevPacer 自己开发 `chief-missions list --limit`，验证真实 Codex worker 是否能完成“计划 -> 开发 -> pytest 验收 -> merge”。

### 本轮已完成

- 创建隔离狗粮仓库：`D:\助手codex\.agent-workspace\dogfood_devpacer_repo`。
- 真实 Codex worker 在 worktree 中完成：
  - `src/visual_agent/cli.py`：`chief-missions` 增加 `--limit`。
  - `src/visual_agent/cli_chief.py`：list 输出在 JSON/Markdown 渲染前应用 limit。
  - `tests/test_missions.py`：新增 JSON 和 Markdown limit 测试。
- 验证通过后合并到狗粮仓库 `master`，merge commit `41b7c3687c10d44d0b9c8589943a9f71310f6d0f`。
- 将狗粮仓库已验证功能同步回主仓库。

### 暴露并修复的问题

- 产品自身写入 `.gitignore` / `强制测试记录.md` 会污染后续真实 mission 的 dirty gate。
- worker 未执行时，空 workflow + 命令门禁 pass 曾可能导致 mission 假阳性 verified。
- Windows 下把 repo map 直接塞入 `codex exec` argv 会触发命令行长度上限。
- git diff/status 的中文文件名在默认 quotePath 下会变成八进制转义，过滤逻辑失效。
- 计划阶段 changed files 会被 Checkpoint 运行时产物污染。

### 本轮验证记录

- 狗粮 V2 最终命令：
  - `checkpoint mission start --goal "...chief-missions list 命令添加 --limit..." --agent codex --test-command "python -m pytest tests/test_missions.py tests/test_cli.py tests/test_chief_run.py -q" --execute --merge --allow-coverage-gap --workspace-root D:\助手codex\.agent-workspace\dogfood_devpacer_repo\.agent-workspace --repo-root D:\助手codex\.agent-workspace\dogfood_devpacer_repo --max-rounds 2 --max-worker-minutes 35 --timeout-seconds 2100 --format json`
  - 结果：`status: verified`，worker 耗时约 324 秒。
  - gate：`70 passed in 18.00s`。
- 主仓库回归：
  - `python -m pytest tests/test_missions.py tests/test_cli.py tests/test_chief_run.py tests/test_chief_dispatch.py tests/test_chief_engineer.py tests/test_git_diff.py -q`
  - 结果：115 passed。
- 人工 CLI：
  - `python -m visual_agent.cli chief-missions list --workspace-root .agent-workspace --limit 2 --format json`
  - `python -m visual_agent.cli chief-missions list --workspace-root .agent-workspace --limit 2 --format markdown`
  - 结果：均只输出 2 条 mission。
- Checkpoint：
  - `python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root . --format markdown`
  - 结果：命令退出 0，但 verdict 为 COVERAGE GAP；原因是当前工作树有大量既有归档、狗粮仓库和 worktree 产物，命中的 workflow 均为 inspection-only。

### 结论

- V2 通过。DevPacer 已完成一次真实自举狗粮开发。
- 下一步应推进 V3 队列托管，重点验证多个任务排队执行、状态流转和报告生成。

## 2026-07-04 成熟度计划 Phase 1 / V3：队列托管 3 个任务

### 目标

- 在隔离 Python 项目中提交 3 个独立小任务到 mission queue，用 `chief-worker --watch` 托管处理，验证队列执行、pytest gate、自动 merge 和终态记录。

### 本轮结果

- 首批 3 个 queue item 全部失败于 `permission_required`。
  - 原因：基线项目跑 pytest 后留下 `__pycache__`，项目未忽略 Python 缓存，clean repo gate 正确阻断。
  - 处理：提交 `.gitignore` 忽略 `.agent-workspace/`、`__pycache__/`、`*.pyc`、`强制测试记录.md`。
- 第二批 3 个 queue item 全部成功：
  - `mq-8fcb4437c4a5` / mission `20260703-213147-92567d`：添加 `square`，verified，merge `660cca07fff5a7a6b46cfe9108bf11bc78545685`。
  - `mq-3042f334ac8c` / mission `20260703-213148-f0bcb6`：添加 `cube`，verified，merge `5ebe3d62dddc08b26182077af4a54f4a2aac1f22`。
  - `mq-06a291b596ed` / mission `20260703-213148-0883c2`：添加 `title_words`，verified，merge `857dbc2312f938b58a47bd36381e99a28333cb1a`。

### 验证记录

- `checkpoint chief-worker --workspace-root .agent-workspace --watch --poll-seconds 2 --max-items 3 --max-seconds 5400 --format json`
  - 结果：`processed_items: 3`，3 个 `Mission verified`，耗时约 523 秒。
- `checkpoint chief-queue list --workspace-root .agent-workspace --format markdown`
  - 结果：第二批 3 个 `[success] ... -> verified / verified`，首批 3 个 `[failed] ... -> stopped / permission_required`。
- `python -m pytest -q`
  - 结果：5 passed。
- `$env:PYTHONPATH='src'; python -c "...square/cube/title_words assertions..."`
  - 结果：通过，输出 `25 27 Hello World`。
- `git status --short`
  - 结果：干净。

### 结论

- V3 CLI 队列托管核心链路通过。
- 看板视觉流转未在本轮截图/OCR验证，后续做 GUI/看板专项或 Phase 3 稳定性时补。

## 2026-07-04 成熟度计划 Phase 1 / V4：中断恢复

### 目标

- 启动一个后台 mission，中途强杀进程，验证主分支干净、状态明确、`mission resume` 能续跑或干净重跑。

### 本轮发现与修复

- `mission start --execute --background` 的 mission facade 没有把 `--test-command` / `--merge` / `--allow-test-edits` 透传给 hidden background worker。
  - 已修复 `chief_background.py`、`cli.py`、`cli_chief.py`，并补测试。
- 强杀后遗留 worktree，`mission resume` 被 `Worktree path already exists` 阻断。
  - 已修复 `create_worktree`：干净既有 git worktree 可复用，有未提交改动则继续阻断。
- Codex resume 时触发用量限制。
  - 切换到 `claude-code`，实际路由 MiMo backend，继续完成任务。

### 验证记录

- 后台中断 mission：`20260703-214619-ac1518`
  - 强杀后：`mission status` 为 `stopped / worker_error`，background 为 `orphaned`。
  - 主分支：`git status --short` 干净。
- 最终恢复命令：
  - `checkpoint mission resume --mission 20260703-214619-ac1518 --agent claude-code --test-command "python -m pytest -q" --execute --merge --allow-coverage-gap --allow-test-edits ...`
  - 结果：`status: verified`，merge `3f053d80e716d94bcd33e39b9a28fdda15db2abd`。
- `python -m pytest -q`
  - 结果：6 passed。
- `$env:PYTHONPATH='src'; python -c "...reverse_words assertion..."`
  - 结果：通过，输出 `three two one`。

### 结论

- V4 CLI 核心链路通过。
- 看板状态展示仍需 GUI 复核；当前只验证了 `mission status` 不是僵尸态。
