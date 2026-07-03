# Checkpoint 修复与开发计划

> 版本：v1（2026-06-12 制定）
> 适用范围：`src/visual_agent/` 全量代码、`docs/`、`tests/`
> 目标：在不引入屎山（spaghetti / god-module）的前提下，闭合已知安全与质量缺陷，并把产品收敛到一条可发布的主线。

本文件是**可执行的工程计划**，不是愿景文档。每个条目都包含：现状 → 方案 → 落地点（文件/函数）→ 验收标准。所有改动必须遵守第 1 节的工程原则，否则不予合并。

---

## 1. 工程原则（硬性约束，避免屎山）

这些是**合并门禁**，不是建议。Review 时逐条核对。

1. **单一数据源（Single Source of Truth）**
   同一个概念只能定义一次。例如 run-profile 的权限序当前在 `run_profile.py`、`cli.py`、`cloud_server.py` 各有一份隐含定义 —— 必须收敛到 `run_profile.py` 一处导出，其余 import。

2. **解析与执行分离（Parse, don't validate-inline）**
   外部输入（HTTP body、CLI args、YAML）先解析成**类型化对象**再进入业务逻辑，禁止 `request.get("x") or default` 这类散落取值穿透到执行层。云服务器的请求体应有一个 `CloudRunRequest` 数据类负责解析 + 校验，执行函数只接收已校验对象。

3. **模块行数红线**
   单文件 > 800 行视为需要拆分的信号；> 1500 行禁止再新增公共函数，只能拆。当前 `cli.py`(4233)、`mcp_server.py`(2244)、`workspace.py`(1871) 已越线，新功能不得继续往里堆。

4. **函数圈复杂度红线**
   单函数 ≤ 50 行、嵌套 ≤ 3 层。`cloud_server.do_GET` 这类长 dispatch 用**路由表**（dict 映射）替代 if-elif 链。

5. **错误语义明确**
   "请求非法（4xx）"、"执行失败（业务）"、"系统异常（5xx）"三类错误必须用不同类型/状态码区分，禁止全部塌缩成 `status: "failed"`。（已在 `CloudRequestError` 起步，需推广。）

6. **测试先行**
   每个缺陷修复必须先有一个**复现该缺陷的失败测试**，再改代码使其变绿。每个新公共函数必须带单元测试。

7. **无静默兜底**
   降级（如无密钥回退模板）必须在返回值里标记来源（`source: template_fallback`）并记录，禁止 catch-all 后无声吞掉。

8. **依赖方向单一**
   `security.py`、`run_profile.py`、`models.py` 是底层，不得反向 import 上层（`cli.py`、`mcp_server.py`、`cloud_server.py`）。用分层依赖防止环。

---

## 2. 缺陷修复计划

优先级：**P0** = 安全/数据风险，立即；**P1** = 正确性/体验，本迭代；**P2** = 整洁/长期。

### P0-1　云服务器内联工作流 YAML 无限堆积（已完成）
- **现状**：`cloud_server.materialize_request_workflow` 每个内联请求写 `workflows/cloud_request_<uuid>.yaml`，永不清理，长期运行塞满磁盘，也污染 `workflow_index`。
- **方案**：内联工作流写入**专用临时子目录** `workflows/.cloud_inline/`，执行结束后 `finally` 清理；该子目录纳入 `.gitignore` 与 retention。不要把临时件和用户工作流混在同一目录。
- **落地点**：`cloud_server.py`（`materialize_request_workflow` + `execute_cloud_run_request` 的 finally 清理）、`workspace.py`（索引扫描排除 `.cloud_inline/`）。
- **验收**：连续发 50 个内联请求后 `workflows/` 下不残留 `cloud_request_*`；新增 `test_cloud_server_inline_workflow_is_cleaned_up`。
- **完成记录**：内联工作流已写入 `workflows/.cloud_inline/`，`execute_cloud_run_request` 通过 `finally` 清理临时文件；`discover_workflows` 排除 `.cloud_inline/`，避免污染工作流索引。

