# Pacer 重构核心模块 & Delta Action 深度源码审查报告

审查范围以你提供的 **完整源码** 为准：`mcp_server.py`、`subagent_orchestrator.py`、`canvas_manager.py`、`local_indexer.py`、`diff_patcher.py`、`examples/delta_action/core.py`、`examples/delta_action/test_delta.py`。Codex 四点涉及的 `workbuddy_bridge.py` / `model_router.py` / `game_2048/core.py` / `cli.py` **不在本次源码包内**，裁决按缺陷描述的可成立性给出，并标明证据边界。

---

## 1. 架构与安全性真实评分

| 维度 | 分数 | 一句话理由 |
|---|---|---|
| **架构设计** | **7.4 / 10** | Stateless Canvas + 本地 AST + SEARCH/REPLACE + 验证门禁的主链路清晰，工程完成度高；但 DAG 实际被压成固定三步、后端选择名存实亡、自愈是递归重入而非状态机。 |
| **安全性** | **5.8 / 10** | 有路径守卫、测试篡改检测、验收链保护、stdio 隔离等正确方向；关键信任边界（workspace 根、验证命令、补丁允许列表、Content-Length、锁降级）没有闭合。 |
| **Delta Action 领域模型** | **7.6 / 10** | 干员/穿甲/保险箱/撤离的主路径可运行且测试能钉住；`REVIVING` / `CONTESTED` 未落地，耐力与竞赛是死状态。 |
| **可上线成熟度（本地可信开发者）** | **6.5 / 10** | 可作为 `pacer code` / Claude MCP 的实验通道；不能暴露给不可信 tool-args，也不能当高保证验收网关。 |

**综合：6.6 / 10。** 这是一次方向正确、实现密度很高的重构，但还不是“可对外宣称 can_trust”的系统。

---

## 2. 源码级分析

### 2.1 MCP Server：路径安全、协议一致性、`can_trust` 防伪

#### 架构定位

`mcp_server.py` 把 Pacer 嵌进 Claude Code / Codex / Cursor 的 stdio MCP，零第三方传输依赖，工具面收敛为 6 个。这是正确的产品边界：把重活从主对话窗口挪走。

问题在于：**MCP 进程默认继承宿主权限，而 tool arguments 被当成半可信输入。** 当前实现更像“给自己用的 RPC”，不像“给模型用的能力沙箱”。

#### 路径安全

`pacer_index` 是唯一做了显式 traversal 拒绝的入口：

```python
resolved_file = candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
try:
    resolved_file.relative_to(cwd)
except ValueError:
    return { "isError": True, ... "Path traversal rejected" }
```

优点：

- `Path.resolve()` 会展开符号链接，cwd 内 symlink 指到 cwd 外会被 `relative_to` 挡住。
- 拒绝信息明确，失败不继续索引。

缺陷（防御纵深断裂）：

1. **校验的是 `resolved_file`，真正调用传的是原始 `file_path`。** `index_file_symbols()` 会再解析一遍，且 **越界时并不拒绝读取**，只是把 display path 退回原字符串。MCP 层挡住了这一条，但 Orchestrator / 其它调用方没有同等守卫。
2. **`cwd` 本身来自客户端：**
   ```python
   cwd = Path(str(arguments.get("workspace_root") or arguments.get("repo_root") or Path.cwd())).resolve()
   ```
   任意 `workspace_root=/` 或家目录即可把后续 canvas / 验证 / Codex 工作目录打到整机。
3. `begin_pacer_task` / `complete_pacer_task` 再次遍历：
   ```python
   for ws in [arguments.get("workspace_root"), Path(cwd) / ".agent-workspace", cwd]:
       ws_path = Path(ws).expanduser().resolve()
       ...
       update_active_launch(ws_path, ...)
   ```
   **没有 `relative_to(cwd)`。** 这是第二条写路径，可在工作区外写 launch/event。

结论：`pacer_index` 的路径检查是真的，但是 **点状的**；工作区根绑定和 launch 写入是开放的。对“本机开发者驱动 MCP”可接受，对“模型可被 prompt inject 改 tool args”不可接受。

#### 协议一致性

实现了 `initialize` / `ping` / `tools/list` / `tools/call` / 空的 resources&prompts，通知（无 `id`）返回 `None`，stdout 重定向到 stderr，Content-Length 与 NDJSON 双帧，这些都对。

