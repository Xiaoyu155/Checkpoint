# Pacer (DevPacer / Checkpoint) 深度架构审查报告与可行性验证考题集

## 一、 产品全景与核心定位评估

**Pacer（包名 `visual-agent`，双入口命令 `pacer` / `checkpoint`）** 是一套面向本地 AI 编程工具（Claude Code、Codex Plus、Gemini CLI、WorkBuddy 桌面网关及第三方 API）的 **多模型聚合调度与工程验收网关系统**。

其核心产品主张是解决高频重度 AI 开发者面临的两大痛点：
1. **主力订阅额度（Quota）过快耗尽**：例如 Codex Plus 的 5 小时窗口限额、Claude Code 的长会话（Monster Sessions）上下文重读损耗（实测 96% 额度消耗来自单会话超过 150k/10M token 的历史重复读取）。
2. **Agent 虚假报喜（False Verified）与破坏性改动**：Agent 常因“改弱单测”、“只跑空测试”、“遗漏未提交文件”而谎称任务完成，或者因进程被强杀留下满地孤儿 MCP 进程与脏工作区。

---

## 二、 核心架构深度审查：工程亮点 vs. 致命断层

通过对完整代码库（38 个源码与测试文件）的逐行审计，Pacer 呈现出一种非常鲜明的**“底层原语极其硬核，但顶层编排与底层严重割裂”**的双层架构状态。

### 2.1 六大工程亮点（极具生产价值的硬核设计）

