# 模型路由与额度守卫方案（虚拟用户的"调度大脑"）

日期：2026-07-03
依据：负责人两次共识——(1) 瘦打工人/胖平台：上下文住在 DevPacer 虚拟大脑里，Claude Code 是无状态搬砖工；(2) 本次新增：外部便宜 API 作为调度大脑，对用户订阅额度做合理分配，并可把便宜模型穿插进开发。
状态：方案已定，2026-07-03 负责人验收后已落地第一批（见下）。

**2026-07-03 已落地**：
- `repo_map.py`：零 token 项目架构记忆（git + ast/正则本地索引，增量缓存 `.agent-workspace/repo_map.json`，按目标相关性预算渲染），自动注入每次 worker/repair prompt；CLI `checkpoint repo-map`。
- `subscription_quota.py` + `checkpoint quota` / `quota-statusline`：Claude Code statusline 的官方 `rate_limits`（five_hour / seven_day used_percentage + resets_at）落盘为账户级快照，派发预览显示额度水位并在 ≥80% 时预警（只警不拦，快照可能过期）。**订阅窗口额度 ≠ API 成本记账，两者字段与展示完全分离。**
- 测试防篡改门：`--test-command` 验收前机械检测 worker 是否改动测试文件（对 worktree base 的 git diff + 未跟踪文件），命中即拒绝验收并生成"先还原测试再修代码"的修复简报；`--allow-test-edits` 显式豁免。

---

## 一、问题定义

用户（人类）做不到模型的合理使用：不管任务多小都用最贵的模型。实测证据：Claude Code 一次 trivial 调用 $0.042（~16k token 固定开销 + Opus 单价）。改一行文案用 Opus 和用 DeepSeek 结果相同，价格差 50 倍以上。同时用户会"跑着跑着没额度"，因为没有任何东西在替他记账、设限、降级。

## 二、总架构

```text
用户 ──一句需求──> DevPacer 虚拟用户（胖平台，记忆在本地）
        │
        ├─ ① Spec 大脑（DeepSeek，几分钱）
        │     需求门澄清 → 高密度 Spec（<150 行精准任务）
        │
        ├─ ② 路由器 Router（规则优先，LLM 分类兜底）
        │     trivial/mechanical ──> Claude Code 壳 + 便宜后端
        │                            （MiMo / DeepSeek Anthropic 兼容端点，烧 API 额度）
        │     normal ──────────────> Claude Code 订阅（sonnet 档）
        │     complex ─────────────> Claude Code 订阅（opus 档）
        │
        ├─ ③ 预算守卫 Budget Guard
        │     实时成本台账（已落地：真实 total_cost_usd 入 worker record）
        │     mission 级 max_cost_usd 熔断
        │     429/限流 → 自动降级便宜后端 或 暂停（mission 可恢复）
        │
        └─ ④ Checkpoint 验收（确定性裁决）
              路由保守 + 验收兜底：路由错了最多浪费一轮便宜额度，
              绝不把垃圾交付出去
```

## 三、设计原则（延续既有铁律）

1. **外部 API 是增强层不是地基。** 无 key 时：规则路由 + 纯订阅照样跑（优雅降级）。有 key 时：Spec 大脑与便宜打工人激活。
2. **路由决策本身必须便宜。** 规则优先（关键词/涉及文件数/diff 预估/领域），只有规则拿不准时才调一次 DeepSeek 分类（几分钱）。不允许用贵模型做路由。
3. **验收永远是确定性裁决。** LLM 不当裁判（原则 9 不变）。便宜模型翻车由 Checkpoint 拦住，修复轮升档不降档（与"repair 保持 strong"的既有决断一致：重试档位 ≥ 首发档位）。
4. **记忆只从落盘证据聚合**（原则 10 不变）。路由器的历史依据（哪类任务便宜档成功率高）来自 worker records 的真实结果，不靠模型转述。

## 四、诚实边界

- **订阅剩余额度不可查询。** 实现方式 = 实时记账 + 上限熔断 + 429 降级/暂停恢复，不承诺"预知余额"。
- **便宜模型代码能力弱。** 默认只路由机械/小范围/低风险任务；成功率数据积累后再逐步放宽。
- **并发扇出受订阅速率限制约束**，产品文案不得宣传"无限并行"。

## 五、实施阶段

### P1：路由器 v1 + 后端抽象（先做，纯本地逻辑为主）
- `model_router.py`：任务 → 档位（cheap / standard / strong）。规则：目标关键词（rename/文案/格式化→cheap；重构/架构/并发→strong）、涉及文件数、领域标准数量、历史失败签名。
- worker 后端字段：dispatch 拉起 Claude Code 进程时按路由注入环境变量（`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`）切换到便宜后端；不注入则走订阅。
- 能力档案新增 `backends:` 段（mimo / deepseek-anthropic 端点、可用模型、verified_on），保持数据驱动。
- 预览必须显示路由结果与预估成本，用户可一眼看到"这活派给了谁、预计花多少"。

### P2：预算守卫
- `budget.json` 增加 `max_cost_usd`；台账（已有真实 cost 记录）聚合出 per-mission / per-repo 累计报表；超限 → `budget_exhausted` 熔断（机制已有，加成本维度）。
- 429/限流检测 → 按策略自动降级到便宜后端重试一次，或把 mission 置为可恢复的 `rate_limited` 暂停态。
- 派发前估算：用台账里同类任务的历史单轮成本给出预估，超预算直接拒绝派发（呼应"拒绝派发是省钱功能"）。

### P3：DeepSeek Spec 大脑
- chief-plan 的澄清与 Spec 压缩接 DeepSeek（有 key 才启用；无 key 回退现有确定性规则）。
- 失败三分类（环境问题/代码问题/预期问题）用便宜模型，产出只作为证据与路由输入。

### P4：OpenRouter 接入（一个适配器打开一堆便宜模型）
- `PROVIDER_DEFAULTS` 加 `openrouter`（OpenAI 兼容，base_url https://openrouter.ai/api/v1）。负责人已有 key。
- 路由器把 OpenRouter 上的便宜模型纳入 cheap 档候选。

### 与"硬 Hook 验收"的关系
硬 Hook（worker 不过验收退不出去）与本方案互补：Hook 杀死循环，路由省单价。实施顺序建议 P1 → 硬 Hook → P2 → P3 → P4，因为 P1 的后端抽象是 Hook 注入 `.claude/settings.json` 时要一起写进 worktree 的。

## 六、MiMo 续费结论

走"便宜后端借 Claude Code 壳"这条路，MiMo（¥40/月，1.6 亿 credits，明确支持作为 Claude Code 后端）从"可有可无"变成 **cheap 档的首选后端**，建议续费一个月实测；DeepSeek Anthropic 兼容端点作为第二后端互为备份。

## 七、验收标准（本方案自身的 Done 定义）

1. 同一个 mission，机械小任务被路由到便宜后端执行并通过 Checkpoint 验收，worker record 里可见后端与成本。
2. 复杂任务被路由到订阅 Opus 档，预览中显示路由理由。
3. 设置 `max_cost_usd` 后超限熔断，final_report 说明停在哪、花了多少。
4. 拔掉所有 API key，系统回退纯订阅 + 规则路由，全链路仍可跑通。