真实问题：

| 点 | 风险 |
|---|---|
| `protocolVersion` 原样回显客户端值 | 未做已知版本协商，异常客户端会以为能力已对齐 |
| `Content-Length` 无上限、无符号校验 | `int(...)` 抛 `ValueError` 会直接打死 stdio 循环；负数 `read(-1)` 读到 EOF；超大 length 可内存打爆 |
| JSON 解析失败 `continue` | 静默丢请求，客户端会挂起等 id |
| 内部错误把 `traceback.format_exc()` 放进 JSON-RPC `data` | 向宿主泄漏绝对路径、密钥环境、内部模块 |
| `get_pacer_memory` 忽略 `detail` | schema 撒谎 |
| `changed_*` 在 mcp_server 中 import 后未使用 | 说明验证守卫本计划在 MCP 完成路径复用，但没接上 |

`tools/call` 把 `{isError, content}` 作为 JSON-RPC `result` 返回，符合 MCP 工具结果惯例，这条没问题。

#### `can_trust` 凭证防伪造

`complete_pacer_task` 的默认值是对的：**`can_trust = False`，只有 `passed and trust_verdict in ("yes", "with_limits")` 才抬升；验证失败强制打回。** 没有 active launch 时也不抬升。这是正确的 deny-by-default。

但凭证链在输入侧是可伪造的：

```python
if steps and isinstance(steps[0], dict):
    argv = steps[0].get("argv")
    if isinstance(argv, list) and argv:
        cmd = " ".join(str(x) for x in argv)
if not cmd:
    cmd = choose_verification_command(cwd) or "python -m pytest -q"
verif = run_command_verification(command=cmd, repo_root=cwd)
passed = verif.get("verdict") == "pass"
```

**P0：验证命令完全由 MCP 参数决定。** 模型/注入方可传 `steps=[{"argv":["true"]}]` 或 `["python","-c","print('ok')"]`。`can_trust` 后续依赖 `audit_task_completion(...)`，而 `summary` / `completion_evidence` / `requested_steps` 同样来自客户端。本次源码包没有 `task_review.py`，因此 **无法证明审计会独立核对 git diff、基线、真实测试命令**。只要审计弱、或 launch 上下文被同一调用方写入，`can_trust=true` 就可以被合成。

更严重的是：`begin_pacer_task` 自己写 `task_lifecycle.status=running`，`complete_pacer_task` 再写 `completed`。生命周期事件与信任结论都在同一非特权 tool 通道里，**没有独立签名、没有 nonce、没有与真实 git HEAD 绑定的服务端凭证。** 这是“状态文件当证书”，不是防伪。

`pacer_auto` 更直接：拉起 `SubagentOrchestrator`，最终可执行带

`--dangerously-bypass-approvals-and-sandbox`

的 Codex。MCP 工具 = 工作区级 RCE。对本地助手是特性，对信任模型是黑洞。

**MCP 小结：** 协议骨架能用；路径守卫只覆盖 index；`can_trust` 是 UI 徽章，不是密码学/审计学意义上的凭证。

---

### 2.2 Subagent Orchestrator：DAG、自愈死循环、补丁原子回滚

#### DAG 编排

`graphlib.TopologicalSorter` 用法基本正确：缺依赖进 `invalid_ids`，`CycleError` 把未调度节点标无效，`run()` 里失败依赖会 skip 下游。

真实限制：

1. **`pacer_auto` 强制 `fast_local_plan=True`，Gemini DAG 分解在主路径上是死代码。** 实际永远是 indexer → coder → verifier 三节点。文档写的“2–5 step DAG”与 MCP 主入口不一致。
2. `select_best_backend()` 对 coder **恒返回 `"codex"`**，`workbuddy_available` / `repeated_failure` 参数无效。WorkBuddy 分发分支存在，但规则计划选不中。
3. 任务 id 重复时 `task_map` 后者覆盖前者，静默丢任务。
4. 环检测后仍把无效节点 append 进 `ordered`，再靠 `invalid_ids` skip——能工作，但状态语义含糊（ordered 里出现了本不该执行的节点）。
5. `_inject_dependency_context` 把上游 output 切 1200 字塞进 `instructions`。coder 真正发送的是 canvas 的 stateless prompt（以注入后的 `instructions` 为 `base_instruction`），所以这条是通的；但 canvas 同时再塞 goal/scratch/focus，**token 预算会被上游原文和 canvas 重复信息一起挤压**，1.5k 上限下容易把 SEARCH 格式说明截掉（`tail_preserve_ratio=0.35` 截的是整段 assembled，格式块在末尾，有一定保护，仍可能被二次 truncate 伤到）。

