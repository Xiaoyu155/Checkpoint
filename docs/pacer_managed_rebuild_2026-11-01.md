# Pacer 自用高可用与托管重构交付记录

开发依据：用户提供的 `pacer_codex_2026_11_01.md`。基准 `92d14f4`，开发目录 `D:\pacer-recovery\20261101\worktree\xiao`。全程直接编辑和原生 pytest；运行测试时设置 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`。

## 阶段交付

| 阶段 | 交付行为 | 本阶段验收 |
|---|---|---|
| Phase 1 · `0c92e3d` | 验证子进程默认隔离 pytest 插件；失败输出最后 80 行；指定文件摘要与历史工作区变更数分开显示 | 19 passed |
| Phase 2 · `6cfbd53` | 工作区及配置目录 OS 排他锁 + PID 标记；任务终态撤销补丁写权限；WorkBuddy 禁止直接写盘；禁止名单外新文件；去掉非原子写入降级 | 62 passed |
| Phase 3 · `5708543` | `PACER_API_POOL` / `PACER_API_MODEL`；节点故障冷却和立即切换；Gemini coder 使用完整实际提示词 | 54 passed（包含相邻 API 适配器回归） |
| Phase 4 · `60b1541` | 保存并校验 SHA-256；合并 coder 目标文件检查；丢失文件、同长度篡改和旧的无哈希记录均拒绝跳过；本地扫描输出真实 AST 和 subprocess 调用点 | 6 passed, 28 deselected |
| Phase 5 · 本文所在提交 | `auto --worktree` 建立隔离分支，默认本地规划；通过真实验证命令才自动提交；失败或中断保留工作树；GC 可识别新目录 | 核心 90 passed；相邻 86 passed |

Phase 5 的真实 Git 集成测试覆盖成功和失败两条路径：模拟 API writer，运行真实 pytest，检查主工作区运行期间及运行后的 SHA-256、HEAD、原有未跟踪文件，确认只有隔离分支包含通过验收的提交。集成测试同时发现并修复了基线探针缺失 `os` 导入的问题。

收尾进一步关闭了 Codex writer 的直接写权限（只读 sandbox），Gemini CLI 不启用 yolo；网关关闭时持有实例锁直到当前执行任务结束。

## 使用恢复工作树中的代码

本机原有安装可能仍指向旧源码。启动当前实现前，在 PowerShell 会话设置源码路径：

```powershell
Set-Location D:\pacer-recovery\20261101\worktree\xiao
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:PACER_CODER_CASCADE = 'api'
# 按供应商账户实际可用的模型替换 YOUR_MODEL；密钥通过对应供应商环境变量提供。
$env:PACER_API_POOL = 'deepseek:YOUR_MODEL,qwen:YOUR_MODEL'
python -m visual_agent.cli auto '修复目标文件中的具体问题' --worktree --verify-cmd 'python -m pytest tests/test_target.py -q'
```

单节点可设置 `PACER_API_MODEL=provider:model`。多个自定义端点可用 JSON 配置；密钥值通过环境变量引用，不写进 JSON：

```json
[
  {"provider":"openai","model":"YOUR_MODEL","base_url":"https://YOUR_GATEWAY/v1","api_key_env":"PRIMARY_TEST_API_KEY"},
  {"provider":"openai","model":"YOUR_BACKUP_MODEL","base_url":"https://YOUR_BACKUP_GATEWAY/v1","api_key_env":"BACKUP_TEST_API_KEY"}
]
```

配置池中的鉴权错误、429、502/503/504、529 和传输故障触发节点冷却；不插入等待。测试证明模拟 429 后切换的调度开销小于一秒，真实网络请求耗时仍由上游响应及超时决定。池内节点只能访问其配置的端点；未配置池时沿用原有供应商配置。

`--worktree` 从 **已提交 HEAD** 创建新分支，不复制主工作区未提交内容。应用位于仓库子目录时保留其相对路径。失败分支保留，便于审查；已有隔离任务使用其 `--workspace-root` 配合 `--resume` 继续，不重复指定 `--worktree`。自动提交后只输出审阅与合并命令，不自动合并。

本地静态输出标为 `index_only / local_static_only`，展示函数/类数量、文件哈希、subprocess 调用行号，以及是否直接传入窗口隐藏参数。这是语法和调用点证据，不证明运行时窗口行为，也不覆盖所有动态包装调用。

## 最终原生命令与真实结果

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_subagent_orchestrator.py tests/test_diff_patcher.py tests/test_pacer_model.py tests/test_cli.py -v --tb=short --junitxml=D:/pacer-recovery/20261101/final-core-results.xml
```

