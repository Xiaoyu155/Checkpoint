# Pacer 开发总计划（本文档是给 Codex 的执行规范，不是参考建议）

制定时间：2026-07-09
制定依据：`docs/Pacer狗粮开发审查报告.md`、`docs/pacer_dogfood_review_report.md`、对 `src/visual_agent` 核心闭环的逐行审查（chief_run / chief_dispatch / command_verification / chief_background / mission_progress）。

**给 Codex 的第一条指令：本文档中所有"必须 / 禁止 / 固定"字样都是硬性约束。遇到本文档没有覆盖的决策点，停下来写入 blocker 报告，不要自行发挥。**

---

## 0. 硬性纪律（先读，违反任何一条即视为任务失败）

1. **禁止引入任何新依赖**。`pyproject.toml` 的 dependencies 保持现状：`mss`、`Pillow`、`portalocker`、`pyautogui`、`PyYAML`（可选 `playwright`）。所有新代码只用 Python 3.10+ 标准库。
2. **禁止触碰冻结目录**（见第 2 节冻结清单）。哪怕看到明显的 bug 也不修，记录到 `docs/冻结区待办.md` 即可。
3. **禁止修改任何测试的断言来让测试变绿**。测试失败说明代码错了，改代码。
4. **禁止重构与任务无关的代码**。每个任务只改它指定的文件，diff 越小越好。
5. **一个任务一个 commit**。commit message 格式：`[P{阶段}.{任务号}] 一句话说明`，例如 `[P1.1] worker 未完成时不再判 verified`。做完一个任务、跑完该任务的验收命令、commit，然后才开始下一个任务。
6. **每个任务做完必须跑该任务指定的验收命令**，并把输出摘要（passed 数量）写进 commit message 正文。
7. 所有新 `subprocess` 调用必须：`encoding="utf-8", errors="replace"`，git 命令加 `-c core.quotePath=false`，需要隐藏窗口的用 `subprocess_window.hidden_subprocess_kwargs()`。
8. 所有新文件读写必须显式 `encoding="utf-8"`。
9. 平台基准是 **Windows 11**。任何路径处理都要能吃中文路径与反斜杠。
10. 任务按顺序执行，**禁止跳序**。Phase 1 没全部完成前不许动 Phase 2。

---

## 1. 产品定位（背景，用于理解，不是任务）

Pacer = 托管开发调度层：把一个开发目标派发给隔离 worktree 里的 coding worker（默认 Codex CLI），用用户提供的测试命令做验收门禁，通过后受控合并回主仓库，全程留证据链。

- DevPacer 是 mission/编排层；Checkpoint 是验证引擎；Codex 是默认执行 worker。
- 核心价值排序：**不误报 verified > 不污染用户仓库 > 证据可读 > 省额度**。省额度排最后，前三条不成立时省额度是伪命题。
- 当前阶段定位是**有监督托管**，不是无人值守。所有设计按这个定位收口。

## 2. 技术栈约定（固定）

| 层 | 固定选型 | 说明 |
| --- | --- | --- |
| 语言 | Python 3.10+，纯标准库 | 不加依赖，不用 asyncio 重写现有同步代码 |
| 测试 | pytest（现有 121 个测试文件的既有风格：`tmp_path` + 真实 git 仓库 fixture） | 新测试模仿 `tests/test_chief_dispatch.py` 的既有写法 |
| 持久化 | JSON 文件（`.agent-workspace/missions/<id>/`） | 禁止引入 SQLite 以外的任何数据库；现有 json 结构只增字段不删字段 |
| Dashboard 前端 | 原生 JS + 现有 `static/app.js` | 禁止引入任何前端框架、打包器、npm 依赖 |
| 进程管理 | `subprocess` + `taskkill /T /F`（Windows） | 不引入 psutil |
| worker 通道 | Codex CLI（主）、MiMo patch 模式（低成本 failover） | 不新增 worker 类型 |