#### 自我修复与死循环防护

重试上界写的是 `max_retries = 2`，循环条件 `while task.retries <= task.max_retries`，即 **最多 3 次尝试**，不是无限循环。这一点成立。

危险的是控制流，不是计数器：

```python
if task.role == "verifier" and task.backend == "local":
    ...
    upstream_coder.retries += 1
    ...
    self._execute_task_with_retry(upstream_coder)  # 递归重入
    continue                                      # 不看返回值
```

问题清单：

1. **递归而非把 coder 重新入队。** verifier 栈上再跑完整 coder（含验证、再升级 Codex）。coder 自己也有 `verification_command`（规则计划里 t2/t3 命令相同），一次失败可能变成：coder 内验证重试 × verifier 外层重试 × 递归 coder。有上界，但最坏是指数级浪费（3×3 量级的嵌套，再加 180s Codex timeout）。
2. **忽略递归返回值。** coder 已经 failed，verifier 仍 `continue` 再 dispatch。local verifier 的 dispatch 几乎是空操作，然后再次跑同一条测试。
3. **共享可变任务对象。** 递归期间 `upstream_coder.status/retries/instructions/backend` 被原地改；外层 `run()` 若已把 coder 标 completed，这里会把历史状态打乱。`completed_ids` 不会回滚，canvas scratchpad 会留下交错的 attempt。
4. 升级策略 `gemini/workbuddy → codex` 只发生一次方向，不会在 Codex 与 Gemini 之间振荡，**模型乒乓死循环不存在。**
5. 验证缓存 `(command, focus_fingerprint)` 用 focus 文件的 `(mtime_ns, size)`。未进入 canvas 的文件改动不会使缓存失效；反过来，coder 若改了非 focus 文件且没 `upsert`，verifier 可能 **误复用成功回执**。当前 upsert 覆盖了 patched_files，主路径大体安全，边路径不安全。

**结论：没有无限死循环，但有“有界的递归风暴”和状态机污染。** 这不是生产级自愈，是把失败当栈重放。

另外，规则计划让 **coder 先跑验证，verifier 再跑一次**。缓存是为这个重复设计的。设计可以更干净：coder 只出补丁，verifier 只做门禁，失败则把 error 写 canvas 后把 coder 设回 pending，由 DAG 驱动而不是递归。

#### 补丁原子回滚

`diff_patcher.py` 单文件内是认真做了的：

- 按文件分组，内存 staged，**同文件后续 block 失败则整文件不落盘**，已匹配成功的 op 标 `rolled_back`。
- `tmp + os.replace`，POSIX 上替换原子。
- 三层匹配：exact → rstrip → indent/blank-tolerant。
- `..`、workspace escape、`allowed_files` 在 `resolve_safe_workspace_path` 里有实现。

致命缺口在调用方：

```python
patch_res = apply_search_replace_text(
    run_output,
    workspace_root=self.workspace_root,
    default_file=default_target,
    # 没有 allowed_files
)
```

**Orchestrator 没用允许列表。** LLM 只要在输出里写路径，就可以改工作区内任意文件（含测试、CI、`.pacer/`、密钥文件）。测试篡改守卫发生在 **写盘之后** 的 git 比对，属于检测而非阻止。验收链文件同理。

其它补丁语义问题：

| 行为 | 影响 |
|---|---|
| 跨文件非原子 | A 已写、B 失败 → 工作区半应用 |
| 空 SEARCH + 已有内容 = **追加** | 与“新文件才允许空 SEARCH”的 prompt 不一致，会重复插入 |
| `str.replace(..., 1)` | 公共片段会打到第一处，可能错位点 |
| indent/非空行匹配过宽 | 可能贴到错误函数 |
| basename 唯一匹配回退 | `foo.py` 可打到允许列表里另一个目录的同名文件 |
| 无 fsync、无文件锁 | 崩溃/并发下 canvas 有锁（还可能是空锁），源码文件没有 |
| 与 `extract_and_apply_code_blocks(..., allowed_files=task.target_files)` 不一致 | 两条写盘路径安全策略不同 |