```text
90 passed in 33.16s
```

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
python -m pytest tests/test_api_cascade_recovery.py tests/test_llm_providers.py tests/test_acceptance_discrimination.py tests/test_worktree_gc.py tests/test_model_health_and_checkpoint.py tests/test_stateless_canvas_engine.py tests/test_subprocess_window.py -q --tb=short --junitxml=D:/pacer-recovery/20261101/final-adjacent-results.xml
```

```text
86 passed in 22.23s
```

## 真实密钥测试边界

初次五阶段验收时，仅在内存中读取用户提供的外援凭据，尝试一次带鉴权的 `/v1/models` 列表请求。返回 **HTTP 403** 后停止，未继续付费生成。当时没有证明上游生成能力。

用户随后明确这些是 Sub2API 密钥，追加定向诊断证实：同一个 Key 的模型列表请求使用 Python 默认 User-Agent 返回 Cloudflare `1010`，加入明确的客户端标识后返回 200。两个 Key 随后分别通过 Claude 原生 `/v1/messages` 返回 `OK`，其中第二个通过修复后的 Pacer `run_llm_completion` 适配器调用。每个 Key 只做了一次生成测试，输出上限 16 token；第一个实际计量为输入 6、输出 1 token。没有输出、持久化或提交密钥，也没有启用日常自动付费调用。详细记录见 `sub2api_connection_test_2026-11-01.md`。

服务锁使用 OS 排他锁作为权威，PID 文件用于诊断；进程退出后 OS 自动释放锁，遗留 PID 不会永久阻塞启动。终态租约保护经编排器提交的补丁，不是对整台机器其他程序的写入沙箱。外部命令和现有进程管理方式仍需按用户自身环境运行。

## 第二轮：可观测性尾项 + Phase 6–9 收口（2026-11-01 续）

### 运行环境更正（重要）

本仓库唯一可用的解释器是 **`C:\Users\xiaoyu\AppData\Local\Programs\Python\Python311\python.exe`**（Python 3.11，已 `pip install -e .` 且装有 `portalocker 2.10.1`）。

系统默认 `python` 是 3.13.14，几乎没有第三方包。**用默认解释器跑 pytest 会产生 17 个假失败**（`ModuleNotFoundError: No module named 'portalocker'`），全部集中在 `test_pacer_model.py`（14 个）与 `test_cli.py`（3 个）。本轮所有验收均使用 3.11：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
& 'C:\Users\xiaoyu\AppData\Local\Programs\Python\Python311\python.exe' -m pytest ...
```

### 基准核对（与规格书不一致，已按真实状态处理）

规格书写明基准提交 `30eda03`。**接手时工作树 HEAD 实为 `877e0b7`**：Phase 6（`0c81b37`）、Phase 7（`ae37e57`）、Phase 8（`877e0b7`）已提交，工作区另有 Phase 9 的未提交改动。

逐条比对真实源码后确认 Phase 6/7/8 是**完整实现而非桩代码**，因此采取「补残 + 验收」而非重写的收口方式。

### 本轮交付

| 提交 | 内容 | 验收 |
|---|---|---|
| `5ee5052` | 可观测性尾项（规格书第一阶段的两个尾项） | 涉及套件 121 passed；全量 15 failed / 988 passed |
| `9f1dfae` | Phase 9 统一 `providers.json` 模板 + 热重载 + 无密钥状态输出 | 121 passed |
| `a2a259b` | Phase 8 残留：海外 VPS 账号池为空时零秒回退（不等待超时） | 涉及套件 128 passed；全量 15 failed / 995 passed |

### 尾项 ① — `model_health.py`：巡检必须与实际调用同源

