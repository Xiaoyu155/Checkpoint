# Pacer 完整 Release Matrix 执行计划（2026-07-17）

依据：

- `.pacer/release.json`
- `docs/pacer_five_pillars_release_audit_2026-07-17.md`（明确：15 项 managed sample 尚未宣称完成）
- `src/visual_agent/release_gate.py` / `release_evidence.py` / `cli_quality.py`

## 1. 范围澄清

审计所说的 **15 项** = 3 仓库 × 5 场景 managed sample：

| 仓库 | repository_root | 场景 |
| --- | --- | --- |
| pacer | `.` | implementation / test / documentation / read_only / fault_recovery |
| demo-app | `examples/demo-app` | 同上 |
| nextjs-demo | `examples/nextjs-demo` | 同上 |

完整 `release.json` 还包含：

| 类型 | case_id | 本阶段关系 |
| --- | --- | --- |
| deterministic | `deterministic-core` | 应先跑，秒级门禁 |
| managed_sample ×15 | 见下表 | **本计划主目标** |
| dogfood ×3 | `dogfood-1..3` | 审计已有三次 100 分 OIDC；matrix 汇总时引用既有证据，不重写标准 |

Manifest digest（本地 `release_manifest_digest`）：

```text
aaa50981eb0ed72d2b1402303b6010828f022aef3da198ee7563dbcb5c84802a
```

`validate_release_manifest`：passed，reason_codes 空。

## 2. 硬性约束（禁止为了绿而改）

1. **不修改** `.pacer/acceptance.json`、`.pacer/dogfood.json`、`.pacer/release.json` 验收目标。
2. 使用 digest-lock：`pacer-release-manifest-check --expected-digest <digest>`。
3. fail-closed：首个 failed / unstable case 停止矩阵；**重试通过 ≠ clean release**。
4. 性能门禁（manifest performance_policy）：
   - managed_sample：单 case ≤ 300s wall，≤ 250k tokens
   - managed_aggregate：mean ≤ 180s，p95 ≤ 300s，mean tokens ≤ 180k
   - dogfood：单次 ≤ 900s，≤ 600k tokens
5. 证据必须是服务器/runner 生成的 receipt，不接受手工改 JSON 刷绿。

## 3. 15 项 case 清单与建议执行序

执行序原则：

1. 先 `deterministic-core`
2. 再 pacer 仓库五场景（产品自托管最关键）
3. 再 demo-app 五场景
4. 最后 nextjs-demo 五场景（通常最重）
5. dogfood 三项引用已通过的三次独立 run 证据包

| # | case_id | kind | 通过标准（摘要） | 建议 runner |
| ---: | --- | --- | --- | --- |
| 0 | deterministic-core | deterministic | 本地确定性检查全绿 | `pacer` 内置 deterministic runner |
| 1 | pacer-implementation | managed_sample | 真实实现 + 验收 pass + managed SUCCEEDED | Codex 订阅/健康中转 |
| 2 | pacer-test | managed_sample | 测试向改动 + command gate | 同上 |
| 3 | pacer-documentation | managed_sample | 文档向、范围受控 | 可用 cheaper tier |
| 4 | pacer-read-only | managed_sample | 无产品写、只读结论 | 只读合同 |
| 5 | pacer-fault-recovery | managed_sample | 故障可恢复路径有证据 | 可注入可控故障 |
| 6 | demo-app-implementation | managed_sample | demo-app 实现场景 | 隔离 worktree |
| 7 | demo-app-test | managed_sample | demo-app 测试场景 | 同上 |
| 8 | demo-app-documentation | managed_sample | demo-app 文档 | 同上 |
| 9 | demo-app-read-only | managed_sample | demo-app 只读 | 同上 |
| 10 | demo-app-fault-recovery | managed_sample | demo-app 故障恢复 | 同上 |
| 11 | nextjs-demo-implementation | managed_sample | nextjs 实现 | 注意依赖安装预算 |
| 12 | nextjs-demo-test | managed_sample | nextjs 测试 | 同上 |
| 13 | nextjs-demo-documentation | managed_sample | nextjs 文档 | 同上 |
| 14 | nextjs-demo-read-only | managed_sample | nextjs 只读 | 同上 |
| 15 | nextjs-demo-fault-recovery | managed_sample | nextjs 故障恢复 | 同上 |
| D1–D3 | dogfood-1..3 | dogfood | 三次独立 OIDC 100 分 | **引用**审计已完成 run，不重复造标准 |

