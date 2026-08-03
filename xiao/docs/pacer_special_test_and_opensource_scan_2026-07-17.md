# Pacer 专项测试 + GitHub 开源/黑科技扫描

日期：2026-07-17

## A. 专项测试结果

### A1. 信任内核单测

命令：

```powershell
python -m pytest `
  tests/test_chief_run.py::test_chief_run_surfaces_provider_5xx_instead_of_generic_worker_error `
  tests/test_chief_run.py::test_chief_run_worker_failure_is_not_verified_by_passing_command `
  tests/test_chief_run.py::test_chief_run_worker_failed_tests_pass_needs_manual_merge `
  tests/test_release_gate.py -q
```

结果：**22 passed**。

覆盖：

| 场景 | 期望 | 结果 |
| --- | --- | --- |
| provider 5xx 文案 | 不引导 agents doctor | 通过 |
| worker 失败 + 测试绿 | 非 verified | 通过 |
| worker_failed_tests_pass | 需人工 merge | 通过 |
| release gate | digest / 首败停止 / dogfood 规则 | 通过 |

### A2. Release matrix 预检

`python scripts/release_matrix_preflight.py`

| 项 | 结果 |
| --- | --- |
| status | passed |
| release_ready | **false**（故意） |
| digest 锁定 | `aaa50981…` |
| managed_sample | 15 |
| missing-runner 门禁 | 生效 |
| wrong-digest 门禁 | 生效 |

### A3. 失败路径 dry-run（不烧长额度）

临时仓库 `pacer-special-20260717-195601`：

| 输入 | stop_reason | 评价 |
| --- | --- | --- |
| `改一下 --interview` | `needs_clarification` | 正确：模糊目标先停 |
| `Add multiply and test_multiply`（无 execute） | **也** `needs_clarification` | **偏严**：合理实现目标被当模糊，上手摩擦 |

说明：澄清门对明显实现类英文目标仍可能拦截，会伤害“一句话就能预览”的体感。建议后续单独收口 intake 启发式，而不是再堆长任务。

### A4. 既有真实路径（本日早前）

| 路径 | 结果 |
| --- | --- |
| Claude + allow-test-edits | `verified` |
| Codex 中转 503 | `worker_error`（内部 `provider_5xx`；文案修复已合入源码，待装新包生效） |
| Codex divide | `worker_failed_tests_pass`（代码有、测试过、worker 未正常结束；不假 verified） |
| 错误 Python / 无 pytest | worker 卡环境，产品侧应默认解析到带 pytest 的解释器 |

### A5. 专项测试结论

**够用来判断：信任内核可靠；发布矩阵未执行；intake 过严与环境解析是下一刀顺手度问题。**

不必再刷同质 happy-path 长任务。

---

## B. GitHub 扫描：什么值得学

资料主入口：