| 审查维度 | 对应模块 | 核心机制与代码实现亮点 |
| :--- | :--- | :--- |
| **1. 原生窗口单任务轮转与零 Token 交接** | `pacer_task.py`<br>`claude_rotation.py` | 放弃了早期容易破损的“伪造 REPL（`claude_chat.py`）”，直接启动真实的原生 `claude` 进程（100% 保留原生 TUI、快捷键与 Todo 面板）。任务结束后，直接离线解析 `~/.claude/projects/*.jsonl` 会话记录，提取目标、修改过的文件（追踪 `Edit`/`Write`/`MultiEdit` 工具调用）与最后回复，生成交接摘要注入下一个窗口，实现**零额外模型 Token 消耗**的跨窗口记忆传递。 |
| **2. 内核级进程树防泄漏守卫** | `process_guard.py`<br>`subprocess_window.py` | 针对 Windows 下强杀启动器会导致 Claude/Codex 派生的 `mcp_server` 成为孤儿进程并锁死工作区的问题，直接通过 `ctypes` 调用 Win32 API `CreateJobObjectW` 并绑定 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`。只要主控句柄关闭（含崩溃或被任务管理器强杀），操作系统内核会自动清理整棵子进程树；同时兼容处理了 Windows 下 npm `.cmd` shim 无法被 `CreateProcess` 直接启动的痛点。 |
| **3. 验收区分度探针（防“伪绿”机制）** | `acceptance_discrimination.py`<br>`command_verification.py` | 深刻洞察了“测试通过 $\neq$ 目标达成”。在独立临时 `git worktree` 中对改动前的 `base_ref` 先跑一次验收命令：只有 **Base 失败（Red）且改动后通过（Green）** 才判定为 `verified`；若 Base 本来就通过，仅降级判定为 `regression_clear`（只证明没改坏，不证明完成了新需求）；同时通过 `_GATE_LOAD_FAILURE_MARKERS` 区分“因功能未写而失败”与“测试脚本自身语法错误/0 用例收集（`gate_broken`）”。 |
| **4. 净室交付验证（防“本机自嗨”）** | `delivery_check.py`<br>`dependency_bootstrap.py` | 解决“工作目录能跑通，但依赖未提交符号导致别人拉代码跑不通”的顽疾。将已提交分支单独 `git worktree add --detach` 到空目录，通过 Windows Junction（`mklink /J`）或符号链接挂载 `node_modules`/`.venv` 后重跑真实测试，严格区分 `delivered`、`not_delivered` 与 `unchecked`。 |
| **5. 零 Token 仓库符号地图** | `repo_map.py` | 不消耗任何大模型额度，利用 `git ls-files` + Python `ast` 语法树 + 多语言正则（支持 TS/JS/Go/Rust/Java 等 14 种后缀），按文件 `[size, mtime_ns]` 签名做增量缓存，并根据用户 `goal` 词元（含中文双字 Bigram 切分）对相关文件做符号级展开、其余文件折叠为目录树摘要。 |
| **6. 严密的源码基线归属与防篡改审计** | `task_review.py`<br>`pacer_launch_context.py` | 启动前对工作区建立 SHA256 指纹基线（`capture_task_source_baseline`），在任务完成时由服务端推导真实文件增删改（拒绝采信大模型自报的 `files` 列表），并通过 HMAC 签名收据（`_TRUST_SECRET`）防伪造；同时严查测试文件篡改（`changed_test_files`）与验收配置链篡改（`package.json`/`pyproject.toml`）。 |

---

### 2.2 八大关键缺陷与可行性断层（必须在实战前修复）

在审查 `subagent_orchestrator.py`、`workbuddy_bridge.py`、`gemini_bridge.py` 与 `cli.py` 时，发现了多处足以导致 `pacer auto` 主流程在真实环境中直接失效的**架构断层与实现 Bug**：

| 缺陷编号与等级 | 定位文件与行号 | 缺陷详情与失效机理分析 | 修复方向建议 |
| :--- | :--- | :--- | :--- |
| **P0-1：顶层编排器与底层硬核能力完全脱节（“两张皮”架构）** | `subagent_orchestrator.py`<br>`cli.py` | README 与仓库中引以为傲的 `acceptance_discrimination.py`、`delivery_check.py`、`diff_review.py`、`task_review.py`、`process_guard.py` **在 `pacer auto` 中完全没有被调用！** `SubagentOrchestrator` 只是一个极其简陋的 `while retries <= max_retries` 循环 + 裸跑 `subprocess.run(cmd, shell=True)`，甚至如 README 第 136 行坦言：“它们原本只通过已删除的 dispatch 层触达，目前还没有 CLI 入口”。 | 将 `command_verification`、`acceptance_discrimination` 与 `delivery_check` 正式接入 `SubagentOrchestrator._execute_task_with_retry()`，并暴露独立的 `pacer verify` CLI 命令。 |
| **P0-2：WorkBuddy 扮演 Coder 时存在“只聊天不落盘”黑洞** | `workbuddy_bridge.py` (L121-249)<br>`subagent_orchestrator.py` (L214, L365) | 当任务为 `standard` 级别且 WorkBuddy 在线时，`select_best_backend` 会将 `coder` 角色分配给 `workbuddy`。然而 `WorkBuddyGatewayClient.create_run()` 向 `http://127.0.0.1:port/api/v1/runs` 发送的 payload **既没有传递当前项目的 `cwd` / `repo_root` 路径，返回后也只把流式文本拼接到 `result.response` 字符串中，根本没有写入任何本地代码文件**！如果 WorkBuddy 网关端不在当前目录直接改文件，后续验证必定失败并白白浪费一次重试后升级给 Codex。 | 在 `create_run` 的 payload 中显式传入 `workspace`/`cwd` 上下文；或解析返回中的代码块/工具调用落盘，若 WorkBuddy 仅支持纯对话问答，则禁止将其分配给 `coder` 角色。 |
| **P0-3：DAG 调度器是假拓扑排序，且子代理之间完全失忆** | `subagent_orchestrator.py` (L247-291) | 1. **非拓扑执行**：`run()` 直接按 `for task in self.plan.tasks:` 顺序遍历，一旦 Gemini 生成的 JSON 数组中 `task_2` 排在 `task_1` 前面但依赖 `task_1`，`task_2` 会直接因 `unresolved` 被标记为 `skipped`！<br>2. **上下文断链**：`task_1 (indexer)` 分析出的仓库结构存入 `task_1.output` 后，**从未拼接到 `task_2 (coder)` 的 `instructions` 中**！`coder` 拿到的是孤立指令，`indexer` 白跑一趟。<br>3. **无并发执行**：即便两个任务 `dependencies=[]` 互不依赖，也是纯串行阻塞执行。 | 1. 引入 `graphlib.TopologicalSorter` 进行真拓扑分层，同层无依赖任务用 `ThreadPoolExecutor` 并发执行。<br>2. 将上游依赖任务的 `task.output` 摘要自动注入下游任务的 `instructions`。 |
| **P1-1：硬编码幻觉模型名与不稳定的 CLI 标志** | `gemini_bridge.py` (L91)<br>`subagent_orchestrator.py` (L205, L223) | 1. 代码与提示词多处硬编码 `"Gemini 3.8 Flash"` / `gemini-3.8-flash-n`（实为虚构版本号，真实環境为 `gemini-2.5-flash` 等），若传给 `-m` 会直接报错。<br>2. `invoke_gemini` 强行注入 `--skip-trust`（L91），若用户安装的 `@google/gemini-cli` 版本不支持该 flag，每次调用都会直接返回非零退出码。<br>3. Fallback 规则硬编码了 `pytest -p no:hydra_pytest -q`（L223），对非 Python 项目（Node/Go/Rust）直接报错。 | 移除硬编码的 `pytest -p no:hydra_pytest`，改用仓库自带的 `verification_profiles.choose_verification_command(repo_root)` 动态探测项目测试命令；探测 `--help` 后再决定是否加 `--skip-trust`。 |
| **P1-2：JSON 提取器贪婪切片 Bug** | `gemini_bridge.py` (L59-66) | `extract_json_from_text` 的第三步回退逻辑先遍历 `("{", "}")` 再遍历 `("[", "]")`。当 Gemini planner 返回的是 JSON 数组 `[{"id": "task_1"}, {"id": "task_2"}]` 且未包裹代码块时，`raw.find("{")` 与 `raw.rfind("}")` 会截取 `{"id": "task_1"}, {"id": "task_2"}`，导致 `json.loads` 抛出 `Extra data` 异常，随后寻找 `[` 到 `]` 才成功；但若外层说明文字里恰好带有 `{` 或 `}`（例如 `Please edit {file}`），则会优先干扰或切错边界。 | 按字符出现位置判断最外层是 `[` 还是 `{`，或使用 `json.JSONDecoder().raw_decode()` 扫描首个合法 JSON 结构。 |
| **P1-3：子字符串匹配导致的模型误路由（False Positive）** | `model_router.py` (L20-48, L75-76) | `strong_hits = {term for term in _STRONG_TERMS if term in text}` 使用纯子串 `in` 判断而非词边界匹配。例如英文单词 `"author"`、`"authority"`、`"oauth_button_label"` 会命中 `"auth"`；`"thread_id"` 文案重命名会命中 `"thread"`；甚至当用户写 **“只改注释，不要重构架构、不要动安全鉴权”** 时，因为包含否定词后的 `"重构"`、`"架构"`、`"安全"`，且 `strong` 优先级高于 `cheap`，会被强制路由到最昂贵的 `strong (Codex/Opus)` 模型！ | 对英文词元采用 `\b` 正则词边界匹配，并引入像 `task_review.py` 中 `_NEGATED_MUTATION_MARKERS` 一样的否定前缀剔除逻辑（剔除“不要重构/无需修改安全”等否定短语后再匹配）。 |
| **P1-4：跨平台与命令行长度上限风险** | `claude_rotation.py` (L414)<br>`codex_launcher.py` (L579) | 1. `_kill_tree` 在 POSIX 系统上仅调用 `process.terminate()`（只给主进程发 SIGTERM，不杀子进程组），与 `subprocess_window.py` 中完善的 `os.killpg` 不一致。<br>2. `codex_launcher.py` 将长控制指令与任务全部塞入命令行位置参数 `argv`，在 Windows 下若交接摘要 + 任务描述超过 `CreateProcess` 32,767 字符限制会直接崩溃。 | 统一复用 `subprocess_window.terminate_process_tree()`；当 Prompt 超过 8KB 时自动切换为 `stdin`（`-`）管道注入。 |
| **P2-1：双品牌环境变量分裂与配置割裂** | 全局多处 (`env.py`, `cli.py`, `subscription_quota.py`) | 仓库处于从 `VISUAL_AGENT_` -> `CHECKPOINT_` -> `PACER_` 的三重品牌迁移期。例如配额快照存在 `~/.checkpoint/subscription_quota.json`，而配额失败记录存在 `~/.pacer/quota_failures.json`，用户极易在排查配置时迷失。 | 统一收敛至 `~/.pacer/` 与 `PACER_*` 环境变量前缀，保留旧前缀只读向后兼容并在 `pacer backends` 中打印统一诊断路径。 |

