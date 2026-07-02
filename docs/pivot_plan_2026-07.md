# Checkpoint 转向计划：从"验收工具"到"AI 开发总工程师"

日期：2026-07-02
状态：待执行
前置阅读：`docs/chief_engineer_self_review.md`

---

## 一、产品现状审查

### 已有资产（不要丢掉）

Checkpoint 目前真正值钱的东西是验收引擎，不是编排：

- 工作流引擎：YAML 工作流、浏览器 DOM / UIA / OCR / 视觉多层执行、`verify-now`、`codex-check`。
- 证据体系：结构化失败（`structured_failure`）、运行报告、截图、`summarize-latest-failure`。
- Agent 接入面：MCP server、`coding-agent-brief`、`context-snapshot`、权限档位（dry-run / approved）。
- 总工雏形：`chief-plan`（`src/visual_agent/chief_engineer.py`），能把目标 + git 变更 + 工作流覆盖度合成任务包。

### 真实问题（自省文档 + 本次审查确认）

1. **验收基线是红的。** `codex-check` 当前失败（checkout fixture 路径缺失、OCR 缺 engine 输入、覆盖度 fallback_only）。一个验收工具自己的验收都不过，产品没有可信度，这是搁置的直接原因。
2. **chief-plan 只是文本。** 不创建 worktree、不启动 agent、不收集结果、计划不落盘、覆盖度不足时也退出码为 0。
3. **产品只覆盖了漏斗的最后一环。** 用户从"有个想法"到"验收通过"要经过：表达需求 → 配置 agent → 派发执行 → 验收。Checkpoint 只做了第 4 步，而用户在第 1、2 步就已经卡死了。

### 关键洞察（本次转向的依据）

产品负责人自己就是目标用户，而他遇到的问题不是"缺一个测试工具"：

- **不会表达需求**：输入任务变成聊天对话，token 在闲聊中烧掉，agent 拿不到可执行的任务。
- **不了解 agent 能力**：不知道 Codex 支持并行多任务开发，不知道该用什么模型、什么审批/沙箱模式、什么权限档位，更不知道 subagent、worktree、headless 模式这些高级用法的存在。
- **没有验收标准**：AI 说"做完了"就只能信，没有证据。

GitHub 上已有大量项目分别解决这些点（spec-kit 做规格化需求、BMAD-METHOD 做角色化流程、Vibe Kanban / claude-flow / agent-orchestrator 做多 agent 编排），但**没有一个把"需求表达 → 正确调度 → 证据验收"闭环做完**。验收恰好是 Checkpoint 已有的、别人没有的部分。

## 二、新定位

> 用户对 Checkpoint 说人话，Checkpoint 作为总工程师：把模糊意图变成工程任务包，用正确的模型、权限、并行方式调度 Codex / Claude Code 干活，最后用产品证据验收，不让任何 agent 无证据地宣称完成。

一句话：**用户不需要学会 Codex 和 Claude Code，Checkpoint 替他学。**

三条铁律（沿用自省文档并加一条）：

1. More agents are not the product. **Verified delivery is the product.**
2. 需求不清晰就不派发 —— 拒绝派发本身就是产品价值（省 token）。
3. Agent 能力知识必须是**数据文件**而非硬编码 —— Codex / Claude Code 每次升级，Checkpoint 更新一份 YAML 就能跟上，这是"粘着它们发展"的技术手段。

## 三、开发计划

### Phase 0：修复验收基线（先做，约 1 周）— ✅ 已完成 2026-07-02

这是信用问题，不修完不许开新功能。

