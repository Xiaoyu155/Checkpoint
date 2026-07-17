# Pacer 五大核心支柱与 Release Dogfood 审计报告

- 审计日期：2026-07-17
- 审计仓库：`Xiaoyu155/Checkpoint`
- 审计基线：`79459d6cb80c8c146645529264d186f2af5ee190`
- 工作模型：`gpt-5.6-sol`
- 自定义 provider：`sub2api_dogfood`
- Worker 权限：`--ask-for-approval never --sandbox danger-full-access`

## 执行摘要

按仓库现有验收标准，Routing、Memory、Managed、Acceptance、Dogfood 五大核心支柱均已取得可信通过证据。Release Dogfood 进一步完成三次独立 GitHub OIDC run，三次均为 100 分、使用同一个 candidate wheel、具有不同的 run identity，并通过 GitHub artifact attestation 与 provenance 验证。

本轮没有降低或绕过任何验收标准。修复集中在任务合同语义、Memory receipt/launch 绑定和审计 harness 可执行性。服务端仍保持 fail-closed：未知 memory ID、错误 receipt、错误 claim 绑定和不可信 completion 均继续被拒绝。

结论：五大核心功能按当前标准可以判定通过，Release Dogfood 的三次独立运行要求也已满足。仍需单独治理模型提交格式波动、自定义 provider 503 和 GitHub pending run 取消等运行稳定性问题。

## 审计范围与原则

本轮坚持以下约束：

1. 不修改 `.pacer/acceptance.json`、`.pacer/dogfood.json`、`.pacer/release.json` 中的验收目标。
2. Codex worker 保持最大自由度，不限制仓库读取和分析能力。
3. 不通过 retry/loop 把失败伪装成通过；每个任务的 completion policy 仍为单次提交。
4. 只接受服务器生成的 task contract、verification receipt、history、pillar assessment、GitHub attestation 和 provenance。
5. Focused tests 绿色不自动等于五大支柱通过，必须同时满足对应支柱的语义证据。

## 五大支柱结论

| 支柱 | 结论 | 关键证据 |
| --- | --- | --- |
| Routing | 通过 | runtime-owned decision、request 与实际 provider/model 一致；`sub2api_dogfood / gpt-5.6-sol`；policy match 通过。 |
| Memory | 通过 | `lookup_hit=true`、`relevant_hit=true`、`injected_hit=true`、`used_hit=true`；真实 ID 为 `pacer-native:20260717-044309-cff24bd1`；task review trust 为 `yes`。 |
| Managed | 通过 | Managed state history、合法状态迁移、idempotency key、预算状态与 trusted completion 均有真实记录。 |
| Acceptance | 通过 | 锁定 acceptance contract、真实 pytest/Ruff batch、服务端 claim binding 和 task review trust 均通过。 |
| Dogfood | 通过 | Pacer A 管理自身 candidate patch，fresh wheel B 独立安装验证，三次 OIDC provenance 与 attestation 均通过，质量分均为 100。 |

核心支柱审计证据来自以下独立 run：

- `29555495316`：Routing、Memory、Acceptance 通过；Memory artifact 明确记录 `used_hit=true` 和真实 memory ID。
- `29555979163`：Routing、Managed、Acceptance 通过；Memory acknowledgement 与 completion 已完成，但 provider 在返回最终文本时发生 HTTP 503，导致 job 红灯。该 503 不覆盖此前已持久化的 Memory 成功证据。

这里采用独立证据聚合，不把某一次 matrix 的整体红绿状态冒充产品结论。

## Release Dogfood 三次独立 OIDC 结果

三次有效 run 均基于同一提交和同一 candidate wheel：

| GitHub run | 结果 | 质量分 | Candidate wheel SHA-256 | Run identity digest |
| --- | --- | ---: | --- | --- |
| `29576436205` | 通过 | 100 | `245a6fff1459ea85d8bf6aaad2ac14dd6977176dcc72be2d3ed25eedac04abc0` | `4a6f3c283e4f6624a795177be19befc26ad56964382f1accbc660d0c660a940a` |
| `29576440577` | 通过 | 100 | `245a6fff1459ea85d8bf6aaad2ac14dd6977176dcc72be2d3ed25eedac04abc0` | `881733a0e83da0b426f828fe0f1fcfaf434d4c1b5e71293c5d942133bda6ce80` |
| `29576836769` | 通过 | 100 | `245a6fff1459ea85d8bf6aaad2ac14dd6977176dcc72be2d3ed25eedac04abc0` | `1f72c4ea1c89784234ba23ad8e5f4936caea8efc7dcb3ffabbb215bccb7fd7b5` |

每次 run 都完成：