验证通过但补丁未应用时，代码把这种情况当失败并升级 Codex，这是对的，能挡住“假绿”。这条逻辑是本模块最有价值的护栏之一。

#### Canvas / Indexer 对编排的支撑

Canvas 的 token 切片（goal 18% / task 22% / scratch 22%+tail / focus 吃剩余）是这次重构的真正核心，质量明显高于编排器。`failed_hypotheses[-5]`、`completed_steps[-8]`、错误截断 1200，都是为无状态 agent 准备的正确形状。

降级点：`portalocker` 缺失时 `_file_lock` 直接 `yield`，**跨进程 canvas 无锁。** 与 Codex 的 psutil 问题是同一类“可选依赖变成空安全”。

Indexer 作为 `<20ms` 替换 LLM indexer，AST 抽取、包围函数窗口、traceback 选非测试帧，都合理。但：

```python
# discover_target_files_for_goal 的正则字符类包含 '.'
# 因此 '..' 可作为路径分量
explicit = _GOAL_FILE_RE.findall(goal)
valid = [rel for rel in explicit if (repo_root / rel).is_file()]
```

`../secret.py` 若存在，会被收进 `target_files`，随后 `index_file_symbols` `resolve()` 后 **照读**。这是 Orchestrator 侧真实的路径穿越，不依赖 MCP。

---

### 2.3 Delta Action：干员状态机、穿甲、保险箱

这是压力测试夹具，不是引擎内核。作为“能否在无历史对话下改对代码”的靶子，结构合格；作为战术射击模拟，有几处文档承诺未兑现。

#### 干员状态机

```
ACTIVE --HP<=0--> DOWNED --bleedout/二次伤害--> KIA
DOWNED --revive(kit)--> ACTIVE (35% HP)
ACTIVE --zone timer--> EXTRACTED
timeout --> die()
```

实现与测试对齐的部分：

- Assault 耐力 120、Medic 自救针、头 2.5x、倒地 30s、复活 35% HP，全部被测试钉住。
- `EXTRACTED` / `KIA` 免疫伤害。
- 倒地后再命中（哪怕四肢）直接 `die()`：extraction shooter 的处决语义，合理。
- `is_alive` 含 DOWNED/REVIVING，撤离 tick 却要求 `state == ACTIVE`，倒地者占区但不结算撤离，合理。

缺口：

1. **`OperatorState.REVIVING` 从未被赋值为进入。** `revive()` 从 DOWNED 直接 ACTIVE。枚举和 `take_hit` 分支是死代码。
2. **`ExtractionZoneStatus.CONTESTED` / `INACTIVE` 从未使用。** 文档写了 zone contest，代码是“区内全员到点一起走，然后 EXHAUSTED”。多干员同时在场不会对抗。
3. 耐力只回复、不消耗；Assault 大招“回满耐力”在当前模型里几乎无意义。
4. 倒地干员仍可通过 `is_alive` 进入撤离区，污染 `occupants`。

状态机是 **线性主路径正确、并行/对抗状态未建模**。对当前测试集足够，对文档中的“zone contests / bleedout 完整 SM”不够。

#### 穿甲计算

```python
effective_tier = tier * (0.35 + 0.65 * durability_ratio)
tier_diff = bullet.penetration_tier - effective_tier
```

分段阈值（≥1.5 → 0.98，≥0.5 → 0.85，≥0 → 0.50+，≥-1 → 低穿，否则 0.02）能解释测试：

- M995 T5 vs PACA T2 满耐久：diff=3 → 0.98
- Pst T2 vs Defender T5：diff=-3 → 0.02
- 未击穿：`blunt = flesh * 0.18`，54×0.18=9.72 < 15
- 击穿后仍扣耐久，破甲后 `is_broken` 跳过装甲，逻辑闭环

需要点名的模型问题（不一定是 bug，但是审查要说清）：

- **暴击倍率加在装甲结算之后**，未击穿的头部钝伤也会 ×2.5，偏怪。
- `random.random()` 无注入时不可复现；测试都传了 `Random(42/1)`，生产战斗无法回归。
- `armor_damage_factor` 与相对等级线性放大，T6 打 T1 会很快打碎装甲，符合战术，但没有材料差异（ceramic/steel 字段未参与公式）。
- 胃部走背心、四肢无甲，简单但自洽。

作为压力测试，公式有稳定断言，适合当验收靶。不要把它当成已平衡的射击数值系统。

