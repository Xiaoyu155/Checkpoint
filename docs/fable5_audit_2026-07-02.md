# Fable5 审计报告：Codex 工作真实性核查与下一步计划

日期：2026-07-02
审计对象：`docs/work_report_and_product_audit_2026-07-02.md` 及其对应的代码改动
审计方法：不信任报告文字，逐项重跑测试、重跑命令、读代码验证声明

---

## 一、真实性核查结果：属实

报告中每一项可验证声明都被独立复现，无一虚构：

| 声明 | 核查方法 | 结果 |
|---|---|---|
| chief 相关测试 37 passed | 重跑 7 个测试文件 | **37 passed, exit 0** ✓ |
| 非 e2e 全量 1209 passed | 重跑全量（5 分 3 秒） | **精确 1209 passed, exit 0** ✓（报告写 1 warning，实际 2，无关紧要） |
| e2e 52 passed | 重跑 tests/e2e（3 分钟） | **精确 52 passed, exit 0** ✓ |
| `chief-run-demo` 无 token 闭环 | 现场执行 | 埋雷 → 验收失败（证据指向 `assert_checkout_button`）→ 修复 → 验收通过 → mission `verified`，exit 0 ✓ |
| mission 文件落盘 | 检查 demo repo | `mission.json` / `budget.json` / `rounds.jsonl` / `final_report.md` 都在 ✓ |
| queue 无第二套执行引擎 | 读 `chief_queue.py` | `run_next_mission_queue_item` 调用 `run_chief_mission(resume_mission_id=…, execute=True)`，与 `chief-run --resume --execute` 同一条路径 ✓ |
| claim 防双领 | 读锁实现 + 测试 | OS 文件锁 + fsync 原子更新；`test_claim_next_mission_queue_item_prevents_double_claim` 存在且通过 ✓ |
| Gemini 只做 inspection lane | 读 `chief_dispatch.py` + gemini.yaml | 档案 `primary_role: multimodal_inspection`；dispatch 拒绝 inspection lane；`--execute` 仅限 Codex ✓ |
| codex-check exit 0 + coverage gap | 现场执行 | exit 0，`COVERAGE GAP`，与报告一致（如实报告而非掩盖）✓ |
| `.visual-agent-status.md` PASSING | 读文件 | PASSING（seeded_check）✓ |

**特别重要的一项**：上一轮 fable5 审计发现的 26 个预存测试失败（在 pristine HEAD 51a482d 即失败），本轮被 Codex **真正修复而非掩盖**。核查了 `test_visual_guard.py` / `test_executor_actions.py` 的 diff：修复方式是给 fake locator 补上 actionability 回执接口（`first/count/is_visible/is_enabled/bounding_box`），使 fake 与生产的 operation-receipt 契约对齐；断言不但没有变弱，反而从 `assert_text` 升级为带 `forbidden_any` 的 `assert_text_contract`，仍要求 L4/L3。这是正确的修复方向。

## 二、审计中发现并已修复的缺陷

**`chief_dispatch.py` 把 `inspection_only` 判成 `verified`（dispatch 层）。**

- 位置：`dispatch_chief_plan` 的状态归结，原代码 `verdict in {"pass", "inspection_only"}` → `status = "verified"`。
- 影响面：mission 层（`chief_run._stop_reason_from_dispatch`）直接读 `latest_verification.verdict`，`inspection_only` 正确地不算 verified；queue 又只认 mission `verified`。所以 **mission/queue 路径无恙**。但直接使用 `chief-dispatch --execute` 的用户会把"只做了页面检查"误读为"验收通过"，违反报告自己的原则 6（dry-run 不能伪装成真实验收）。
- 处置：已修复为独立的 `status = "inspection_only"`，并新增回归测试 `test_chief_dispatch_inspection_only_verdict_is_not_verified`。修复后 dispatch/run/queue 19 个测试全过。

这正是报告第 9 节自己请求审核的第 2 个问题，说明报告的自我风险评估是准确的。

## 三、对报告本身的评价

报告诚实度高：没有夸大成果（明确自称"可信 headless prototype，不是普通用户可上手的产品"）、如实报告 coverage gap、如实承认真实 Codex token 执行未做最终验收、如实说明 queue 只是单机 JSON 文件锁。第 9 节主动列出审核问题清单，其中一条（inspection-only 语义）确实命中了真实缺陷。

命名审计结论：同意 **DevPacer（产品/mission 层）+ Checkpoint（验收引擎）** 的分层，同意 CLI 暂不改名。品牌层级已在 docs 中落地，暂无进一步动作必要。

## 四、下一步开发计划（按优先级）

基本采纳报告第 8 节，做三处调整：

1. **【新增，最高优先级】分批提交当前工作。** 工作树有 40+ 个未提交文件横跨 P0-P4.5，这是当前最大的审计与回滚风险——任何误操作都可能损失全部工作。建议按逻辑分组提交（P0 基线修复 / P1+P2 计划与能力档案 / P3+P4 dispatch 与 mission / queue 与 background / docs）。提交本身也会消掉 codex-check 的大 diff coverage gap。**需要产品负责人批准后执行。**
2. **真实 Codex worker 干净 demo 验收**（报告优先级 1，维持）：隔离 repo、种缺陷、真实 `codex exec`、supervised 验收、记录 token/时间。这是"deterministic demo 证明框架"到"真实模型证明产品"的最后一步。
3. **mission/queue 的 workflow coverage**（报告优先级 2，维持）：给 chief-run/queue CLI 关键路径建 verification workflow，让 codex-check 对核心路径不再报 uncovered。
4. **queue cancel/retry**（报告优先级 3，维持，保持简单）。
5. **入口收敛**（报告优先级 4，维持）：`checkpoint mission start/status/queue` 高层命令，文档从命令字典改为用户工作流。
6. **真实预算统计**（报告优先级 5，维持）：先 wall time / round count，后 token。
7. **Gemini inspection 实际接入**（报告优先级 6，维持）：读截图/失败证据/final report，输出视觉与语义风险；不写代码。
8. **多 worker 比较**（报告优先级 7，维持最后）。

## 五、审计留痕

- 全量测试：`1209 passed`（非 e2e）+ `52 passed`（e2e），两次 exit 0，无管道掩码。
- demo mission：`20260702-031132-6ee944`，产物在 `.agent-workspace/demo_repos/devpacer-checkout-demo-20260702-031132/`。
- 本轮审计新增改动：`src/visual_agent/chief_dispatch.py`（inspection_only 状态修复）、`tests/test_chief_dispatch.py`（回归测试）。
