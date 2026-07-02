# DevPacer 最终愿景：差距分析与产品路线图

日期：2026-07-02
依据：产品负责人的最终需求陈述 + `docs/fable5_audit_2026-07-02.md` 审计结论
性质：这是产品的**北极星文档**，后续所有开发以此对齐

---

## 一、最终产品定义（负责人原话的工程化拆解）

> 用户在这个平台上给出开发计划（无论是 CLI 用户还是已打开 Code/Codex 的用户），工具作为用户与 Codex 之间的中介：驱动 Codex 开发、合理利用各种模型、合理规划额度、有记忆、熟悉单一工作的进程、做端到端测试验收、Codex 跑偏时及时纠正。最终目的：解放用户、省时间、省额度、高效开发、避免开发死循环和测试死循环。

拆解为 8 项产品能力，逐项对照现状：

| # | 能力 | 现状 | 差距 |
|---|---|---|---|
| 1 | 需求中介：接住用户的开发计划，转成可执行任务 | ✅ 已有：chief-plan 清晰度门、interview/answer、领域化验收标准、计划落盘；`checkpoint mission start/status/list/queue/worker/memory` 已提供高层入口 | 仍缺"从一份开发计划文档批量生成 missions"的能力 |
| 2 | 驱动 Codex 开发 | ✅ 已有：chief-dispatch/chief-run，worktree 隔离，Codex-only adapter，preview→execute 安全门 | **真实 token 场景未做最终验收**（deterministic demo 已证明框架） |
| 3 | 合理利用各种模型 | 🚧 部分：能力档案（codex/claude-code/gemini YAML）+ recommend_worker_config（strong/fast 按任务选型）；budget.json 已有 model_policy 槽位（implementation/repair/classification/visual_review） | model_policy 还没有被执行路径真正消费——现在派发始终用同一模型；"简单修复用 fast 模型"未落地 |
| 4 | 合理规划额度 | 🚧 部分：budget.json（max_rounds/wall_minutes/worker_minutes/repair_rounds），budget_exhausted 停机 | 无 token/费用统计；无"这个任务预计花多少、值不值"的预估；无跨 mission 的额度台账 |
| 5 | 有记忆、熟悉单一工作的进程 | 🚧 v1 已落地：`project_memory.py` 从 missions/plans/rounds/final_report 聚合项目记忆；`chief-memory` 可查询；`chief-plan` 会把相关记忆注入 worker handoff | 仍缺更深的跨 mission 失败签名升级、有效修复模式归纳、常改文件区和额度台账联动 |
| 6 | 端到端测试验收 | ✅ 最强资产：Checkpoint 引擎（DOM/UIA/OCR/视觉、L1-L4 验收分级、结构化失败证据） | 对任意用户项目的 workflow 自动生成/录制还需要打磨（现在 demo 项目最顺） |
| 7 | Codex 跑偏时及时纠正 | 🚧 部分：验收失败→repair brief→auto-repair-once；same_failure_repeated 停机 | 纠偏只有"验收失败"一种触发；缺进程中信号（改了不该改的文件、diff 规模异常、偏离验收标准）；修复只有一轮 |
| 8 | 避免开发/测试死循环 | ✅ 设计正确：same_failure_repeated + budget_exhausted + 需求门拦截 | 失败签名只在单 mission 内比对；跨 mission 的"这个坑上次就踩过"要靠 #5 的记忆 |

**结论：8 项能力中 3 项已成立、5 项部分成立。骨架是对的，愿景可达；记忆 v1 已补上，但还需要从"查历史"升级到"主动防止重复踩坑"。**

## 二、Gemini 世界模型评估（应负责人要求）

### 判断：想法正确，但角色必须是"证据补充"，不是"验收裁判"

**价值真实存在。** Checkpoint 现在的证据是结构化的（DOM 断言、文本契约、OCR），它能回答"页面上有没有 Order Confirmed 这几个字"，但回答不了：

- "这个结账页在人类眼里像不像一个正常的成功状态？"
- "错误提示对老年用户来说清楚吗？"
- "布局是不是乱了、按钮是不是被遮挡了、金额显示是不是反直觉？"

这类问题需要世界模型的视觉与常识理解，正是 Gemini 多模态的强项。它补的是 **DOM 断言与人类判断之间的语义缝隙**。

**但有一条不可越过的线：Gemini 的输出永远是 advisory evidence（参考证据），不能单独作为验收通过/失败的裁决。** 原因是第一性的：本产品的核心承诺是"不信 AI 说做完了，要产品证据"。如果验收裁决交给另一个 AI 的主观判断，等于把"Codex 说做完了"换成"Gemini 说做完了"——承诺就崩塌了。正确的层级：

