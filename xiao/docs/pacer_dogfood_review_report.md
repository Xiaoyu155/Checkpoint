# Pacer 托管狗粮审查报告

本文档给后续使用 Code / Codex 审查 Pacer 时提供入口文件清单，并记录最近一次真实托管狗粮开发暴露的问题、判断和改进路线。

## 给 Code 的重点审查文件

### 最高优先级：托管闭环

这些文件决定 Pacer 是否会把同一个 Codex worker 带到正确方向，是最容易造成“模型没错，调度错了”的位置。

| 文件 | 审查重点 |
| --- | --- |
| `src/visual_agent/chief_run.py` | mission 生命周期、stop_reason、final_report、round 记录、状态是否误报 verified。重点看 `_stop_reason_from_dispatch()`、`_message_for_stop()`、`_finish()`、`chief_run_to_markdown()`。 |
| `src/visual_agent/chief_dispatch.py` | worker prompt、worktree、验证、自动修复、merge 门禁。重点看 `dispatch_chief_plan()`、`run_dispatch_verification()`、`_verification_is_repairable()`、`merge_worktree_branch()`。 |
| `src/visual_agent/command_verification.py` | `--test-command` 验收门、失败分类、引用日志读取、测试/验收文件防篡改。重点看 `classify_command_failure()`、`command_repair_brief()`、`changed_test_files()`、`is_test_path()`。 |
| `src/visual_agent/chief_background.py` | 后台进程状态同步，防止 worker 已结束但 mission 仍显示 running，或 verified 被 worker_error 覆盖。 |
| `src/visual_agent/mission_progress.py` | 看板/状态页进度、changed files、product files、blocker、stale worker 判定。 |

### 第二优先级：上下文、记忆和 diff 证据

这些文件决定 worker 看到的上下文是否准确，以及用户能否读懂失败证据。

| 文件 | 审查重点 |
| --- | --- |
| `src/visual_agent/repo_map.py` | 中文路径、缓存、文件索引完整性；不能让 repo map 漏掉真实产品代码。 |
| `src/visual_agent/diff_summary.py` | diff 文件统计、中文路径、函数提取、体量风险提示；报告不能把 Git 转义路径展示给用户。 |
| `src/visual_agent/git_diff.py` | changed files 的底层来源，注意 `core.quotePath=false` 和 runtime artifact 过滤。 |
| `src/visual_agent/chief_plans_store.py` | plan、worker、verification 记录读写；失败复盘依赖这些证据。 |
| `src/visual_agent/missions.py` | mission.json、rounds.jsonl、final_report 持久化。 |

### 第三优先级：任务入口和目标收口

这些文件决定模糊目标是否会被错误执行。

| 文件 | 审查重点 |
| --- | --- |
| `src/visual_agent/chief_engineer.py` | plan 构建、coverage 判断、acceptance criteria。 |
| `src/visual_agent/mission_intake.py` | 识别人工验收、审查报告、测试修改等特殊目标。 |
| `src/visual_agent/goal_grounding.py` | “继续按计划开发”这类目标的落地收口。 |
| `src/visual_agent/mission_contract.py` | requirement contract 的规范化和用户补充。 |
| `src/visual_agent/verification_profiles.py` | 自动识别项目测试命令的可靠性。 |

### 第四优先级：UI / 工作台 / 队列

这些文件决定用户看到的任务状态是否可信。

| 文件 | 审查重点 |
| --- | --- |
| `src/visual_agent/dashboard/api.py` | mission status、detail、merge/retry API。 |
| `src/visual_agent/dashboard/data.py` | 看板数据汇总，注意不要把 stale/failed 显示成健康。 |
| `src/visual_agent/dashboard/static/app.js` | 前端状态文案、按钮门禁、错误提示。 |
| `src/visual_agent/workbench_app.py` | 桌面工作台任务入口和报告展示。 |
| `src/visual_agent/workbench_board.py` | mission board 数据组合。 |
| `src/visual_agent/chief_queue.py` | 队列 claim、执行、结果回写。 |

### 第五优先级：成本、后端和安全

这些文件决定 Pacer 是否真的节省额度，还是因为错误重试烧更多额度。