---

## 三、 产品可行性验证考题集（Feasibility Verification Exam Suite）

为了客观、定量地验证 Pacer 是否真正具备其宣称的 **“省额度、分流准、自愈强、防假绿、可交付”** 五大核心能力，以下设计了 **5 大维度、共 12 道实战验证考题（含具体复现指令、预期行为、底层考察点与评分标准）**。你可以直接在本地终端逐步执行这套考题来进行验收测试。

---

### 维度一：多模型智能分流与成本防线测试（Routing & Quota Protection）

#### 考题 1：三级任务路由精准度与“否定词抗干扰”压力测试
* **考察目标**：验证 `model_router.py` 能否准确将轻/中/重任务分流至 Gemini/WorkBuddy/Codex，并测试其在自然语言否定句下的脆弱性。
* **测试步骤**：在 Python 环境中运行以下断言脚本或通过 `pacer plan` 观察后端分配：
  ```python
  from visual_agent.model_router import route_task

  # Case A: 纯机械小改 -> 应为 cheap
  assert route_task(objective="把 login.py 里的变量名 usr_nm 重命名为 username 并修正注释错别字").tier == "cheap"

  # Case B: 高风险架构/资金安全 -> 应为 strong
  assert route_task(objective="修复微信支付异步回调 webhook 的签名验签逻辑与并发重入锁").tier == "strong"

  # Case C: 单文件契约明确的小函数 -> 应为 cheap
  assert route_task(objective="In utils/math_helper.py implement clamp(x, low, high) that returns exactly the bounded value with unit test").tier == "cheap"

  # Case D (进阶压力项 - 否定语义陷阱): 明确要求只改文案、不要重构
  res_d = route_task(objective="修改 README.md 里的提示语文案错别字，注意不要重构代码，不要动认证鉴权模块")
  print("Case D 路由结果:", res_d.tier, res_d.reason)
  ```