#### 保险箱资产“持久化”

`drop_on_kia()` 只清空 backpack、保留 `safe_box`，测试覆盖了 GPU+U 盘进盒、医疗包掉落、安全箱价值不变。**匹配内语义正确。**

但“持久化”在源码里 **没有跨对局、没有落盘、没有与 Operator 序列化绑定**。`total_extracted_value` 在未撤离时把 backpack 也算进去，命名会误导统计。

`add_loot` 在 safe box 满时把 `is_secure=True` 的物品塞进背包并 **清掉 secure 标记**，高价值物会在阵亡时丢掉。这是真实逻辑漏洞，测试没覆盖满箱路径。

`test_interactive_game_session_demo` 依赖未在本次包中的 `examples.delta_action.play.GameSession`。若 `play.py` 不存在或 demo 不稳态，整条压力测试会红。审查时把它视为 **外部隐式依赖**，不是 core.py 的问题，但是发布风险。

---

## 3. 对 Codex 四点发现的裁决

> 这四个文件未包含在你粘贴的源码中。以下裁决基于缺陷描述的普遍正确性，以及它们与本次已审代码暴露出的同类问题是否同构。

### 3.1 `workbuddy_bridge.py`：psutil 未声明

**裁决：成立（P1，包装/运行时）。建议修。**

与本次已确认的 `portalocker` 模式同构：`canvas_manager` 已经 `try: import portalocker except: None` 然后把锁变成空操作。psutil 若在 import 顶层硬依赖，未声明就会让 WorkBuddy 探测直接 ImportError，MCP 的 `WorkBuddyGatewayClient().is_available` 变脆。

修复建议（二选一，优先容错）：

```python
try:
    import psutil
except ImportError:
    psutil = None

def _pid_alive(pid: int) -> bool:
    if psutil is None:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    return psutil.pid_exists(pid)
```

同时在 `pyproject.toml`/`requirements` 把 `psutil` 放 optional extra：`pacer[workbuddy]`。不要让核心 MCP 路径硬依赖它。

### 3.2 `model_router.py`：否定词误伤后续正面诉求

**裁决：成立（P1，正确性）。建议修。**

“Do not refactor but secure payment” 这类句子，若用 `do not X` 直接切掉 X 及之后全部 token，会把 `secure payment` 一起丢掉。这是指令路由里的经典失败。

修复建议：否定辖域在转折处截断，而不是删到句末。

```python
_CONTRAST = re.compile(
    r"\b(?:but|however|instead|except|rather|不过|但是|而是|只是)\b",
    re.I,
)
_NEG = re.compile(
    r"\b(?:do not|don't|never|cannot|严禁|不要|不能|禁止)\b.{0,80}?",
    re.I,
)

def strip_negated_spans(text: str) -> str:
    parts = _CONTRAST.split(text, maxsplit=1)
    head, tail = parts[0], (parts[1] if len(parts) > 1 else "")
    head = _NEG.sub("", head)
    return (head + (" but " + tail if tail else "")).strip()
```

要点：

- 先按转折切，再只在前半段做否定删除。
- 中英标记都要有（本次 orchestrator 的 `_goal_allows_test_edits` 已经是中英双表，路由层应对齐）。
- 加单测：`Do not refactor but secure payment` → 仍含 `secure payment`；`never modify tests, add feature X` → 不含 test edit 许可。

注意：`subagent_orchestrator._goal_allows_test_edits` 用的是子串包含，**没有这个问题**（否定表优先 return False）。不要把那边的逻辑“复用”到 router 的删除式实现上。

### 3.3 `game_2048/core.py`：默认初始化应生成起始方块

**裁决：成立（P2，产品正确性）。建议修。**

标准 2048 在空盘上生成 2 个方块（90% 为 2，10% 为 4）。若 `Board()` / `reset()` 后 `empty`，所有依赖“开局可移动”的测试和 demo 都会假失败或假成功。

修复建议：

```python
def reset(self) -> None:
    self.grid = [[0]*4 for _ in range(4)]
    self.spawn()
    self.spawn()
```

`__init__` 走 `reset()`，并保证 `spawn` 在无空格时 no-op。单测断言开局非零格数为 2。这与 Delta Action 无关，不要把两个 example 的验收搅在一起。

### 3.4 `cli.py`：写入 `~/.claude/commands/pacer.md` 应保护已有文件

