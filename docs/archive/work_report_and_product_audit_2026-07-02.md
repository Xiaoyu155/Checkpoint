# DevPacer / Checkpoint 工作报告与产品审计报告

日期：2026-07-02
状态：阶段性完成，等待外部审核

本报告面向产品负责人和后续审核者。它不把当前工作包装成“已经可商业发布”，而是说明：做了什么、证据是什么、产品现在用起来到底有没有价值、还差什么。

## 1. 结论

这个产品原来的“测试验收工具”方向不应该丢，但必须降级为底层引擎。新的产品应叫 **DevPacer**：一个能把模糊开发目标变成 mission，合理选择模型/权限/预算，派发 Codex 工作，自动多轮验收和修复，并在用户离开电脑后留下可审计结论的本地开发运营器。

`Checkpoint` 保留为底层验收引擎；CLI 暂时继续使用 `checkpoint`，避免重命名造成额外破坏。

当前阶段最有价值的进展不是“接了更多 agent”，而是建立了一个可审计闭环：

1. 模糊需求先被拦截，不浪费一次 coding agent run。
2. Agent 能力被数据化，用户不需要自己记 Codex / Claude Code / Gemini 的模型、权限、并行能力。
3. Codex 是默认唯一 coding worker，Gemini 是多模态 inspection lane，不制造多 agent 合并混乱。
4. `chief-run` 把计划、派发、验证、修复、预算、停机理由写成 mission。
5. `chief-run --background` 和 `chief-worker --watch` 让用户不必守着电脑。
6. 所有结果落盘，有报告、有 rounds、有 stop_reason，可审计。

产品方向是成立的，但当前仍是开发者可用的 headless 产品原型，不是普通用户可直接使用的完整产品。

## 2. 已完成工作

### P0：验收基线修复

已修复最关键的信任问题：验收工具自己不能验收通过。

- 修复 `checkout_verification` 的相对路径错误。
- 将需要真实桌面 / 本地服务的 OCR、Alipay workflow 标记为 `slow`，移出默认快门。
- 给 checkout workflow 补精确 `affects`。
- `codex-check` 增加 `--strict`，覆盖缺口可在 CI 中变成非零退出。
- 修复 dry-run 假失败：dry-run 跳过交互后，后续状态断言不再被误判为产品失败，而是 `inspection_only`。

### P1：需求访谈与任务包

已实现需求清晰度门，解决“输入任务变聊天、烧 token”的根问题。

- `chief-plan --interview` 输出澄清问题。
- `--answer` 可把澄清结果写入 acceptance criteria。
- 空洞目标进入 `needs_clarification`，不生成 handoff，不派发 worker。
- `chief-plan --save` 与 `chief-plans list/show` 支持计划落盘和恢复。
- 根据目标关键词生成领域化验收标准，例如 checkout、login、form、dashboard、amount。

### P2：Agent 能力档案

已把 agent 能力知识从代码中抽出来，变成版本化 YAML。

- `agent_profiles/codex.yaml`
- `agent_profiles/claude-code.yaml`
- `agent_profiles/gemini.yaml`
- `checkpoint agents doctor`
- `checkpoint agents show/list`

Codex / Claude Code / Gemini 的模型、沙箱、权限、headless 命令、并行方式、MCP/hooks、常被忽略的能力，均进入数据档案。后续 agent 升级时优先更新 YAML，不改核心逻辑。

### Gemini 定位

Gemini 不作为默认 coding worker。

它的定位是 **multimodal inspection lane**：读截图、失败证据、视觉状态、验收标准，判断用户实际看到的界面是否合理。这个角色有价值，因为 DOM/OCR/断言无法完全替代世界模型的视觉理解。

原则：Gemini 增强验收，不制造第二份竞争代码 diff。

### P3/P4：派发与验收反馈闭环

已实现第一版 Codex-only dispatch。

- `chief-dispatch --dry-run` 预览 worktree、branch、worker 命令、verification 命令。
- 真实执行必须显式 `--execute`。
- 默认拒绝脏树、弱覆盖、inspection lane、非 Codex coding adapter。
- Codex worker 在隔离 worktree 中运行。
- worker 退出后自动跑 `codex-check`。
- 验证失败会生成 repair brief。
- 可选 `--auto-repair-once` 进行一次修复重派。
- worker 与 verification 记录落盘。

### Mission 层

已实现 `chief-run`，把 P1-P4 串成一个可恢复的开发 mission。

mission 文件：