```text
结构化证据（DOM/UIA/OCR 断言）→ 裁决层（pass/fail 由它决定）
Gemini 视觉评审               → 证据层（附在报告里：视觉风险、语义疑点、建议人工复核项）
```

Gemini 可以把验收从"通过"升级为"通过但有 2 个视觉风险需人工瞄一眼"，也可以在失败时帮 repair brief 说清"页面实际长什么样"——但它不改变 verdict。

### 落地路径（成本低于预期，基础设施已备）

审计确认三件事已就绪：① `llm_providers.py` 已有 Gemini generateContent 适配器；② `vlm.py` 已有视觉观察通道（本地 qwen2-vl/moondream 同路径）；③ `budget.json` 的 model_policy 已预留 `visual_review: multimodal` 槽位。所以接入是**接线工作而非架构工作**：

1. `vlm.py` 增加 gemini 后端（走已有 model_credentials 体系，API key 用户自配）。
2. 新增验收后处理步骤 `visual_review`（flag 显式开启，默认关）：把失败/通过报告的截图 + 验收标准发给 Gemini，产出结构化评审（`visual_risks[]`、`semantic_concerns[]`、`human_review_suggested: bool`），存为 run artifact。
3. repair brief 组装时若有视觉评审，把"页面实际状态描述"并入 repair prompt——这是它对纠偏（能力 #7）的直接贡献。
4. 成本护栏：只在 mission 的最终验收轮和失败轮调用（不是每步都调）；额度计入 budget。

## 三、路线图（按对愿景的贡献排序）

### M1：自用可用（dogfood 里程碑）——"负责人自己每天用它派活给 Codex"

1. **分批提交当前工作**（前置，防丢失；需负责人批准）。
2. **真实 Codex token 干净验收**：隔离 repo + 真缺陷 + 真 `codex exec` + supervised 验收 + 记录 wall time/rounds。这是产品可信度的最后一块。
3. **项目记忆 v1（能力 #5）— 已落地第一刀**：
   - `project_memory.py` 从既有 mission/plan/rounds/final_report 聚合 per-repo 记忆：最近 missions、结果、stop_reason、失败签名、验收标准、final report。
   - `checkpoint chief-memory --goal "..."` 可查询相关历史。
   - `chief-plan` 注入项目记忆：worker prompt 会看到相关历史的 stop_reason / repeated failure / verified mission。
   - 不新建数据库：全部从已有 JSON/JSONL 聚合，保持本地、可审计。
4. **项目记忆 v2**：
   - repair brief 注入更细的"过往上下文"块。
   - 跨 mission 失败签名比对：同一签名第二次出现直接升级给人（死循环防护从单 mission 扩展到项目级）。
   - 归纳有效修复模式和常改文件区。
5. **入口收敛 — 已落地第一刀**：`checkpoint mission start/status/list/show/queue/worker/memory` 作为高层 facade；内部仍复用 `chief-run`、`chief-status`、`chief-queue`、`chief-memory`，没有第二套执行引擎。下一步是把文档继续改成用户工作流叙事，并支持从开发计划文档批量生成 missions。

**M1 完成标准**：负责人给一句开发计划 → 一条命令 → 走完派发+验收 → 回来看 final report；第二个 mission 能自动引用第一个的记忆。

### M2：首批用户可用

6. **model_policy 真正生效（能力 #3）**：repair 轮用 fast 模型、implementation 用 strong 模型，按能力档案映射到具体 model id；省额度直接可量化。
7. **额度台账（能力 #4）**：per-mission 和 per-repo 的 wall time / rounds / worker minutes 累计报表；`codex exec` 输出若含 token 信息则一并记录。
8. **Gemini visual_review 接入**（按第二节设计，flag 默认关）。
9. **queue cancel/retry + mission/queue 的 workflow coverage**（审计遗留项）。
10. **进程中纠偏 v1（能力 #7 增强）**：worker 结束后、验收前，做 diff 规模与触碰范围检查（改了 plan 未涉及的目录 → 警告并入报告）。

**M2 完成标准**：一个非负责人的真实用户，用自己的 repo 跑通一个 mission，且账单/时间可解释。

### M3：产品化

11. 多 worker 比较（此前一直押后，维持押后）。
12. 简单 dashboard（读 mission/queue JSON 渲染，只读）。
13. DevPacer CLI alias 与品牌落地。

## 四、原则重申（新增两条）

沿用既有 8 条产品原则，新增：

9. **Gemini（及任何 LLM 评审）只产证据，不产裁决。** verdict 永远来自结构化断言。
10. **记忆只从落盘证据聚合，不靠模型转述。** 项目记忆的每一条都能回溯到某个 run/report/mission 文件。