### P0-2　云服务器默认零认证（已完成）
- **现状**：`api_key` 默认空串即放行；CLI 公开 `--host 0.0.0.0`。绑非环回地址 + 无密钥 = 局域网内任意主机可执行工作流。
- **方案**：在 `serve_cloud_server` 启动时，若 `host` 非环回（不在 `127.0.0.0/8`、`::1`、`localhost`）且既无 `api_key` 又无 `required_org`，**拒绝启动**并打印明确指引；环回绑定保持免密钥（本地开发便利）。复用 `security.py` 已有的环回判定逻辑，不要重写。
- **落地点**：`cloud_server.serve_cloud_server`、`security.py`（导出一个 `is_loopback_host(host: str) -> bool`，与 `validate_workflow_url` 共用同一判定）。
- **验收**：`serve_cloud_server(host="0.0.0.0")` 无密钥时抛 `CloudServerConfigError`；环回绑定不受影响。新增两个测试。
- **完成记录**：新增 `CloudServerConfigError` 与 `validate_cloud_server_auth_config`；非环回 host 在无 `api_key` 且无 `required_org` 时拒绝创建/启动服务，环回 host 保持本地开发免认证。

### P1-1　`run_profile` 权限序的单一数据源（已完成）
- **现状**：权限序 `dry-run < supervised < semi-auto < approved` 在 `cloud_server.RUN_PROFILE_PRIVILEGE` 是我本次新加的局部常量；`run_profile.py` 和 `cli.py` 另有 `RUN_PROFILE_CHOICES`/`SAFE_RUN_PROFILE_CHOICES`。三处各说各话。
- **方案**：在 `run_profile.py` 定义权威的 `RUN_PROFILE_ORDER`（有序）和 `run_profile_privilege(name) -> int`，并把 `RUN_PROFILE_CHOICES` 也从这里派生。`cloud_server.py`、`cli.py` 全部 import，删除本地副本。
- **落地点**：`run_profile.py`（新增导出）、`cloud_server.py`、`cli.py`。
- **验收**：全仓 `grep` 不再出现第二处硬编码的 profile 顺序；现有云服务器/CLI 测试保持绿。
- **完成记录**：`RUN_PROFILE_ORDER`、`RUN_PROFILE_CHOICES`、`SAFE_RUN_PROFILE_CHOICES` 与 `run_profile_privilege` 已集中到 `run_profile.py`；`cloud_server.py`、`cli.py`、`mcp_server.py` 均改为 import 或派生使用。

### P1-2　云请求解析层抽象（消除散落取值）（已完成）
- **现状**：`execute_cloud_run_request` 里 `request.get("workspace")`、`request.get("run_profile")`、`request.get("workflow_yaml")` 等散落取值与校验交织，是典型屎山起点。
- **方案**：定义 `@dataclass CloudRunRequest`，提供 `from_payload(payload: dict, server) -> CloudRunRequest` 类方法集中完成所有解析 + 校验（含 P0、P1 的 workspace/profile 校验），校验失败抛 `CloudRequestError`。`execute_cloud_run_request` 只接收已校验的 `CloudRunRequest`。
- **落地点**：`cloud_server.py`（把 `resolve_request_workspace`/`resolve_request_run_profile` 收编为 `CloudRunRequest.from_payload` 的内部步骤）。
- **验收**：`execute_cloud_run_request` 函数体不再出现任何 `request.get(...)`；解析逻辑 100% 单元测试覆盖（合法 + 各类非法）。
- **完成记录**：新增 `CloudRunRequest` dataclass，`from_payload` 集中解析 workspace、run_profile、workflow、source、inputs 与临时文件；`execute_cloud_run_request` 仅执行已校验请求对象。

### P1-3　`repair.py` 的 `verify=False` 语义澄清（已完成）
- **现状**：`repair.py:196` 的 `verify=False` 是"跳过工作流复跑"的内部参数名，但与 TLS 关闭的 `verify=False` 同形，易误读为安全问题。
- **方案**：参数重命名为 `rerun_verification=False`（或加显式注释说明它与 TLS 无关）；优先重命名。
- **落地点**：`repair.py` 及其调用方、`suggest_workflow_repair` 签名。
- **验收**：全仓不再有裸 `verify=False`；`grep` 安全审计无误报。
- **完成记录**：内部参数改为 `rerun_verification`；CLI/MCP 调用点已切换，新实现保留旧 `verify` 关键字兼容入口但不再产生裸 `verify=False` 误报。