### 冻结清单（本计划期间禁止修改的目录/文件）

```
cloud_api/            vscode-extension/       src/visual_agent/marketplace 相关
src/visual_agent/licensing.py                 src/visual_agent/cloud*.py
src/visual_agent/ocr.py  vision.py  vlm.py    src/visual_agent/browser_smoke*.py
src/visual_agent/recorder.py                  src/visual_agent/benchmarks.py
templates/  examples/  workflows/             src/visual_agent/telemetry.py
```

理由：信任内核（dispatch→verify→merge）没有连续零误报之前，外围功能只会放大错误状态。

---

## 3. 阶段总览

| 阶段 | 主题 | 任务数 | 完成标志 |
| --- | --- | --- | --- |
| P0 | 仓库卫生 | 3 | 工作区干净、修复有 commit 粒度 |
| P1 | 信任内核：不误报 verified | 4 | 指定测试全绿 + 案例 B 复演不再误 merge |
| P2 | 状态可信：不说谎的进度 | 4 | 案例 A 复演全程 stage/activity 准确 |
| P3 | preflight：派发前拦截浪费 | 3 | 缺环境任务在 60 秒内 preflight_blocked |
| P4 | 报告与工作台收口 | 3 | final report 三段式、dashboard 显示 activity |
| P5 | 跨项目 dogfood 验证 | 持续 | 3 个案例项目连续 3 轮零误报 |

---

## Phase 0：仓库卫生（半天）

### P0.1 提交现有未提交修复

当前工作区有 78 处未提交变更，其中包含狗粮报告声称的全部修复。逐模块分组提交（不要一个大 commit）：

1. `git add` 按主题分组：中文路径修复一组、tamper guard 扩展一组、verification_environment_missing 一组、post-merge 复验一组、dashboard 一组。
2. 每组 commit 前跑：`python -m pytest tests/test_test_tamper_guard.py tests/test_mission_progress.py tests/test_chief_run.py tests/test_repo_map.py tests/test_chief_dispatch.py tests/test_command_verification.py tests/test_diff_summary.py -q`（当前基线：149 passed，提交过程中不得低于此数）。

### P0.2 清理仓库根目录污染

- 删除或移入 `.gitignore`：`tmp_bugteam_workspace/`、`tmp_intake_probe/`、`__pycache__/`、`runs/`、`artifacts/`（如含需要保留的证据，移到 `.agent-workspace/`）。
- 根目录的 `强制测试记录.md` 按报告已定的规则移到 `.agent-workspace/missions/<id>/` 下，根目录不留。
- 确认 `model_api_keys.txt` 保持 gitignore（已确认 ignored，不动内容，只确认）。

### P0.3 建立本计划的追踪文件

新建 `docs/开发进度.md`：一行一个任务（P0.1 ~ P4.3），状态列（未开始/进行中/完成+commit hash）。每完成一个任务更新此文件并纳入该任务的 commit。

---

## Phase 1：信任内核（最高优先级，预计 3~4 天）

### P1.1 worker 未完成时禁止判 verified

**问题**：`src/visual_agent/chief_dispatch.py` 中 `existing_verified_change`（约 649-656 行）与 `elif existing_verified_change: status = "verified"`（约 681-685 行）允许 worker 失败/崩溃/quota 中断时，只要 worktree 有产品改动（可能是半成品）或该 mission 曾经 verified 过，且测试碰巧通过，就判 verified 并可自动 merge。

**改法（精确执行，不要变通）**：

