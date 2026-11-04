# Pacer GPU 外骨骼与 A* 架构审查：两小时初步结果

日期：2026-11-03

## 结论

1. 采用选择性替换：激进重做调度、验证与故障恢复内核；保留已验证的无状态上下文投影、worktree 隔离、差异应用、反例验证和状态记录能力。整库推倒会丢失现有实证；把 GPU 搜索直接嫁接到无硬期限的旧调度器也会放大挂起风险。
2. 本轮新增的是隔离的参数化策略搜索切片，不是代码编辑 A*。PyTorch CUDA 在 RTX 5060 上完成批量情景评分，CPU 独立复算并验证 holdout；候选必须优于空仓基线才可入选。
3. 在 128 候选 × 256 情景 × 1024 步、5 次中位数测试上，CUDA 评分 0.0601 秒，CPU 评分 1.3621 秒，评分快 22.67 倍；含校验、哈希、验证与回放的总时间分别为 0.2919 秒和 1.7082 秒，端到端快 5.85 倍。两端排名第一候选一致，但合成场景下所有候选均未优于空仓，因此没有候选入选。
4. Gemini 3.8 生成的 8 个种子扩展为 64 个候选，32 情景 × 256 步验证中 CUDA 与 CPU 排名一致，完整搜索总时间为 0.00887 秒与 0.06035 秒；所有候选仍未胜过空仓。该结果证明链路可运行，不证明交易策略有效。
5. Opus 5.5 的裁决是“不整库重写，也不简单增量嫁接”，而是先替换中心调度/验收内核。代码修改上的 A*/MCTS 没有合理的廉价转移与启发式；应采用异步云端稀疏候选 + 本地 best-first 排队 + 分层确定性门禁。GPU 用于数值/情景并行，而不是为了显示利用率而常驻大模型或图框架。

## 本机环境实测

| 项目 | 实测结果 |
|---|---|
| Windows | Windows 11 家庭中文版，Build 26200，64 位 |
| WSL | Ubuntu 与 Docker Desktop 均运行于 WSL 2 |
| GPU | NVIDIA GeForce RTX 5060 Laptop GPU，8151 MiB |
| 驱动 | NVIDIA 573.22，`nvidia-smi` 报告 CUDA 12.8 |
| Python | 固定解释器 3.11.9，Windows AMD64 |
| PyTorch | 2.9.1+cu128；`torch.cuda.is_available()` 为 true；设备名为 RTX 5060 Laptop |
| 已安装科学包 | NumPy 1.26.3、SciPy 1.17.1、Pandas 2.3.3、Numba 0.67.0、TorchAudio 2.9.1+cu128 |
| 未安装/未发现 | TorchVision、Triton、CuPy、cuGraph、cuVS、FAISS-GPU、NVIDIA Warp |
| 空闲状态快照 | GPU 利用率 0%，显存占用 27 MiB / 8151 MiB；这是基准前快照，不是基准运行时利用率 |

当前实测的 PyTorch CUDA 工作负载可直接在固定 Windows Python 运行。RAPIDS/cuGraph、cuVS、FAISS-GPU、Warp 等尚未安装，本轮没有验证这些框架与 RTX 5060 sm_120、WSL2 或显存分配器的兼容性。先继续使用 PyTorch CUDA + NumPy + SQLite；CPU 侧图切片可用 SciPy，等真实瓶颈出现再加库。

## 本轮实现与验证

- [strategy_search.py](/D:/pacer-recovery/20261101/worktree/xiao/src/visual_agent/vnext/strategy_search.py)：严格参数候选、有限邻域扩展、固定费用/风险规则、float64 PyTorch CPU/CUDA 批量评分、训练集前若干名的 Python 独立复算、独立 holdout 门禁、空仓基线拒绝和含形状/双精度值的输入哈希。
- [pacer_vnext_gpu_demo.py](/D:/pacer-recovery/20261101/worktree/xiao/scripts/pacer_vnext_gpu_demo.py)：可运行 CPU/CUDA 比较和可选 Gemini 3.8 种子流程。模型只提参数种子，不能修改评分政策。
- [test_vnext_strategy_search.py](/D:/pacer-recovery/20261101/worktree/xiao/tests/test_vnext_strategy_search.py)：10 项定向测试通过，覆盖严格 schema、邻域上限、空仓门槛、独立 holdout、无 lookahead、CPU/CUDA 一致性、回放状态和输入哈希。
- 最终验证：固定 Python `py_compile` 成功；`pytest -q tests/test_vnext_strategy_search.py` 为 `10 passed in 2.09s`。没有重跑旧版长回归。

核心 GPU 基准（最终运行）：CPU scoring 1.3621 s，CUDA scoring 0.06008 s，22.67×；CPU full 1.7082 s，CUDA full 0.29194 s，5.85×；CUDA 峰值分配 104.07 MiB；CPU/CUDA 排名第一候选相同；`selected_candidate_id = null`。性能比值只针对这组合成任务，CUDA full 包含校验、哈希、训练评分、独立复算与 holdout。

Gemini 种子链路（真实调用）：本机 Gemini 3.8 返回 8 个参数种子，最大 lookback 按 256 步任务约束到 255；邻域展开为 64 个候选。第一次短步数尝试曾返回超场景 lookback，本地校验拒绝该结果；随后把有效上限加入模型提示与扩展器，并成功重跑。32×256 任务评分快 7.72×，总耗时 CPU 0.06035 s、CUDA 0.00887 s；排名一致、没有候选越过空仓。生成的高 lookback/高阈值候选大多保持空仓，显示生成多样性不等于搜索质量。

本轮不声称收益、市场有效性、代码修改加速或 GPU 满载。GPU 的实测用途是密集数值评分；总耗时中的 Python 校验/哈希与数据转换开销仍明显。

## 独立模型审查与凭证

### Opus 5.5

- 请求模型：`claude-opus-5-5`；服务返回模型：`claude-opus-5-5`；HTTP 200；`stop_reason=end_turn`。
- 用量：52,691 输入 tokens，14,523 输出 tokens；耗时约 275.86 秒。
- Prompt SHA-256：`e50e1796240f2a63c21094b1611ef216979d20dfb73466f37a0b41f547261ee4`。
- 完整原始 SSE 事件、prompt、回复和 metadata 保存在 `D:\pacer-recovery\20261101\final-audit\vnext_mvp_review_20261103_opus.*`。metadata 记录凭证来源，不保存密钥明文。
- Opus 的关键发现已落实到切片：候选必须胜过空仓；必须提供不同的 holdout 才可能选择；训练集前 `top_k` 回放；区分未运行与通过；float64 降低信号边界分歧；哈希加入形状与 float64 输入；计时从调用入口开始。
- Opus 对旧 Pacer 的网关断连/无期限等待、提交线程失败锁泄漏、测试归属、focus 预算和路由复杂度提出了高风险检查点。它明确说未读取多数模块实现；应把这些作为源码核查项，而不是全部视作已证实根因。

### Gemini 3.8

- 本机 `127.0.0.1:8080` Antigravity HTTP 快路径曾返回真实模型标识 `gemini-3.8-flash-low`，但被服务截断为 164 completion tokens，`finish_reason=length`；这份只作为不完整调用凭证。
- 随后通过配置的 Gemini 3.8 CLI 回退完成短版独立审查，成功、耗时 59.73 秒。配置的 Antigravity 上游 `127.0.0.1:18193` 当时不可达；CLI 回退请求模型为 `gemini-3.8-flash-low`，CLI 原始输出未包含独立的 returned-model 字段。
- Gemini 撤回了“回撤初始峰值漏算”的疑点：当前 `lookback >= 2` 且前两步强制空仓，PnL 曲线先出现 0，因此该指控不可达。该审查结果体现了对模型 findings 的反例核验。
- Gemini 还指出显存预算最好按字节而不是元素数估算，并确认这类 GPU 加速对代码编辑本身没有直接价值。它把 22.67×称为理论加速；本轮实测评分比值确为 22.67×，但端到端为 5.85×，报告采用实测定义。
- 完整 CLI 审查回复与原始输出保存在 `D:\pacer-recovery\20261101\final-audit\vnext_mvp_review_20261103_gemini_full.json`；首次截断回复在 `vnext_mvp_review_20261103_gemini.json`。

## 架构裁决与落地清单

### 现在应该做什么

1. 将 Pacer 运行改为单一确定性内核：typed task spec、预检 Early Exit、清晰路由表、单调墙钟 deadline、每阶段超时、固定并发上限、任务事件日志和可恢复隔离。
2. 保留现有已经产生实证的能力：`canvas_manager` 无状态 prompt 投影、`session_projection` 状态折叠、worktree/checkpoint/diff 应用、反例验证和基础模型协议适配。每个保留模块以当前源码和测试复核，避免按文件名推断。
3. 先处理网关可靠性：客户端断开捕获 `ConnectionError/OSError`；所有结果队列写入和事件读取设 deadline；`executor.submit` 失败也必须释放 `run_lock`；对子进程设置独立 wall timeout 和可杀进程组。
4. 将测试目录、pytest 配置、收集到的 test ID 和基线哈希设为不可变验收输入。worker 可产出代码候选，但不能独自改验收标准。三振熔断应保存 patch、worktree 和 checkpoint 到 quarantine，禁止对用户工作树 `git reset --hard`。
5. 代码修改使用异步云端 Best-of-N 候选 + CPU best-first 队列。候选按 patch 应用、AST parse、lint/import、影响测试、完整套件分层筛选并缓存；在廉价 gate 阶段 GPU 没有已证明收益。把候选状态与结果写入数据集，等标签规模足以胜过逻辑回归后再评估 value probe/GNN。
6. GPU 保留给批量策略/数值模拟或真实的大规模差分测试任务。当前切片先按实际字节预算限制内存、避免无界并发调用，并记录 GPU 用量与端到端时间；不安装 cuGraph/cuVS、mctx、TreeQuest、Scallop、Lobster 或 Genesis，直到具体任务证明必要。

### 一到两周验收路线