## 4. 本地 / CI 命令骨架

### 4.1 锁 manifest + 矩阵预检（已落地）

```powershell
cd xiao
$digest = "aaa50981eb0ed72d2b1402303b6010828f022aef3da198ee7563dbcb5c84802a"
pacer pacer-release-manifest-check --manifest .pacer/release.json --expected-digest $digest --format markdown

# 预检：digest 锁、15 managed 清单、missing-runner / wrong-digest 门禁
# 明确 release_ready=false，不会伪造成矩阵已通过
python scripts/release_matrix_preflight.py --format markdown
```

2026-07-17 本地预检结果：`status=passed`，`release_ready=false`，`managed_sample=15`，missing-runner / wrong-digest 门禁均生效。

### 4.2 跑矩阵（概念）

入口在 `release_gate.run_release_matrix` + `release_evidence.run_release_evidence_bundle`，CLI：

```powershell
# 证据包评估（需已有 case result 与 attestation）
pacer pacer-release-check `
  --repo-root . `
  --manifest .pacer/release.json `
  --expected-digest $digest `
  --evidence-root <trusted-evidence-root> `
  --bundle .pacer/release-evidence.json `
  --format markdown
```

Managed sample 的具体 runner 必须由外部注入（模块注释：*never starts a process itself*）。  
下一阶段应提供一个 **orchestrator 脚本**（建议 `scripts/run_release_matrix.ps1`）负责：

1. 为每个 case 创建隔离 worktree / temp workspace
2. 调用 `checkpoint mission start ... --execute` 或对应 scenario harness
3. 写 `case-result.json`（status / metrics / receipt paths）
4. 调用 `run_release_matrix` 汇总；首败停止

### 4.3 Dogfood 串行注意

- concurrency group 目前按 workflow+ref；连续触发时 pending 可能被挤掉
- 三次 dogfood 必须串行，或使用独立 concurrency key
- 已有通过 run（审计）：`29576436205` / `29576440577` / `29576836769`

## 5. 通过 / 失败判定

矩阵 **clean pass** 仅当：

1. manifest digest 锁定匹配
2. 全部 15 managed_sample = passed（非 skipped 伪装）
3. deterministic-core = passed
4. dogfood streak ≥ `required_clean_dogfood_streak`（3），且 same candidate 规则满足
5. 性能聚合未超 performance_policy
6. 无 “retry 后才绿” 充当 clean release

任一 managed case `failed` 或 `unstable` → 停止，输出首败 case_id 与 reason_codes。

## 6. 分阶段落地（建议 3 天）

| 阶段 | 内容 | 出口 |
| --- | --- | --- |
| D0 | digest lock + deterministic-core + orchestrator 骨架 | 本地可跑空跑/干跑 |
| D1 | pacer 五场景真实 managed sample | 5/5 或首败定位 |
| D2 | demo-app 五场景 | 5/5 或首败定位 |
| D3 | nextjs-demo 五场景 + 汇总 release-evidence | 15/15 或明确 blocker |
| 收尾 | 绑定既有 dogfood 三连 + `pacer-release-check` | 可签名发布证据包 |

## 7. 本轮已完成 / 未完成

### 已完成

- 读取并校验 `.pacer/release.json` 结构与 digest
- 列出完整 15 managed + dogfood/deterministic 边界
- 明确 fail-closed、性能门禁、不降标准
- 与五大支柱审计的“未宣称完成”边界对齐

### 未完成（诚实边界）

- **尚未**实际执行 15 个 managed_sample runner
- **尚未**生成 `.pacer/release-evidence.json` 全量证据包
- **尚未**合并为“可发布”结论

因此本文件是 **执行计划与可运行骨架**，不是 release green 证明。

## 8. 建议的下一步命令（人工确认后执行）

```powershell
cd D:\助手codex\xiao
$digest = "aaa50981eb0ed72d2b1402303b6010828f022aef3da198ee7563dbcb5c84802a"
pacer pacer-release-manifest-check --manifest .pacer/release.json --expected-digest $digest --format markdown

# 先只跑确定性门禁（若已有 CLI/ harness）
# 再按 case_id 串行执行 pacer-implementation ... 并落 case-result
```

Provider 建议：使用**健康** Codex 订阅或已恢复额度的中转；避免在 503 窗口开启 15 项长跑。