1. 删除 `existing_verified_change` 判 verified 的分支。
2. 新增终态 `worker_failed_tests_pass`：条件 = `not worker_completed and latest_verdict == "pass" and has_product_changes`。该状态：不可 merge、`needs_attention=True`、用户文案说明"worker 未正常完成，但现有改动通过了测试命令；请人工检查 worktree 后用 chief-merge 手动合并"。
3. `has_prior_verified` 只允许用于**重跑验证**场景（`mission_intake` 判定目标为"复验/继续"类时），不允许作为新开发任务的 verified 依据。实现：给 `dispatch_chief_plan` 加参数 `allow_prior_verified_evidence: bool = False`，只有 chief_run 的 resume-verified 路径显式传 True。
4. `chief_run.py::_stop_reason_from_dispatch` 增加 `worker_failed_tests_pass` → stop_reason `worker_failed_tests_pass`；`_message_for_stop` 增加对应中文文案。
5. `mission_progress.py` 的 `_stage_label`、`_stage_message`、`_needs_attention`、`_blocker` 同步认识这个状态。

**必写测试**（加到 `tests/test_chief_dispatch.py`）：

- `test_worker_failed_with_passing_tests_is_not_verified`：构造 worker record status="failed" + 命令验证 pass + worktree 有产品改动 → 断言 status == "worker_failed_tests_pass" 且 payload 无 merge 或 merge["status"] == "skipped"。
- `test_prior_verified_evidence_does_not_verify_new_work`：prior verified 存在 + 本轮 worker failed + 无新产品改动 → 断言 status != "verified"。
- `test_worker_completed_with_pass_and_changes_still_verified`：回归保护，正常路径仍是 verified。

**验收命令**：`python -m pytest tests/test_chief_dispatch.py tests/test_chief_run.py -q`

### P1.2 失败分类器分层重写

**问题**：`src/visual_agent/command_verification.py::classify_command_failure` 用全输出模糊字符串匹配，两个方向都会误判：

- `"no such file or directory"`、`"the term "`、`"enoent"` 在正常测试失败输出中极常见，会把**该修的代码失败**误判成 `test_command_invalid`（不可修复，直接停）。
- `_ENVIRONMENT_MISSING_MARKERS` 全是抖音快手项目的专属字符串，换项目就失效。

**改法（分三层，按顺序判定）**：

```
第 1 层 launch 层（最可信）：命令根本没启动
  - OSError → command_launch_error（现状保留）
  - exit code ∈ {127, 9009} → test_command_invalid
  - 输出前 10 行（且仅前 10 行）匹配 shell 级报错才算 invalid：
    "is not recognized as an internal or external command"
    "not recognized as the name of a cmdlet"
    "missing script:"
  - 删除 "the term "、"no such file or directory"、"enoent"、
    "cannot find the path"、"could not find a part of the path" 这五个全文匹配。

第 2 层 声明层（用户契约，新增）：
  - mission.json 新增可选字段 verification_env（list[dict]）：
    [{"kind": "env_var", "name": "QWEN_API_KEY"},
     {"kind": "marker", "pattern": "external ai judge missing"}]
  - env_var 类：跑命令前检查 os.environ，缺失 → 直接
    verification_environment_missing，命令都不用跑（省时间省额度）。
  - marker 类：失败输出匹配用户声明的 pattern → verification_environment_missing。
  - CLI 入口 chief-run 新增 --require-env NAME（可重复）写入该字段。

第 3 层 兜底层（现有硬编码 markers 降级保留）：
  - 现 _ENVIRONMENT_MISSING_MARKERS 保留，但命中时结果记为
    failure_kind="verification_environment_missing" 且新增字段
    classification_confidence="heuristic"（第 1、2 层命中的记 "definitive"）。
  - heuristic 级分类在报告里必须标注"启发式判定，建议人工确认"。
```

其余全部归 `command_failed`（可修复）。

**必写测试**（加到 `tests/test_command_verification.py`）：

