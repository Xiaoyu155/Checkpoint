# DevPacer / Checkpoint 技术报告与阶段开发报告

日期：2026-07-02
撰写：Claude (fable5)，本轮由我亲自 code（Codex 已封号，外部不可用）
用途：供产品负责人整体审核
性质：不做营销包装。所有数字和状态均为本轮实跑复现，不凭记忆。

---

## 一、一句话现状

产品已经从"测试验收工具"演进成"**带验收闭环的本地开发任务运营器（DevPacer）**"：用户给目标 → 需求门拦截模糊需求 → 生成带模型/权限/并行建议的任务包 → 隔离 worktree 里派 Codex 干活 → 用 Checkpoint 做产品级验收 → 失败自动生成修复简报重派 → 全程落盘可审计。**底层闭环已成立并有测试覆盖，但仍是"可信的 headless 原型"，不是普通用户能直接上手的成品。**

分层命名：**DevPacer**（mission/编排层）+ **Checkpoint**（验收引擎）。CLI 暂仍叫 `checkpoint`，未改名。

## 二、代码规模与架构地图

- Python 模块：**122 个**（`src/visual_agent/`），测试文件：**86 个**。
- CLI 命令面：约 150 个子命令（`chief-*`、`mission`、`agents`、`verify-*`、`codex-check`、`workspace-*` 等）。

分层（自底向上）：

| 层 | 关键模块 | 职责 |
|---|---|---|
| 验收引擎（最强资产） | `workflow.py` `dispatcher.py` `dom.py` `uia.py` `ocr.py` `vlm.py` `acceptance.py` `structured_failure.py` | YAML 工作流、DOM/UIA/OCR/视觉多层执行、L1-L4 验收分级、结构化失败证据 |
| 验收入口 | `codex_check.py` `verify.py` `preflight.py` `capabilities.py` | git-diff 感知的快检、覆盖度分析、能力/环境预检 |
| Agent 能力层 | `agent_capabilities.py` `agent_profiles/*.yaml` | 版本化的 Codex/Claude Code/Gemini 能力档案；`agents doctor` 探测本机 |
| 总工规划层 | `chief_engineer.py` `chief_plans_store.py` | 需求清晰度门、领域化验收标准、任务包生成与落盘 |
| 派发层 | `chief_dispatch.py` | worktree 隔离、Codex-only worker、验收反馈、自动修复一轮 |
| Mission 层 | `chief_run.py` `missions.py` `chief_background.py` `chief_run_demo.py` | 预算、轮次、停机理由、后台执行、无 token 确定性 demo |
| 队列层 | `chief_queue.py` | 单机 JSON 文件锁队列、原子 claim、watch 守护 |
| 记忆层 | `project_memory.py` | 从落盘 mission/plan/rounds 聚合项目记忆 |
| 计划导入 | `mission_plan_import.py` | 从 Markdown 开发计划生成 mission drafts |
| 高层门面 | `mission` 命令 | `mission start/status/list/queue` 收敛入口 |

铁律（已固化在设计里）：① 验收交付才是产品，不比 agent 数量；② 需求不清不派发；③ 默认单 Codex coding worker，Gemini 只做多模态 inspection 不产裁决；④ 真实执行必可预览/恢复/审计；⑤ queue/background 不绕过 mission 执行路径；⑥ dry-run 不能伪装成真实验收；⑦ inspection-only 永不报 verified。

## 三、本阶段完成工作

### 已提交（git 历史 751456e → 59f9914）
- `751456e` 稳定验收基线（P0）
- `79c6309` DevPacer mission 编排（P3/P4：dispatch + mission）
- `405d7c0` 证据聚合的项目记忆（记忆 v1）
- `3cff5e8` / `2fe23ec` 产品方向文档 + AGENTS.md
- `966211c` 高层 mission 命令门面
- `59f9914` mission 计划导入

### 本轮我亲自完成（未提交，6 个文件）

**Item #1：Playwright 验收 blocker —— 已修复，基线转全绿。** 两层根因：
1. 代码 bug：`playwright_env.ensure_playwright_browsers_path` 盲信"配置了但无 chromium 的 `PLAYWRIGHT_BROWSERS_PATH`"，残缺 `.pw-browsers` 遮蔽系统 Chromium。改为自愈回退 + 新增 `tests/conftest.py` 隔离该环境变量防跨测试泄漏。
2. 环境 gap：`.venv` 的 Playwright 1.60 缺 `chromium_headless_shell-1223`（无头启动必需）；`ms-playwright\__dirlock` 陈旧锁导致 `playwright install` 静默 exit 0 无输出；npmmirror 缺该 cft 构建（404），官方 CDN（中国网络慢）最终装成。

**Item #5：model_policy 真正生效 —— 已实现。** 之前 `model_policy` 只是 budget.json 摆设。现在从 mission budget 穿到 `dispatch_chief_plan`→`build_worker_command`，加 `phase`(implementation/repair) + tier→模型映射（strong→gpt-5-codex，fast→gpt-5-codex-mini）。修复轮按 policy 的 repair tier 选模型；默认仍 strong（不制造意外降级），用户设 fast 即省额度。

