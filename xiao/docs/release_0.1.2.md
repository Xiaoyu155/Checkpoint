# Visual Agent 0.1.2 发布记录

发布日期：2026-07-15

## 发布结论

`0.1.2` 已完成源码回归、Python 3.11 裸安装验证和最终安装态稳定性矩阵。最终候选 wheel：

- 文件：`D:\助手codex\.runs\pacer-release-0.1.2-20260715-v4\visual_agent-0.1.2-py3-none-any.whl`
- SHA-256：`429AE92D2F7C6C7917C5AF0EF3A9160CC01303E67D0AAA94ABB0FFB439B544E6`
- 裸安装环境：`D:\助手codex\.runs\pacer-release-0.1.2-py311-smoke-v4`
- Python：3.11.9
- Codex CLI：0.144.4

裸安装未使用 `[mcp]` extra。基础安装自动解析 `mcp 1.28.1`，`pip check`、`visual-agent --version`、`pacer --version` 和安装态模块导入均通过。

## 主要变更

- `mcp>=1.0.0` 进入基础依赖，`mcp` extra 保留为空兼容项；默认安装即可启动 Pacer MCP。
- launcher 在启动 Codex 前检查当前 Python 的 MCP 可用性；缺失时以退出码 78 和明确重装提示 fail closed。
- 文档修改任务可被稳定识别为 documentation artifact，不再被包含 `compileall` 的复合要求误判为产品实现修改。
- 仅在不可变可信合同确认任务是纯文档修改时，允许用户指定的 compile-only 验收；普通实现任务仍要求 test/build/analyze，安全门槛未放宽。
- `不要修改`、`不得修改`、`do not modify` 等否定约束会先从正向动作判定中剥离，不再反向生成必须修改实现文件的合同。
- 新增发布元数据、MCP 启动前检查、任务分类和完整 MCP 闭环回归。

## 真实闭环

- v8 真实修复闭环：launch `20260714-183412-82a6c4a7`，verification `20260714-183537-92c515fe`，`completed / trust=yes / warnings=0 / can_trust=yes`，只修改 `calculator.py`。
- v8 前一 launch `20260714-183137-1520479c` 在 begin 后遇到上游 503；仓库未读写，Pacer fail closed。该外部故障没有计入成功样本。
- `0.1.2` 最终矩阵全部从 v4 wheel 的独立 Python 3.11 环境启动，样本仓库均位于 `.runs`，没有在主项目上伪造真实任务结果。

## 最终稳定性矩阵

矩阵根目录：`D:\助手codex\.runs\pacer-stability-0.1.2-20260715-v4`

| 类别 | 成功/样本 | 可信 | 警告 | 平均耗时 | 耗时范围 | 总 tokens | 平均 tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| 实现修复 | 3/3 | 3/3 | 0 | 101.906s | 85.375-132.156s | 385,048 | 128,349 |
| 新增测试 | 3/3 | 3/3 | 0 | 153.547s | 127.984-201.781s | 580,999 | 193,666 |
| 只读审查 | 3/3 | 3/3 | 0 | 100.646s | 89.266-122.703s | 289,286 | 96,429 |
| 文档修改 | 3/3 | 3/3 | 0 | 109.635s | 90.453-124.344s | 335,472 | 111,824 |
| 合计 | 12/12 | 12/12 | 0 | 116.434s | 85.375-201.781s | 1,590,805 | 132,567 |

累计 input 1,567,875、cached input 1,334,016、output 22,930；reasoning output 4,231 已包含在 output 中，不重复相加。总墙钟 1,397.203 秒。12 个最终 launch 的 HTTP/上游 5xx 精确匹配为 0。

文件范围全部符合任务：实现类只修改 `calculator.py`，文档类只修改 `README.md`，新增测试类只新增 `tests/test_validator.py`，只读类 Git 工作树保持干净。

## 发现并关闭的发布阻断

- v1 候选的文档任务 0/3 可信：合同把文档加 compileall 误判成必须修改产品实现。修复后 v3 文档任务 3/3 可信且一次完成。
- v2 候选暴露否定动作回归：`不要修改 validator.py` 被误判成实现要求。修复后 v3 新增测试任务 3/3 可信，`validator.py` 均未修改。
- v3 进一步发现 support/Dashboard 读侧会把已通过的纯文档 compile-only 批次降为 self-reported；v4 统一读写策略后，3 个最终文档样本读侧均为 `verified_batch`、`verification_batch_valid=true`。
- v1/v2/v3 的中间矩阵与 wheel 均保留为开发证据，不作为正式发布物。

## 已知限制

- 新增测试类 3/3 最终成功，但共发生 6 次完成证据拒绝；模型先使用 `modified`、Git 术语 `added` 或不准确的 unchanged 文件声明，再改为接口要求的 `created`。Pacer 均在运行验收前拒绝不准确证据，没有误报成功；这是模型提示和交互效率问题。
- 分批回归按测试设计环境合计 594 项通过；读侧一致性修复后，MCP/task review/support 关键子集 231 项再次通过。单进程全仓测试在 604 秒工具上限超时，没有被记为通过。
- compile-only 例外严格限于可信合同中的纯文档修改，不能用于实现、修复或新增测试任务。
- 本轮未运行 `tests/test_chief_queue.py`，未执行 PID/worker-lock、`os.kill`、taskkill 或进程终止探测，也未关闭现有 CMD、浏览器、Codex 或项目服务。