- `test_enoent_in_test_output_is_repairable_code_failure`：输出含 `Error: ENOENT: no such file or directory, open 'fixtures/a.json'`、exit 1 → 断言 kind == "command_failed"。
- `test_powershell_the_term_in_assertion_text_not_invalid`：输出正文含 "the term used in ..."、exit 1 → command_failed。
- `test_exit_9009_is_invalid`、`test_shell_error_in_head_is_invalid`：launch 层仍然工作。
- `test_declared_env_var_missing_blocks_before_run`：声明 QWEN_API_KEY、环境未设 → verification_environment_missing 且 `exit_code is None`（证明没跑命令）。
- `test_declared_marker_match`、`test_legacy_marker_is_heuristic_confidence`。

**验收命令**：`python -m pytest tests/test_command_verification.py tests/test_chief_dispatch.py tests/test_chief_run.py -q`

### P1.3 验收链防篡改扩展到命令定义文件

**问题**：tamper guard 只看测试文件。worker 改 `package.json` 的 `"test"` script、改 `Makefile`、加 pytest 插件配置，也能让门禁假绿。

**改法**：

1. `command_verification.py` 新增 `acceptance_chain_files(repo_root, command) -> list[str]`：返回定义了验收命令的文件。规则（不要做更聪明的推断）：
   - 命令含 `npm ` / `pnpm ` / `yarn ` → `package.json`
   - 命令含 `pytest` / `python -m pytest` → `pyproject.toml`、`pytest.ini`、`setup.cfg`、`tox.ini`、根级 `conftest.py`（存在的才列入）
   - 命令含 `make ` → `Makefile`
   - 命令含 `cargo ` → `Cargo.toml`
2. `changed_test_files` 同款逻辑新增 `changed_acceptance_chain_files(repo_root, command, base_ref)`。
3. dispatch 验证前检查：命中 → `repair_brief.source = "acceptance_chain_tampering"`，不可修复（加入 `_verification_is_repairable` 的拒绝列表），repair prompt 要求 revert 这些文件。
4. 例外：worker 任务目标本身就是改这些文件时（`allow_test_edits=True` 已有的开关同样覆盖此检查）。

**必写测试**（新建 `tests/test_acceptance_chain_guard.py`）：

- `test_npm_command_flags_package_json_change`
- `test_pytest_command_flags_conftest_change`
- `test_unrelated_file_change_not_flagged`
- `test_allow_test_edits_bypasses_chain_guard`

**验收命令**：`python -m pytest tests/test_acceptance_chain_guard.py tests/test_test_tamper_guard.py tests/test_chief_dispatch.py -q`

### P1.4 merge 门禁收口

**问题**：a) `merge_worktree_branch`（`chief_dispatch.py` 约 2467-2477 行）无条件把 `.gitignore` 从暂存区剔除，用户任务若本来就要改 `.gitignore`，改动静默丢失还报 verified；b) 自动 merge 缺少明确的授权语义。

**改法**：

1. `.gitignore` 处理：用已有的 `_gitignore_change_is_only_devpacer_block()` 判断——只有当 worktree 的 `.gitignore` 变更**仅为 Pacer 自己写入的块**时才剔除；含用户内容的变更保留在提交里。
2. merge 语义固定为三档，CLI 与 dashboard 一致：
   - 默认（无 `--merge`）：verified 后停在 worktree，final report 打印推荐的合并命令 + post-merge 验证命令。
   - `--merge`：仅当 `status == "verified"` 且 `worker_completed` 为真才合并（P1.1 已保证），合并后必跑 post-merge verification（现有逻辑保留）。
   - `worker_failed_tests_pass` / `verified_blocked` / 任何其它状态：`--merge` 也不合并，reason 写清楚。
3. post-merge 失败时（现有 `merged_verification_failed`），final report 必须包含：合并产生的 commit hash、`git revert -m 1 <hash>` 的回滚建议命令。不要自动 revert。

**必写测试**（加到 `tests/test_chief_dispatch.py`）：

- `test_merge_keeps_user_gitignore_changes`：worktree 中 `.gitignore` 增加用户行 → merge 后主仓库 `.gitignore` 包含该行。
- `test_merge_drops_pacer_only_gitignore_block`。
- `test_merge_refused_for_worker_failed_tests_pass`。
- `test_post_merge_failure_report_contains_revert_hint`。

