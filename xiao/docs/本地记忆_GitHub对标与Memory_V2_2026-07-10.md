# 本地记忆 GitHub 对标与 Memory V2

日期：2026-07-10

## 结论

DevPacer 的本地记忆不应替代 Codex 的仓库探索，而应减少重复读取历史任务的成本。Memory V2 采用“本地结构化证据索引 + 关键词/路径/符号排序 + 小预算渐进披露”，不引入向量数据库，也不调用模型生成摘要。

## GitHub 对标

### Aider

- 仓库：https://github.com/Aider-AI/aider
- 调研 commit：`5dc9490bb35f9729ef2c95d00a19ccd30c26339c`
- 参考文件：`aider/repomap.py`
- 可复用点：按 token 预算生成仓库地图；优先提及文件和标识符；通过缓存避免重复解析。
- 本地采用：固定字符预算、路径/符号高权重、增量索引。

### Serena

- 仓库：https://github.com/oraios/serena
- 调研 commit：`e08e964d0c8703401f7ad419b9bf69d85d35188d`
- 参考目录：`src/serena/memories/`
- 可复用点：先披露记忆名称，再按需读取详情；记忆保持人类可读和可寻址；区分项目指令与任务记忆。
- 本地采用：`memory_id`、`source_paths`、compact handoff 和完整 JSON/Markdown 两层输出。

### OpenHands

- 仓库：https://github.com/All-Hands-AI/OpenHands
- 调研 commit：`4a607c6d94a3c30f8045722fb25bb06791dec38c`
- 参考文件：`skills/agent_memory.md`、`.openhands/microagents/repo.md`
- 可复用点：只保存跨任务仍有价值的仓库知识，不把当前 issue 的临时细节永久化。
- 本地采用：preview/inspection 任务降权，优先 verified、失败签名和实际验证证据。

### Cline

- 仓库：https://github.com/cline/cline
- 调研 commit：`78c83cdf33d6d2441820a67520fa4be23b735309`
- 参考文件：`docs/best-practices/memory-bank.mdx`
- 可复用点：将项目背景、系统模式、活动上下文、技术上下文和进度分层。
- 本地采用：项目指令与 mission episode 分层，二者都进入 handoff，互不遮蔽。

## Memory V2 行为

- 数据来源：`mission.json`、`rounds.jsonl`、`plan.json`、`workers.jsonl`、`verification.json`、`final_report.md`。
- 单条 episode：目标、状态、实际变更文件、计划范围、符号、失败签名、worker 结果、验证命令/结论、源证据路径。
- 排序：精确路径、精确符号、目标短语、失败签名、目标词、验收词；preview/inspection 降权。
- 防误召回：低于相关性阈值的任务不注入。没有相关任务时，只保留项目指令。
- 渐进披露：worker 初始提示默认最多 3 条、1200 字符；完整证据仍可通过 memory ID 和 source path 打开。
- 能力边界：提示明确声明记忆是 advisory/non-exhaustive，不构成文件白名单，Codex 可继续检查任何必要文件。
- 增量索引：`.agent-workspace/project_memory/index.json` 按源文件 `mtime_ns + size` 失效；第二次构建不重复读取任务详情、worker 日志和验证文件。

## 本地复现

对应 `tests/test_project_memory.py`：

- `test_memory_v2_ranks_exact_paths_and_symbols_above_generic_preview`
- `test_memory_v2_keeps_instruction_and_episode_notes_under_budget`
- `test_memory_v2_extracts_changed_files_symbols_and_verification`
- `test_memory_v2_reuses_and_invalidates_incremental_index`

真实工作区对照：

- 查询 `Windows worker lock and mission queue safety`：旧实现选择 8 条伪相关任务；V2 选择 0 条任务，只注入 213 字符项目指令。
- 第二次构建：31 条活动 mission 全部命中索引，`hits=31`、`misses=0`。
- 查询具体历史文件时：实际变更文件优先于计划范围，避免宽泛 plan 产生虚假的精确路径命中。