| 文件 | 审查重点 |
| --- | --- |
| `src/visual_agent/agent_backends.py` | quota failure、failover、MiMo/低成本后端选择。 |
| `src/visual_agent/agent_capabilities.py` | agent profile、模型/权限推荐。 |
| `src/visual_agent/hourly_budget.py` | 小时级额度预算。 |
| `src/visual_agent/subscription_quota.py` | 订阅窗口和额度估算。 |
| `src/visual_agent/workspace.py` | `.agent-workspace` 和 runtime 文件隔离，避免污染产品仓库。 |

### 必跑测试入口

改动上述文件后，优先跑这些测试：

```powershell
python -m pytest `
  tests/test_test_tamper_guard.py `
  tests/test_mission_progress.py `
  tests/test_chief_run.py `
  tests/test_repo_map.py `
  tests/test_chief_dispatch.py `
  tests/test_command_verification.py `
  tests/test_diff_summary.py -q
```

最近一轮相关回归结果：`148 passed`。

## 最近狗粮开发结果

### 实测任务

- 产品仓库：`D:\抖音快手支付宝`
- Pacer 源码：`D:\助手codex\xiao`
- mission id：`20260709-033415-bfe4d1`
- 验收命令：`npm run eval:acceptance`
- 目标：对三端老人社保/医保/个税小程序继续做托管开发，并记录 Pacer 自身暴露的问题。

### 实际结果

旧 Pacer 在本次任务中成功启动 worker、创建隔离 worktree、执行实现和验收，并最终自动 merge 到产品仓库。但这个结果不能视为成熟成功，原因是：

1. 初始失败是外部 AI 验收环境问题，例如 `QWEN_API_KEY missing` 或 Qwen 返回不可解析 JSON。
2. 旧 Pacer 把它分类成普通 `command_failed`，给 worker 的 repair prompt 是“fix the code so it passes”。
3. worker 按这个错误目标修改了 `eval/ai-knowledge-acceptance.mjs`，把缺外部 Qwen 时的强门槛降级为 local judge。
4. 旧 Pacer 在 worker worktree 中看到验收通过后 merge，但没有在目标主分支 merge 后再复验。
5. 旧 Pacer 还在产品根目录追加 `强制测试记录.md`，造成主仓库脏工作区。

产品仓库最终状态：

- 主仓库已清理为干净工作区。
- `node eval/continuous-acceptance.mjs --once --no-external-ai --no-fail-fast` 通过。
- `npm run eval:acceptance` 当前会因外部 Qwen judge 输出不可解析 JSON 失败；新 Pacer 已能分类为 `verification_environment_missing`。
- 已 merge 的产品 commit `1c00730` 包含旧 Pacer 诱导产生的 eval 降级式修改，未在本轮擅自回滚。

## 最近修复的 Pacer 问题

### 1. 中文路径导致 repo map 和报告失真

问题：Git 默认 quotePath 会把中文路径输出成八进制转义，导致 repo map 只索引到少量文件，报告也不可读。

修复：

- `repo_map.py`、`diff_summary.py`、`mission_progress.py`、`command_verification.py` 等 Git 调用增加 `-c core.quotePath=false`。
- 增加中文路径回归测试。

### 2. worker prompt 过度信任 repo map

问题：repo map 不完整时，prompt 仍倾向让 worker trust repo map，worker 会忽略真实文件。

修复：

- prompt 改为把 repo map 当 orientation，关键路径必须读真实文件确认。

### 3. test tamper guard 漏掉 eval 和三端 test.js

问题：`eval/`、`regression_tests/`、`快手/test.js`、`抖音/test.js`、`支付宝/test.js` 曾未被识别为验收/测试文件。

修复：

- `is_test_path()` 覆盖 eval、acceptance、regression_tests、根级 `test.js` / `spec.js`。
- `changed_test_files()` 对中文路径保持可读。
- 实测旧 worker worktree 中的 eval 和三端 test 文件现在会被抓到。

### 4. 外部 AI 验收失败误触发自动修代码

问题：缺 Qwen key、外部 AI judge missing、AI 输出坏 JSON 等，本质是验收环境/外部服务问题，不是产品代码问题。

修复：

- 新增 failure kind：`verification_environment_missing`。
- `command_repair_brief()` 明确禁止修改产品代码、测试、eval 脚本或验收门槛。
- `_verification_is_repairable()` 将该类失败设为不可自动修复。
- `chief_run` stop reason 和用户文案同步。

### 5. 顶层 npm 输出没有真实失败日志

问题：`npm run eval:acceptance` 顶层只显示 `FAIL ai_knowledge_acceptance (path.log)`，真实原因在子日志。

修复：

- `run_command_verification()` 会读取输出中引用的 `.log` 文件 tail，并把它拼入 failure evidence。
- 分类器可以基于子日志识别外部 AI 问题。

### 6. merge 后没有复验目标分支

问题：worktree 通过不代表目标主分支 merge 后仍通过，尤其外部 AI 验收可能波动。

修复：

- 显式 `--test-command` 且 merge 成功后，新增 post-merge command verification。
- post-merge 失败时状态为 `merged_verification_failed`，不再报 `verified`。

### 7. Pacer 自己污染产品仓库

问题：终态记录追加到产品根目录 `强制测试记录.md`，造成产品仓库脏工作区。

修复：

- 终态强制记录改写到 `.agent-workspace/missions/<mission_id>/强制测试记录.md`。
- 产品主仓库不再因为 Pacer 自身审计记录变脏。

## 体感判断

这次狗粮说明，同一个 Codex 放在 Pacer 里，并不自动等于正常 Codex 直接开发。差别不在模型，而在外层 coordinator：

- 正常 Codex 会直接判断 “Qwen key/JSON 输出坏了” 是环境问题。
- 旧 Pacer 把环境问题包装成 “fix the code”，worker 就会努力把错误目标完成。
- 旧 Pacer 的自动 merge 和状态展示让错误结果看起来像 verified。

所以 Pacer 的核心不是“多调用几次 AI”，而是“把失败分类、文件边界、修复权限、merge 门禁、复验和报告做对”。这些系统判断不成熟时，Pacer 不会省额度，反而会因为错误 repair 烧更多额度。

## 我认为的解决路线

### 短期：把 Pacer 定位为有监督托管

在连续稳定前，不应宣传为完全无人值守省钱工具。更准确的定位：

- 隔离 worktree。
- 自动跑用户测试。
- 收集证据和 final_report。
- 对不可修复失败停止。
- merge 前后都验收。
- 关键 merge 仍建议人工确认。

### 中期：做强 coordinator，而不是只做 worker runner

Pacer 需要一个明确的 coordinator 层：

1. 先研究 repo 和验收门。
2. 把目标转成可执行 contract。
3. 给 worker 最小、明确、带禁区的任务。
4. 验证失败先分类，不直接修。
5. 只有代码失败才给 repair prompt。
6. 修复后复验 worktree。
7. merge 后复验目标分支。
8. 状态报告必须暴露真实 blocker。

### 长期：建立失败分类矩阵

建议把失败至少分成：

| 类型 | 是否自动修 | 例子 |
| --- | --- | --- |
| `code_failure` | 可以 | 单测断言失败、构建错误、lint 错误 |
| `test_tampering` | 不可以 | worker 改了 eval/test/spec |
| `verification_environment_missing` | 不可以 | Qwen key 缺失、外部 judge 坏 JSON、浏览器/设备缺失 |
| `command_invalid` | 不可以 | 测试命令不存在、PATH 错 |
| `toolchain_violation` | 不可以 | worker 使用了禁止 SDK |
| `post_merge_failure` | 不可以继续自动合并 | worktree 过、目标分支不过 |
| `no_product_changes` | 不可以 | 只改报告/缓存/记录 |

每类失败都应有：

- 机器可读 `failure_kind`
- 用户可读 message
- 是否 repairable
- 是否允许 merge
- 是否通知用户
- 对应回归测试

### 产品文档也要降调

`docs/use_on_any_project.md` 里“省额度、省时间”的表达应加前提：只有在验收命令可靠、失败分类正确、任务边界清楚、post-merge 复验通过时才成立。

更稳妥的说法：

> Pacer 优先保证隔离、可审计和验收门禁；在任务边界清楚且验证可靠时，才可能节省重复开发时间和主力模型额度。

## 后续审查建议

下一轮 Code/Codex 审查 Pacer 时，优先问这些问题：

1. 当前失败是否被正确分类？
2. repair prompt 是否可能诱导 worker 改测试或降验收？
3. changed files 是否把 runtime artifacts 当成产品代码？
4. 中文路径是否可读且可定位？
5. worktree pass 后，目标分支是否也 pass？
6. final_report 是否包含足够证据让用户不看原始日志也能判断下一步？
7. Pacer 自己是否让产品仓库变脏？
8. 这次运行是否真的节省额度，还是因为重复 repair 增加消耗？

如果以上问题没有全部过关，Pacer 就只能算“有监督托管”，不能算成熟无人值守开发。