**裁决：成立（P1，用户数据）。建议修。**

无条件覆盖会毁掉用户自定义 slash command。这是安装器经典事故。

修复建议：

```python
dst = Path.home() / ".claude/commands/pacer.md"
dst.parent.mkdir(parents=True, exist_ok=True)
if dst.exists():
    old = dst.read_text(encoding="utf-8")
    if old == new_content:
        return
    bak = dst.with_suffix(".md.bak")
    if not bak.exists():
        shutil.copy2(dst, bak)
    # 若旧文件不含 Pacer 管理标记，拒绝覆盖并打印路径
    if "<!-- pacer-managed -->" not in old:
        print(f"skip existing custom command: {dst}")
        return
dst.write_text(new_content, encoding="utf-8")
```

原则：**托管文件用标记；无标记不覆盖；覆盖前留一份 bak。**

---

## 4. 总体结论与上线建议

### 这次重构真正值的部分

- **Canvas 作为外置工作记忆** 替换多轮聊天，是架构主胜负手，实现也是四份核心代码里最干净的。
- **本地 AST indexer + 三层 SEARCH/REPLACE** 把“读整文件进上下文”这条成本打掉了，方向对。
- **测试篡改 / 验收链文件** 检测、补丁未应用则不准报绿、验证指纹去重，说明作者理解假绿问题。
- Delta Action 作为压力靶，主路径测试密度足够，能锻炼补丁器与验证环。

### 上线前必须修的 P0（否则不要宣传 can_trust）

1. **禁止客户端用 `steps[].argv` 指定验收命令。** 命令只允许来自 `choose_verification_command(repo)` 或 launch contract 里已签名的 allowlist。`complete_pacer_task` 的 `can_trust` 必须绑定：真实 pytest/profile 命令、git diff 相对 `source_baseline`、测试文件未改（除非 goal 显式允许）。
2. **工作区根钉死：** MCP 启动时锁定 `PACER_WORKSPACE`，tool args 不得再把 cwd 指到任意绝对路径；launch/event 写入必须 `relative_to(root)`。
3. **Indexer / goal 文件发现拒绝 `..` 与 root escape**，越界不读文件。
4. **`apply_search_replace_text` 必须传 `allowed_files`**（目标文件 ∪ canvas focus，且永远排除 `tests/`、CI、`.git`、密钥路径，除非 goal 白名单）。
5. **Content-Length 上限**（例如 2–8MB）+ 解析异常不得杀死 stdio 循环；JSON-RPC 错误禁止回传 traceback。
6. **自愈改成状态机：** verifier 失败只写 canvas 并把 coder 设为 pending，禁止 `_execute_task_with_retry` 递归；跨文件补丁改为 all-or-nothing（先全部 staged，再写，或写前备份）。

### P1（不修也能内部用，但不能当 1.0）

- `portalocker` / `psutil` 要么正式依赖要么有非空降级策略，并在文档写明无锁则禁止多进程。
- Codex 的 `--dangerously-bypass-approvals-and-sandbox` 必须是显式 opt-in，默认只读/workspace sandbox。
- 空 SEARCH 对已存在文件应失败，而不是追加。
- Delta Action：要么实现 `REVIVING` 与 `CONTESTED`，要么从文档/验收描述删除；补 `safe_box` 满溢测试；`play.py` demo 纳入包内或从测试删除。
- 落地 Codex 四点（psutil、否定辖域、2048 开局、claude command 备份）。

### 上线建议（直接、可执行）

- **可以：** 以 “local experimental MCP / `pacer code` 内部通道” 灰度，受众 = 仓库主人，默认信任边界 = 整机用户权限。打开时明确打印：模型 tool-call 等价于本机非沙箱 agent。
- **不可以：** 把 `can_trust=true` 当作向终端用户交付的质量证书；不可以给多租户、CI 门神、或会把 MCP 暴露给不可控 prompt 的 IDE 默认开启。
- **发布门槛：** P0 六条合入 + 针对“伪造 steps 命令”“`../` 读文件”“跨文件半应用”“verifier 递归重入”的回归测试全绿。Delta Action 测试作为压力套件保留，但不要用它代替对 MCP 信任边界的测试。

**一句话：重构的认知是顶级的（无状态画布、本地索引、差分补丁、假绿防护），信任边界的闭合是中级的。先锁门，再打 can_trust 徽章。**