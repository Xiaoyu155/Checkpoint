# Pacer 长时间托管

默认**省额度**。激进能力做成可选模式，不强迫用户烧 token。

## 模式（越往下越吃额度）

| 模式 | 额度 | 适合 | 命令 |
|------|------|------|------|
| **economy**（默认） | 低 | 大多数用户、推广默认 | `pacer host run --goal "..." --execute` |
| **standard** | 中 | 要稳一点并行/自愈 | `--mode standard` |
| **unleash** | 高 | 通宵换效率、你认额度 | `pacer host unleash ...` 或 `--mode unleash` |
| **race** | 很高 | 双模型竞速、败者杀掉 | `--mode race` 或 `--race` |

### economy（默认）在省什么

- **并发 1**：一次只跑一个 mission  
- **resume 最多 1 次**  
- 不竞速、不拆目标、不抢占自愈、不默认 merge  
- 额度挂了：说清楚并停新任务（不空转烧探测以外的调用）

### unleash / race 开了什么（显式才开）

- 额度回血续跑、多 resume、pytest 自愈插队  
- unleash：可 merge、可拆目标  
- race：双助手并行 + **先 verified 赢、败者杀进程**（省后续额度，但启动就 2 倍）

race 在 host 中按 tick 检查赢家，不会同步阻塞整条 host 循环。终止败者前会校验 PID
确实属于对应 mission；无法确认归属或终止失败时保留 PID 和任务状态并报告阻塞。

## 最短路径

```bash
# 预检
pacer host doctor

# 若额度已经恢复但本机仍命中最近失败缓存
pacer host doctor --clear-quota-cache

# 省额度托管（推荐默认）
pacer host run --goal "给登录页修报错并让测试通过" --execute

# 均衡
pacer host run --mode standard --goals-file goals.txt --hours 2 --execute

# 吃额度换效率（可选）
pacer host unleash --goal "..." --hours 3 --execute

# 竞速（很吃额度，可选）
pacer host run --mode race --goal "修登录" --hours 1 --execute

pacer host status
pacer host stop
```

`status`、`doctor`、未加 `--execute` 的预览和交互 `/托管` 不会自动恢复任务。
孤儿任务只会在正在运行的 host tick 中按策略恢复；存在 STOP 文件时禁止恢复。

交互：`/托管`

## 任务闭环不是五个独立开关

每次 mission 收尾都会自动写入 `missions/<mission_id>/journey.json`，把同一任务的五段证据串起来：

`路由选择 -> 本地记忆 -> 托管执行 -> 强验收 -> 结果交付 / Dogfood`

`pacer status`、`pacer host status`、`pacer mission status` 和交互 `/状态` 读取同一份闭环。
如果相关记忆被找到却没有进入实际 worker，或者 worker 产物与验收不属于同一 plan，闭环会标记为
`broken`，不会因为某一个测试单独通过就宣称完整交付。验收已通过但尚未合并时显示
`verified_pending_delivery`；Pacer 修改自身但尚未绑定严格发布 Dogfood 证据时显示
`verified_pending_dogfood`。

### 出错后的本地兜底

托管任务失败不是“从头再来”：每轮的停止原因、验收失败签名、worker 输出摘要和变更范围都会写入
mission 本地证据。恢复同一个 mission 时，Pacer 会在下一次 worker 启动前刷新这份记忆，把最近失败
和历史相似任务作为提示交给 worker；因此重试能先处理已知问题，而不是盲目重复原动作。额度耗尽、
供应商 5xx、未登录等外部供应问题不会被无脑重试，host 会等待或停下并留下可读原因。

`codex login status` 只能证明登录，不能证明额度已经恢复，因此不会自动清掉最近配额失败缓存。
真实 worker 成功后会清缓存；如果额度已恢复但缓存仍在，可显式执行
`pacer host doctor --clear-quota-cache`，不会删除任务或账本。

## 推广口径（建议）

**主推：** 白话目标 + 测试验收 + 隔离执行 + **默认省额度**。  
**可选加购叙事：** 需要通宵/竞速时再开 `unleash` / `race`，并标明更吃 token。

## 策略

- 环境变量：`PACER_HOST_MODE=economy|standard|unleash|race`  
- 文件：`.agent-workspace/host/host_policy.json`  
- `PACER_AUTO_RESUME_MAX` 覆盖 resume 次数  
- standard 每个 host 会话最多触发 1 次 pytest self-heal；unleash/race 最多 2 次，并有探测间隔。

## 诚实边界

- 账号/K12 杀配额时，Pacer 会停或等待，不会假装还在写代码。  
- 默认不自动 merge（economy/standard）；unleash/race 才默认 merge（仍要 verified）。  