* **合格标准（Pass Criteria）**：
  * **基础及格（60分）**：Case A、B、C 全部符合预期，且 `pacer plan` 将 `indexer` 分配给 `GEMINI`、将重度任务分配给 `CODEX`。
  * **优秀满分（100分）**：修复 `model_router.py` 后，Case D 能够识别 `"不要重构"`、`"不要动认证鉴权"` 为否定约束，正确路由至 `cheap` 而非误判为 `strong`。

#### 考题 2：配额耗尽（429 / 5h Limit）与上游 5xx 故障熔断切换测试
* **考察目标**：验证 `agent_backends.py`、`provider_liveness.py` 与 `hourly_budget.py` 在主力模型额度告急或接口报错时的保护能力。
* **测试步骤**：
  1. **伪造 429 与 Request ID 混淆测试**：验证 `looks_like_quota_exhaustion("Error: request_id=7e23-4294-a7ef HTTP 502 Bad Gateway")` 是否返回 `False`（不被 UUID 中的 `429` 误导），而对 `"You've used all your 5-hour usage limit, resets at 18:00"` 返回 `True`。
  2. **高水位熔断测试**：构造 `quota_snapshot = {"providers": {"codex": {"rate_limits": {"five_hour": {"used_percentage": 85.0}}}}}`，调用 `build_hourly_plan(tasks=[{"task_id": "t1", "objective": "重构数据库引擎", "worker_tier": "strong", "estimated_strong_minutes": 40}], quota_snapshot=quota_snapshot)`。