- [x] 修复 `checkout_verification` 的失效 fixture 路径。根因不是文件缺失，而是 URL 前缀错误：`normalize_url` 以 run 目录（`.agent-workspace/runs/<ts>/`）为基准解析相对路径，`../../` 会落到不存在的 `.agent-workspace/examples/web/`。改为 `../../../examples/web/checkout_verification_demo.html`，指向 repo 根 `examples/web/` 的规范文件（也是公开 demo 脚本注入回归的同一文件）。
- [x] `desktop_ocr_real_acceptance` 与 `alipay_miniprogram_browser_acceptance` 标记 `slow`。两者都是 real-io（需要活桌面 / 本地服务器），移出默认快门。
- [x] 给 `checkout_verification` 补精确 `affects: examples/web/checkout_verification_demo.html`，消灭 `fallback_only`：改动 demo 页时精确命中，改动无关源码时诚实报 `uncovered`。
- [x] `codex-check` 加 `--strict`：覆盖度为 `coverage_gap`（uncovered / fallback-only）时退出非零；默认行为不变（向后兼容）。
- [x] 修复 dry-run 假失败：dry-run 会跳过点击，导致其后"断言下单成功"硬失败。新语义——**跳过交互之后的失败判为 inconclusive（inspection-only）而非 failed**，跳过交互之前的静态失败仍判 failed（真回归不被掩盖）。加了两个回归测试守护。
- [x] `.visual-agent-status.md` 回到 PASSING（checkout_verification）。

**完成标准（已验证）**：
- `verify-now --workflow checkout_verification --live` → PASS（L4）。
- `codex-check --run-profile supervised` → checkout 真交互 PASS；默认 dry-run → INSPECT（诚实提示改用 supervised），不再假 FAIL。
- 公开 demo 环（绿 → 把 `Proceed to Checkout` 改成 `Next Step` → 在 `assert_checkout_button` 红 → 复原绿）复现通过。
- 38 个相关测试通过。

**遗留给后续**：交互式产品验收必须用 `--run-profile supervised` 或 `verify-now --live`（dry-run 天然无法验证交互后状态，只做 inspection）。这一点已在下方铁律外补充，Phase 1 的访谈/派发产物应默认对交互式验收选 supervised。

### Phase 1：需求访谈与任务包落盘（约 2 周）— ✅ 已完成 2026-07-02

解决"输入任务变成聊天"的问题。

- [x] `chief-plan --interview` + `--answer`：`--interview` 把澄清问题显式列出来帮用户收敛目标；`--answer`（可重复）把回答折进 acceptance_criteria 并满足清晰度门。实现为"输出待澄清清单供聊天一次性回答"，不做阻塞式交互。
- [x] 需求清晰度门（`assess_goal_clarity`）：目标缺少可验证完成标准时状态 `needs_clarification`，**不生成 handoff、不生成 worker_tracks、退出码非零**，并列出缺什么。保守判定——只拦真正空洞的目标（"改一下""fix it"），具体目标照常放行。
- [x] 计划落盘（`chief_plans_store.py`）：`.agent-workspace/chief_plans/<plan_id>/plan.json`，`chief-plan --save`、`chief-plans list`、`chief-plans show <id>`，plan_id = 时间戳+目标哈希，可跨终端/会话恢复。
- [x] 领域化验收标准（`DOMAIN_CRITERIA`）：按目标关键词（结账/登录/仪表盘/表单/金额…，含中英）追加具体标准，例如"修结账总价"会生成关于订单总额、货币、确认态的标准，替换泛化四条。

**完成标准（已验证）**：模糊需求 `改一下` → needs_clarification + 三个澄清问题 + exit 1；补 `--answer` 后放行；`--save` 落盘后 `chief-plans list/show` 可恢复。

### Phase 2：Agent 能力档案（约 2 周，本次转向的核心新增）— ✅ 已完成 2026-07-02

解决"不知道 Codex 有并行开发、不知道用什么模型和权限"的问题。