## 四、验证证据（本轮实跑）

| 项 | 结果 | 备注 |
|---|---|---|
| 全量非 e2e 测试 | **1228 passed, 0 failed, exit 0**（4分40秒） | 修 Playwright 前是 16 failed，修后全绿 |
| e2e 测试 | **52 passed, exit 0**（3分钟） | fable5 审计时复现 |
| chief/mission/queue/agents 组 | **40 passed** | 含本轮 2 个 model_policy 新测试 |
| browser-marked 测试 | 19 skipped | opt-in，不在默认门（非失败） |
| 无 token 确定性 demo | ⚠️ 见风险 3 | 单测通过；live 现结 coverage_gap |

关键：本轮我坚持不用 `| tail` 掩盖退出码（此前 Codex 曾因此把失败误报为全绿）。所有 pass/exit 均为文件重定向捕获的真实退出码。

## 五、未完成 / 受阻 / 风险

**风险 1（外部阻塞）：真实 Codex token 验收做不了。** Codex 账号封号。deterministic demo 能证明框架，但"真实模型在真实 token 场景下稳不稳"目前无法验证。这是 M1 唯一无法自力完成的一环。

**风险 2（中国网络，产品级）：Playwright 浏览器下载在中国很慢/易卡。** 官方 CDN 慢（112MB 下了约 30 分钟），npmmirror 缺新版 cft 构建，且陈旧 `__dirlock` 会让安装静默失败。**这会真实地卡住每一个中国用户的首次安装。** 建议 bootstrap 显式处理：检测/清 `__dirlock`、给镜像回退、失败时明确提示，而不是静默 exit 0。

**风险 3（待排查）：live `chief-run-demo` 现结 `coverage_gap` 而非 `verified`。** fable5 审计时是 verified。我已用 `git stash` 撤掉本轮 model_policy 改动验证——**撤掉后仍 coverage_gap，所以不是本轮改动导致**，是环境或某个已提交 commit 引入。demo 单元测试仍过（核心逻辑没坏），是 live 真实覆盖检测的差异。这个 demo 是"无 token 证明主循环"的关键资产，需单独排查一次。

**风险 4（既有）：queue 是单机 JSON 文件锁**，只 submit/list/show/run/watch，无 cancel/retry（注：workspace-queue 另有 cancel/retry，但 mission queue 无）。符合当前阶段，不能包装成分布式调度。

**风险 5（既有）：入口仍偏工程师。** 有 `mission start` 门面了，但底下 chief-plan/dispatch/run/queue/worker/status 一大堆，普通用户不知从哪开始。

**风险 6（既有）：预算是轮次/时间级，无真实 token/费用统计。**

## 六、未提交改动清单（本轮，待你决定是否提交）

```
 M src/visual_agent/chief_dispatch.py    # item#5 model_policy 消费 + phase
 M src/visual_agent/chief_run.py         # item#5 model_policy 穿透
 M src/visual_agent/playwright_env.py    # item#1 配置路径自愈
 M tests/test_chief_dispatch.py          # item#5 2 个新测试
 M tests/test_playwright_env.py          # item#1 2 个自愈回归测试
?? tests/conftest.py                     # item#1 PLAYWRIGHT_BROWSERS_PATH 隔离
```
环境侧另装了 `chromium_headless_shell-1223`（不入库）。

## 七、下一步计划（优先级）

1. **排查 demo coverage_gap（风险 3）** —— 关系产品可信度，先做。
2. **项目记忆 v2（#3）**：repair brief 注入更细历史；跨 mission 失败签名比对，同一坑第二次直接升级给人；归纳有效修复模式与常改文件区。
3. **计划导入 v2（#4）**：任务依赖/优先级/批量预算预估；同计划 missions 建 parent import 汇总视图。
4. **额度台账（#6）**：per-mission/per-repo 的 wall time/rounds/worker minutes 累计报表；有 token 信息则一并记。
5. **进程中纠偏 v1（#9）**：worker 后验收前查 diff 范围，改了计划外目录/diff 过大就警告或停。
6. **bootstrap 加固中国网络浏览器安装（风险 2）**。
7. Gemini visual_review 接入（advisory evidence，默认关）。
8. 真实 Codex token 验收（等账号恢复）。
9. 多 worker 比较（维持最后）。

## 八、给审核者的重点问题

1. 本轮两个 item 的改动是否要现在提交？（6 文件，测试全绿）
2. demo coverage_gap（风险 3）是否要作为下一步第一优先排查？
3. 中国网络浏览器安装（风险 2）是不是该提到前面做——因为它会卡住你未来每一个用户的首次体验？
4. model_policy 默认 repair 用 strong（保守，不降级）是否合理，还是你希望默认就 repair 用 fast 省额度？
5. Codex 封号期间，要不要把 Claude Code 也做成可执行 coding adapter（目前只有 Codex 能 execute），以免单点依赖一个被封就全停？