* **合格标准**：
  * UUID 中含 `429` 的 502 报错必须被准确归类为 `looks_like_provider_5xx == True` 且 `looks_like_quota_exhaustion == False`。
  * 当 5 小时额度使用率达到 85%（超过默认的 82% 警戒线）时，`build_hourly_plan` 必须将 `strong` 任务放入 `deferred` 队列，同时仅放行 `cheap` / `research` / `doc` 任务。

---

### 维度二：子代理 DAG 拆解与自愈闭环测试（Subagent Orchestration & Self-Healing）

#### 考题 3：DAG 乱序依赖拓扑执行与跨节点上下文传递测试（暴露 P0-3 缺陷）
* **考察目标**：检验 `SubagentOrchestrator` 是否具备真正的 DAG 拓扑调度能力与子代理间的信息传递能力。
* **测试步骤**：
  1. 构造一个包含 3 个任务的 `SubagentPlan`，故意将任务列表顺序排列为 `[task_3 (verifier, 依赖 task_2), task_2 (coder, 依赖 task_1), task_1 (indexer, 无依赖)]`。
  2. 让 `task_1` 输出 `"KEY_FACT: 目标函数名必须叫 calculate_discount_v2"`，观察 `task_2` 执行时收到的 `instructions` 是否包含该信息。
* **合格标准**：
  * **现状预期（未修复前）**：`task_3` 和 `task_2` 会直接因为排在 `task_1` 前面而被错误标记为 `skipped`！且 `task_2` 收不到 `task_1` 的任何输出。
  * **修复后达标要求**：编排器能自动按 `task_1 -> task_2 -> task_3` 拓扑序执行，且 `task_2` 能读取到 `task_1` 产出的架构分析摘要。

#### 考题 4：低配模型写挂代码 $\rightarrow$ 自动捕获 Traceback $\rightarrow$ 升级 Codex 自愈闭环测试
* **考察目标**：验证 `SubagentOrchestrator._execute_task_with_retry` 的核心卖点——当免费/低成本模型（Gemini / WorkBuddy）生成的代码未通过单测时，能否自动附带报错堆栈并升级（Escalate）至 Codex 修复。
* **测试步骤**：
  1. 在一个临时 Git 仓库中创建 `calc.py`（内含故意写错的 `def divide(a, b): return a * b`）和 `tests/test_calc.py`（断言 `divide(10, 2) == 5` 且 `divide(1, 0)` 抛出 `ZeroDivisionError`）。
  2. 运行：
     ```powershell
     pacer auto --verify-cmd "python -m pytest -q" "修复 calc.py 中的 divide 函数使其通过单元测试"
     ```
  3. 模拟首次 `gemini`/`workbuddy` 只修了除法没处理除零异常（导致 `pytest` 返回 Exit Code 1），观察第二次重试行为。
* **合格标准**：
  * 第一次验证失败后，`task.status` 变为 `escalated`，`task.backend` 自动从 `gemini`/`workbuddy` 切换为 `codex`。
  * `task.instructions` 末尾被自动追加 `[CRITICAL: Previous attempt failed verification]: ... ZeroDivisionError ...`。
  * 第二次调用成功后，`pytest` 返回 0，整体 `summary["success"] == True`，且 `task.history` 完整记录了第一次失败的后端与报错摘要。

---