| 阶段 | 工作 | 验收证据 |
|---|---|---|
| 0–2 天 | 在外层硬超时下完成已停止的 Pacer 全量回归并 reload 8091 gateway；修网关 deadline 和锁泄漏 | commit SHA、完整 pass/fail 记录、断连/卡死注入测试按时退出 |
| 2–4 天 | 冻结只读测试集、验证文件哈希和测试收集 ID；统一 typed task spec 和 backend deadline | cheat-bait 测试修改为 0 次被接受；验收基线在 worker 之外 |
| 4–7 天 | 重放 Pacer 历史修复任务，记录每个候选、gate、成本和结果 | 每项任务可复现；隔离回滚成功；运行超时为 0 |
| 8–10 天 | 同一任务集比较旧 Pacer 与新内核 | solve rate、false accept、regression、token/call 成本、median/p95 wall time |

建议的真实非金融验证集是从仓库历史中抽取 10–20 个“父提交失败、新修复提交通过”的 bug 修复任务，固定 acceptance 测试并加入测试篡改诱饵。GPU 是否参与由 profiling 决定；先比较可靠性、挂起率、误接受和总时延，而不是 GPU 利用率。

## 尚未完成和证据边界

- `STOPPED-STATUS.json` 记录的最新 Pacer 全量回归和 8091 gateway reload 仍未完成。本轮没有运行它们。
- Opus 有完整原始输出，但输入只含模块清单与选定源码摘录，不是对 40+ 模块逐行审查；它自己列出了未查看的模块和待核证据。
- RAPIDS、Warp、Genesis、mctx、TreeQuest、Scallop/Lobster 的平台/版本兼容性没有在本机安装验证。Opus 表中的说法是有条件的技术判断，不是环境实测。
- 合成情景不是市场数据；负分与拒绝候选是正确的门禁表现，不构成任何策略表现结论。
- 本次只验证新增切片的 10 项定向测试。旧 Pacer 的历史测试结果不能替代当前 HEAD 的全量回归。

## Opus 5.5 原始完整审查报告

以下为 `claude-opus-5-5` 的完整回复原文（未改写）。

# Independent review: Pacer, the GPU exoskeleton, and the vnext strategy-search MVP

## Evidence scope

- **Observed:** the source excerpts above, the module name inventory, STOPPED-STATUS.json, and your reported benchmark numbers.
- **Not observed:**
  - Contents of most Pacer modules (only names).
  - Line numbers, so findings cite file and function.
  - `gemini_bridge.invoke_gemini`, `tamper_guard.py`, `acceptance_discrimination.py`, `process_guard.py`, `subprocess_window.py`, and `model_admin.handle`.
  - Which Python runtime (Windows or WSL) produced the PyTorch observations.
  - The raw benchmark JSON.
- **Current state of Pacer:** the full regression and the 8091 gateway reload are recorded as not completed. Nothing below claims the current tree passes.

---

## 1. Correctness defects in the MVP (`strategy_search.py`)

Ordered by severity.

**D1. The selected candidate is worse than doing nothing, and the system cannot detect it.** (`search_strategies`, `_summarize`, `_sort_scores`)
- Eligibility checks only `worst_max_drawdown <= max_drawdown`. There is no null baseline.
- A flat strategy (never enter) scores exactly 0.0. You observed that all top scores were negative, so every "selected" candidate lost to flat.
- **Fix:** add a mandatory flat baseline and require `score > baseline_score + margin` for eligibility. Until then, `selected_candidate_id` means "least bad", not "acceptable".

**D2. Selection and evaluation use the same scenarios.** (`search_strategies`)
- The argmax over 128 candidates is reported on the data it was chosen from, which biases the reported score upward.
- There is no train/holdout split, walk-forward, or multiple-comparison correction.
- On synthetic data this is a design flaw. On real data it would invalidate any score claim.
- **Fix:** split scenarios into selection and holdout sets, select on the first, and report and gate on the second.

**D3. float32 tensor scoring against float64 replay can flip discrete decisions.** (`_evaluate_torch` vs `_evaluate_python`, `_scores_match`)
- *Mechanism:*
  - `torch.tensor(scenarios, dtype=torch.float32)` and float32 `thresholds` feed `momentum > threshold`.
  - The Python path compares in float64. For example, 0.001 as float32 is 0.0010000000475.
  - float32 cumsum error over 1024 to 8192 steps can reach roughly 1e-7 to 1e-6.
  - Any step where |momentum − threshold| falls inside that error band can take a different position.
- *Consequence:* one flip in one scenario changes that scenario's total by roughly the fee plus one return (about 0.01). Averaged over 256 scenarios, that is about 4e-5, which exceeds `replay_tolerance = 2e-5`.
- *Inference, not measured:* with σ = 0.01 returns, short lookbacks, and 262k decisions per candidate, I estimate an expected near-boundary count on the order of one per candidate at your benchmark size. It grows with steps and scenarios.
- Your one fixture passed, which does not show this cannot happen.
- *Current behavior:* a mismatch sets `selected_id = None`. That fails closed, which is safe but brittle.
- *Missing evidence:* a count of decisions with |momentum − threshold| < 1e-6 per run.
- **Fix:** score in float64 (cost is likely small at these sizes, but measure it), or compute the signal margin and treat near-threshold decisions as "ambiguous", replaying them exactly.

**D4. The input hash does not identify the input.** (`_input_hash`)
- Shape is not hashed. A 2×2 scenario set and a 1×4 set with the same flattened values produce the same digest. The equal-length validation does not prevent this.
- `struct.pack("<f", value)` hashes the float32 rounding of float64 inputs. Two float64 inputs that the Python replay scores differently can therefore share a `run_id`.
- `entry_threshold=0` (int) and `0.0` serialize differently in `asdict` JSON but compare equal as dataclasses. The hash and equality disagree.
- **Fix:** hash shape, dtype, `"<d"` values, a schema version, torch version, and backend.

**D5. Reported "full search" time excludes validation and hashing.** (`search_strategies`)
- `started` is set after `_validate_inputs` and `_input_hash`. Both are pure-Python loops over every return value (262k at your benchmark size), and `struct.pack` runs once per value.
- `scoring_seconds` includes the CPU-side `torch.tensor(nested_list)` conversion and the host-to-device copy. The 4.39× figure is therefore function-level, not kernel-level.
- The 1.90× "full search" figure understates true wall time on both devices.

**D6. `top_k` is effectively dead.** (`search_strategies`, `SearchPolicy.top_k`)
- `eligible[:top_k]` is computed, but only `eligible[0]` is replayed.
- If the leader's replay fails, there is no fallback to rank 2.
- The test `test_cuda_rank_is_cpu_replayed_before_selection` locks in `cpu_replay_count == 1` with `top_k=2`.
- The docstring ("accept only candidates that pass CPU replay") implies more than one is replayed.

**D7. `replay_passed=True` when no replay ran.** (`search_strategies`)
- This happens when nothing is eligible, and when `torch is None` (in that case scoring was already pure Python, so this is consistent but unreported).
- Downstream consumers cannot tell "verified" from "not attempted".
- **Fix:** use a tri-state: `passed`, `failed`, or `not_run`.

**D8. The replay is implementation-independent but not specification-independent.**
- `_evaluate_python` encodes the same rules: same prefix-sum signal, same additive PnL, same fee on turnover.
- It catches tensor and indexing bugs. It cannot catch a wrong economic model.
- Examples of model choices it would never question:
  - Equity is a sum of simple returns, not compounded.
  - `max_drawdown` is in additive return units, not a fraction of equity, despite the 0–1 bound.
  - No exit fee is charged at the end of the series.
- These are acceptable choices for a synthetic fixture, but they should be documented as the model rather than presented as validated risk semantics.

**D9. Minor validation inconsistencies.**
- The Gemini prompt says `lookback 2..512`, but the parser accepts `2..8192`. A lookback greater than or equal to `steps` silently becomes a flat strategy.
- `candidate_id` is validated stripped but stored unstripped, so `" a"` and `"a"` are distinct.
- `_validate_inputs` accepts `"0.1"` and `True` via `float()`.
- `expand_candidate_neighborhood` does not check output-id uniqueness. `_validate_inputs` catches collisions later, which fails closed.

**D10. Test gaps.** No tests exist for:
- Replay-mismatch behavior.
- The no-eligible path.
- The hash collision in D4.
- Near-threshold float32 flips.
- `request_gemini_seed_candidates` with a stubbed `invoke_gemini` (oversize payload, `parsed_json=None`, extra keys).
- CUDA vs Python at benchmark scale. The CUDA test uses 2×2×8.

`test_cpu_tensor_scores_match_independent_python_reference` also does not compare `return_std`.

`test_signal_uses_only_returns_before_the_evaluated_step` is correct. I checked the arithmetic: −0.5 − 2 × 0.0008 = −0.5016. It does show that the position at step t uses only returns before t.

**Benchmark provenance (inference).**
- 128 candidates equals the local 16×8 grid. The Gemini path yields at most 8 seeds × 9 neighbors = 72 candidates.
- The reported benchmark therefore did not exercise Gemini seeding.
- The "CPU/GPU selected the same candidate" check compares torch-CPU float32 with torch-CUDA float32, not either one with Python float64.
- The demo compares only `rows[-1]`. Run-to-run determinism across the 5 repeats was not checked.

**Memory (observed and consistent).**
- About 40 bytes per element (float32 intermediates plus int64 `cummax` indices) times the 1.5M element cap is about 60 MB, which matches the observed 57 MiB.
- OOM is not a risk in this slice.
- The GPU is mostly idle, because each 5-candidate chunk forces a `.cpu().tolist()` host sync.

---

## 2. Pacer findings from the supplied excerpts

**P1. Possible gateway wedge on client disconnect.** (`pacer_model.py`, `_stream` and `publish_result`)
- `_stream` sets `disconnected` only on `BrokenPipeError` and `ConnectionResetError`.
- On Windows sockets, a client abort commonly raises `ConnectionAbortedError` (WinError 10053), which is neither of those.
- If that happens, `disconnected` is never set. Once 256 progress events fill the queue (progress is dropped silently with `put_nowait`), `publish_result` loops forever on `put(timeout=0.25)`.
- `execute`'s `finally` then never runs, `run_lock` is never released, and every later task gets 409.
- *Requires a check* of which runtime hosts the gateway.
- **Fix:** catch `ConnectionError` and `OSError`, and give `publish_result` a deadline.

**P2. No wall-clock deadline on a run.**
- The non-stream path blocks on `events.get()` with no timeout.
- The stream path loops on 5-second gets forever.
- For OpenAI protocols no ping is sent, so a dead client is never detected while the engine is silent.
- This is consistent with the historical hang reports. Root cause not proven.

