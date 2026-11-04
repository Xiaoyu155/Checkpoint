# Pacer 定型版结论 — 2026-11-02

## 定型结论

唯一推荐采用**确定性任务主干 + 按需跨供应商模型协作**：8091 网关作为客户端直接 Base URL；L1/L2、编号清单与阶段任务用本地规则规划，L3 或用户明确指定 Gemini 4 时才走 Gemini 4 规划；首轮并发保留已分配且健康的 Gemini 3.8、WorkBuddy、国产 API 通道，真实失败后才升到 Sub2API Codex/Opus；审查必须跨模型家族，物理测试和锁定验收仍是最终门槛。

这是一项**工程架构推荐**，不是统计意义上的 Arena 冠军。现有盲测没有在相同任务集、相同验收与完整可用的候选渠道下完成对照；本地 Arena 的三种候选均有未完成的写码或审查请求。把任何模型称作实测“最优”都超出证据。

对照用户提供的《Pacer 终极全景总结…定型方案》：该文档的 19 项失败属于历史快照。本工作树先前按指定命令记录的基线为 12 failed / 1,294 passed；此前阶段修复后为 1,020 passed。本轮继续修复并新增 9 项回归覆盖，最新全量结果为 **1,029 passed / 0 failed**。文档声称的“16,422 降到 1,200/1,500”没有直连网关 usage 对照，因此仍是待验证目标；其 SuperAgent 5 秒 ACK 和 `.pacer/lessons.md` 自动蒸馏同样不在当前代码中。

`SuperAgent` 5 秒后台工单和自动失败经验蒸馏仍是建议架构，当前代码没有实现对应的完整工单状态 API、隔离 worktree 生命周期或经验去重/预算注入。本次不会把这两项写成已交付功能。

## 三方会审