### 维度三：原生窗口轮转与系统级健壮性测试（Session Rotation & Process Guard）

#### 考题 5：`pacer task` 跨窗口零 Token 交接与模型动态切换实战
* **考察目标**：验证 `pacer_task.py` 在真实交互工作流中的体验：自动按任务选模型 + 退出窗口后自动提炼 JSONL 交接存档。
* **测试步骤**：
  1. 在测试仓库下启动 `pacer task`。
  2. 在 `下一个任务>` 提示符下粘贴一段**多行文本**（包含重度关键词，如 `"第一行：请重构 auth.py 的鉴权签名逻辑\n第二行：补充异常处理"`）。
  3. 观察 Pacer 是否将多行粘贴合并为单次任务（`_has_buffered_line`），并自动选择 `opus` 模型拉起原生 Claude 窗口。
  4. 在 Claude 窗口内修改一个文件后输入 `/exit` 退出。
  5. 在 Pacer 提示符下输入下一个轻任务：`"把刚才修改的文件里的注释错别字改一下"`。
* **合格标准**：
  * 多行粘贴不会触发多次窗口启动。
  * 第一个窗口退出后，`.pacer/session-handoffs/rotation-*.md` 文件被成功创建，内含“本会话最初的目标”、“本会话动过的文件”与“最后一条助手回复”。
  * 第二个任务自动降级选用 `sonnet` 模型，且其初始 Prompt 中自动携带了第一个窗口的交接摘要。

#### 考题 6：Windows 强杀主进程下的 MCP 孤儿进程清理测试（Job Object 验证）
* **考察目标**：验证 `process_guard.py` 中的 Win32 Job Object 是否能防止 `visual_agent.mcp_server` 泄漏。
* **测试步骤**：
  1. 使用 `guarded_run` 启动一个派生了孙子进程（例如 `python -c "import subprocess, time; subprocess.Popen(['python', '-c', 'import time; time.sleep(300)']); time.sleep(300)"`）的测试脚本。
  2. 在外部终端使用 `Stop-Process -Id <ParentPID> -Force` 或 `taskkill /PID <ParentPID> /F` 强行杀死父进程（不给 Python 执行 `finally` 块的机会）。
  3. 检查系统中是否还残留 `time.sleep(300)` 的孙子 Python 进程。
* **合格标准**：
  * 父进程被强杀的瞬间，内核自动销毁 Job Object，所有派生的子进程与孙子进程在 1 秒内全部消失，零残留。

---

### 维度四：工程验收防伪与防篡改攻防考题（Anti-Cheat & Acceptance Gate）

#### 考题 7：“假绿”识破考题（Base-Diff 区分度探针测试）
* **考察目标**：验证 `acceptance_discrimination.py` 能否拦住“用原本就绿的无关测试冒充新功能验收通过”的行为。
* **测试步骤**：
  1. 在 Git 仓库中已有通过的 `tests/test_existing.py`。
  2. 下达任务：`"新增一个导出 CSV 报表的功能模块 exporter.py"`，但将验收命令指定为 `"python -m pytest tests/test_existing.py"`。
  3. 调用 `probe_base_command` 与 `classify_acceptance` 进行评估。
* **合格标准**：
  * `probe_base_command` 探测出该命令在 `HEAD` 基线上本来就是 `passed_on_base`。
  * `classify_acceptance` 必须拒绝给出 `verified`，而是返回 `tier == "regression_clear"`（标签：`"只证明没弄坏"`），且 `pacer_voice.user_story` 输出明确的警告标题：`"改完了，但我不能替你打包票说这件事做到了。"`

#### 考题 8：“裁判损坏”识破考题（Gate Health 语法错误与 0 用例拦截）
* **考察目标**：验证 `assess_gate_health` 能否区分“业务代码未实现导致的测试失败”与“测试命令本身写坏/0 用例收集”。
* **测试步骤**：
  1. 场景 A：验收命令跑了一个含 `SyntaxError` 或 `ModuleNotFoundError` 的测试脚本（返回码非 0）。
  2. 场景 B：验收命令执行了 `pytest -k non_existent_test`（输出 `collected 0 items`）。