- [awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators)
- [bradAGI/awesome-cli-coding-agents](https://github.com/bradagi/awesome-cli-coding-agents)
- 对标样本：Vibe Kanban、CliDeck、Crystal、Emdash、Bernstein、Goose、Cline、Aider

### B1. 市场格局（对 Pacer 的含义）

2026 的开源/半开源赛道已极度拥挤：

- **并行 worktree 跑 agent**：Crystal、Emdash、dmux、claude-squad、Orca、parallel-code…
- **看板调度**：Vibe Kanban（**已宣布 sunsetting**）、agent-kanban、openkanban…
- **多 agent 会话壳**：CliDeck、agentpipe、thurbox…

**含义：Pacer 不要再把自己讲成“又一个并行 agent 看板”。**  
护城河应继续是：

> **本地订阅 CLI + 强验收 + fail-closed 证据 + 不假 verified。**

并行 UI 是“包装层”，不是差异化核心。

### B2. 高价值可借鉴（按顺手 / 推广排序）

#### 1) 一键分发（推广第一刀）

| 项目 | 可学点 | 给 Pacer 的动作 |
| --- | --- | --- |
| [vibe-kanban](https://github.com/BloopAI/vibe-kanban) | `npx vibe-kanban` 一条命令进产品 | 做 `pipx install pacer` / `npx` 包装 / `irm \| iex` 安装脚本；默认打开“新建任务”而不是命令森林 |
| [goose](https://github.com/aaif-goose/goose) | curl 装 CLI + 桌面端双路径 | Windows：`winget`/`scoop` + 便携 exe；安装后 `pacer doctor` 自动跑 |
| Cline CLI | `npm i -g cline` 心智 | 包名统一 `pacer`，弱化 checkpoint/visual-agent |

#### 2) 实时“人话状态”（顺手第一刀）

| 项目 | 可学点 | 给 Pacer 的动作 |
| --- | --- | --- |
| [Rich](https://github.com/Textualize/rich) / [Textual](https://github.com/Textualize/textual) | 终端进度、表格、live refresh，零重前端 | mission 运行时：stage / activity / 失败一类句 / token 条；`pacer watch` |
| [clideck](https://github.com/rustykuntz/clideck) | 聊天侧栏 + 多 CLI 会话 + 手机可看 | Dashboard 默认只留：任务、状态、原因、下一步；日志进二级 |
| [agentpipe](https://github.com/kevinelliott/agentpipe) | TUI 多面板：状态/成本/会话 | 可观测性面板合并为一条成本+状态条 |

#### 3) “协调零 token”与预算硬停（黑科技向）

| 项目 | 可学点 | 给 Pacer 的动作 |
| --- | --- | --- |
| [bernstein](https://github.com/chernistry/bernstein) | **协调层不烧 LLM token**，只 spawn CLI + 测 + commit | 调度/状态机继续纯本地；禁止用模型做“该不该 merge” |
| [MartinLoop](https://github.com/Keesan12/martin-loop) | 硬预算停、verifier gate、rollback 证据、可检查 receipt | 对齐现有 managed budget；补 “receipt 可分享” 一页导出 |
| [AGX](https://github.com/ramarlina/agx) | wake-work-sleep checkpoint + HITL gate | 长任务卡死恢复；sleep 不杀进程但可恢复证据 |

#### 4) Worktree / 并行会话体验（学交互，不抄整产品）

| 项目 | 可学点 | 给 Pacer 的动作 |
| --- | --- | --- |
| [crystal](https://github.com/stravu/crystal) | 多 Codex/Claude 并行 worktree | 已有 worktree；缺“一键打开 worktree / 看 diff / 合并” |
| [emdash](https://github.com/generalaction/emdash) | 多 agent + 文件编辑 + dashboard | 工作台加 worktree diff 预览，不必做完整 IDE |
| [dmux](https://github.com/standardagents/dmux) / claude-squad | tmux + worktree 会话管理 | Windows 可用 Windows Terminal 标签建议替代 tmux |

#### 5) 符号级/协议级协调（进阶，勿优先）

| 项目 | 可学点 | 风险 |
| --- | --- | --- |
| [wit](https://github.com/amaar-mc/wit) | Tree-sitter 函数级锁，减少并行写冲突 | 实现成本高 |
| [gnap](https://github.com/farol-team/gnap) | Git 原生任务板，无中心 orchestrator 进程 | 与 Pacer 中心状态机哲学不同 |
| [swarm-protocol](https://github.com/phuryn/swarm-protocol) | MCP claim/heartbeat 协调 | 先稳单 worker |

### B3. 不建议跟风

1. **再做一个 Vibe Kanban 克隆**（对方已 sunset，赛道同质化）。  
2. **41-agent swarm / 全公司自动化**（定位漂移，验收更难讲清）。  
3. **为了并行而并行**：Pacer 的卖点是可信托管，不是舰队规模。  
4. **把核心绑死到某一商业看板 SaaS**。

### B4. Pacer 差异化话术（推广用）

别人：跑更多 agent。  
Pacer：

1. 用你本机订阅的 Codex/Claude  
2. 隔离 worktree  
3. **你的测试说了算**  
4. **不假 verified、不污染主分支**  
5. 证据可审计  

一句话：

> **Pacer is the seatbelt for coding agents — not another agent.**

---

## C. 建议落地优先级（30 天）

| 优先级 | 项 | 来源灵感 | 预期效果 |
| --- | --- | --- | --- |
| P0 | `pacer` 一键安装 + `pacer` 默认进对话/任务，不暴露命令森林 | Goose / vibe-kanban npx | 推广转化 |
| P0 | 失败第一句人话（503/环境/测试篡改/worker 未完成） | 本日专项 | 信任与支持成本 |
| P0 | test-command 解析到**真实可用** Python（带 pytest） | 本日 Codex 卡死 | 成功率 |
| P1 | `pacer watch`：Rich live 状态（stage/activity/blocker） | Rich/Textual | 顺手 |
| P1 | Dashboard 默认 4 卡：新建 / 当前 / 原因 / 下一步 | CliDeck 简化 | 降低驾驶舱感 |
| P1 | intake：明确实现目标勿误伤 `needs_clarification` | 本日专项 | 上手 |
| P2 | worktree diff + 一键 merge UI | Crystal/Emdash | 完成感 |
| P2 | 15 项 matrix 串行 runner | 自身 release_gate | 发布签字 |
| P3 | 符号锁 / swarm MCP | wit / swarm-protocol | 仅多 worker 时 |

---

## D. 最终判断

1. **专项测试**：信任内核可用；发布矩阵未跑；intake/环境是体验债。  
2. **开源扫描**：并行 agent 壳已经卷完；Pacer 应 **借分发与状态 UX**，**守验收与证据**。  
3. **最划算的“黑科技”** 不是新模型，而是：  
   - 协调零 token（本地状态机）  
   - 硬预算 + verifier gate  
   - 一键安装 + 失败人话 + 可用工具链探测  

**下一步若只做三件事：安装路径统一、失败人话、test-command 解释器解析。** 这三件对“顺手 + 好推广”的 ROI 高于再写一个并行看板。