Codex 的源代码审阅、Opus 5.5 与 Gemini 4 独立评审均使用 `Pacer-Client/1.0`、真实模块源码/签名与实测报告。第一轮共同上下文 SHA-256：`c9dca4f9393a058837a83d55cd14da730706903a45efe55e47438bee2d64fa51`。Opus 5.5（`claude-opus-5-5`）响应完整，约 130 秒；Gemini 4（`gemini-4-argon`）第一轮输出触及 7,000-token 上限，末尾被截断，故没有把未返回部分当成意见。针对后台工单，两者随后都完整返回了独立补充评审，响应约 44 秒。原始脱敏调用记录保存在工作树外的 `D:\pacer-recovery\20261101\final-audit\`。

| 议题 | Codex 源码审阅 | Opus 5.5 | Gemini 4 | 会审定案 |
|---|---|---|---|---|
| 8091 双协议与 Token | 让客户端直接把 Base URL 指向 Pacer；分别处理 Messages、Chat Completions、Responses；未知上游 usage 不伪报。 | 16,422 是 Codex CLI 宿主上报，不是直连 8091 的对照数据；须做直连实验。 | 同意绕过宿主工具/历史上下文的机制解释，但强调当前没有真实直连账单证明。 | 服务端输出必须遵守请求协议；只有客户端直连 8091 才可能少付宿主上下文成本。将 ≤1,500 视为提示词预算目标，不作为已经测得的账单降幅。 |
| 混编并发与升档 | 保留规划时分配的健康首轮后端；逐任务记录实际 provider/family；免费/国产层失败后才升档；按 coder 模块异构审查。 | 本地/免费规划和首轮通道优先，Sub2API 作为失败后的攻坚层，Gemini 4 做异构审查。 | 支持本地规划、免费层轮询与高成本升级；指出现有并发实验并未跨供应商。 | 选定“本地确定性规划 + 健康通道首轮 + 真实失败升档 + 异构审查”。本轮修正了 `configure_plan` 覆盖多后端并行计划的问题；显式负面审查必须阻断，审查服务不可用不能伪装成批准。 |
| `.pacer/lessons.md` | 仅记录白名单失败类别；验收通过后原子写入、加锁去重；不把原始提示、堆栈或模型生成指令变成不变量；预算占用尽量低。 | 建议失败到修复后蒸馏并做长度/注入预算门禁。 | 建议单条最多 50 词、脱敏去重、容量上限、按目标文件筛选。 | 每条最多 50 词；只写可复现、白名单化的失败类别和修复规则；凭据、绝对路径、原始提示与堆栈不入库；验收成功后原子追加并去重；仅注入相关模块，且在现有总提示词预算内分配不超过 50 tokens。当前 helper 只有基础追加能力，这套门禁尚未实现。 |
| SuperAgent 5 秒工单 | 先持久化工单 ID，再异步运行；各工单一开始就使用独立 Git worktree；支持授权查询、恢复；不自动合并到 dirty 主树；同步模式显式保留。 | 建议持久化排队、worktree、状态机与重启恢复；5 秒从请求接收到工单落盘并 ACK 计时。 | 同意“先落盘再 ACK、后台创建 worktree、带幂等与状态/取消/恢复 API”，并建议把慢磁盘超时映射为 503。 | 唯一设计为持久化队列 + 幂等 ticket + 独立 worktree + 授权的状态/日志/结果/取消/恢复接口；ACK 时限从完整请求接收到持久化 `QUEUED` 并发出 202 响应头计，5 秒内完成。现有线程池仍等待最终结果，不能算作该功能。 |

### Arena 结果与限制

本地 Arena rubric 分为正确性 45、token 25、延迟 20、隔离 10。原始综合分仅作诊断：

| 候选组 | 首轮物理测试 | 总耗时 | 原始分 | 有效性 |
|---|---:|---:|---:|---|
| `gemini_free_first` | 175/219 | 34.14s | 53.00 | 3 个 coder 中 1 个失败；没有完成 reviewer 断言 |
| `opus_source_review` | 175/219 | 116.52s | 42.94 | 3 个 coder 中 2 个失败；没有完成 reviewer 断言 |
| `codex_specialized` | 179/219 | 24.30s | 66.04 | 3 个 coder 中 2 个失败；Gemini reviewer stream 未完成 |

最高分只代表该次不完整运行的分项得分，不能作为冠军。`arena_results.json`、评分规则和各渠道回执已归档在工作树外；缺失 usage 始终记作未知，而不是 0。

## 五道物理考题

| 考题 | 实际证据 | 能证明什么 | 不能证明什么 |
|---|---|---|---|
| 1. 定价与无状态注入 | `pricing_exam` 成功路径 100 passed / 0.52s；后续 `pricing_safety` 回执 119 passed / 0.72s。Codex CLI 上报输入 16,422，其中缓存 8,320、非缓存输入 8,102、输出 3,247。 | 定价实现与相应回归测试通过；CLI 宿主确实上报了该 usage。 | 没有 8091 直连的同任务 usage 对照，不能宣称实际降到 1,500 或节省某个比例；失败渠道 usage 未知。 |
| 2. 三模块并发 | `parallel_exam` 三个同供应商 OpenAI worker 重叠 5.20s；并行开发段 17.15s，三个 worker 时长合计 38.05s；207 passed / 11.22s。 | 并发 wave、补丁整合及物理验证可运行。 | 三个 worker 都属 OpenAI/Sub2API；没有验证 Gemini/WorkBuddy/国产 API 的跨供应商调度或胜率。 |
| 3. 14 日涨幅做空流水线 | `top_gainer_short` 阶段 A 19 passed、B 34 passed、C 累计 258 passed / 16.39s；Schema v4 checkpoint 及 SHA-256 `--resume` 跳过通过。Codex 写码。 | A→B→C 检查点、续跑与该功能验收有效。 | 没有同题多模型对照；Token usage 未知。阶段计数是累计值，不相加。 |
| 4. Pacer 自举全仓 | 早期修复阶段按指定命令为 **1,020 passed / 0 failed / 2 warnings**；本轮新增 9 项回归覆盖后重跑同一命令为 **1,029 passed / 0 failed / 2 warnings / 423.36s**。 | 验证当前工作树的生产代码修复与新增并发/路由回归；不能沿用旧材料中的 19 failed，也不能以单次全仓回归证明 Arena 模型排名。 |
| 5. 订单撮合异构盲审 | `.pacer/order_matcher_exam/report.json`：28 个考试断言先在基线失败；Codex 写码一次通过；Gemini 4 审阅经过 4 轮（两轮网关异常、一轮 reviewer oracle 预期值错误被沙箱拦截，最后一轮 3 个反例全过）；合计 31 passed，`failed_on_base` 有效。 | 真实 OpenAI Codex + Google Gemini 跨家族写码/盲审链路有一个成功案例。 | 单个案例、无对照组；不能证明 Codex+Gemini 是统计最优组合；Token usage 未知。 |

## 8091 网关定型

`pacer_model.py` 按 `/v1/messages`、`/v1/chat/completions`、`/v1/responses` 分别生成对应响应与流事件；OpenAI Responses 输入支持字符串和 `input_text` 消息块，流事件序号连续。OpenAI 两种协议的 usage 在当前适配边界不可得时返回 `null`，不伪造为零。新增本地回归覆盖 Chat/Responses 非流式与流式协议形状，4 项通过。

节省宿主上下文的前提是客户端直接配置 8091 Base URL 并将模型选为 Pacer。CLI 脚本内部再调用 Pacer 并不会消除 CLI 已经生成的 system/tools 上下文；本次没有跑同一请求的直连账单 A/B，故成本下降仍是待验证假设。

## 当前代码交付与状态

- `model_workflow.py` 保留 Gemini 4 规划失败的显式失败/授权 fallback；普通 L1/L2 Wish 在入口走本地规则计划，避免测试或普通任务误触发真实远端。L3 与明示 Gemini 4 的任务才走远端规划。
- `subagent_orchestrator.py` 保留首轮成本级联和显式后端优先级，修复重试升档、Grok/WorkBuddy 回退、实例级 verifier mock 的真实基线探测穿透，并在重复失败后熔断写码。
- `gemini_bridge.py` 的可用性检查使用无副作用 PATH 探针；`model_admin.py` 随 wheel/sdist 打包无内嵌密码的 v2 扩展资源。
- `canvas_manager.py` 保持 `version="2.0"` 与兼容标题。
- `cli.py` 的后端状态按安装、可达、鉴权和生成验证分层报告；Gemini 4 VPS 明确显示 0/6 空池不可用。
- Gemini 4 的规划/审查路径检查实际使用的旧版 VPS 配置，避免可选 Provider slot 关闭时漏过空池；仅在密钥显式配置时运行该前置池检查，未配置时保留原有调用错误与显式 fallback 语义。
- `pacer_model.py` 阻止不同工作区的并发请求进入当前任务队列，限制同工作区排队量，并在流客户端断开后释放 HTTP 处理线程。
- `configure_plan` 保留多任务计划中已分配的混合 provider；`grok_bridge.py` 不再修改进程级 socket 默认超时。
- 原有测试断言未被放宽；本轮新增 9 项回归用例，并为后端状态测试固定探针结果、保留并增加对“不误报 ready”的断言。
tests/` 中的已有协议测试仍只在 loopback 本地服务器运行，不调用外网。