* **合格标准**：
  * 两个场景下 `assess_gate_health` 均必须返回 `status == "broken"`（分别给出 `gate_failed_to_load` 与 `gate_executed_nothing`），绝不能将其当作正常的 `failed_on_base`（契约红灯）派发给 Worker 去瞎改代码。

#### 考题 9：Worker 篡改测试文件与篡改 `package.json` 拦截测试
* **考察目标**：验证 `command_verification.py` 与 `task_review.py` 的防作弊机制。
* **测试步骤**：
  1. **篡改测试文件**：模拟 Worker 为了让失败的断言通过，直接修改了 `tests/test_auth.py` 中的 `assert status == 200`。运行 `changed_test_files(repo_root=...)` 与 `tamper_repair_brief(...)`。
  2. **篡改验收配置链**：模拟 Worker 修改了 `pyproject.toml`（例如把 pytest 的 `addopts` 改为空）或 `package.json` 中的 `"test"` 脚本。运行 `changed_acceptance_chain_files(...)`。
  3. **越界修改受保护文件**：下达任务 `"优化 login.py 的日志输出，但不要修改 config/security.py"`，随后模拟 Worker 修改了 `config/security.py`，调用 `audit_task_completion(...)`。
* **合格标准**：
  * 场景 1 立即触发 `test_tampering` 拦截，并生成要求 Worker 执行 `git checkout <base> -- tests/test_auth.py` 的修复指令。
  * 场景 2 立即触发 `acceptance_chain_tampering` 拦截。
  * 场景 3 中 `build_task_contract` 自动从 `"不要修改 config/security.py"` 提取出 `protected_paths: ["config/security.py"]`，且 `audit_task_completion` 返回 `verdict == "rejected"`、错误码 `protected_path_changed`。

#### 考题 10：净室交付检查考题（“未提交幽灵依赖”拦截测试）
* **考察目标**：验证 `delivery_check.py` 能否抓出“本地脏工作区测试全绿，但漏提交新文件/新函数”的交付事故。
* **测试步骤**：
  1. 在测试 Git 仓库的 `feature` 分支上修改 `app.py`，使其 `from helper_untracked import magic_value`，并提交 `app.py`（但故意不 `git add helper_untracked.py`，让 `helper_untracked.py` 留在本地未跟踪状态）。
  2. 在本地工作区直接跑 `pytest` —— 显示 **100% 通过**。
  3. 调用 `run_delivery_check(repo_root=repo, branch="feature", command="python -m pytest -q")`。
* **合格标准**：
  * `run_delivery_check` 在全新临时 worktree 中检出 `feature` 分支并运行 `pytest`，精准捕获 `ModuleNotFoundError: No module named 'helper_untracked'`，返回 `status == "not_delivered"`。
  * `pacer_voice.user_story` 将原本的 `verified` 降级拦截，输出：`"改完了，也测过了，但现在还交不到你手上。"`

---

### 维度五：模糊目标落地与自举证据链测试（Goal Grounding & Dogfooding）

#### 考题 11：模糊目标文档寻根（Goal Grounding）测试
* **考察目标**：验证当用户输入一句模糊的 `"按照最新的开发计划继续推进"` 时，`goal_grounding.py` 能否不报错拒绝，而是自动扫描仓库文档提取下一个待办事项。
* **测试步骤**：
  1. 在测试仓库下创建 `docs/roadmap.md`，写入：
     ```markdown
     # 开发路线图
     ## 已完成
     - [x] 初始化项目结构
     ## 下一步待办 (Remaining)
     - [ ] 实现用户导出 PDF 账单接口 (`src/billing/pdf_export.py`) 并通过 `pytest tests/test_pdf.py`
     - [ ] 接入邮件通知服务
     ```
  2. 调用 `ground_goal("参照最新的开发计划继续推进", repo_root=repo, enable_model=False)`（测试无 API Key 时的确定性降级）以及注入 Mock LLM 返回的 `enable_model=True` 模式。