- `mission.json`
- `budget.json`
- `rounds.jsonl`
- `final_report.md`
- `background.json`

能力：

- `chief-run --goal ...` 默认 preview。
- `chief-run --resume <mission_id> --execute` 可恢复执行。
- `chief-run --background` 后台执行。
- `chief-status --mission <id>` 查看状态并做后台健康检查。
- `chief-missions list/show` 查看历史 mission。
- `chief-run-demo` 提供无 token 的确定性验收演示：先失败，再修复，再通过。

### Mission Queue / Watch

已实现本次最新步骤：mission queue / worker。

- `chief-queue submit --mission <id>`
- `chief-queue list`
- `chief-queue show --queue-id <id>`
- `chief-worker --run-once`
- `chief-worker --watch --poll-seconds N`

队列只排已有 mission，不复制计划和预算。worker 原子 claim 一个 pending mission 后，执行同一条 `chief-run --resume <mission_id> --execute` 路径。

队列只在 mission 返回 `verified` 时标记 `success`；其他 stop reason 都标记为 `failed` 并保留结果，方便用户回来看报告。

## 3. 验证证据

当前 `.visual-agent-status.md`：

- `Status: PASSING`
- passing workflow：`seeded_check`
- failing：无

最终验证结果：

- `tests/test_chief_queue.py`：7 passed
- chief queue/run/dispatch/demo/missions/background/engineer 相关测试：37 passed
- 非 e2e 全量：1209 passed, 1 warning
- e2e：52 passed
- `codex-check`：退出 0，无 workflow failure

`codex-check` 当前仍报告 `COVERAGE GAP`。这是预期结果，因为主工作树存在大量未提交的源码/文档改动，且并非每个改动都有精确 workflow coverage。它不是 workflow failure，但说明当前工作树不能被宣称为“覆盖完整”。

## 4. 第一性原理产品审计

### 用户真正买的不是测试，也不是多 agent

用户真正要买的是：

- 少守电脑。
- 少浪费 token。
- 少把需求聊散。
- 少在模型/权限/命令之间试错。
- 少出现“agent 说完成了，但产品其实坏了”。
- 能回来看到结论，而不是看到一堆终端日志。

因此产品的核心价值函数不是“跑了多少 agent”，而是：

```text
节省时间 = 减少的澄清成本
        + 减少的模型/权限选择成本
        + 减少的盯屏等待成本
        + 减少的失败定位成本
        - 产品本身的配置和理解成本
```

如果 DevPacer 不能让这个公式为正，它就只是另一个复杂工具。

### 当前产品用起来的真实效果

有效的地方：

- 对模糊目标会拦截，不会盲目派发，确实能省 token。
- 计划、验收标准、worker 命令、模型/权限建议已经结构化。
- Codex dispatch 有 dry-run 预览，不会突然执行。
- mission 记录让一次开发任务有了可恢复的生命周期。
- background 和 queue 让用户可以离开电脑。
- Checkpoint 的验收证据比普通 “tests passed” 更接近产品行为。
- deterministic demo 可以无 token 证明主循环，不依赖真实模型发挥。

不够好的地方：

- 当前仍偏工程师工具，普通用户不知道该从哪个命令开始。
- CLI 名字仍是 `checkpoint`，产品叫 `DevPacer`，品牌层级还没完全落地。
- 主工作树很脏，虽然测试通过，但审计成本高。
- `codex-check` 对当前大 diff 报 coverage gap，说明覆盖策略还需要继续建设。
- 真实 Codex token worker 尚未在干净 demo repo 中做最终验收。
- queue 只有 submit/list/show/run/watch，没有 cancel/retry/requeue。
- 没有 UI/dashboard，mission 状态查看仍靠命令。
- budget 目前是轮次/时间级别，还没有真实 token/cost 统计。

审计结论：方向正确，底层闭环已经形成；但产品仍处于“可信 headless prototype”，不是“普通用户可直接上手的 autonomous dev product”。

## 5. 产品命名审计

建议采用：

- 产品名：`DevPacer`
- 验收引擎：`Checkpoint`
- 当前 CLI：继续 `checkpoint`
- 未来 CLI alias：可增加 `devpacer`
- 核心任务单位：`mission`

理由：

- `Checkpoint` 更像验收点、检查点，不足以表达自动规划、派发、预算、后台执行。
- `DevPacer` 表达的是节奏控制和开发推进，符合“降低开发时间、纠正方向、合理用模型”的目标。
- 不立即改 CLI 是务实选择，避免破坏已有脚本、测试和文档。