- [x] 版本化能力档案 `src/visual_agent/agent_profiles/{codex,claude-code}.yaml`，每个 agent 维护：模型及适用场景（role: default/fast）、沙箱模式（Codex `--sandbox read-only/workspace-write/danger-full-access`，Claude `--permission-mode plan/default/acceptEdits` + `--dangerously-skip-permissions`）、审批模式（Codex `--ask-for-approval` / `--full-auto`）、并行能力（Codex 本地 worktree + Cloud 并行；Claude subagents + 后台任务 + worktree）、上下文机制（AGENTS.md/CLAUDE.md、MCP、hooks、resume）、headless 命令（`codex exec` / `claude -p`）、`verified_on` 日期与 source 链接、`capabilities_often_missed` 清单。已加 `[tool.setuptools.package-data]` 确保打包。
- [x] `checkpoint agents doctor [agent]`：`shutil.which` 探测本机安装 + 版本（Windows `.cmd` shim 走 `cmd /c`），对照档案输出"你没用上的能力"清单。本机实测：codex-cli 0.142.4 + Claude Code 2.1.187 均检测到。另有 `agents show <agent>` / `agents list`。
- [x] `chief-plan` 消费能力档案（`agent_capabilities.recommend_worker_config`）：worker_tracks 输出带模型选择、沙箱/审批参数、并行建议的**可直接复制执行完整命令**——例如 `codex exec --model gpt-5-codex --sandbox workspace-write --ask-for-approval on-request "..."`；Claude 的沙箱+审批合并到单个 `--permission-mode`（去重，不重复）。
- [x] 更新机制：知识全在 YAML，新版本只改数据 + 跑对照测试；`agents doctor` 的 `verified_on` 是校准锚点。

**完成标准（已验证）**：同一任务 `chief-plan --agent codex --agent claude-code` 输出两条 worker track，各带具体模型+沙箱+并行建议，命令可追溯到档案条目；5 个 `agents` 相关测试通过。

### Gemini 的定位：世界模型 / 多模态检查线，不是默认 coding worker

用户提出的关键修正是对的：Checkpoint 禁止把"多 agent"本身做成卖点，但 Gemini 作为多模态世界模型有独特价值。结论如下：

- **不把 Gemini 放进默认编码派发。** 默认 `chief-plan` 现在只有一个 coding worker：Codex。Gemini 不参与生成竞争 diff，不制造并行合并复杂度。
- **把 Gemini 建模为 `multimodal_inspection`。** 新增 `src/visual_agent/agent_profiles/gemini.yaml`，角色是读取截图、视觉证据、失败报告、验收标准，指出视觉风险和证据缺口。
- **Gemini 的产品价值在验收侧。** DOM/UIA/OCR 是结构化证据优先；当这些证据解释不了"用户看到的东西是否合理"时，Gemini 作为视觉问答/世界模型补位。它适合回答："这个结账页是否像成功态？错误提示是否清楚？截图里的金额、按钮、状态是否符合验收语义？"
- **能力仍然数据化。** Gemini CLI / Gemini API 的模型和参数会变，所以它也走 YAML 能力档案 + `agents doctor/show`，而不是散落在代码里。
- **执行安全门。** `chief-dispatch --execute` 只支持 Codex adapter；选择 Gemini track 会被拒绝，原因是 inspection lane 不是 coding worker。

这使 Gemini 不破坏"More agents are not the product"这条铁律，同时补上 Checkpoint 在视觉世界理解上的短板。

### Phase 3：Dry-run 派发 + 第一个真实 worker（约 3 周）— 🚧 第一版已落地 2026-07-02

对应自省文档 Phase 2 + 3，顺序不变。

- [x] `checkpoint chief-dispatch --plan <id> --dry-run`：生成 worktree 名、branch、worker 完整命令、验证命令、记录路径，只预览不执行。覆盖度弱时默认拒绝（可用 `--allow-coverage-gap` 做显式实验）。
- [x] worktree 生命周期第一版：分支命名 `checkpoint/<plan_id>/<track_id>`；worktree 默认放在 repo 同级 `<repo>.checkpoint-worktrees/<plan_id>/<track_id>`，避免嵌套进主仓库；执行前做 git repo 检查和脏树检测，默认脏树拒绝。
- [x] 第一个 worker adapter：**只做 Codex**。执行路径探测 `codex` 可执行文件，创建 worktree，运行 `codex exec`，捕获 stdout/stderr 到 plan 日志，支持 timeout，退出码写入 worker record。Claude Code / Gemini 不进入可执行 coding adapter。
- [x] worker 运行记录写入 `chief_plans/<plan_id>/workers.jsonl`。
- [x] CLI 安全门：默认就是 dry-run；真实执行必须显式 `--execute`。`needs_clarification`、`needs_workflow_coverage`（除非显式允许）、inspection lane、非 Codex coding adapter 都会拒绝。

