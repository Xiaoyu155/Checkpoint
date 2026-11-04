# Pacer GitHub 对标与重构审计

日期：2026-07-15

## 结论

Pacer 不需要推倒重写。它真正有产品价值的是任务契约、独立验收、启动归属、工程记忆和本地优先交付；通用的 Git 变化识别、MCP 数据建模、浏览器等待、任务状态、重试和遥测不应继续自研。

本轮先完成最高收益重构：文件事实由服务端依据可信启动基线和 Git 机器协议自动生成，模型不再填写 `created/modified/deleted`。保留逐需求语义结论，但把可机械判断的事实全部收回系统。

后续采用渐进替换，不做大爆炸式迁移：

1. Git 状态与完成证据：直接采用 Git 稳定机器接口。
2. MCP 输入输出：逐工具迁移到官方 Python SDK 的类型生成与输出校验。
3. 浏览器：继续收缩到 Playwright locator、expect、trace、storage state。
4. 队列与重试：本地队列保留轻量存储，状态和重试语义对齐 Celery/Prefect；云端直接复用现有 Celery 依赖。
5. 遥测：本地 JSON 审计仍是事实源，增加可选 OpenTelemetry 导出，不再扩展私有 trace 协议。

## 源码拆分

当前 `src/visual_agent` 有 164 个 Python 模块。按职责拆成以下功能域；同一域内的模块应共享成熟底层能力，而不是各自解析 Git、状态、错误或进程输出。

| 功能域 | 模块 | 核心职责 |
|---|---|---|
| 包入口与 CLI | `__init__`, `__main__`, `cli`, `cli_chief`, `cli_cloud`, `cli_external_samples`, `cli_quality`, `cli_repair`, `cli_runner`, `cli_runtime`, `cli_verification`, `cli_workflow`, `cli_workspace`, `console`, `sdk` | 命令注册、参数适配、SDK 外观、终端输出 |
| Pacer 目标与计划 | `goal_intake`, `goal_grounding`, `planner`, `planner_generate`, `chief_engineer`, `chief_plans_store`, `mission_contract`, `mission_intake`, `mission_plan_import` | 目标澄清、不可变需求、任务拆分、计划持久化 |
| Mission 编排与队列 | `chief_run`, `chief_run_demo`, `chief_dispatch`, `chief_queue`, `chief_background`, `missions`, `mission_pipeline`, `mission_progress`, `milestone_checkpoint`, `simple_task`, `programs`, `program_scheduler`, `scheduler`, `pacer_management` | 有界执行、状态迁移、后台调度、program/autopilot |
| Agent 与执行运行时 | `agent_backends`, `agent_capabilities`, `codex_exec`, `codex_launcher`, `command_verification`, `interactive_agent`, `dynamic_model_selector`, `model_router`, `model_credentials`, `llm_providers`, `hourly_budget`, `subscription_quota`, `mimo_efficiency`, `subprocess_window`, `verification_profiles` | Agent 探测、命令构造、会话执行、模型与额度选择 |
| 上下文、记忆与归属 | `context_audit`, `context_ingestion`, `project_memory`, `repo_map`, `pacer_context`, `pacer_events`, `pacer_launch_context`, `pacer_support`, `session`, `state`, `diff_summary`, `git_diff` | 启动隔离、上下文包、增量记忆、变化摘要、事件索引 |
| 完成、验证与遥测 | `task_review`, `pacer_verification`, `codex_check`, `codex_rollout_telemetry`, `execution_alignment`, `execution_benchmark_runner`, `execution_benchmarks`, `rollout_observability`, `telemetry`, `verification_status`, `verify`, `real_acceptance`, `acceptance` | 完成证据、可信验证、成本账本、基准、最终 verdict |
| Checkpoint 工作流内核 | `workflow`, `workflow_types`, `workflow_diff`, `workflow_index`, `workflow_generator`, `workflow_synthesis`, `workflow_quality`, `validation`, `preflight`, `run_profile`, `runner`, `quality`, `dsl` | YAML/JSON 模型、执行、生成、质量、风险预检 |
| 浏览器与桌面观察 | `providers`, `browser_smoke`, `browser_smoke_suite`, `playwright_env`, `auth_state`, `dom`, `html_provider`, `capture`, `ocr`, `uia`, `vision`, `vlm`, `fixtures`, `recorder` | Playwright、DOM、UIA、OCR、VLM、截图、录制与 fixture |
| 选择与动作 | `models`, `selector`, `dispatcher`, `actions`, `capabilities` | 观察模型、目标定位、动作分发、能力声明 |
| 守卫、故障与修复 | `product_guard`, `product_issues`, `product_state`, `visual_rules`, `visual_status`, `diagnostics`, `failure_summary`, `structured_failure`, `repair`, `repair_history`, `security`, `locks` | 产品/视觉守卫、故障归因、修复与并发保护 |
| Workspace 与报告 | `workspace`, `workspace_regression`, `workspace_reports`, `workspace_risk_policy`, `audit`, `reports`, `templates`, `resources`, `external_samples`, `benchmarks`, `ci_templates`, `github_pr` | 项目空间、回归晋升、报告、模板、CI/PR 集成 |
| MCP 与集成 | `mcp_common`, `mcp_doctor`, `mcp_helpers`, `mcp_repair`, `mcp_response`, `mcp_server`, `mcp_workspace_read`, `connect`, `integrations`, `plugins` | MCP tools/resources、错误适配、外部工具集成 |
| Dashboard 与工作台 | `dashboard`, `dashboard_static_acceptance`, `gui`, `workbench_app`, `workbench_audit`, `workbench_board`, `workbench_model_config`, `portfolio_dashboard`, `portfolio_worker` | 本地控制面、任务看板、多项目视图、静态验收 |
| Cloud 与商业边界 | `cloud`, `cloud_server`, `commercial_config`, `licensing`, `db`, `notifications` | API、worker、认证/许可、存储和通知 |
| 支撑与发布 | `env`, `versioning`, `pytest_plugin`, `user_profile`, `reference_research` | 环境、版本、pytest 插件、用户设置、外部参考包 |