| 问题 | 修复 |
|---|---|
| Codex `--version` 退出码非 0 仍报 `degraded`（二进制存在 ≠ 可用） | 非 0 退出即 `offline`；输出含 auth 线索时 `authenticated=False` |
| Grok 探针用 `Pacer-HealthCheck/1.0`，而真实调用 `invoke_grok` **完全不发 User-Agent** | 两者共用 `GROK_USER_AGENT = "Pacer/1.0"`；探针 HTTP 200 只报 `degraded`（生成未验证），401/403 报 `offline` + `authenticated=False` |
| 401 靠字符串匹配 `"401" in err_msg` | 改读 `HTTPError.code`；异常路径回退扫描已知状态码 |
| 聚合巡检超时后**直接丢弃**慢探针 | 超时作为总截止时间下发到每个探针，超时项报 `degraded` / `error="Timeout"` |
| 探针与实际调用配置可能不同源 | 探针与生成路径共用 `get_grok_api_key()` / `get_grok_api_base()` |
| VPS `available_accounts=0` | 已确认报 `degraded` + `authenticated=False`，不报 online |

Grok 的 User-Agent 缺失是一个**真实故障**：探针通过但每一次真实请求都被 Cloudflare 拒绝，正是「巡检说在线、实际调用失败」的活实例。

### 尾项 ② — `pacer_trace.py`：去掉无依据的 8500/1200 固定常数

旧实现把每个任务与两个**凭空设定的常数**（8,500「传统」token vs 1,200「Pacer」token）作比较，并在**零条 trace 时仍返回 94.5% 的节省率**——失败的任务也因此显示成「省了钱」。

新实现只报告实际观测到的量：

- trace 新增 `prompt_tokens` / `model_calls` / `usage_measurement`（`measured` | `unknown`）；
- `savings_pct` / `total_tokens_saved` / `estimated_usd_saved` 在**没有记录到实测基线**时一律为 `None`；
- 无样本 = 「无数据」，不是一个数字；
- `usage_measurement` 在没有观测值时自动降级为 `unknown`，杜绝「声称已测量但无证据」；
- 编排器从 `_prompt_dispatches` 汇总真实提示词 token 与真实调用次数，审查器调用也计为一次模型调用。

### 顺带关闭的同区域可观测性缺陷

`dashboard.py` 三处：把记录的 `t.goal` 拼进 `innerHTML` 造成**存储型 XSS**（改为 DOM API + `textContent`）；`verification_verdict === 'pass'` 时把**失败任务显示为 PASSED**（改为要求 `status === 'success'`）；以及凭空显示 `3.2s` 耗时（缺失时显示 `—`）。空指标显示 `—` 而非 `null%`。

### Phase 6–9 逐条验收结论（对照真实源码）

| 阶段 | 规格要求 | 真实源码核对结论 |
|---|---|---|
| Phase 6 | 本机反重力 Gemini 3.8 HTTP 直连优先、CLI 兜底 | ✅ `invoke_gemini38` 端口可达走 HTTP（`User-Agent: Pacer-Client/1.0`，anthropic/openai 双协议），端口不通回退 `invoke_gemini(yolo=False)`；排除自身端口 8091；模型取 `PACER_GEMINI38_FALLBACK_MODEL` 或 `gemini-3.8-flash-low` |
| Phase 7 | 四级成本阶梯、失败自动升档 Sub2API Codex | ✅ `cost_candidates(retries, complexity)`：首轮免费优先（gemini38→workbuddy→domestic→premium），`retries>=1` 或 L3 反转为 premium 优先；`PACER_CODER_CASCADE` 可显式覆盖；含熔断冷却 |
| Phase 8 | Code 窗口去阻塞 + 热重载 + 协作轨迹 | ✅ 规划未配 Key 时 0 秒走本地规则；审查不可用时保留 `result["success"]=True` 并记 `skipped_unconfigured_or_quota`（仅 Gemini 4 明确否决才判失败）；`/models`、`/status`、`模型状态` 免重启热读 `providers.json`；结束汇报输出「协作轨迹」与 API 请求次数 |
| Phase 9 | 统一 `providers.json` 模板 + `status` 全景 | ✅ `ensure_template()` 以独占创建（`open("x")`）落模板、绝不覆盖用户数据、权限 600；`load_providers()` 校验各层结构；`status` 输出 PID、端口与 Tier 1–4 就绪表且**不打印任何密钥** |

### Phase 8 残留修复：海外 VPS 账号池为空时的零秒回退

规格书 8.3.1 明确要求规划阶段「先检查……（或健康且 `available_accounts > 0` 的网关）」，否则「白白等待超时才回退」。