**完成标准**：用户能先看到"将要发生什么"再批准；批准后 Codex 在隔离 worktree 里真实跑完一个任务并留下可审计记录。
**当前验证**：dry-run / coverage gate / worker record / mock Codex execution 的自动化测试已通过；当前主仓库是脏树且存在预存红测试，未现场启动真实 Codex 消耗 token。下一步应在干净 demo repo 上跑一次 `--execute` 完整链路。

### Phase 4：验收反馈闭环（约 2 周）— 🚧 第一版已落地 2026-07-02

对应自省文档 Phase 4。这是把老方向（测试验收）真正做好的地方 —— 验收不再是孤立工具，而是派发循环的守门员。

- [x] worker 退出后自动跑 `codex-check`，解析 `pass / inspection_only / coverage_gap / fail`，并把 markdown 证据一起保存。
- [x] 失败时复用 `diagnose-latest-failure` / `build_failure_evidence_pack` 生成 repair brief，保存 `run_id`、workflow、failed_step、repair_prompt。
- [x] 可选自动重派一次（`--auto-repair-once`）：同一个 Codex worktree 收到 repair prompt，之后再次跑 `codex-check`。默认关闭，只适合 supervised 实验。
- [x] 验收结果写入 `chief_plans/<plan_id>/verification.json`；worker 过程写入 `workers.jsonl`，日志写入 `chief_plans/<plan_id>/logs/`。

**完成标准**：种一个已知缺陷 → 派发 → 验收失败 → 自动生成修复简报 → 重派 → 验收通过，全程一条命令加一次人工批准。
**当前验证**：mock Codex 下已覆盖失败生成 repair brief、verification.json 持久化、auto-repair 接口路径。真实 demo 还需要干净仓库 + 明确缺陷样例来跑通。

### Phase 4.5：DevPacer mission / chief-run（Phase 5 前先做）— 🚧 第一版已落地 2026-07-02

用户的新要求是产品不能只会生成计划，也不能让用户守着电脑看终端。下一步不是多 agent 比较，而是把 P1-P4 串成一个有预算、有轮次、有停机理由的 mission。

- [x] 产品命名分层：公开产品名 `DevPacer`；`Checkpoint` 保留为本地验收/自动化引擎；CLI 暂时继续叫 `checkpoint`，未来再加 `devpacer` alias。
- [x] mission 状态落盘：`.agent-workspace/missions/<mission_id>/mission.json`、`budget.json`、`rounds.jsonl`、`final_report.md`。
- [x] `checkpoint chief-run`：构建/读取 plan → 创建 mission → dispatch preview → 默认停止在 preview；真实执行必须显式 `--execute`。
- [x] 第一版预算策略：`--max-rounds`、`--max-wall-minutes`、`--max-worker-minutes`；默认 2 轮（初始实现 + 一次自动修复）。
- [x] 停机理由写入 mission：`verified`、`needs_clarification`、`coverage_gap`、`budget_exhausted`、`same_failure_repeated`、`permission_required`、`worker_error`、`inspection_only`。
- [x] `checkpoint chief-missions list/show`：用户可以离开终端后回来查看 mission 状态。
- [x] worktree 验证路径修正：执行时把 workflows/inputs/fixtures 准备到 worktree 的 `.agent-workspace`，但 verification/worker 记录仍写回原 workspace，避免本地 HTML workflow 验到原仓库而不是 worker worktree。
- [x] `checkpoint chief-run-demo`：隔离 demo git repo 中提交绿色基线，再提交 `Next Step` 缺陷；确定性本地 worker 第一轮不修导致 supervised Checkpoint 在 `assert_checkout_button` 红，repair 轮把按钮改回 `Proceed to Checkout`，第二轮 PASS。这个 demo 不调用真实 Codex 模型，适合作为审核复现路径。
- [x] mission resume：`checkpoint chief-run --resume <mission_id> --execute` 继续已有 preview mission，不重建 plan，不覆盖旧 rounds，后续 round 编号递增。
- [x] background/status 第一版：`checkpoint chief-run --background ...` 先创建/确认 preview mission，再用后台进程运行同一条 `chief-run --resume <id> --execute` 路径；`checkpoint chief-status --mission <id>` 可查看 mission、rounds、final report、pid、stdout/stderr 日志。
- [x] background health/reclaim：后台 worker 完成时写 `exit_code`、`completed_at`、result status 和 stop reason；`chief-status` 会探测进程，仍存活则报告 running，超过 `max_wall_minutes` 则终止并标记 `budget_exhausted`，进程消失但没有 completion receipt 则标记 `worker_error` 并刷新 `final_report.md`。
- [x] mission queue / worker 第一版：`checkpoint chief-queue submit --mission <id>` 把已 preview 的 mission 写入 `.agent-workspace/mission_queue/queue.json`；`chief-worker --run-once` 原子 claim 一个 pending mission 并执行同一条 `chief-run --resume <id> --execute` 路径；`--watch` 可作为简单本地守护进程。队列默认只接受 `created` / `preview` mission，已完成/阻塞 mission 需显式 `--force`。