### P2-1　仓库根目录文档治理（已完成）
- **现状**：根目录堆了 `CODEX_*.md`、多份中文开发日志、`*蓝图*.md` 等历史规划件，虽多数已在 `.gitignore`，但根目录视觉杂乱、新人难定位。
- **方案**：保留的移入 `docs/archive/`（该目录已存在）；纯历史的从工作树删除（git 历史仍可追溯）。根目录只留 `README.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`、`LICENSE`。
- **验收**：根目录 `*.md` ≤ 6 个；`docs/archive/` 有索引 `README.md` 说明归档内容。
- **完成记录**：历史规划、日志、审查、交接文档已迁入 `docs/archive/`；根目录保留 `README.md`、`README_MCP.md`、`CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md` 和生成状态文件 `.visual-agent-status.md`。

### P2-2　品牌与命名统一收敛（已完成：运行时环境变量层）
- **现状**：包名 `visual-agent`、产品名 `Checkpoint`、CLI 双别名、环境变量 `VISUAL_AGENT_*` 并存，长期增加心智与文档成本。
- **方案**：**不急于改包名**（破坏性大）。本阶段只做：(a) 文档统一以 `checkpoint` 为主命令；(b) 新增环境变量一律 `CHECKPOINT_*`，旧 `VISUAL_AGENT_*` 保留为 alias 并在读取处集中做映射（一个 `env.py` 兼容层），避免双名散落各文件。
- **落地点**：新增 `src/visual_agent/env.py` 集中环境变量读取；其余模块改为调用它。
- **验收**：全仓运行时代码不再直接 `os.environ.get("VISUAL_AGENT_...")`；环境变量读取经 `env.py` 统一映射；文档与帮助文本可保留旧名作为兼容说明。
- **完成记录**：新增 `src/visual_agent/env.py`，提供 `env_get`、`env_present`、`provider_api_key_env_names`；`cloud.py`、`cloud_server.py`、`cli_cloud.py`、`licensing.py`、`model_credentials.py`、`vlm.py`、`vision.py`、`telemetry.py` 已切换为集中读取。新 `CHECKPOINT_*` 优先，旧 `VISUAL_AGENT_*` 自动兜底；provider API key 支持 `CHECKPOINT_<PROVIDER>_API_KEY`、`VISUAL_AGENT_<PROVIDER>_API_KEY` 和原生 provider 环境变量。

---

## 3. 架构治理（god-module 拆分）

这是防屎山的核心工程，单独立项，**与功能开发并行但不混在同一 PR**。

### 3.1　`cli.py`（4233 行）拆分
- **问题**：`build_parser` + 巨型 `main` dispatch 把所有子命令的参数定义和执行逻辑塞在一个文件。
- **目标结构**：
  ```
  cli/
    __init__.py        # main() 入口，组装 parser + 路由表
    parser.py          # 仅参数定义（每个子命令一个 add_*_parser）
    commands/
      verify.py        # 一个子命令一个 handler 模块
      workspace.py
      cloud.py
      doctor.py
      ...
  ```