## 6. 核心产品原则

后续开发必须守住这些原则：

1. More agents are not the product. Verified delivery is the product.
2. 不清楚就不派发，拒绝派发是省钱功能。
3. 默认一个 Codex coding worker，Gemini 只做视觉/世界模型 inspection。
4. 所有真实执行都必须可预览、可恢复、可审计。
5. queue 和 background 不能绕过 mission 执行路径。
6. dry-run 不能伪装成真实验收。
7. 没有 final report 的后台任务不能算完成。
8. 不在覆盖不足时盲目宣布成功。

## 7. 当前主要风险

### 风险 1：真实模型执行还没做最终干净验收

deterministic demo 能证明框架，但不能证明 Codex 在真实 token 场景下表现稳定。

建议：下一步在干净 demo repo 中跑一次真实 `codex exec`，不碰主仓库。

### 风险 2：覆盖缺口会削弱验收可信度

当前主工作树 `codex-check` 是 `COVERAGE GAP`。这不是失败，但会影响“我已经验收完整”的可信度。

建议：为新的 mission/queue/chief-run 路径增加一个专门 workflow，覆盖 CLI 层关键行为。

### 风险 3：队列是单机 JSON 文件锁

这符合当前产品阶段，但不能包装成分布式调度器。

建议：先用本地 JSON queue 跑通真实使用，再考虑 SQLite 或服务化。

### 风险 4：用户入口仍然复杂

目前用户要理解 chief-plan、chief-run、chief-queue、chief-worker、chief-status。产品价值已经有了，但入口还不够短。

建议：增加一个高层命令或向导，例如：

```powershell
checkpoint mission start --goal "..."
checkpoint mission queue --last
checkpoint mission status
```

这可以等底层稳定后再做。

### 风险 5：Agent 能力档案会过期

Codex / Claude Code / Gemini 变化快。

建议：把 `agents doctor` 纳入维护流程；每次工具升级，只更新 YAML + 跑能力档案测试。

## 8. 下一步建议

优先级从高到低：

1. **真实 Codex worker 干净 demo 验收**
   - 新建隔离 repo。
   - 创建一个小缺陷。
   - 让真实 Codex 执行。
   - 用 Checkpoint supervised 验收。
   - 记录 token/时间/结果。

2. **补 mission/queue 的 workflow coverage**
   - 让 `codex-check` 对这些核心路径不再只能报 uncovered。
   - 至少覆盖 queue submit/list/run-once 的 CLI happy path。

3. **queue 增加 cancel/retry**
   - 先做简单 CLI。
   - 不做复杂调度、不做分布式。

4. **产品入口收敛**
   - 保留底层命令。
   - 新增更短的 mission 级命令。
   - 文档从“命令字典”改成“用户工作流”。

5. **真实预算统计**
   - 先记录 wall time、round count、worker timeout。
   - 后续接 token/cost 统计。

6. **Gemini inspection 接入**
   - 不让 Gemini 写代码。
   - 让它读取截图、failure evidence、final report，输出视觉/语义风险。

7. **最后再做多 worker 比较**
   - 前提：单 worker mission 足够稳定。
   - 输出应是推荐决策，不是两个终端结果。

## 9. 对 fable5 审核的建议问题

建议重点审核：

- mission queue 是否真的只调用同一条 `chief-run --resume --execute`，有没有第二套执行逻辑。
- dry-run / supervised 语义是否清晰，是否会把 inspection-only 误报成 pass。
- queue claim 是否能防止双领任务。
- mission stop_reason 是否足够解释用户下一步。
- Gemini inspection lane 是否破坏“默认单 coding worker”原则。
- 当前 docs 是否把 `DevPacer` / `Checkpoint` 的品牌层级讲清楚。
- 测试是否覆盖了 CLI 接线，而不只是模块函数。

## 10. 最终判断

这个产品现在已经从“验收工具”转成了“带验收闭环的开发任务运营器”。这一步是必要的，也符合用户的真实痛点：不是不会写代码，而是不知道如何把需求、模型、权限、预算、验收和多轮修复组织成一个可靠流程。

当前版本最值得保留的资产是：

- Checkpoint 验收引擎。
- chief-plan 的需求拦截。
- agent capability profiles。
- Codex-only dispatch。
- mission state。
- deterministic demo。
- background/status。
- mission queue。

当前最不该做的是过早上多 agent 面板。下一步应该继续把单 mission 闭环做硬，直到它在干净 repo + 真实 Codex worker + supervised Checkpoint 下稳定跑通。