**验收命令**：`python -m pytest tests/test_chief_dispatch.py -q`，然后全量：`python -m pytest tests -q`（Phase 1 结束门槛：全量测试通过，数量 ≥ 基线）。

---

## Phase 2：状态可信（预计 2~3 天）

### P2.1 终态保护

**问题**：`mission_progress.py::record_worker_output` 无条件把 stage 重置为 `worker_running` 并清空 `stop_reason`/`blocker`。终态写入后如果又飘来一段迟到的 worker 输出，状态被冲回 running。

**改法**：定义终态集合 `_TERMINAL_STAGES = {"verified", "verified_blocked", "blocked", "post_merge_verification_failed", ...}`（放模块级常量）。`record_worker_output` 在锁内先读现有 progress，若 `stage ∈ _TERMINAL_STAGES` 则只追加 `last_output_*` 字段，不改 stage/status/blocker。

**必写测试**（`tests/test_mission_progress.py`）：`test_late_worker_output_does_not_reset_terminal_stage`。

### P2.2 activity 改为执行侧主动上报，关键词推断只做兜底

**问题**：`_infer_activity` 里 `node_modules` 关键词优先于 `tests_running` 判定，而测试失败堆栈几乎必然含 node_modules 路径 → 测试挂掉时界面显示"Installing dependencies"。

**改法**：

1. 新增主动上报：在以下位置直接调用 `save_mission_progress(..., activity=..., activity_command=..., activity_started_at=...)`：
   - `run_command_verification` 开跑前 → `activity="verification"`，结束后清除。
   - `_run_worker_attempt` 启动 worker 前 → `activity="worker_executing"`。
   - `_run_mimo_patch_attempt` 同上。
2. `_infer_activity` 改为：saved 里有未过期（10 分钟内）的主动上报 activity 时直接采用；否则才走关键词兜底。
3. 兜底关键词修正：`tests_running` 判定优先于 `dependency_install`；`dependency_install` 的 marker 收紧为行首/命令形态（`npm ci`、`npm install`、`silly tarball`、`extracting by manifest`），删除裸 `node_modules`；删除裸 `"test "`。
4. progress 增加字段：`activity_command`（当前命令原文）、`activity_started_at`、`activity_elapsed_seconds`（build 时计算）。

**必写测试**：`test_stack_trace_with_node_modules_is_not_dependency_install`、`test_reported_activity_wins_over_inference`、`test_stale_reported_activity_falls_back`。

### P2.3 后台 watchdog（不再依赖用户来查状态）

**问题**：预算超时只在 `inspect_background_state` 被调用时检查；没人看 status，挂死 worker 永远挂着。

**改法**：`chief_background.py::run_background_worker` 内起一个 `threading.Thread(daemon=True)`，每 60 秒：写 `heartbeat_at` 到 progress；检查 `mission_wall_budget_exceeded`，超限则记录 `budget_exhausted` 并 `os._exit(124)` 前先把 background.json 和 progress 写成 timeout 终态。不引入新进程，不用信号（Windows）。

**必写测试**（`tests/test_chief_background.py`）：`test_watchdog_writes_heartbeat`（把间隔参数化成可注入 0.1s）、`test_watchdog_marks_timeout_state_before_exit`（terminator 注入，不真退出）。

### P2.4 验证命令超时动态化

**问题**：`run_command_verification` 默认 900s；实测 `npm ci` 一项就要 9 分钟，正常项目会撞线，然后被归为不可修复的 `command_timeout`。

**改法**：