- **手法**：用**命令路由表** `COMMANDS: dict[str, Handler]` 替代 `if args.command == "...": elif ...` 长链。每个 handler 签名统一为 `(args) -> int`。
- **节奏**：每个 PR 只迁移 1–2 个子命令，保持测试绿，禁止一次性大重写。
- **进展记录（2026-06-12）**：第一刀已完成，`cloud-run-plan`、`cloud-run`、`cloud-pull-workflow`、`cloud-server` 迁入 `src/visual_agent/cli_cloud.py`；`cli.py` 仅通过 `CLOUD_COMMANDS` + `handle_cloud_command(args)` 分发，保持行为不变。
- **进展记录（2026-06-12）**：第二刀已完成，`diagnose-latest-failure`、`repair-workflow`、`auto-repair`、`repair-history`、`repair-health`、`repair-rollback` 迁入 `src/visual_agent/cli_repair.py`；`cli.py` 仅通过 `REPAIR_COMMANDS` + `handle_repair_command(args)` 分发。
- **进展记录（2026-06-12）**：第三刀已完成，`workspace-status`、`workspace-dashboard`、`workspace-list`、`workspace-validate`、`workspace-runs`、`workspace-reports`、`workspace-report-index`、`workspace-report-detail`、`workspace-report-tags` 迁入 `src/visual_agent/cli_workspace_read.py`；执行类 `workspace-run`、队列、录制、标注命令仍留在主 CLI，后续单独拆分。
- **进展记录（2026-06-12）**：第四刀已完成，`workspace-queue-submit`、`workspace-queue-list`、`workspace-queue-cancel`、`workspace-queue-retry`、`workspace-queue-run-next`、`workspace-queue-worker`、`workspace-queue-migrate-sqlite`、`workspace-queue-rollback-json` 迁入 `src/visual_agent/cli_workspace_queue.py`；主 CLI 只保留 parser 定义和命令集合分发。
- **进展记录（2026-06-12）**：第五刀已完成，执行路径 `workspace-run` 单独迁入 `src/visual_agent/cli_workspace_run.py`；preflight、inputs 校验、锁、报告导出与 markdown/json 输出语义保持不变。
- **进展记录（2026-06-12）**：第六刀已完成，`workspace-record-browser` 迁入 `src/visual_agent/cli_workspace_record.py`；主 CLI 保留 `record_browser_session` 注入点，兼容现有 monkeypatch 测试与外部调用习惯。
- **进展记录（2026-06-12）**：第七刀已完成，`external-sample*` 全部迁入 `src/visual_agent/cli_external_samples.py`；外部样例 CLI 行为与返回码保持不变。
- **进展记录（2026-06-12）**：第八刀已完成，workspace 管理类命令（报告标注、产品问题、回归 fixture/test、planner draft、template install）迁入 `src/visual_agent/cli_workspace_manage.py`。
- **进展记录（2026-06-12）**：第九刀已完成，quality/release/MCP smoke/demo 检查迁入 `src/visual_agent/cli_quality.py`，保留 `visual_agent.cli.run_release_trial` 注入点。
- **进展记录（2026-06-12）**：第十刀已完成，runtime/config/status 类命令迁入 `src/visual_agent/cli_runtime.py`；基础 runner/browser/report 命令迁入 `src/visual_agent/cli_runner.py`；verify/codex/connect 迁入 `src/visual_agent/cli_verification.py`，保留 `visual_agent.cli.run_codex_check` 注入点。
- **进展记录（2026-06-12）**：第十一刀已完成，workflow 生成/校验/发布命令与 helper 迁入 `src/visual_agent/cli_workflow.py`；`visual_agent.cli.generate_from_diff_cli_markdown` 与 `visual_agent.cli.verify_impl_cli_markdown` 保留兼容 re-export。
- **进展记录（2026-06-12）**：第十二刀已完成，workflow、quality/release、external-sample 的 parser 注册迁入对应 CLI 模块，`cli.py` 降至 1497 行，低于 1500 行禁止线。

### 3.2　`mcp_server.py`（2244 行）拆分
- 按工具域拆：`mcp/tools/workspace.py`、`mcp/tools/generate.py`、`mcp/tools/repair.py`，`mcp_server.py` 只做注册与分发。
- 工具注册改为**声明式表**（工具名 → handler + schema），新增工具不改 dispatch 主体。
- **进展记录（2026-06-12）**：第一刀已完成，通用 MCP helper（workspace 校验、预算裁剪、安全 artifact、preflight 摘要）迁入 `src/visual_agent/mcp_common.py`；只读 workspace/report payload（`list_workflows`、`validate_workflow`、`get_run_report`、`list_run_artifacts`、`get_workspace_dashboard`）迁入 `src/visual_agent/mcp_workspace_read.py`，`mcp_server.py` 保留 import re-export 与 dispatch 行为。
- **进展记录（2026-06-12）**：第二刀已完成，repair payload 迁入 `mcp_repair.py`，benchmark payload 迁入 `mcp_benchmarks.py`，browser smoke payload 迁入 `mcp_browser.py`，session/status payload 迁入 `mcp_session.py`；`mcp_server.py` 继续只做 import + dispatch。
- **进展记录（2026-06-12）**：第三刀已完成，MCP 响应裁剪与错误 payload 迁入 `src/visual_agent/mcp_response.py`，MCP 配置/profile 限制迁入 `src/visual_agent/mcp_policy.py`，审计写入迁入 `src/visual_agent/mcp_audit.py`，generation formatter 迁入 `src/visual_agent/mcp_generation_format.py`；`mcp_server.py` 降至 1480 行，低于 1500 行禁止线。