**当前验证**：新增 7 个 mission queue 测试覆盖 CLI submit/list、duplicate guard、atomic claim、run-once success/failure；37 个 chief queue/run/dispatch/demo/missions/background/engineer 相关测试通过；`chief-run-demo` 真实跑通（fail → repair → pass）；真实主仓库 dry-run 因当前脏树覆盖不足停在 `coverage_gap`，显式 `--allow-coverage-gap` 可进入 preview 并落盘。这是正确安全姿态，不应在覆盖不足时盲派发。

**仍未完成**：真实 Codex token worker 的干净 repo 执行（当前 demo 为 deterministic no-token worker，但走真实 dispatch/worktree/Checkpoint 验证路径）；队列目前是单机 JSON 文件锁，不是分布式调度器。

### Phase 5：并行比较与合并评审（约 3 周，Phase 3/4 稳定后再做）

对应自省文档 Phase 5。这是用户想要的"多线程一起开发"的产品化形态。

- [ ] 多 worker 并行（多 worktree），比较各自 diff 与验收证据。
- [ ] 风险标注（谁改动更大、谁碰了不该碰的文件）、推荐一个候选。
- [ ] 永不无策略自动合并。

**完成标准**：同一任务派给 2 个并行 worker，产出一份"推荐 A，因为验收证据 X、改动范围 Y"的可评审决策，而不是两堆终端输出。

## 四、不做什么

- 不做"又一个多 agent 面板"，不比 agent 数量。
- 不在 adapter 稳定前接第二个 agent。
- 不做自动合并。
- Phase 0 完成前不写任何新功能代码。

## 五、度量

每个阶段结束记录三个数：

1. **派发前拒绝率**：多少模糊需求被 `needs_clarification` 拦下（拦下 = 省下的 token）。
2. **一次通过率**：worker 首次提交通过验收的比例。
3. **基线健康度**：`codex-check` 默认门连续保持绿色的天数。

## 六、风险

| 风险 | 应对 |
|---|---|
| Codex / Claude Code CLI 参数频繁变化，命令模板失效 | 能力档案数据化 + `agents doctor` 对照检测，把破坏面收敛到 YAML |
| 访谈模式做成新的"聊天"，反而更啰嗦 | 追问上限（如 3 个问题），超过即输出待澄清清单一次性回答 |
| PTY / 进程管理在 Windows 上的坑 | Phase 3 只支持 Codex + Windows 本机，先窄后宽 |
| 又一次范围失控 | 每个 Phase 有单一"完成标准"，未达标不开下一阶段 |