**核对发现该前置检查并未实现**：`is_gemini4_vps_available()` 只看配置里有没有 base_url 与 api_key，不看账号池；而 Gemini 4 的默认 base_url 正是海外反代池 `https://direct.aiboxz.com/gemini4`，池空时规划会先发起一次注定失败的调用再回退。

修复：

- 新增 `gemini4_vps_client.gemini4_pool_state()` / `is_gemini4_vps_pool_healthy()`，探测 `/gemini4/health` 读取 `available_accounts`，短超时并短暂缓存；
- `provider_runtime.architect_node()` 在该节点确实指向反代池且池为空（或网关不可达）时追加 `empty_account_pool` 阻断项，使 `is_available()` 为假；
- 于是规划**立即**走本地规则拆解、审查立即记 `skipped_unconfigured_or_quota`，全程零等待；
- 非反代池地址（例如直连 Google API Key）**不受**该闸门影响；已禁用的槽位**完全不会**触发网络探测。

性能：两个网关别名**并行**探测（最坏耗时为一次超时而非两次），结果缓存 30 秒。

### 全量测试耗时说明

同一台机器上全量用例耗时在 5m28s ~ 13m24s 之间波动。经 A/B 对照确认与本轮改动**无关**：

| 配置 | 全量耗时 | 结果 |
|---|---|---|
| HEAD（不含账号池闸门） | 791.20s | 15 failed / 988 passed |
| 含账号池闸门 | 804.60s | 15 failed / 995 passed |

差异 13.4s（1.7%，噪声量级），失败集合逐条比对完全相同。波动来自机器上的常驻负载（QQ、Chrome、v2rayN/sing-box 代理），并非代码。同一子集单独 A/B：HEAD 52.85s vs 含闸门 59.85s。

### 全量测试的既有失败（非本轮引入）

全量结果 **15 failed / 995 passed**（含本轮新增用例）。这 15 项在**基线（stash 掉本轮全部改动后）同样失败**，已逐条核对为既有问题，本轮未引入、未修复：

`test_codex_launcher.py::test_launch_record_atomic_writes_use_unique_temporary_files`、`test_exam_driven_handoff.py::test_orchestrator_acceptance_exam_gate_rejection_and_escalation`、`test_gemini_bridge.py::test_gemini_availability_check`、`test_model_admin.py::test_bundled_extension_is_v2_and_has_no_embedded_password`、`test_model_catalog.py::test_gemini4_workflow_assigns_real_worker`、`test_model_catalog.py::test_review_fails_closed_instead_of_treating_any_text_as_approval`、`test_orchestrator_exam_spec.py::test_same_failure_stops_after_two_coder_calls`、`test_pacer_collaboration_baseline.py::test_grok_failed_acceptance_escalates_to_codex`、`test_pacer_feasibility_exam.py::TestPacerFeasibilityExam::test_q04_self_healing_escalation_and_workbuddy_file_materialization`、`test_phase3_exam.py::test_planner_failure_does_not_become_a_local_plan`、`test_phase3_exam.py::test_generic_planner_receives_local_repo_map_and_ignores_secret_paths`、`test_phase3_exam.py::test_gemini38_replans_once_after_unsafe_acceptance_command`、`test_stateless_canvas_engine.py::StatelessCanvasEngineTestSuite::test_04_orchestrator_stateless_closed_loop_under_3_seconds`、`test_witness_verifier.py::test_orchestrator_witness_gate_rejects_trivial_assert_in_test_patch`、`test_witness_verifier.py::test_orchestrator_keeps_real_diff_when_existing_suite_is_blind`。

其中 `test_phase3_exam.py` 的三项根因已定位：`plan_with_gemini4` 实际调用的是 `provider_runtime.invoke_architect`，而测试 monkeypatch 的是 `gemini4_vps_client.invoke_gemini4_vps`，**补丁打在了不被调用的符号上**。本轮未改动这些语义，留待专项处理。

另有 4 个**收集错误**（非失败）：`test_browser_binding_api.py`、`test_gemini4_gateway.py`、`test_gemini_session_recovery.py`、`test_token_exchange_security.py`——它们依赖本工作树中不存在的 `vps_gemini4_gateway/` 与 `examples/pacer_token_exchange/`（VPS 网关是独立部署）。基线同样如此。验收命令统一加 `--ignore` 排除这 4 个文件。