* **合格标准**：
  * `goal_references_plan("参照最新的开发计划继续推进")` 返回 `True`。
  * `discover_plan_documents` 将 `docs/roadmap.md` 排在第一位（分数最高）。
  * 无模型模式下准确提取出 `pending_items` 列表并向用户发起确认提问；有模型模式下自动将模糊目标解析为具体的 `"实现用户导出 PDF 账单接口..."` 并附带原文引用证据。

#### 考题 12：Pacer 自举（Pacer-on-Pacer Dogfood）全链路密码学证据校验
* **考察目标**：验证 `dogfood_evidence.py` 与 `release_gate.py` 的最高等级防伪能力（用 Wheel A 管理源码修改 $\rightarrow$ 产出 Wheel B $\rightarrow$ 在全新环境安装 Wheel B $\rightarrow$ 完成自检并验证 HMAC 签名与 SHA256 哈希链）。
* **测试步骤**：
  1. 构造一份 `.pacer/dogfood-evidence.json`，测试以下 3 种篡改攻击：
     * **攻击 A（原地踏步伪造）**：令 `candidate.wheel_sha256 == orchestrator.input_wheel_sha256`（试图拿旧包冒充新包）。
     * **攻击 B（场外手动偷改）**：令 `source_repo.out_of_band_changes = True` 或修改的文件不在 `src/visual_agent/` 下。
     * **攻击 C（重放攻击）**：在 `evaluate_clean_streak` 中连续传入 3 次完全相同的 `evidence_digest` 企图凑满 3 连胜（Clean Streak）。
* **合格标准**：
  * 攻击 A 被 `dogfood_candidate_matches_input_wheel` 精准拒止。
  * 攻击 B 被 `dogfood_out_of_band_changes_detected` / `dogfood_pacer_product_change_missing` 拒止。
  * 攻击 C 被 `release_dogfood_evidence_duplicate` 拒止，`streak["passed"]` 严格为 `False`。

---

## 四、 可行性总结与演进路线图建议

1. **当前可行性评级**：
   * **单任务交互模式（`pacer task` / `pacer claude` / `pacer codex`）**：**高度可用（生产就绪度 85%+）**。其原生窗口保持、JSONL 离线交接提取、Win32 Job Object 进程树防泄漏以及 Codex MCP 契约锁定的完成度非常高。
   * **全自动多模型子代理模式（`pacer auto`）**：**原型阶段（生产就绪度 45%）**。主要受限于 `SubagentOrchestrator` 尚未与底层强大的验收模块打通、WorkBuddy 缺少文件落盘闭环、以及 DAG 拓扑排序与上下文传递缺失。
2. **最优先的 3 步重构建议（最快打通完整闭环）**：
   * **第 1 步（1 天）**：在 `SubagentOrchestrator` 中用 `graphlib.TopologicalSorter` 替换顺序循环，并将上游依赖任务的 `output` 注入下游任务的 `instructions`。
   * **第 2 步（1~2 天）**：修复 WorkBuddy 桥接器——要么在调用本地网关时绑定工作区路径使其直接在磁盘写文件，要么将无写文件能力的网关仅限用于 `planner`/`reviewer`，把 `coder` 交还给具备 `--yolo` / `exec` 本地写文件能力的 Gemini CLI、Claude Code 或 Codex。
   * **第 3 步（2 天）**：把 `command_verification.py`、`acceptance_discrimination.py` 和 `delivery_check.py` 串联封装为 `pacer verify` 子命令，并将其作为 `SubagentOrchestrator` 的默认 Verifier 引擎，彻底兑现 README 中承诺的“自动编排 + 防伪验收”完整闭环。