### 3.3　`workspace.py`（1871 行）拆分
- 分离 I/O（读写报告/索引）、领域逻辑（run 编排）、查询（summaries/index）。优先抽出 `report_store.py` 和 `run_index.py`。
- **进展记录（2026-06-12）**：第一刀已完成，报告导出、报告列表、报告索引、历史访问控制、报告标签迁入 `src/visual_agent/workspace_reports.py`；`workspace.py` 保留原 public API re-export，调用方无需迁移。
- **进展记录（2026-06-12）**：第二刀已完成，回归 fixture 导出、promote、回归测试运行与回归索引迁入 `src/visual_agent/workspace_regression.py`；`repair.py`、CLI 与测试继续通过 `visual_agent.workspace` 兼容入口调用。
- **进展记录（2026-06-12）**：第三刀已完成，workspace manifest、GUI action history 风险策略、auto repair policy、risk policy apply/validate 迁入 `src/visual_agent/workspace_risk_policy.py`；`workspace.py` 从 1871 行降至 705 行，低于 800 行预警线。

> 拆分原则：**行为保持不变（pure refactor），由现有 1021 个测试守门**。每个拆分 PR 必须零行为变更、测试全绿，diff 以"移动 + 重命名"为主。

---

## 4. 产品路线图（收敛优先）

战略判断：当前战线过宽（CLI + GUI + MCP + 云服务器 + 插件 + licensing + marketplace）。建议按下列阶段收敛，**先把一条主线做到惊艳**。

### 阶段 A（4 周）— 主线打磨：「AI 改完代码自动验证」
- 目标用户：用 Codex / Claude Code 的开发者。
- 关键结果：
  1. MCP 契约稳定，失败诊断结构化、可读（`structured_failure.py` 做深）。
  2. Onboarding 零摩擦：`bootstrap.ps1` 后自动跑 `doctor` + 内置 fixture 工作流，60 秒内见绿灯。
  3. 闭合本文件 P0/P1 全部缺陷。
- 暂缓：GUI 新功能、marketplace 商业化、licensing 分层。
- **进展记录（2026-06-12）**：`scripts/bootstrap.ps1` 新增 `-Step smoke`，`-Step all` 末尾自动运行 `doctor` 与 `demo-workspace-check --format markdown`；`docs/quickstart.md` 已同步说明。

### 阶段 B（4 周）— 结构化 provider 覆盖率
- 把 DOM / UIA / OCR 的命中率和稳定性做上去（VLM 仅兜底，定位正确，不追视觉模型）。
- 完成第 3 节 `cli.py`、`mcp_server.py` 拆分。

### 阶段 C（4 周）— 云服务器安全模型成熟后再谈商业化
- 强制认证、租户隔离、配额限流落地（依赖 P0-2、P1-2）。
- 此后再开 Pro / Team / Enterprise 分层；在没有远程校验前，licensing 仅作本地软门禁，不作收入依赖。

### 阶段 D（按需）— 跨平台
- 优先补 macOS / Linux 的纯 Playwright 浏览器路径（已跨平台）；UIA 桌面自动化晚一步。

---

## 5. 质量门禁与验收

合并到 `main` 必须同时满足：

| 门禁 | 标准 |
| --- | --- |
| 测试 | `pytest tests/ --ignore=tests/e2e` 全绿；新代码有对应单测 |
| 覆盖率 | 新增/改动模块行覆盖 ≥ 85% |
| 安全扫描 | 无裸 `verify=False`、`shell=True`、`eval/exec`；`/security-review` 通过 |
| 模块红线 | 不新增 > 800 行的文件；不向 > 1500 行文件加公共函数 |
| 错误语义 | 4xx / 业务失败 / 5xx 三类区分 |
| 文档 | 用户可见行为变更同步更新 README / docs |

---

## 6. 执行顺序与里程碑

```
里程碑 M1（第 1 周）：P0-1、P0-2、P1-1  —— 安全与单一数据源（已完成）
里程碑 M2（第 2 周）：P1-2、P1-3       —— 请求解析层 + 命名澄清（已完成）
里程碑 M3（第 3-4 周）：3.1 CLI 拆分起步 + 阶段 A onboarding + P2 收敛（已完成）
里程碑 M4（第 5-8 周）：3.1 CLI 拆分、3.2 MCP 拆分、3.3 `workspace.py` 拆分（已完成）；阶段 B provider 覆盖率属于后续长期路线
当前回归基线：2026-06-12 全量非 e2e 测试 1032 passed, 1 warning。
```

每个里程碑结束跑一次全量 `pytest` + `/code-review high` 作为回归闸。

---

*本计划遵循"小步重构、测试守门、解析与执行分离、单一数据源"四条主线。任何为赶进度而违反第 1 节原则的改动，应记为技术债并在下个里程碑偿还，而不是默许其留存。*