**尚待独立实现的两项**：lesson 的脱敏/去重/预算化注入；持久化后台 ticket 与 worktree 隔离生命周期。本报告为架构选型结论，不把它们描述为本次代码交付。

## 可复现验收

插件自动加载关闭后的验收命令：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
& 'C:\Users\xiaoyu\AppData\Local\Programs\Python\Python311\python.exe' -m pytest --ignore=tests/test_vps_gemini4_gateway.py --ignore=tests/test_token_exchange.py -q --tb=short
```

最终实测：**1,029 passed, 0 failed, 2 warnings in 423.36s**。两条警告分别为 pytest 插件自动加载关闭后未注册的 `asyncio` mark，以及并发锁初始化窗口中的空文件读取警告；均未造成测试失败。插件禁用时长约 7 分钟；失败项为 0。

计数口径：当前工作树的 `pytest.ini` 将发现范围限定为 `tests/`，`conftest.py` 对依赖已裁剪的 `vps_gemini4_gateway` 和 `examples/pacer_token_exchange` 源码目录的 4 个测试模块设置了 collection ignore；指定命令再排除两个外部测试路径。本次未改动这些被排除模块；它们不计入 1,029。此口径与用户文档“排除已裁剪的外部目录”一致。

考题源码、实验运行目录、临时测试文件和 `.pacer` 运行回执在此前结论汇总后移至工作树外的 `D:\pacer-recovery\20261101\exam-evidence-archived-20261102\`，不会混入产品仓库。当前工作树内的临时盲测目录不存在。

## 2026-11-02 四小时日志与运行状态追加审计

### 日志取证

`D:\pacer_4h_log_report.txt` 中 Pacer Gateway Runs/Trace/Live.log 区域为空。可见的 Claude/Codex 交互集中在 `D:\广告流量\miniprogram\game\core.js`，该项目的最终 diff 列出 `game/catcafe/systems.js` 和 `tests/test_systems.js`。因此该日志能证明可见会话主要在另一个小程序仓库工作，**不能证明这四小时内 Pacer 收到网关生成请求、产生提交或被用作写码引擎**。宿主 CLI 的 16,422-token 用量不与空的 8091 网关日志矛盾；报告没有覆盖到的隐藏进程与归档仍属未知。

### 本机渠道实测

本次通过 `PYTHONPATH=src` 调用当前工作树的 `pacer backends`，结果是状态快照而非实际生成验收：

| 渠道 | 本次观察 | 可作出的结论 |
|---|---|---|
| Gemini CLI | 可执行文件已安装 | 登录状态与模型生成未验证 |
| WorkBuddy | 本地网关可达 | 鉴权与模型生成未验证 |
| Codex CLI | `codex-cli 0.166.0` 可执行 | OAuth 登录、额度与生成未验证 |
| Grok / Sub2API | 前一轮 `/models` 返回 HTTP 200；最终快照探测失败 | 状态有波动；生成未验证，不能按可用模型计 |
| Gemini 4 VPS | 先前快照为 `0/6`；最终快照显示反代池 `1/6` | 当前池可达且有 1 个账号，但模型生成未验证；0/6 时路由会判不可用 |
| DeepSeek / Qwen / Sub2API provider slots | 禁用或缺少 API key | 当前未配置可用的分层 API 后端 |

没有为了“验证生成”发送带真实额度的模型生成请求。两次快照显示 Grok 端点和 Gemini 4 账号池会变化，不能把单次 HTTP/health 成功等同于稳定生成能力。手工运行源码布局命令时应设置 `PYTHONPATH=src` 或安装本工作树；未设置时 Python 可能导入旧的已安装版本。

### 本轮外部评审边界

Opus 5.5 对四小时日志与当前源码的紧凑审计已完整返回，结论与本报告对日志、`backends` 假就绪、VPS 空池以及 Arena 证据边界的判断一致。Gemini 4 的长审计第一次只返回截断的部分答复，紧凑版第二次请求超时；因此本轮 Gemini 4 没有完整的当前源码审计意见。此前会审中 Gemini 4 对 `.pacer/lessons.md` 和 5 秒工单设计的补充意见仍保留，但不应把它表述成本轮完整复核。原始脱敏材料位于工作树外的 `D:\pacer-recovery\20261101\final-audit\`，不含在代码提交中。

### 尚未交付的能力

本次修复完成的是可用性误报、Gemini 4 空池路由、并行任务混合 provider 覆盖、跨工作区队列串扰和流断开处理等生产代码问题。持久化 5 秒后台 ticket/独立 Git worktree、失败经验安全蒸馏，以及同题同预算的 Arena 统计对照仍是独立功能或实验，**不属于已完成项**。Arena 推荐仍是工程配置建议，不是实测冠军。