**P3. Lock leak if `executor.submit` raises.**
- `run_lock` is acquired before `self.server.executor.submit(execute)`.
- If the executor is shut down, or anything between acquisition and submit raises, the lock is never released.

**P4. Admin handler runs before authentication.**
- `model_admin.handle(self, "POST")` runs before `_authorized()`.
- If `model_admin` does not authenticate internally, this is an authentication bypass on admin endpoints.
- *Requires a check* of `model_admin.py`.

**P5. Amendment loss.**
- A queued follow-up receives a 200 "entered the workflow" response.
- If the first run raises, the follow-up is dropped, and only the first client is told.

**P6. Tests are model-authored.** (`subagent_orchestrator._decompose_numbered_file_items`)
- Module coder tasks are instructed to "Create and run the named tests".
- `coder_tests` writes the shared acceptance tests with a cloud backend.
- The same model population writes both the code and its judge. STOPPED-STATUS mentions SHA-delta checks and acceptance tests, so some mitigation exists, but I cannot see it.
- This is the largest anti-cheating gap relative to Architecture C.

**P7. Verification commands built by string concatenation.**
- `verification_command` is built as `"python -m pytest " + '"{p}"'` from paths parsed from goal text.
- If it runs through a shell, a path containing `"`, `$`, or `` ` `` is an injection vector.
- *Requires a check* of the executor. **Fix:** pass an argv list.

**P8. Focus starvation in `build_stateless_prompt`.** (`canvas_manager.py`)
- The fixed caps sum to 99% of budget: goal 8, task 18, invariants 12, scratch 15, exam 6, profile 10, contract 12, format 18.
- Focus files (the AST signatures, the only code context) get whatever remains.
- When the other blocks are near their caps, focus approaches zero.
- The final middle-truncation (`tail_preserve_ratio=0.35`) can then cut the focus block or the SEARCH/REPLACE format text.
- The historical 736-token figure says nothing about whether the right code was in those tokens.

**P9. Unbounded state growth.**
- Every assembled prompt is appended to `session_projection`.
- `snapshot()` folds the whole log on every call.
- Without compaction this grows linearly, which matters for long unattended operation.

**P10. Routing complexity.** (`_dispatch_with_failover`, `cost_candidates`)
- Four ordering layers interact:
  1. The `PACER_CODER_CASCADE` override, read in two places.
  2. `cost_candidates`.
  3. The caller-preferred free backend.
  4. The family-fallback splice, which can move `gemini38` or `codex` ahead of the cost order.
- The resulting order is hard to predict without running it.
- *Inference:* the historical 64 s vs 260 s gap for one vs two modules looks like serialization rather than parallelism. STOPPED-STATUS supports this: Gemini timed out and Codex finished both modules.

**P11. Silent reviewer loss.** (`model_workflow._adversarial_reviewer_candidates`)
- Seven `except Exception: pass` blocks mean reviewers can disappear without a trace.
- Discovery makes synchronous reachability probes on each call.
- STOPPED-STATUS records that the no-reviewer fail-closed patch is unverified.

**P12. Family classification by substring.** (`task_review.backend_model_family`)
- Substrings such as `"meta"` and `"glm"` can misfire.
- `workbuddy` is treated as its own family, but its underlying model is unknown, so cross-family independence through WorkBuddy is unproven.

**Name-only concern.** `claude_rotation.py`, `user_token_market.py`, `subscription_quota.py`, and `channel_sniffer.py` suggest account rotation or quota arbitrage. I have not seen their contents. If they rotate consumer accounts against a provider's terms, they are an operational and terms-of-service liability, not just complexity. Review them before keeping them.

---

## 3. Verdict: selective replacement

**Why not a rewrite.** Pacer has dogfooded pieces that would take weeks to rebuild and re-verify. STOPPED-STATUS records real runs with SHA-delta checks and sandbox review. A rewrite discards that evidence.

**Why not an incremental graft.** The failure modes are concentrated in orchestration and control flow:
- No deadlines.
- Lock and queue edge cases.
- Unpredictable cascades.
- Model-authored tests.

Grafting GPU search onto that control flow adds load to the weakest part.

**Keep** (based on excerpts and names, with contents unverified where noted):
- `pacer_model.py` as the protocol front door: OpenAI/Anthropic normalization, `latest_prompt` statelessness, body limits. Fix P1 through P5 first.
- `canvas_manager.build_stateless_prompt`: keep the concept and fix P8 with a reserved minimum for focus.
- `session_projection.py`: keep the event-log-and-fold design and add snapshot compaction.
- `local_indexer.py` and `repo_map.py` for AST signatures (unseen).
- Worktree handling: `worktree_gc.py`, `managed_checkpoint.py`, `change_set.py`, `diff_patcher.py` (unseen; dogfooding claimed).
- Verification: `tamper_guard.py`, `acceptance_discrimination.py`, `witness_verifier.py`, `structured_failure.py`, `locks.py`, `process_guard.py` (unseen; re-review each).
- `is_infra_or_quota_error`: keep the split between infrastructure failure and task failure. The new strike logic needs exactly this distinction.

**Retire or collapse:**
- The `_dispatch_with_failover` ordering stack. Replace it with one declarative routing table: ordered backends, per-backend deadline and cooldown, read once.
- Goal-text regex decomposition (`_numbered_goal_items`, the Chinese/English test regex). The kernel should receive a typed task spec.
- Model-authored acceptance tests in the same run (P6).
- Reviewer discovery sprawl (P11). Use an explicit list with fail-closed behavior when it is empty.
- Speculative surfaces until they show measured use: `pacer_voice`, `dashboard`, `canvas` UI pieces, `user_token_market`, `channel_sniffer`, `codex_rollout_telemetry`. This is a name-based judgment; confirm with import and usage counts.

**Keep vnext separate.** Do not wire `strategy_search` into Pacer execution until D1 through D7 are fixed. It currently has no consumer that needs it.

---

## 4. Is A*/MCTS over code modifications justified?

**Short answer: not as an MVP.** A simple best-first queue over a handful of cloud-proposed patches with tiered gates is justified. MCTS is not.

**Why classical search fits poorly here:**
- **Transition generation is the expensive part.** New code states come from cloud calls, which take tens of seconds and cost money.
- **The exact evaluator is slow.** pytest takes seconds to minutes per node.
- **There is no cheap rollout.** MCTS value comes from many cheap simulations. Here a "simulation" is a pytest run.
- **There is no meaningful admissible heuristic.** One patch can fix any number of failing tests, so the only admissible bound on remaining edits is close to 0 or 1, which gives no guidance.
- **Optimality is not the goal anyway.** Correctness comes from gates, not from search guarantees. Use weighted best-first and claim no optimality.

**A viable design:**
- **State:** `(base_sha, frozenset(patch_hunk_hashes), apply_ok, gate_level_reached, failing_test_ids, protected_hash_ok)`.
- **Transitions:**
  - A cloud batch of k patches per request, issued asynchronously, never awaited per node.
  - Deterministic local operators: drop a hunk, combine hunks from two candidates, revert the file to base.
- **g (spent cost):** α·cloud_tokens + β·wall_seconds + γ·changed_lines.
- **h (non-admissible):**
  - Count of failing targeted tests at the highest gate reached.
  - Plus a penalty for each gate not yet passed.
  - Plus a penalty for touching files outside the localized set.
- **Gate ladder, cheapest first, with results cached by `(tree_hash, gate, test_id)`:**
  1. Patch applies.
  2. `ast.parse`.
  3. ruff or pyflakes.
  4. Import of touched modules under `python -I`.
  5. Impacted tests, selected with a per-test coverage map (`coverage` dynamic contexts, built once per base SHA).
  6. The full suite, for the final candidate only.
- **Avoiding waits:**
  - The kernel keeps a pending-proposal queue and a pending-evaluation queue.
  - It always evaluates queued candidates while the next cloud batch is in flight.
  - Only gate 6 serializes.

**CPU/GPU boundary:** everything above is CPU-bound process orchestration. For code search, the GPU has no demonstrated role in this MVP.

**0.5B–0.8B value probe: no MVP role.**
- It needs thousands of labeled `(state features → eventual pass/fail)` examples that do not exist yet.
- With tens of trajectories it would add noise.
- Log every node's features and outcome now. Revisit after you have enough labels to beat a logistic regression on the same features.

**GPU GNN: no MVP role.**
- Personalized PageRank over an import/call graph of a repo this size (about 100 modules) runs in milliseconds on CPU with scipy.sparse or networkx.
- cuGraph would add a dependency with no measurable gain.

**What is and isn't demonstrated:**
- *Strategy-parameter search:* a bounded grid evaluation with replay of one candidate. It is not iterative search; there is no expansion from scores.
- *AST/code-plan search:* nothing has been shown.

---

## 5. Windows/WSL2 feasibility and the smallest practical stack

**Observed:**
- PyTorch 2.9.1+cu128 runs CUDA on the RTX 5060 Laptop GPU (8151 MiB), with driver 573.22 reporting CUDA 12.8.
- cuGraph, cuVS, CuPy, FAISS-GPU, and Warp are not installed.
- Unknown: whether that PyTorch is the Windows or the WSL interpreter.

**Library assessment** (each claim requires a check before use):

| Library | Platform claim (requires check) | MVP need |
|---|---|---|
| cuGraph / cuVS (RAPIDS) | Linux and WSL2 only, no native Windows; Blackwell (sm_120) support depends on recent CUDA 12.8+ builds | None: CPU PPR is sufficient at this repo size |
| CuPy | Wheels exist for CUDA 12.x; sm_120 support depends on version | None: torch covers it |
| FAISS-GPU | Mainly conda/Linux builds; not native Windows | None: brute-force torch matmul handles ≤1e5 vectors (1e5 × 384 fp16 ≈ 77 MB) |
| NVIDIA Warp | pip `warp-lang`, Windows and Linux; check sm_120 support | None unless physics simulation is required |
| Genesis | Physics simulation stack | No identified task needs it |
| mctx | JAX-based; JAX next to torch on 8 GB needs `XLA_PYTHON_CLIENT_PREALLOCATE=false` to avoid grabbing most of the VRAM | Not justified (section 4) |
| TreeQuest | Python, LLM-tree oriented, CPU | Optional later; a hand-written best-first queue is about 100 lines |
| Scallop / Lobster | Scallop has Rust-based Python bindings, build friction likely; Lobster is a research prototype, availability uncertain | None: policy rules belong in plain Python predicates plus SQLite queries |

**Smallest practical stack:**
- Installed PyTorch (CUDA) and NumPy.
- SQLite for state.
- scipy.sparse for PPR.
- coverage.py for test impact.
- Nothing else until a measured bottleneck requires it.

**WSL2 integration risks:**
- **Filesystem:** keep repos and worktrees on WSL ext4. `/mnt/c` goes through 9P and is slow for git and pytest; Windows Defender scanning adds more. Avoid mixed Windows/WSL access to the same worktree, which brings line-ending, permission, and path-translation problems.
- **Process control:** POSIX process groups work inside WSL. A Windows-hosted gateway (P1) needs Job Objects instead. Pick one side for the kernel; WSL is the simpler choice.
- **GPU sharing:** WSL CUDA runs over WDDM. Long kernels risk TDR resets (requires check on this driver); the current chunked kernels are short.
- **Pinned memory:** WSL documents limitations on pinned memory (requires check).
- **Laptop thermals:** the laptop GPU shares the display and is subject to power states and thermal throttling. Pin the power mode during benchmarks and report variance.
- **Memory caps:** set limits in `.wslconfig`, or Linux OOM kills will look like hangs.

---

## 6. Fused kernel design

The kernel is a single-process deterministic state machine with no agent-to-agent chat. Every transition is an event in SQLite, reusing the `session_projection` pattern with compaction.

```
run(task_spec):
  PREFLIGHT  (all with hard timeouts; any failure -> EXIT with reason, 0 cloud calls)
    base = git rev-parse HEAD in a fresh worktree from a pinned SHA
    protected = hash(tests/**, conftest.py, pytest.ini, pyproject [tool.pytest],
                     setup.cfg, tox.ini, CI config)        # stored in event log
    collected = pytest --collect-only -q (timeout 60s); exit 5 or 0 tests -> EXIT
    acceptance must FAIL on base (discriminating) else EXIT "already satisfied"
    budget check (tokens, $, wall deadline) else EXIT

  LOOP until deadline, budget, solved, or breaker open:
    if proposals_in_flight < K: submit async cloud request (deadline D_cloud)
    cand = eval_queue.pop_best()          # best-first on g+h; non-blocking, timeout
    reject at parse time if diff touches a protected path (before apply)
    gates 1..5 in subprocess sandbox (section 4), each with its own timeout
    after each test run: re-hash protected set; mismatch = tamper strike, quarantine
    run tests from a base-SHA copy of tests (git show base:path into the sandbox,
      chmod read-only); require collected test-id set == baseline set;
      reject new skip/xfail markers, os._exit, sys.exit in source,
      or runtime writes to conftest
    record outcome as (state features, gate, result) for future value models

  CAUSAL PROBE (when a failure has more than one candidate cause):
    H: "failure is caused by X"; intervention: minimal patch or monkeypatch of X;
    placebo: same-size edit to unrelated code.
    Accept H only if outcome flips under the intervention AND not under the placebo.
    (This is a controlled A/B with a placebo. Calling it do-calculus overstates it.)

  STRIKES (task failures only; infra/quota errors go to the provider breaker)
    strike = gate regression, or no reduction of failing_test_ids after a batch
    3 strikes without progress -> OPEN:
      move worktree branch to refs/quarantine/<run>/<n>, save diff as patch file,
      new worktree from last green checkpoint SHA, write lesson, escalate to user.
      Never reset --hard in the user's tree. Delete quarantine only with confirmation.
```

**GPU jobs:**
- One GPU worker process with a bounded queue.
- Admission by estimated bytes, using the D-slice formula of about 40 B × elements.
- A cancel token checked between chunks. A running CUDA kernel cannot be cancelled, so hard cancellation means killing the worker process.

**Deadlock controls:**
- Fixed lock order: `run_lock` before `amendment_lock`, with nothing else held across I/O.
- No blocking `get` or `put` without a timeout; every loop has a deadline.
- Release `run_lock` in a `try`/`finally` that also covers `submit` failure (P3).
- Treat any `ConnectionError` or `OSError` on stream writes as a disconnect (P1).
- Subprocesses:
  - `start_new_session=True` and `stdin=DEVNULL`.
  - stdout and stderr go to files, not pipes, so pipe buffers cannot deadlock.
  - On timeout, kill the process group with SIGTERM, then SIGKILL after a grace period.
  - pytest-timeout per test (requires install check) plus an outer wall timeout.
- A watchdog heartbeat in the event log. A stale heartbeat triggers kill and quarantine.

**OOM controls:**
- `torch.cuda.set_per_process_memory_fraction`.
- Chunk sizing by bytes rather than elements.
- On `torch.cuda.OutOfMemoryError`: halve the chunk and retry once, then fall back to CPU, and log both.
- Run pytest subprocesses under `ulimit -v` or `systemd-run --scope -p MemoryMax=` (requires a check that systemd is enabled in this WSL distro).
- Cap worktree disk use.
- `.wslconfig` memory limit.

---

## 7. Plan, current proof, and next validation

**What this MVP proves today:**
- PyTorch CUDA works on this host for this workload.
- torch-CPU and torch-CUDA chose the same candidate on one synthetic fixture.
- Python float64 replay matched the leader within 2e-5 on that fixture.
- The seed schema rejects extra keys (unit test with a literal payload).
- Peak memory is about 57 MiB, consistent with the chunking formula.

**What it does not prove:**
- That the Gemini seeding path works end to end. The 128-candidate benchmark used the local grid.
- That replay stays robust at larger sizes (D3).
- Determinism across repeats.
- Profitability or any real-world validity.
- Any benefit for code tasks.
- That current Pacer passes its regression.

**1–2 week sequence:**

| Days | Work | Exit criterion |
|---|---|---|
| 1 | Finish the stopped Pacer full regression under an outer timeout; reload and retest the 8091 gateway | Pass/fail counts recorded with SHA |
| 1–2 | Fix D1–D7; add the D10 tests, including a stubbed Gemini test and a near-threshold flip test | New tests pass on CPU and CUDA; near-boundary count reported |
| 2–3 | Fix P1–P4, P7; hardened subprocess runner (section 6) | Injected-hang test terminates within deadline; disconnect test releases `run_lock` |
| 3–4 | Protected-test enforcement built on `tamper_guard`, plus a reserved minimum focus budget (P8) | Cheat-bait patches rejected |
| 4–6 | Kernel state machine: preflight, gate ladder, cache, strike breaker with quarantine refs | Kernel unit tests; quarantine is restorable |
| 6–7 | Build the historical-fix dataset (below) and CPU PPR localization | Dataset frozen, with hashes |
| 8–9 | Run current Pacer and the kernel on the same tasks | Metrics table below |
| 10 | Decide on best-first with more than one candidate per task, based on whether a second candidate ever solved what the first did not | Written decision with data |

**Next validation task: replay real Pacer bug fixes from its own git history.** It is non-financial, bounded, and real.

- **Selection:**
  - Choose 20 commits that change source code and add or modify tests.
  - Keep a commit only if its new or changed tests fail on the parent and pass on the commit.
  - Expect 10–15 to survive.
- **Setup:**
  - The task is the commit message only.
  - The fix commit's tests are installed read-only as acceptance tests.
  - Add 3 cheat-bait variants where editing a test is the easiest way to pass.
- **Pre-registered metrics:**
  - Accepted tamper attempts: must be 0.
  - Runs exceeding the 15-minute deadline without terminating cleanly: must be 0.
  - Solve rate: kernel vs current Pacer on the same tasks.
  - False accepts: patches accepted by the kernel that fail the fix commit's full suite.
  - Regressions: tests passing on the parent that fail after the patch.
  - Per task: cloud calls, tokens, median and p95 wall time.
  - Localization recall@4: share of the fix commit's touched files that appear in the prompt focus block. This checks P8 and PPR without a GPU.
  - Strike-breaker openings, and whether each quarantine restored cleanly.

**Still missing to firm up this review:**
- Contents of `tamper_guard.py`, `acceptance_discrimination.py`, `process_guard.py`, `subprocess_window.py`, `model_admin.py`, and `gemini_bridge.invoke_gemini`.
- The benchmark raw JSON and the exact command line.
- Which interpreter (Windows or WSL) hosts the gateway and PyTorch.
- The evidence files listed in STOPPED-STATUS.

## Gemini 3.8 完整审查回复

以下为通过配置的 Gemini 3.8 CLI 回退通道返回的回复原文。

### 1. 疑点核对：撤回“漏掉初始权益峰值”
**撤回该疑点**。
**核对事实**：当 `lookback >= 2` 时，`active` 强制要求 $t \ge \text{lookback}$，因此 $t \in \{0, 1\}$ 时 `positions` 恒为 0，交易费用为 0。故 `curves` 序列前两个元素恒为 $0.0$。后续算子 `torch.cummax(curves, dim=2)` 在首项即捕获 $0.0$，即使后续所有交易亏损致净值全负，`peaks` 最小值仍保底为 $0.0$。在当前约束下**不存在漏算基线峰值的可达反例**。

---

### 2. 核心风险审查 (按严重度)

#### [Medium] `_evaluate_torch` 中间张量显存倍增与 OOM 风险
- **事实**：`prefix.unsqueeze(1).expand(...).gather(...)` 与后续 `momentum`、`positions`、`pnl`、`curves`、`peaks` 在 float64 下同时展开形状为 `(scenarios, chunk_size, steps)` 的三维大张量。
- **推测/风险**：中间瞬时张量多达 6~8 个。若场景数与步长进一步扩大（如高频步长），仅依靠 `_TARGET_TENSOR_ELEMENTS // elements_per_candidate` 估算 chunk，在 8GB 笔记本显存上并发调用极易触发 CUDA OOM。

#### [Medium] `_input_hash` 与数据入参导致 Host 性能严重拖累
- **事实**：`_input_hash` 对 256×1024 场景在 Python 循环中逐个执行 `struct.pack("<d", value)`（>26万次函数调用）；且每次执行 `_evaluate_torch` 均将 Python 原生嵌套列表现场转换为 PyTorch CUDA 张量。
- **影响**：纯算分仅 58.9ms，但端到端耗时达 372.8ms。Host 端序列化与 PCIe 传输开销占主导，吞吐虚高。

#### [Low] `_evaluate_torch` vs `_evaluate_python` 浮点归约顺序差异
- **事实**：GPU 上 `cumsum` 与 `std(dim=0)` 采用树状并行归约，Python 为顺序累加。
- **风险**：float64 虽有 53 位有效尾数，但在多达 1024 步的长序列连续累加时末位必然存在舍入差异。若 `active_policy.replay_tolerance` 设为 0 或过紧，将导致 CPU replay 偶发性判定失败。

---

### 3. GPU 与算法适用性判定

1. **GPU 在此任务中是否有实际作用？**
   - **量化计算有加速，但对代码智能体无直接价值**：对一维时序动量策略矩阵计算有 23 倍理论加速，但这是金融量化回测范式，并非代码编辑。
2. **A* / MCTS 是否适用于代码编辑？**
   - **不适用（高成本、弱反馈）**：
     - **状态评估极慢**：代码编辑有效验证依赖编译或测试（耗时秒级），无法支撑 MCTS/A* 毫秒级数十万次 Rollout。
     - **分支空间与启发式失效**：语法空间无限离散且极脆弱；A* 无法构造保证可采性的启发函数（Admissible Heuristic）；MCTS 盲目搜索 99% 产生语法死节点，稀疏奖励导致树搜索退化。
     - **更优解**：代码生成与编辑更适合基于 LLM + Linter/Compiler 紧反馈的 Best-of-N、Beam Search 或线性反射修复循环。


---

## 既有审查草稿（原文保留）

以下为本轮开始前已存在的报告原文，完整保留用于比较；环境快照与本轮重新采集时间不同，数值差异是不同时间点的显存占用。

# Pacer GPU 外骨骼与 A* 架构独立审查

## 本机环境实测

环境 JSON：
{
  "captured_at": "2026-11-03 local time",
  "windows": {
    "edition": "Windows 11 Home Chinese",
    "version": "10.0.26200",
    "build": "26200",
    "architecture": "64-bit"
  },
  "gpu": {
    "name": "NVIDIA GeForce RTX 5060 Laptop GPU",
    "total_vram_mib": 8151,
    "windows_free_vram_mib": 7777,
    "driver": "573.22",
    "nvidia_smi_cuda_version": "12.8",
    "compute_capability": "12.0",
    "utilization_at_capture": "0%",
    "temperature_c": 45,
    "memory_used_mib": 34,
    "wsl_nvidia_smi": "GPU visible; 34 MiB used, 0%; no running processes reported",
    "nvcc": "not found in Windows or WSL Ubuntu"
  },
  "wsl": {
    "version": "2.6.1.0",
    "kernel": "6.6.87.2-1",
    "wslg": "1.0.66",
    "default_distro": "Ubuntu",
    "ubuntu_state": "Running, WSL 2",
    "docker_desktop_state": "Running, WSL 2"
  },
  "windows_python": {
    "executable": "C:\\Users\\xiaoyu\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
    "version": "3.11.9",
    "torch": "2.9.1+cu128",
    "torch_cuda_available": true,
    "torch_cuda_runtime": "12.8",
    "torch_device": "NVIDIA GeForce RTX 5060 Laptop GPU",
    "torch_free_bytes": 7283408896,
    "torch_total_bytes": 8546484224,
    "installed_relevant_packages": {
      "torchaudio": "2.9.1+cu128",
      "numpy": "1.26.3",
      "scipy": "1.17.1",
      "numba": "0.67.0",
      "faiss-cpu": "1.8.0.post1"
    },
    "not_installed": [
      "torchvision",
      "tensorflow",
      "CuPy",
      "cuDF",
      "cuGraph",
      "cuVS",
      "Warp",
      "Genesis",
      "mctx",
      "JAX",
      "faiss-gpu"
    ]
  },
  "capture_note": "nvidia-smi CUDA 12.8 is the driver-supported CUDA level; nvcc absence means no CUDA compiler was found on PATH. Do not equate this with a separately installed system CUDA Toolkit."
}

## Opus 5.5 调用凭证与原始输出校验

- 请求模型：claude-opus-5-5；服务端返回：claude-opus-5-5
- HTTP：200；完成状态：end_turn；耗时：175.45s
- token 用量：{"cache_creation_input_tokens": 159330, "cache_read_input_tokens": 0, "input_tokens": 0, "output_tokens": 15653}
- 凭证来源：D:\pacer-recovery\20261101\相关外援付费密钥.txt；选取首个识别到的凭证；SHA-256：31177c0de3ee4c27c7f44f1fb78153bb9f736bcaacb16218b5ec6cf1a5d15b0d
- 明文密钥没有写入审查产物；完整提示、原始 SSE 事件和调用元数据分别保存在 final-audit/。
- 提示 SHA-256：74b2ab40f8cc1c1d3fb3cc00eadf24940e7f99f9e458d23ccd479a24477f21ab；原文 SHA-256：7a193301b2cb324151aa8a905fe5ae8c176a943f765d706ad09a099955479052

## 独立事实核查与落地修订

本节是本地源码、实弹回执和上游项目文档的后置核查；下方“Opus 5.5 原始完整审查报告”保留原样，不把本节的判断冒充为 Opus 原话。上游链接按 2026-11-03 检查，版本兼容仍须在目标环境做安装与运行冒烟。

### 核查结论

1. **Pacer 有若干铁笼机制，但不是方案 C 的完整实现。** `src/visual_agent/tamper_guard.py` 会为已选中的验收文件留哈希与备份，并设置只读属性；编排器启用 `enforce_test_tamper_guard` 时锁定测试/契约文件并在 wave 间校验。这个 OS 文件属性和恢复机制是保护措施，不是对同权限恶意进程的安全边界。`subagent_orchestrator.py` 对重复的相同验证失败第二次即停止当前写码尝试，不是“三振”；provider 熔断按 401/403、429、其他瞬时错误分别设置 600/300/60 秒冷却，不以连续失败次数计数。没有在 `src/visual_agent` 找到 `$do$-Calculus` 干预探针或其标记实现。失败回滚是 `restore_workspace_snapshot` 对本次任务的路径快照恢复；源码未使用 `git reset --hard`。
2. **现有索引不是向量库。** `local_indexer.py` 对指定文件做 Python AST/正则符号提取与代码窗口定位；Canvas 保存有限的焦点文件/符号，并延迟重索引最近焦点。源码没有 FAISS、embedding 向量检索或 GPU 图谱。不能把“AST 焦点提取已存在”写成“Canvas 已有向量索引”，也没有证据表明目前线性检索是 50ms 瓶颈。
3. **PageRank 的适用性描述需改正。** cuGraph 可表示有向图并接收边权；因此“PageRank 是无向、等权算法”不准确。它本身仍不会推断因果或反事实：边方向、置信度和时间衰减必须由应用显式定义，Personalized PageRank 只是沿该图排序/检索相关节点。用于检索可以试验，用它宣称实现 do-calculus 不成立。
4. **FAISS 示例的复杂度和平台结论有误。** `IndexFlatL2` 是精确平面扫描，查询仍需与索引向量逐一比较，时间复杂度约为 O(Nd)，不是 O(log N)；ANN 索引可用近似换速度，但会引入召回率/参数选择问题。FAISS 上游当前安装文档提供的 GPU 包是 Linux x86-64 包，`faiss-cpu` Windows 安装不能直接“升级”成现有 Python 里的 GPU FAISS。若将来验证 GPU FAISS，应使用 WSL2 Linux 独立环境，并固定 CUDA/FAISS/驱动版本与 RTX 5060 sm_120 冒烟结果。
5. **其它开源库需要按实际边界分类。** RAPIDS 官方安装指引已提供 Windows 11 + WSL2 路径（25.04 起），所以“WSL2 完全不可用”过时；但本机没有 cuGraph/cuVS、`nvcc` 也不在 PATH，安装并运行尚未验证，不能写成“本机可直接高效跑通”。`mctx` 是 JAX MCTS 实现，不是 A* 引擎；它需要调用方提供表示、状态转移/递归模型与价值/先验。TreeQuest 暴露可批量请求节点、可自定义生成/打分的树搜索接口，但不是 CUDA 搜索库，是否上 GPU 取决于用户的生成器/评分器。Scallop 官方 README 要求 Rust nightly；`P-bibs/Lobster` 是 GPU 加速的 Scallop 分支，但其 README 明示 AOT 特化、批处理不稳定、offload 选择不稳定，不宜当作刚性生产审判器。NVIDIA Warp 有 Windows CUDA wheel；本机驱动 573.22 低于上游当前 PyPI CUDA 13 wheel 的 R580 要求，需固定兼容的 CUDA 12 wheel（上游发布的 `+cu12` 构建）或升级驱动，不能直接装最新 wheel 后假设工作。Genesis 上游当前 PyPI 要求 Python 3.12–<3.14，与本机指定的 Python 3.11.9 不匹配；需选择兼容旧版或隔离解释器。上述 GPU 框架全都没有在本机安装/实测。
6. **实弹数字不能推出 API/pytest 的固定占比。** parallel 回执确实记录总耗时 363.36 秒、计划 ETA 77.6 秒、Codex 任务 355.05 秒、另一个任务 92.52 秒、本地验证任务 7.70 秒；round2 总耗时 112.15 秒。它们支持“当前存在长尾与 ETA 校准问题”，但不能单凭这些回执推出 API 占 90%、pytest 占 10%或 pytest 启动占 70%。387 passed 是 `final-local-regression.json` 的历史结果；`STOPPED-STATUS.json` 明确记录随后最新全量回归与 8091 gateway 重载验证未完成。不得将历史回归描述为当前最新源码已完整回归。原文所称 `environment_capture.json` 在仓库与审计目录均不存在；本地硬件与软件采集值以内嵌在本文开头的环境 JSON 为准。
7. **原始 MVP 代码块不可执行。** `warp_physics_exam.py` 末尾直接返回 `{"success": true, "failure_rate": 0.02}`，不是仿真器，也不是验收器；若被调用会制造假通过。FAISS 代码假设 `StandardGpuResources` 可用，也未处理 Windows GPU wheel、张量生成、空索引、越界 `k`、资源生命周期或召回验证。它们仅是未完成草图，不能复制进生产代码。原文关于 50ms CPU 检索、10 万向量显存、API/pytest 占比、端到端节省 2%也都是未经测量的假设；单次 112 秒回执不足以设置 2%门槛。

### A* / 树搜索的可实现边界

把 GPU 当作候选评估器，而不是代码执行器或云端 API 加速器。一个可审计的代码搜索节点至少应绑定：

- `base_commit` 与工作区文件哈希，确保节点对应唯一、可复现的代码快照；
- 有界编辑操作 ID/目标文件、AST 符号和导入依赖特征；原始 diff 放在 CPU/磁盘，不复制整份源码进显存；
- 被选中的验收命令/测试 ID、测试文件哈希、状态（未运行/通过/失败/超时）；
- 累积成本、剩余 API/时间预算、父节点 ID 和去重状态哈希。

对最小化代价，`g(n)` 可以记录已实际发生的 API token/费用、延迟、沙盒时间与风险惩罚。只有当 `h(n)` 是剩余代价的可证明下界，且动作转移与成本非负可加时，A* 才有最优性保证；一个未校准的 0.5B/0.8B 价值探针或 AST 启发分数不是 admissible heuristic，不能据此硬删掉唯一正确分支。它至多先用于排序。代码节点的展开若要调用云端模型或跑 pytest，分支成本受网络/进程约束，CUDA 不会让这些操作并行变快；可以一次请求稀疏生成有上限的候选，然后 CPU 建立候选快照、跑静态门、再为少数候选跑测试。只有候选特征批量评分或任务本身的数值状态转移适合 GPU 批处理。

建议先用有限宽度的 best-first/beam 搜索和硬验收门，而不是声称“GPU A* 剪掉 95% 幻觉”。若日后采用学习打分器，应使用成功/失败轨迹做离线校准，报告覆盖率、误剪率和收益/成本曲线；任何置信度都不能代替测试结果、测试文件完整性和人工设定的授权边界。

### 两周 MVP 执行清单

1. **保留 Pacer 确定性边界。** 以现有 `subagent_orchestrator.py`、`model_workflow.py`、`managed_state.py`、`canvas_manager.py`、`local_indexer.py`、`diff_patcher.py`、`tamper_guard.py`、`acceptance_discrimination.py`、`task_review.py` 和 `provider_runtime.py` 作为编排/状态/差异应用/验收/路由基础。先删减重复阶段和无边界等待，给每个 API、子进程、pytest 和锁设置可观测的截止时间、取消/子进程树回收与明确的失败态；不要把“绝不挂起”当作可证明保证，目标应是超时后有界失败且可恢复。
2. **先让现有 GPU 做一个独立且可测的工作。** 新增 `src/visual_agent/gpu_batch_sandbox.py`，直接使用已安装且 CUDA 可用的 PyTorch 2.9.1+cu128；首个 MVP 不安装 RAPIDS/JAX/FAISS GPU/Genesis。将量化交易策略或其它明确的数值策略写成封闭的向量化张量计算，对固定数据窗口、随机种子、手续费/滑点/风险约束批量跑参数与噪声情景。模型只可提议参数/有界策略候选；不可编辑评分器、隐藏验收数据或通过 GPU 分数跳过正式测试。首个基准同时跑 CPU 参考实现，逐项比较结果容差、吞吐、p50/p95、GPU 峰值分配/保留量、功耗和任务失败率。
3. **让自动改码走稀疏候选流程。** 可新增 `gpu_candidate_ranker.py`，但起步只接受本地确定性特征（AST 解析结果、改动规模、依赖/测试覆盖映射）；候选扩展仍由有限 API 请求或现有本地规则产生。每个候选进入隔离快照/独立 worktree；先过路径与测试锁、AST/静态约束，再对选中的少量候选运行相同验收命令。未校准前 GPU 排分只排序，不拒绝候选、不判定通过。保留真实任务预算与模型成本统计，不为堆显卡利用率增加无意义 API 调用。
4. **控制 GPU 资源。** 设备快照为 8151 MiB 总显存，采集时 7777 MiB 可用、PyTorch CUDA 正常。建议第一阶段把实际峰值控制在约 6 GiB 以下并留至少约 1.5–2 GiB 给桌面、CUDA context 和驱动；以实测为准，不把 6.5 GiB 当已证明安全预算。`torch.cuda.set_per_process_memory_fraction` 只约束 PyTorch allocator 的使用，不能代表 FAISS/Warp/RAPIDS 的统一全局限额。保持单一 GPU owner，按任务串行取得 GPU lease，避免第一 MVP 同时加载 JAX、RAPIDS 和 PyTorch 多套缓存池。
5. **用真实任务证明端到端收益。** 选择一个可封闭复现的量化策略参数搜索任务，冻结行情样本、随机种子、时间切分、交易费率、滑点、资金费、最大回撤和风控拒绝规则。A 组用 CPU 暴力/网格参考计算，B 组用 GPU 向量化评估同一组候选；另外用相同 Pacer 任务把最终选出的策略改动落到代码、运行现有测试与隐藏反例。分别报告数值结果一致性、每秒情景数、完成耗时、峰值显存、模型 token/费用、超时率和验收通过率。至少重复多轮并报告分布/置信区间；性能阈值按 A 组测得的波动预先设定，不从单次实弹推导“节省 2%”。
6. **达标后再扩栈。** 数值内核证明 PyTorch CUDA 有可重复的吞吐收益之后，再单独评估 Warp（CUDA 12 兼容构建）或 Genesis（兼容 Python 的隔离环境），只对物理/机器人任务启用。需要图检索时，先构造带明确方向、置信度和时间衰减定义的边，再在 WSL2 安装矩阵中选定匹配的 RAPIDS 版本；不要以原生 Windows `faiss-cpu` 升级为 GPU 的方式规划，也不要声称 PageRank 或模拟分数具有因果证明力。

**官方上游核验来源**（版本可能变化，部署前复查）：

- [RAPIDS 安装指南（含 Windows 11 / WSL2）](https://docs.rapids.ai/install/)
- [FAISS 安装平台和 GPU 包说明](https://github.com/facebookresearch/faiss/blob/main/INSTALL.md)
- [cuGraph PageRank API](https://docs.rapids.ai/api/cugraph/stable/api_docs/api/cugraph/cugraph.pagerank/)
- [NVIDIA Warp 安装指南](https://github.com/NVIDIA/warp/blob/main/docs/user_guide/installation.rst)
- [Genesis 安装与当前 Python 要求](https://github.com/Genesis-Embodied-AI/Genesis/blob/main/README.md)
- [DeepMind mctx](https://github.com/google-deepmind/mctx)
- [SakanaAI TreeQuest](https://github.com/SakanaAI/treequest)
- [Scallop](https://github.com/scallop-lang/scallop) 与 [P-bibs/Lobster](https://github.com/P-bibs/Lobster)

## Opus 5.5 原始完整审查报告

# 独立审查裁决：Pacer vs. 8GB GPU "外骨骼"

## 一、核心裁决（12 行）

**Pacer 现有内核：可复用的确定性编排引擎，无需掉头重构。** 模块清晰、验收逻辑刚性、多模型路由可靠。保留核心编排、Canvas、AST 索引、事务式 diff、物理验收；精简文档/策略元数据。

**方案 B（GPU 外骨骼）：工程可行，但核心主张过度承诺。** cuGraph/cuVS、Warp/Genesis 本机可装；Scallop/Lobster 学术原型、JAX mctx 需生产验证。显存预算乐观、PageRank 不适合因果检索、A* 在小搜索空间下 CPU 开销高于收益、"95% 剪枝"缺训练数据支撑。**物理仿真不代表软件工程验收**——pytest 返回码才是唯一权威。GPU 应聚焦向量/符号索引，而非替代 API 调用或 pytest 执行。

**方案 C（铁笼状态机）：合理但已在 Pacer 实现。** 三振熔断、只读测试锁、do-Calculus 标记已存在；虚构置信度门槛和 git reset --hard 风险需替换为 Pacer 现有的 worktree 快照。

**端到端瓶颈：API 延迟与 pytest 执行，而非 CPU/GPU 算力。** 回执显示实际耗时远超 ETA；并行任务等待最慢节点（355 秒 Codex vs 92 秒）。GPU 检索/搜索无法缩短云端生成或本地验证时间。

---

## 二、证据矩阵

### 2.1 用户报告指标的来源差异（标注：用户陈述 vs 回执）

| 指标 | 用户陈述 | 回执证据 | 可比性 |
|------|---------|---------|--------|
| 单模块耗时 | ~64 秒 | round2: 112.15 秒 | **不一致**；样本不同或含规划开销 |
| 双模块耗时 | ~260 秒 | parallel: 363.36 秒 | **不一致**；ETA 77.6 秒但实际 363 秒 |
| 50k 字符压缩 | 压缩到 736 tokens | 未见回执 | **无证据**；需要 Canvas 日志或 trace payload |
| 回归通过率 | 387 passed / 0 failed | final-local-regression.json 吻合 | **一致**；本地回归结果 |
| 跨轮焦点继承 | 声称已实现 | Canvas recent_focus_files 机制存在 | **机制存在**；但未见跨轮复用证据 |

**结论**：单/双模块耗时与回执不符；回执显示 **parallel ETA 误差 367.9%**，说明瓶颈在模型响应速度和任务粒度，而非本地调度。

### 2.2 GPU 方案关键主张的算法与工程证据

| 主张 | 量纲/复杂度分析 | 本机可行性 | 证据等级 |
|------|----------------|-----------|---------|
| cuGraph PageRank 5–15ms | O(E + V log V)；百万节点/边在 RTX 5060 上可达 | **可行**，但 cuGraph 需 RAPIDS 完整栈 | **部分验证**（本机 torch.cuda=True，无 cuGraph） |
| PageRank 适合因果检索 | PageRank 是无向/等权图排序，不支持时间戳、置信度、因果方向 | **不适用** | **推断**：需 temporal graph 或 HyperNetX |
| Scallop/Lobster 微秒级逻辑 | 学术原型，未见生产部署；微秒级需预编译静态规则 | **未验证**；Rust 编译依赖复杂 | **未验证** |
| mctx GPU A* 并发 | mctx 是 JAX MCTS；A* 在小图（<1000 节点）上 CPU 优先队列更快 | **可行**，但编程任务搜索空间未建模 | **推断**：需定义 g(n)、h(n) |
| Warp/Genesis 并发 2048 环境 | Warp 官方基准 1024 刚体<60ms；Genesis 支持 | **可行**，但物理不代表软件验收 | **推断**：pytest 是唯一权威 |
| 显存预算 6.5GB 可用 | 1.5GB 图+向量 + 0.5GB 逻辑 + 1.2GB 搜索 + 2.0GB 仿真 + 1.3GB 预留 = 6.5GB | **乐观**；JAX preallocate、PyTorch cache 冲突 | **推断**：需 allocator 隔离 |
| "95% 错误规划被剪掉" | 无训练数据、无启发函数校准 | **不能成立** | **推断**：需真实任务标注 |

**核心问题**：
1. **PageRank 不支持有向、时间、置信度**——需要 temporal/causal graph 库（如 CausalNex、NetworkX temporal），而非 cuGraph。
2. **A* 适用于**：确定性、低分支、可校准启发函数的搜索（如路径规划）。软件工程任务的**状态空间**（文件内容、依赖图、测试结果）难以编码，**h(n)** 无法事先校准。
3. **物理仿真不能替代 pytest**——模拟器验证的是物理/数值行为，软件工程验收的是业务逻辑、边界条件、异常处理。

---

## 三、算法破局：软件工程搜索的真实建模

### 3.1 状态编码与 GPU 适用边界

**软件工程状态节点**：
- **文件内容哈希** + **AST 符号签名** + **依赖图拓扑** + **测试覆盖率**
- **动作**：SEARCH/REPLACE diff、pytest 子集、依赖安装
- **奖励**：pytest exit_code、覆盖率增量、静态分析得分

**GPU 能提速的部分**：
1. **向量检索**（已实现）：Canvas AST 索引 + faiss-gpu 可将焦点文件检索从 O(N) 降到 O(log N)。
2. **符号依赖图遍历**：用 cuGraph 预计算模块间调用链，辅助焦点定位。
3. **批量静态分析**：用 numba/cupy 并行运行 AST 规则检查（但 Pacer 已用 Python ast，<20ms）。

**GPU 不能提速的部分**：
1. **云端 API 调用**：网络 RTT（20–200ms）+ 模型推理（5–60s），GPU 无能为力。
2. **pytest 执行**：进程启动、import、fixture setup 占 70% 时间，GPU 无法加速。
3. **diff 应用**：I/O 密集，单文件编辑 <1ms，GPU 调度开销反而更高。

### 3.2 A* vs MCTS：适用点区分

| 算法 | 适用场景 | 软件工程适用性 | Pacer 现状 |
|------|---------|---------------|-----------|
| **A*** | 确定性、低分支、可校准 h(n)（如路径规划、拼图） | **不适用**：编辑空间指数爆炸、h(n) 需训练 | 无需引入 |
| **MCTS** | 高分支、随机性、可模拟（如围棋、Dota） | **部分适用**：需价值网络估计 rollout | 可考虑，但需训练 |
| **Beam Search** | 贪心剪枝、多候选保留（如机器翻译） | **更适用**：保留 top-k diff 候选 | 可嫁接到多模型并行 |
| **启发式规则** | 专家知识、静态分析（如复杂度、圈复杂度） | **已实现**：Anti-Slop、complexity 分类 | 继续扩展 |

**推荐方案**：
- **不引入 A* / MCTS**，而是扩展 **Beam Search + 静态启发**：
  1. 每个 coder 任务生成 **3 个候选 diff**（通过温度采样）。
  2. 用 **静态分析得分**（复杂度、测试覆盖率、diff 行数）排序。
  3. **并行验证** top-3 候选，保留首个通过的。
- **成本预算**：3 次 API 调用 vs 1 次调用，但验证并行（总耗时不变）。

### 3.3 "95% 剪枝"承诺的依据缺失

**需要的数据**：
- 历史任务的 **黄金轨迹**（成功路径）vs **死胡同路径**（失败尝试）。
- **特征标注**：复杂度、依赖数、测试数、模型置信度。
- **训练目标**：分类器预测"此候选是否会通过验收"。

**Pacer 现状**：
- 有 **失败重试历史**（task.history），但无成功 vs 失败的对比标注。
- 有 **Anti-Slop gate**（AST 规则），但是硬编码规则，非学习启发。

**结论**：**"95% 剪枝"不能成立**。需要先积累 100+ 标注任务，训练轻量分类器（0.5B 价值探针可行，但需数据）。

### 3.4 GPU vs API 延迟：端到端比例

**回执证据**：
- round2: 112 秒总耗时，其中 **coder backend=gemini38** 占主体。
- parallel: 363 秒，其中 **coder_1 (Codex) 355 秒**，说明模型响应慢。

**推断**：
- **假设** API 调用占 90%（~100 秒），pytest 占 10%（~10 秒）。
- **GPU 检索** 即使加速 10 倍（20ms → 2ms），也只节省 ~18ms，占总耗时 **0.016%**。
- **GPU 搜索/仿真** 即使并行展开 1000 候选，但每个候选仍需调用 API 或 pytest，总耗时取决于**最慢节点**。

**结论**：GPU 提速上限是 **边际的**（<1%），除非能**减少 API 调用次数**（如 Beam Search top-3 剪枝）。

---

## 四、技术栈真伪审计（Windows 11 + WSL2）

### 4.1 本机证据（源码：environment_capture.json）

| 组件 | Windows 原生 | WSL2 Ubuntu | 状态 |
|------|-------------|------------|------|
| **CUDA Driver** | 573.22 | 可见（nvidia-smi） | ✅ 已装 |
| **nvcc** | ❌ not found | ❌ not found | ❌ 未装 |
| **PyTorch** | 2.9.1+cu128 | - | ✅ torch.cuda=True |
| **cuDF / cuGraph / cuVS** | ❌ | ❌ | ❌ 需 RAPIDS |
| **Scallop / Lobster** | ❌ | ❌ | ❌ 需 Rust + 源码编译 |
| **JAX / mctx** | ❌ | ❌ | ❌ 需安装 |
| **Warp / Genesis** | ❌ | ❌ | ❌ 需 pip install |

### 4.2 可安装性与依赖冲突

| 库 | Windows 原生 | WSL2 | 约束 |
|-----|-------------|------|------|
| **cuGraph/cuVS** | ⚠️ 需 RAPIDS conda 完整栈；官方不支持 Windows 原生 | ✅ 可通过 conda-forge | 需 CUDA 11.8+ 和 cuDF |
| **Scallop** | ❌ 学术原型，未见 PyPI；需 Rust nightly + 手动编译 | ⚠️ 同左 | 生产不推荐 |
| **Lobster** | ❌ 研究代码，未见发布包 | ❌ | 不可用 |
| **mctx** | ✅ `pip install mctx` | ✅ | 需 JAX + XLA |
| **JAX** | ⚠️ Windows 实验性支持（官方建议 WSL2） | ✅ | preallocate 与 PyTorch 冲突 |
| **Warp** | ✅ `pip install warp-lang` | ✅ | CUDA 12.x 兼容 |
| **Genesis** | ✅ `pip install genesis-world` | ✅ | 需 CUDA + Vulkan（无头模式不需显示） |

### 4.3 最小可维护技术栈（推荐）

**保留 Pacer 现有栈 + 三个轻量 GPU 模块**：

1. **向量检索**：`faiss-gpu`（已装 faiss-cpu，升级即可）+ cuVS（可选，需 RAPIDS）。
2. **符号依赖图**：保留 Python `ast` + 本地 networkx（CPU 已够快），cuGraph 作优化项。
3. **物理沙盒**：**Warp**（轻量、无依赖）> Genesis（重量级）。

**不推荐**：
- Scallop/Lobster：学术原型，生产不稳定。
- JAX mctx：与 PyTorch allocator 冲突，需隔离环境。

---

## 五、融合架构：无死锁内核设计

### 5.1 状态机（保留 Pacer managed_state.py）

```
PENDING → RUNNING → [RETRY_WAIT | COMPLETED | FAILED | BLOCKED | CANCELLED]
          ↓
      GPU_SEARCH (可选) → RUNNING (继续)
```

**GPU 模块嵌入点**：
- **焦点检索**：Canvas build_stateless_prompt 前调用 faiss-gpu。
- **依赖分析**：生成 diff 后，用 cuGraph 检查循环依赖（可选）。
- **物理验收**：acceptance_exam 中，若任务需物理模拟，调用 Warp。

### 5.2 超时/取消/资源配额

| 机制 | Pacer 现状 | 增强方案 |
|------|-----------|---------|
| **任务超时** | `timeout_seconds` 参数 | ✅ 已有 |
| **进程树清理** | `hidden_subprocess_kwargs()` | ✅ 已有（Windows job objects） |
| **GPU 显存限额** | 无 | ➕ `torch.cuda.set_per_process_memory_fraction(0.7)` |
| **并发控制** | asyncio Semaphore | ✅ 已有 |
| **熔断** | 三振 + cooldown | ✅ 已有（provider_runtime.py） |

### 5.3 证据边界与失败恢复

**证据等级**（从强到弱）：
1. **物理验收**：pytest exit_code=0 + output 匹配。
2. **静态分析**：AST 无 slop、复杂度合格。
3. **模型自评**：置信度、自我检查（不可靠）。

**失败恢复**：
- **重试**：切换模型族（Gemini → Codex → domestic_api）。
- **降级**：L3 → L2（去掉并行）。
- **人工介入**：三振后写 STOPPED-STATUS.json。

### 5.4 用户工作保护

**Pacer 现状**：
- ✅ **worktree 快照**（acceptance_discrimination.py）。
- ✅ **事务式 diff**（diff_patcher.py）：失败时 rollback。
- ❌ **git reset --hard** 不存在于 Pacer，用户方案 C 提出的此操作是错误的。

**方案 C 修正**：
- 替换 `git reset --hard` 为 **worktree 快照恢复**（restore_workspace_snapshot）。
- 保留未提交的用户工作：只回滚 Pacer 修改的文件。

---

## 六、1–2 周 MVP：最小信息量验证

### 6.1 保留文件（Pacer 核心）

```
visual_agent/
├── subagent_orchestrator.py    # 编排引擎
├── canvas_manager.py            # 无状态上下文
├── local_indexer.py             # AST 索引（<20ms）
├── diff_patcher.py              # 事务式 SEARCH/REPLACE
├── acceptance_discrimination.py # 物理验收
├── provider_runtime.py          # 多模型路由
├── model_workflow.py            # 规划器
├── task_review.py               # 对抗审查
└── managed_state.py             # 状态机
```

### 6.2 新 GPU 模块（3 个文件）

```python
# visual_agent/gpu_focus_index.py
"""faiss-gpu 焦点检索，替代 Canvas 线性扫描。"""
import faiss
import numpy as np

def build_faiss_index(symbols: list[tuple[str, np.ndarray]]) -> faiss.Index:
    """768 维向量索引（sentence-transformers）。"""
    dim = 768
    index = faiss.IndexFlatL2(dim)
    if faiss.get_num_gpus() > 0:
        index = faiss.index_cpu_to_gpu(faiss.StandardGpuResources(), 0, index)
    vectors = np.array([v for _, v in symbols], dtype=np.float32)
    index.add(vectors)
    return index

def search_top_k(index: faiss.Index, query: np.ndarray, k: int = 20) -> list[int]:
    """5ms 检索 top-20 相关符号。"""
    distances, indices = index.search(query.reshape(1, -1), k)
    return indices[0].tolist()
```

```python
# visual_agent/gpu_dependency_graph.py
"""可选：cuGraph 依赖检查（仅在安装 RAPIDS 时启用）。"""
try:
    import cudf
    import cugraph
    CUGRAPH_AVAILABLE = True
except ImportError:
    CUGRAPH_AVAILABLE = False

def check_circular_dependency(edges: list[tuple[str, str]]) -> bool:
    """GPU 检测循环依赖（可选优化）。"""
    if not CUGRAPH_AVAILABLE:
        import networkx as nx  # fallback
        G = nx.DiGraph(edges)
        try:
            nx.find_cycle(G)
            return True
        except nx.NetworkXNoCycle:
            return False
    # cuGraph 实现（省略）
    return False
```

```python
# visual_agent/warp_physics_exam.py
"""Warp 物理沙盒验收（仅用于明确需要物理模拟的任务）。"""
import warp as wp

def run_warp_exam(script: str, timeout: float = 10.0) -> dict:
    """并发 1000 个物理环境，返回失败率。"""
    # 实现省略
    return {"success": True, "failure_rate": 0.02}
```

### 6.3 显存预算（保守估计）

| 模块 | 预算 | 峰值 |
|------|------|------|
| PyTorch CUDA context | 0.5 GB | 常驻 |
| faiss-gpu 索引（10 万向量） | 0.8 GB | 常驻 |
| Warp 沙盒（按需） | 1.5 GB | 动态 |
| 系统预留 | 1.5 GB | - |
| **总计** | **4.3 GB** | **可用 8GB 中的 53%** |

### 6.4 唯一最有信息量的任务

**任务**：Pacer 自举（狗粮开发）——用 Pacer 自己修改 `subagent_orchestrator.py`，增加 GPU 焦点检索。

**输入**：
```
在 Canvas.build_stateless_prompt 中，调用 gpu_focus_index.search_top_k 检索相关符号，
替代当前的线性遍历。保持接口不变，fallback 到 CPU 实现。
```

**验收基线**：
```bash
python -m pytest tests/test_subagent_orchestrator.py -q
```

### 6.5 A/B 对比设计

| 维度 | A（Pacer 现状） | B（Pacer + GPU 检索） | 门槛 |
|------|----------------|---------------------|------|
| **端到端延迟** | 112 秒（round2） | ≤ 110 秒 | 节省 >2% |
| **焦点检索时间** | 估计 50ms（线性） | <5ms（faiss-gpu） | 10x 提速 |
| **显存占用** | ~0.5 GB | ~1.3 GB | <2 GB |
| **回归通过率** | 387/387 | 387/387 | 无回归 |
| **Token/费用** | 基准 | 相同（检索不调 API） | 无增加 |

**停止门槛**：
- GPU 检索耗时 **> 50ms**（比 CPU 慢）。
- 显存占用 **> 2 GB**（挤压其他模块）。
- 回归失败 **> 0**。

---

## 七、前五项风险

### 7.1 显存碎片与 allocator 冲突
- **风险**：PyTorch、JAX、RAPIDS 各自管理显存池，导致 OOM。
- **缓解**：只用 PyTorch + faiss-gpu；避免同时加载 JAX。

### 7.2 物理仿真不代表软件验收
- **风险**：Warp 模拟通过，但 pytest 失败（业务逻辑错误）。
- **缓解**：物理沙盒仅用于**明确需要物理直觉的任务**（如力学、几何），不替代 pytest。

### 7.3 A*/MCTS 搜索空间建模失败
- **风险**：软件编辑空间难以编码 g(n)、h(n)，搜索无意义。
- **缓解**：**不引入 A*/MCTS**，改用 Beam Search + 静态启发。

### 7.4 "95% 剪枝"过度承诺
- **风险**：无训练数据，启发函数不准，反而增加延迟。
- **缓解**：先积累标注数据，训练轻量分类器（Phase 2，不在 MVP）。

### 7.5 GPU 提速被 API 延迟淹没
- **风险**：检索加速 10 倍（50ms → 5ms），但总耗时仍是 110 秒（API 占 90%）。
- **缓解**：明确 GPU 目标是**减少无效 API 调用**（Beam Search 剪枝），而非加速单次调用。

---

## 八、哪些主张目前不能成立

1. **"PageRank 5–15ms 精准抠出 20–50 节点"**：PageRank 是无向/等权图排序，不支持时间戳、置信度、因果方向。需要 temporal graph 或 causal inference 库。

2. **"Scallop/Lobster 微秒级逻辑审判"**：学术原型，未见生产部署。Pacer 现有 AST 规则检查已满足需求。

3. **"GPU A* 并发展开数千候选，95% 剪枝"**：软件编辑空间难以建模 h(n)，需训练数据支撑。A* 适用于确定性、低分支的搜索，不适用于高分支、随机性的代码生成。

4. **"物理/数值沙盒用客观结果反制模型作弊"**：物理模拟验证的是物理行为，软件验收的是业务逻辑。pytest 返回码才是唯一权威。

5. **"8GB 显存充裕，零语言模型权重"**：显存预算乐观（6.5GB 可用），但 JAX preallocate、PyTorch cache、RAPIDS 显存池可能冲突。需要 allocator 隔离。

6. **"GPU 提速打破瓶颈"**：端到端瓶颈是 API 延迟（90%）和 pytest 执行（10%），GPU 检索加速的边际收益 <1%。

---

## 九、最终建议

### 9.1 Pacer 保留/精简/废弃

| 模块 | 决策 | 理由 |
|------|------|------|
| subagent_orchestrator.py | ✅ 保留 | 编排核心，逻辑清晰 |
| canvas_manager.py | ✅ 保留 | 无状态上下文，验证有效 |
| local_indexer.py | ✅ 保留 | AST 索引 <20ms，无需 GPU |
| diff_patcher.py | ✅ 保留 | 事务式 SEARCH/REPLACE，刚性验收 |
| acceptance_discrimination.py | ✅ 保留 | 物理验收权威 |
| provider_runtime.py | ✅ 保留 | 多模型路由、熔断机制 |
| model_workflow.py | ✅ 保留 | 规划器、审查器 |
| task_review.py | ✅ 保留 | 对抗审查 |
| managed_state.py | ✅ 保留 | 状态机、预算管理 |
| AGENTS.md | ⚠️ 精简 | 保留核心文档，去掉重复说明 |
| dogfood_*.py | ⚠️ 精简 | 合并到单一狗粮模块 |
| 未使用的策略元数据 | ❌ 废弃 | 减少复杂度 |

### 9.2 增量嫁接路线（不掉头重构）

**Phase 1**（1 周）：faiss-gpu 焦点检索
- 增加 `gpu_focus_index.py`（200 行）。
- Canvas.build_stateless_prompt 调用 faiss-gpu。
- A/B 验证：检索耗时 <5ms，回归通过。

**Phase 2**（1 周）：Beam Search + 静态启发
- 每个 coder 生成 3 候选（温度采样）。
- 静态分析排序（复杂度、覆盖率）。
- 并行验证 top-3，保留首个通过。

**Phase 3**（可选）：Warp 物理沙盒
- 仅用于明确需要物理模拟的任务。
- 不替代 pytest 验收。

### 9.3 不引入的组件

- ❌ cuGraph PageRank（不适合因果检索）。
- ❌ Scallop/Lobster（学术原型）。
- ❌ JAX mctx（与 PyTorch 冲突）。
- ❌ A*/MCTS（搜索空间建模困难）。

---

## 十、逐日 MVP 计划（1–2 周）

| 日期 | 任务 | 输出 | 验收 |
|------|------|------|------|
| Day 1 | 环境准备：安装 faiss-gpu | `pip list \| grep faiss` | faiss-gpu 已装 |
| Day 2–3 | 实现 gpu_focus_index.py | 200 行代码 | 单元测试通过 |
| Day 4 | 嫁接到 Canvas.build_stateless_prompt | 修改 50 行 | 回归 387 通过 |
| Day 5 | A/B 对比：Pacer 自举任务 | 回执 JSON | 延迟 ≤110 秒 |
| Day 6–7 | Beam Search 原型 | 修改 orchestrator | 3 候选生成 |
| Day 8–10 | 并行验证 + 静态排序 | 修改 150 行 | top-3 选择 |
| Day 11–12 | 全回归 + 文档 | README 更新 | 387 通过 |
| Day 13–14 | 生产验证：3 个真实任务 | 回执 + trace | 无回归 |

**停止门槛**：
- GPU 检索耗时 > 50ms。
- 回归失败 > 0。
- 端到端延迟无改善。

---

**结论**：Pacer 是可复用的确定性内核，无需掉头重构。GPU 外骨骼方案工程可行，但核心主张过度承诺。推荐增量嫁接 faiss-gpu 检索 + Beam Search 剪枝，不引入 PageRank/Scallop/A*。物理仿真不替代 pytest。端到端瓶颈是 API 延迟，GPU 提速边际收益 <1%。