- exact candidate patch 校验；
- wheel A 构建和安装；
- Pacer A 对自身变更的单次 trusted completion；
- wheel B 独立构建、fresh install 和 `pip check`；
- candidate wheel attestation；
- canonical evidence attestation；
- GitHub provenance 验证；
- independent verification result attestation。

三次 `run_identity_digest` 互不相同，candidate wheel digest 完全一致，满足 `.pacer/dogfood.json` 中 `required_independent_runs=3` 和 `same_candidate_required=true`。

Run `29576438238` 因 GitHub concurrency 只保留一个 pending run 而被平台取消，未执行测试，不计入三次有效结果，也没有用补跑掩盖产品失败。

## 本轮确认并修复的问题

### 1. Dogfood analysis requirement 被误判为 test

任务文本中的 `test and analyze` 被合同解析器按 `and` 拆开，导致包含测试路径的 Ruff requirement 被归为 `test_run`。模型绑定到 `targeted-analysis` 后被服务端正确拒绝。

修复：调整合同措辞为明确的 analyze 语义，保持 pytest、Ruff 命令、单次 completion 和 semantic binding 校验不变。

### 2. Memory acknowledgement 绑定到错误 launch

workflow 的 `PACER_LAUNCH_ID` 可能仍指向 prelaunch marker，而 launcher 实际运行生成新的 launch ID。Bootstrap memory 使用真实 launch，acknowledgement 却可能按旧环境变量查找错误 cache。

修复：服务端只在同一 repo 中按 exact receipt 和 trusted injected IDs 唯一重绑定实际 launch。未知 ID、错误 receipt 或多重匹配仍失败关闭。

### 3. Compact bootstrap 与 full acknowledgement 的 receipt 漂移

Bootstrap memory view 使用 `detail=compact`，acknowledgement 包装器使用 `detail=full`。旧逻辑把展示 detail 计入 receipt 重算，导致同一已交付 cache 被误判为 receipt 不匹配。

修复：Memory use 绑定到原始已交付 view 的 receipt，同时重新校验当前 source digest、repo identity 和 injected ID 子集。响应 detail 不再改变已交付证据的身份。

### 4. 审计任务合同的语义和长度不稳定

部分只读协议说明被默认归为 implementation，或因提示过长超过 2000 字符合同上限。模型还曾把两个 requirement ID 放入同一 claim。

修复：四类任务均本地验证为 `read_only`、`requires_source_change=false`，长度分别为 1250、1684、1352、1348；明确每个 requirement 使用独立 claim，且每个 `requirement_ids` 数组只允许一个 ID。

## 回归验证

本地回归结果：

- Dogfood workflow 与 task review：`49 passed`。
- MCP server、typed contracts、launch context：`244 passed`。
- 最终 receipt/view 修复后的 MCP server 与 typed contracts：`195 passed`。
- Ruff：相关源码和测试均通过。
- GitHub Dogfood：三次有效 release run 全部成功。

工作区原有未跟踪目录 `.tools/`、`gui/` 未被删除或提交。

## 失败模式与残余风险

### 模型协议遵循仍有波动

工作模型曾出现以下行为：

- 把多个 requirement ID 放入一个 claim；
- 在明确禁止 retry 时重复调用 acknowledgement 或 completion；
- 使用错误字段名后自行尝试多种参数组合；
- completion 成功后仍等待模型生成最终文本。

这些行为多数被服务端 fail-closed 阻断，没有污染可信证据，但会增加运行时间和失败率。关键约束应继续下沉到协议层，不应只依赖自然语言提示。

### Provider 可用性影响 job 绿灯

Run `29555979163` 中 Memory acknowledgement 和 completion 已成功，随后自定义 provider 在最终响应阶段返回 HTTP 503，导致 shell job 退出 1。建议 launcher 在可信 completion 已持久化后，不再要求额外模型轮次才能让任务进程成功退出。

### GitHub concurrency 会取消旧 pending run

即使设置 `cancel-in-progress: false`，同一 concurrency group 仍只保留一个 pending run。Release streak 应串行触发，或为每次审计使用明确的独立 concurrency key。

### 尚未覆盖完整 release matrix

本报告证明五大支柱和 Pacer 自身三次 release Dogfood。`.pacer/release.json` 中三仓库乘五场景的完整 15 项 managed sample matrix 仍是更高一级的发布门禁，不应由本报告自动宣称已完成。

## 最终结论

1. 五大核心支柱均已有可信通过证据。
2. Memory 已从“检索并注入”提升到真实、可审计的 `used_hit=true`。
3. Dogfood 达到 100 分，并完成三次不同 GitHub OIDC run identity、同一 immutable candidate 和完整 artifact attestation。
4. 当前可以认定 Pacer 五大核心功能达到既定标准。
5. 仍不能把“核心支柱通过”等同于“所有发布稳定性工作完成”；下一发布阶段应执行完整 15 项 release matrix，并单独治理模型协议遵循和 provider 503。