## 对标样本

所有仓库以浅克隆、按需读取方式保存在 `.runs/pacer-github-research-20260715`，没有进入产品源码。

| 项目 | 固定 commit | 读取重点 |
|---|---|---|
| [Aider](https://github.com/Aider-AI/aider) | `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` | `aider/repo.py`, `repomap.py`, `history.py`, coder 编辑与脏文件保护 |
| [OpenHands](https://github.com/OpenHands/OpenHands) | `a55f1ded61cac85d6e42aee9e460320ead93ae6a` | event store、sandbox、Git 边界、会话归属 |
| [SWE-agent](https://github.com/SWE-agent/SWE-agent) | `1132b3e80a45487ce8423f75d0e180874bf84caa` | patch artifact、review/retry loop、run hooks |
| [Goose](https://github.com/aaif-goose/goose) | `743609d014833abf77657f36ca0a5ba0a3ae0887` | session manager、compaction、permission router、tool monitor |
| [Cline](https://github.com/cline/cline) | `4a97b46f5f894ee4d1c31b2fa39682d0ebd1a1e5` | checkpoint diff、restore、worktree、task history |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | `2713b53b127afc094dc97d6067df9f69b647661c` | typed tools、structured output、validation、recoverable errors |
| [Playwright Python](https://github.com/microsoft/playwright-python) | `bd499b293b93a1f9e4d2667df06f9708c65f6dde` | locator/expect、event wait、trace、storage state、screenshot |
| [Robot Framework](https://github.com/robotframework/robotframework) | `90fa7f60a9eb7807282c887c54a3607422eb3288` | keyword runtime、PASS/FAIL/SKIP/NOT RUN、JSON result logger |
| [Celery](https://github.com/celery/celery) | `5ea25b51d7c74355f9da5df5453b3df42413fc61` | ready/unready/exception states、worker events、backoff+jitter |
| [Prefect](https://github.com/PrefectHQ/prefect) | `81d5e54752dfedf2043eed6beb335edbded6ba90` | flow/task engine、state、transaction、result、concurrency |
| [Git](https://github.com/git/git) | `55526a18268bbc1ddaf8a6b7850c33d984eac9e9` | `git-status` porcelain v2、`-z` 路径、rename、worktree |
| [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) | `ae8feeb7af9957434c1d68d07a1819d69213fd5d` | span context、attributes、exception/status、exporter 边界 |

## 逐域决策

| Pacer 能力 | 成熟实现结论 | 决策 | 原因 |
|---|---|---|---|
| Git 变化与完成文件状态 | Git 明确保证 `--porcelain` 跨版本稳定，并推荐 `-z` 处理任意路径；v2 原生表达 staged/unstaged/untracked/rename/unmerged | **直接采用，已实施** | 这是 Git 自己拥有的事实，模型和多套自研解析都不应拥有它 |
| 脏基线与任务归属 | Aider 会在编辑前保护脏文件；Cline checkpoint 保存 diff；SWE-agent 把 patch 作为独立 artifact | **借鉴并保留 Pacer 强化版** | Pacer 还必须区分用户启动前改动与本轮改动，单纯 `git diff HEAD` 不够 |
| 编码执行循环 | Aider、SWE-agent、OpenHands、Goose、Cline 都有成熟循环、上下文和重试 | **不引入整套框架** | Pacer 的 worker 是外部 Codex CLI；再嵌套另一套 agent loop 会重复编排并增加成本 |
| 会话恢复与记忆 | Aider 按预算压缩，Goose 区分窗口 usage 与累计 ledger，OpenHands 事件持久化，Cline checkpoint 恢复 | **已借鉴，继续收敛** | 保留原始事件与验收证据，摘要只做渐进披露 |
| 本地隔离与 worktree | OpenHands 以 sandbox 隔离，Cline 提供 worktree/checkpoint | **保留 Git worktree；可选 sandbox** | 本地 Windows 产品不能强制 Docker；分支名校验应直接调用 `git check-ref-format` |
| 队列与状态 | Celery 将 READY/UNREADY/EXCEPTION 明确分组，Prefect 把 state transition、transaction、result 分离 | **对齐语义，不引入 Prefect** | 本地单机文件队列保持轻量；云端已有 Celery extra，应直接复用 Celery |
| 重试 | Celery 使用异常白名单、指数退避、最大退避和 jitter；SWE-agent retry loop 有次数与成本上限 | **采用语义** | Pacer 只应重试可恢复错误，并同时受轮次、耗时、token 和重复失败签名约束 |
| 工作流执行 | Robot Framework 已有 keyword/result/logger；Checkpoint 有产品状态、视觉证据、浏览器/桌面融合 | **不替换内核；增加适配器** | 通用关键字可映射 Robot，Pacer 特有视觉/产品守卫仍需保留 |
| 浏览器自动化 | Playwright 已提供 locator、auto-wait、expect、trace、storage state、network/console events | **直接采用并减少包装** | 不再新增 sleep、轮询、CSS 拼接或自定义等待引擎 |
| 桌面 UIA/OCR/VLM | Playwright 不覆盖桌面；现有 `uiautomation`、`screen-ocr`、Pillow/mss 已是依赖 | **保留薄适配层** | 这里是必要的跨 provider 归一化，不是重复造通用浏览器轮子 |
| MCP | 官方 SDK 能从类型生成 schema，验证 structured output，并区分模型可修复错误和协议错误 | **下一优先级直接迁移** | 当前 `mcp_server.py` 手写巨大 JSON schema，容易出现输入/输出漂移 |
| 验收结果模型 | Robot 统一 PASS/FAIL/SKIP/NOT RUN 并在 JSON logger 记录时长和消息 | **借鉴并兼容现有 Pacer 状态** | Pacer 额外保留 timeout/not_applicable/trust，但不再让各模块发明同义状态 |
| 遥测 | OpenTelemetry 提供 span、上下文传播、属性、异常和 exporter | **增加可选 exporter** | 本地审计 JSON 是交付证据；OTel 只负责外发观察，不能反向决定任务成功 |
| 权限与确认 | Goose permission router、OpenHands sandbox、Aider 编辑确认均把执行权限与模型结论分开 | **保留 fail-closed 门禁** | 任何模型自报、5xx、timeout 或无 receipt 结果都不能转为成功 |
| Dashboard/工作台 | Cline/OpenHands 提供 task history/checkpoint UI，但产品交互差异大 | **借交互模式，不拷 UI** | 数据源必须先统一，前端只读同一 task review/event/state 投影 |
| Cloud/商业网关 | FastAPI/Celery 已在 Pacer optional dependency 中 | **使用现有依赖** | 不再开发第二套 HTTP 框架、任务 broker 或 worker 状态机 |

## 本轮重构

### 旧链路

```text
模型扫描仓库
  -> 模型填写 requirement 原文、kind、result_kind、files[path,state]
  -> 服务端逐条重新扫描并纠错
  -> 状态写错则拒绝
  -> 模型再次扫描、重提
```

问题是同一事实被模型生成、服务端重算、support 读侧再解释。新增测试样本出现的 6 次证据拒绝就是这个重复职责造成的。

### 新链路

```text
可信 launch baseline
  + git status --porcelain=v2 -z
  + git diff --name-status -z baseline..HEAD
  + 非 Git 有界指纹扫描
  -> 服务端 source change set
  -> 契约范围/受保护路径检查
  -> 服务端生成 files[path,state]

模型只提交：requirement_ids + result + verification_steps
```

实现边界：

- 新增、修改、删除由“启动时是否存在 + 当前是否存在 + 内容/索引变化”推导。
- rename 在事实层保留 `renamed_from/renamed_to`，审查层兼容表达为旧路径删除、新路径新增。
- 启动前脏文件只有再次变化才归属 Pacer；未变化不会冒充本轮成果。
- 未跟踪文件、提交后的变化、嵌套 Git 项目和非 Git 项目均走同一输出结构。
- unmerged、扫描不完整、超过 200 个变化等无法完整证明的情况 fail closed。
- 只读任务出现源码变化、测试任务改实现、文档任务改代码、命中“不得修改”路径均拒绝。
- 旧客户端仍可发送 `kind/requirement/files/result_kind`，服务端明确忽略这些事实字段并记录兼容信息。
- 拒绝返回 `pacer_completion_correction` JSON，包含错误 code、修正动作、锁定需求和服务端变化集，模型不必重新扫描仓库。

## 安装态验证

Python 3.11 隔离环境使用正式 wheel 执行真实 Codex/Pacer 闭环。前三个新增测试样本记录控制契约逐步收敛，随后用最终协议覆盖实现修改、只读分析和文档修改；所有正式样本均读取 launch manifest、history task review 和 Git 变化事实，不以终端自报代替证据。

| 样本 | 协议阶段 | 完成调用 | 墙钟 | total tokens | 服务端变化 | trust |
|---|---|---:|---:|---:|---|---|
| `test-addition-1` | 服务端文件事实，旧 launcher 模板 | 2 | 127.844s | 103,676 | `tests/test_validator.py: created` | yes |
| `test-addition-2` | 最小模板，尚未禁止额外 Git step | 2 | 111.375s | 122,036 | `tests/test_validator.py: created` | yes |
| `test-addition-3` | 最终控制协议 | 1 | 98.703s | 83,391 | `tests/test_validator.py: created` | yes |
| `implementation-1` | 最终控制协议 | 1 | 132.172s | 83,448 | `calculator.py: modified` | yes |
| `read-only-1` | 最终控制协议 | 1 | 80.047s | 84,523 | 无变化 | yes |
| `documentation-1` | v4 文档上下文修复 | 1 | 87.360s | 84,852 | `README.md: modified` | yes |

最终四类协议样本合计 4/4 approved、4/4 `trust=yes`、0 warnings、0 完成证据重提；平均 99.571 秒、84,054 total tokens。相对 Step 112 的 12 样本平均值，tokens 下降 36.6%，墙钟下降 14.5%。新增测试最终轮相对旧同类均值 193,666 tokens / 153.547 秒，分别下降约 57% / 36%，完成证据拒绝由每条 2 次降为 0。

真实文档样本还发现并修复了一个契约边界：`更新 README.md，增加“运行方式”小节` 曾在逗号拆分后把第二项误归为 implementation，造成两次 fail-closed 拒绝。v4 只让紧跟明确文档目标的“小节/章节/Usage/heading”等内容要求继承 documentation 角色；`更新 README.md，并修复 app.py` 仍保持 documentation + implementation 混合契约。compile-only 仍只对纯文档变更开放，`app.py` 继续作为 protected path 由服务端核验。

负向记录没有改写为成功：一次文档运行因测试命令漏传可写隔离参数而在只读沙箱中拒绝写入；修正启动参数后的 v3 运行又由上述契约误分类拒绝。两次均未产生可信完成报告，最终表仅采用修复后 v4 的独立 launch。

安装态候选 wheel：`.runs/pacer-github-refactor-0.1.2-20260715-v4/visual_agent-0.1.2-py3-none-any.whl`，SHA-256 `0A3AAF0DBB9CE4C726197F22D8B997F4152D511429164C75E87FCEC2588D8F11`；`pip check` 通过。

最终定向回归 391 项通过：task review + MCP 223、launcher/support/MCP response 95、Dashboard 73。触及文件 Ruff、compileall、`git diff --check` 均通过；Checkpoint 精确覆盖，`pacer_workbench_static_acceptance` 的 2 个步骤和 2 次真实交互通过，L3 strict product acceptance 1/1，最终 verdict `PASS`。

## 后续顺序

1. **MCP 类型化**：先迁移 `complete_pacer_task`、`run_pacer_verification`、`get_pacer_memory` 三个高频工具到官方 typed tool/structured output；保持 wire contract 兼容。
2. **统一 ChangeSet 消费者**：让 `git_diff.py`、`chief_dispatch.py`、Dashboard/support 都读取同一事实层，删除重复 `name-only` 扫描。
3. **队列状态与幂等**：按 Celery/Prefect 语义整理 terminal/unready/retry，增加 transition guard 和 idempotency key；本地不引入 broker。
4. **Playwright 收缩**：审计所有自定义 wait/selector/network capture，能由 Playwright locator/expect/trace 提供的直接替换。
5. **OTel 适配器**：从现有 launch/run/verification ID 建 span link；默认关闭，不影响本地证据和离线使用。
6. **真实性能矩阵**：本轮已完成新增测试三次迭代和最终协议四类各一次泛化验证，证据重提率为 0；后续保留跨时段随机重复，不再为凑固定次数无差别消耗模型额度。

## 不做的事

- 不把 Aider/OpenHands/SWE-agent/Cline 整体嵌入 Pacer。
- 不用 Prefect 替换本地 mission engine。
- 不用 Robot Framework 替换 Checkpoint 的产品状态与视觉验收。
- 不为了降低 token 放松文件归属、验证 receipt、启动绑定或 protected path 门禁。
- 不从 GitHub 复制大段实现；只采用稳定协议、公开状态语义和可验证的设计边界。

## 六阶段落地复核

- 五项统一为 `passed / failed / partial / indeterminate`，普通任务完成不再自动全绿；Memory、Routing、Managed、Acceptance、Dogfood 均从机械证据投影。
- Acceptance 使用版本化 `.pacer/acceptance.json`，分离 evidence integrity、acceptance adequacy 与 product verdict，并拒绝契约摘要漂移、路径逃逸、符号链接和 protected path 篡改。
- Memory 明确区分响应缓存、召回、相关、注入与实际使用，保持 `used ⊆ injected ⊆ retrieved`；Routing 绑定决策、请求与运行时观测，不以静态选择冒充执行。
- Managed 按 Celery/Prefect 的状态与重试语义加入 revision CAS、终态不可变、幂等键、可恢复失败白名单、full jitter 及 wall/token/attempt/repair 硬预算，接入 Mission、Chief 与 Program。
- Dogfood 和 Release 校验真实 A/B wheel、合同、验收/自检回执及外部 HMAC attestation；`.pacer/release.json` 固定 3 仓库 x 5 场景和 3 次独立 Dogfood，首败停止并拒绝重复证据或 artifact。
- MCP 高频工具改为 Pydantic v2 typed schema 与输入/输出双校验；ChangeSet 统一使用 Git porcelain v2 `-z`，浏览器等待使用 Playwright web-first 语义，可选 OTel 旁路默认关闭。

最终源码回归：Chief/Mission `181 passed`，MCP/五项 `190 passed`，新增基础模块与入口专项 `25 passed`。变更文件 Ruff、compileall、`git diff --check` 均通过；Checkpoint 精确覆盖 `.pacer/`，L3 真实交互 1/1，strict verdict `PASS`。六阶段候选 wheel 为 `.runs/pacer-six-stage-wheel-20260715-final3/visual_agent-0.1.2-py3-none-any.whl`，SHA-256 `8441AD7061992253F0E44E58D07366550F08DA7935007DEDC094A19252EF0746`；fresh venv 的 `pip check`、site-packages 导入、包内 workflow 和真实 `pacer.exe` release 命令通过。Dogfood 专项重构后的 release manifest digest 为 `aaa50981eb0ed72d2b1402303b6010828f022aef3da198ee7563dbcb5c84802a`，专项最终 wheel 见下方追加决策。

真实性边界保持不变：仓库尚无外部签名的 `.pacer/dogfood-evidence.json`，因此 Dogfood 检查继续 fail closed，不能宣称三次真实 A-to-B Dogfood 已完成，也不能宣称 release-ready。完整 Ruff 扫描另有 8 个本轮未触及的历史告警；旧 CLI fixture/dry-run 全套另有 5 个既有断言失败，均未改写为通过。

## Dogfood 专项追加决策

- 对标 GitHub Artifact Attestations、SLSA Generator、Ruff、Pants、Sigstore/Cosign 与 uv 后，HMAC-only 完整证据降为 Local 85；candidate 和 canonical evidence 的 GitHub OIDC provenance 达到 CI 95；唯一 run identity 达到 Release 100。
- `gh attestation verify` 是唯一标准 provenance verifier；Pacer 不实现 OIDC、DSSE、Sigstore 或 SLSA 密码学。
- Release streak 固定同一 candidate wheel，要求三份不同 evidence 和三个不同 run identity。候选漂移比候选重复更危险，旧“相同 wheel 算重复 artifact”的规则已移除。
- 详细 GitHub 固定链接、权重、workflow 调用合同和真实性边界见 `docs/Pacer_Dogfood_专项重构_2026-07-15.md`。
- Dogfood 专项候选 wheel 为 `.runs/pacer-dogfood-95-20260715-v2/visual_agent-0.1.2-py3-none-any.whl`，SHA-256 `E7D27FD63E22BF851D627DDDF858B0B394993CA14D40E0231EEFAD26821B923E`；fresh venv 的 policy、manifest、模块、workflow 和失败关闭检查通过。