1. `chief_dispatch` 里计算实际超时：若 repo 缺 `node_modules`（Node 项目）或缺 `.venv`（Python 项目且命令里含 pytest），超时 = `timeout_seconds + 1200`；否则用传入值。判断逻辑放 `verification_profiles.py` 新函数 `estimate_verification_timeout(repo_root, command, base_timeout) -> float`，附带 `reason` 字符串写进 verification payload。
2. `command_timeout` 的 repair brief 文案加上：实际耗时、是否首次安装依赖、建议的 `--timeout-seconds` 值。

**必写测试**（`tests/test_verification_profiles.py` 或新建）：`test_timeout_extended_when_node_modules_missing`、`test_timeout_unchanged_when_deps_present`。

**Phase 2 结束门槛**：全量 `python -m pytest tests -q` 通过；用案例 A（见第 6 节）复演一次，全程 `chief-status` 输出的 stage/activity 与实际阶段一致（人工核对，结果记入 `docs/开发进度.md`）。

---

## Phase 3：preflight 产品化（预计 2 天）

现有 `preflight.py` 已有 `detect_project_type` / `inspect_environment` 骨架，**在其上扩展，禁止另起炉灶**。

### P3.1 dependency preflight

新函数 `preflight.py::dependency_preflight(repo_root, test_command) -> dict`，返回：

```json
{
  "package_manager": "npm|pnpm|pip|cargo|go|none",
  "lockfile": "package-lock.json 或空",
  "deps_installed": true,
  "cache_available": true,
  "native_install_risk": false,
  "estimated_install_minutes": 0,
  "warnings": []
}
```

判定规则（固定，不要发明新的）：Node 看 `node_modules/.package-lock.json` 与 lockfile 是否一致存在；Python 看命令里的解释器能否 `import pytest`；`native_install_risk` = package.json dependencies 含 `node-gyp`/`sharp`/`canvas`/`sqlite3`/`bcrypt` 之一。

### P3.2 派发前门禁

`dispatch_chief_plan` 在创建 worktree **之前**依次跑：

1. `resolve_test_command`（已有）→ 命令无法解析 → `preflight_blocked` / reason `test_command_unresolved`。
2. P1.2 的声明层 env_var 检查 → 缺 → `preflight_blocked` / reason `verification_environment_missing`。
3. `dependency_preflight` → 只警告不阻断（写入 payload["preflight"]，估算写入 P2.4 的超时）。
4. 工具链 preflight（已有 `_toolchain_preflight_for_command`）保持。

`preflight_blocked` 是新的 dispatch status：不建 worktree、不启动 worker、不烧任何模型额度。`chief_run` 映射 stop_reason 同名，中文文案给出确切修复步骤（例如"设置环境变量 QWEN_API_KEY 后重试"）。

### P3.3 preflight 结果进报告

final report 固定新增一节 `## Preflight`，列出上述检查结果表。dry-run / preview 模式也要跑 preflight 并展示（用户在派发前就能看到风险）。

**必写测试**（新建 `tests/test_dependency_preflight.py`）：

- `test_node_project_missing_node_modules_estimates_install`
- `test_python_project_with_pytest_importable_passes`
- `test_missing_declared_env_blocks_dispatch_without_worktree`（断言 worktree 目录不存在、无 worker record）
- `test_native_dep_flags_risk`

**验收命令**：`python -m pytest tests/test_dependency_preflight.py tests/test_chief_dispatch.py tests/test_chief_run.py -q`

---

## Phase 4：报告与工作台收口（预计 2 天）

### P4.1 final report 三段式改造

`chief_run.py::chief_run_to_markdown` 输出固定为三段（顺序、标题固定）：

```
## 结论
status / stop_reason 中文一句话 + verified? merged? + 产品改动文件数
## 证据
验收命令与结果（exit code、耗时、classification_confidence）
changed product files 列表（中文路径直接可读）
worktree 路径、分支名、merge commit（若有）
Preflight 摘要
## 下一步
按状态给出确切命令（合并命令 / 回滚命令 / 补环境变量 / 重试命令），
每条是可直接复制执行的 PowerShell 命令
```

