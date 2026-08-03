# Pacer 残余风险定点验证（2026-07-17）

依据：`docs/pacer_five_pillars_release_audit_2026-07-17.md` 的失败模式与残余风险。  
本轮目标：用代码路径 + 本地实测证据验证风险是否仍在，并给出可执行治理项。

## 验证范围

| ID | 风险（审计原文） | 本轮方法 |
| --- | --- | --- |
| R1 | 模型协议遵循波动 | 代码与审计记录复读；不放宽 fail-closed |
| R2 | Provider 503 影响 job 绿灯 | 本地 Codex custom 中转 503 复现 + dispatch/stop_reason 代码追踪 |
| R3 | GitHub concurrency 取消 pending run | 读 `pacer-dogfood.yml` concurrency 配置 |
| R4 | 完整 15 项 release matrix 未覆盖 | 见配套 `pacer_release_matrix_plan_2026-07-17.md` |

## R2：Provider 503 / 用户文案丢失（已确认仍在，并做局部修复）

### 实测证据

- 演示仓库 mission：`20260717-104139-760881-be1056-dd268e03`
- worker log：连续 `HTTP 503 Service Unavailable` 打到 `http://174.138.75.136:8080/v1/responses`
- dispatch 内部：`managed_runtime.retry.failure_kind = provider_5xx`，`retry=true`
- 用户可见：`stop_reason=worker_error`，文案要求跑 `checkpoint agents doctor`

### 代码根因

1. `chief_dispatch._managed_retry_failure_kind` **已经**能从 stdout 识别 `http 503` → `provider_5xx`。
2. `chief_run._stop_reason_from_dispatch` 在 `status == worker_failed` 时**无条件**返回 `worker_error`，丢掉了 managed retry 分类。
3. `_message_for_stop("worker_error")` 固定指向 agents doctor，误导用户。

### 本轮修复（产品侧）

- 文件：`src/visual_agent/chief_run.py`
- 行为：`worker_failed` 时优先使用 `managed_runtime.retry.failure_kind`（`provider_5xx` / `provider_rate_limit` / `network_timeout` / `process_crash` / `evidence_rejected`）。
- 新增对应中文修复文案；`provider_5xx` 明确说“通常不是本地 CLI 缺失”。
- 测试：`tests/test_chief_run.py::test_chief_run_surfaces_provider_5xx_instead_of_generic_worker_error`

### 仍未关闭的部分（审计 R2 完整建议）

审计还要求：

> launcher 在可信 completion 已持久化后，不再要求额外模型轮次才能让任务进程成功退出。

这是 **MCP / launch / dogfood job** 路径问题，与 mission `chief_run` 用户文案是两条线。  
本轮未改 launcher 退出语义；仍需单独任务：

1. 定位 `complete_pacer_task` 成功后是否还阻塞等待模型最终文本；
2. 若 trusted completion 已写盘，shell job 应以 0 退出（或明确 “evidence_ok_provider_failed” 而不覆盖证据）；
3. 用重放 fixture 覆盖 “completion 成功 + 后续 503”。

## R1：模型协议波动（仍在，服务端 fail-closed 有效）

### 已观察到的行为（审计 + 本机）

- 多 requirement 塞进一个 claim
- 禁止 retry 时仍二次 completion attempt
- 字段名试错
- completion 后仍等最终自然语言

### 代码侧结论

- `mcp_server.record_pacer_outcome_payload`：`completed` 必须带 process-local trusted completion audit，否则拒绝。
- 支柱 assessment 与 task review 保持 fail-closed。
- 自然语言约束不够；应继续把 one-claim-per-requirement、单次 completion token、禁止 completion 后额外 turn 下沉到协议。

### 本轮不改

不在本轮扩大 MCP 协议面；只记录为下一阶段硬约束任务。

## R3：GitHub concurrency（仍在）

`xiao/.github/workflows/pacer-dogfood.yml`：

```yaml
concurrency:
  group: pacer-dogfood-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false
```

`cancel-in-progress: false` **不能**保证多个 pending run 同时保留；同一 concurrency group 仍可能只保留一个 pending。  
审计记录的取消 run `29576438238` 与此一致。

### 建议

1. Release streak 串行触发；或
2. 每次独立审计使用唯一 concurrency key（例如含 `github.run_id` / 手动 `audit_id` 输入）。

本轮不改 workflow（避免干扰已通过的三次 OIDC 证据链），仅写入发布操作手册。

## R4：15 项 matrix

见 `docs/pacer_release_matrix_plan_2026-07-17.md`。  
manifest 校验通过，digest：

`aaa50981eb0ed72d2b1402303b6010828f022aef3da198ee7563dbcb5c84802a`

## 本地回归

```powershell
cd xiao
python -m pytest tests/test_chief_run.py::test_chief_run_surfaces_provider_5xx_instead_of_generic_worker_error tests/test_chief_run.py::test_chief_run_worker_failure_is_not_verified_by_passing_command tests/test_release_gate.py -q
```

2026-07-17 实测：`20 passed`（含 provider_5xx 文案桥接 + release_gate 全套）。

## 结论表

| 风险 | 状态 | 本轮动作 |
| --- | --- | --- |
| R1 协议波动 | 仍在 | 记录；保持 fail-closed |
| R2 503 文案/分类 | 用户可见路径已修；launcher job 红灯未修 | `chief_run` stop_reason 桥接 + 文案 |
| R3 concurrency | 仍在 | 操作建议；未改 workflow |
| R4 15 项 matrix | 未执行 | 独立执行计划文档 |

**不能**因本轮局部修复宣称“残余风险清零”。