### P4.2 plan/report 的 verification_mode 收口

- plan JSON 增加 `verification_mode: "command" | "workflow"`（有显式 test_command 即 command）。
- `codex_check.py` 与 final report：`verification_mode == "command"` 时不再输出 `needs_workflow_coverage`，改为一行说明"workflow coverage 由显式测试命令接管"。
- dashboard `data.py` / `app.js`：command 模式的任务不渲染 coverage risk 徽标。

### P4.3 dashboard 显示 activity 与耗时

`dashboard/api.py` 的 mission status 响应透传 P2.2 新增的 `activity_label`、`activity_command`、`activity_elapsed_seconds`；`app.js` 在任务卡片上显示"当前动作 + 已持续时间"，超过 10 分钟的 `dependency_install`/`tests_running` 显示黄色风险提示（纯 CSS class，禁止引入组件库）。

**必写测试**：`tests/test_chief_run.py::test_final_report_has_three_sections`（断言三个标题都在且顺序正确）、`tests/test_dashboard.py` 增加 activity 字段透传断言。

**验收命令**：`python -m pytest tests -q` 全量 + `python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root . --format markdown`。

---

## Phase 5（持续）：dogfood 验证矩阵

每完成一个 Phase，从下表选案例真实跑一轮，结果记入 `docs/开发进度.md`。**连续 3 轮、3 个案例、零误报（无假 verified、无误 merge、无状态误导）才算信任内核毕业**，届时才讨论解冻外围功能。

| 案例 | 项目 | 验收命令 | 专门检验什么 |
| --- | --- | --- | --- |
| A | `D:\宠物小程序\backend`（Node） | `cmd /d /s /c if not exist node_modules\express\package.json npm ci --cache .npm-cache --prefer-offline ^&^& node --import ./test/setup.js --test test/diagnosisRisk.test.js test/caseAtlasMatcher.test.js` | 正常闭环 + 依赖安装耗时下的 activity/超时表现（P2、P3） |
| B | `D:\抖音快手支付宝`（三端小程序，外部 AI 验收） | `npm run eval:acceptance`，声明 `--require-env QWEN_API_KEY` | 环境缺失分类（P1.2）、不许诱导 worker 降级验收门、preflight_blocked（P3.2） |
| C | Pacer 自身 `D:\助手codex\xiao`（Python） | `python -m pytest tests/test_mission_progress.py tests/test_chief_run.py -q` | 自托管：给 Pacer 派一个小任务（如加一条 stage label），检验 tamper guard 对 pytest 链的防护（P1.3） |

案例 B 的**回归剧本**（必须复演的历史事故）：在未设置 QWEN_API_KEY 的环境下发起任务。旧行为 = 误判 command_failed → worker 降级 eval → 假 verified → 误 merge。新行为必须是：preflight 阶段直接 `preflight_blocked`，不建 worktree、不烧额度，报告告诉用户设置哪个环境变量。

---

## 6. 每日收工检查单（Codex 每个工作日结束时执行）

1. `python -m pytest tests -q` 全量通过，数量不低于前一天。
2. `git status --short` 为空（全部已按任务分 commit）。
3. `docs/开发进度.md` 已更新。
4. 若当天改了 dispatch/verify/merge 任何一处：跑 `python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root .`。

## 7. 遇到不明确怎么办（写给 Codex）

- 本文档没规定的行为选择 → 不做，在 `docs/开发进度.md` 对应任务下记一行 `BLOCKER: <问题>`，继续做下一个不受影响的任务。
- 现有测试与本文档要求冲突 → 以本文档为准修改代码，同步修改该测试并在 commit message 里注明冲突原因。
- 任何时候都不允许：为了让某个案例通过而在分类器/门禁里硬编码该案例的项目路径或专属字符串（第 3 层兜底 marker 是唯一例外，且必须标 heuristic）。
