# Checkpoint 下一阶段开发计划

**计划日期**: 2026-06-09  
**适用场景**: 新开对话框后继续开发时的唯一主计划  
**前置参考**:
- [审计报告](audit_report.md)
- [技术汇报](technical_report.md)
- [快速开始](quickstart.md)

## 1. 目标

这一阶段不再追求“功能面继续扩张”，而是把项目收口到可推广状态。

总目标有三个:

1. 陌生用户能自助完成从零到第一次 `verify`。
2. Codex / Claude Code / 其他 AI agent 能稳定消费结构化输出，不会因为字段漂移静默失效。
3. 对外定位语言统一，不再让用户第一眼把它理解成“单纯的视觉工具”。

## 2. 当前结论

审计之后，主链路已经闭合，当前没有新的硬阻塞缺口。  
刚刚修掉的真实问题是 `verify-impl` 对 workspace-relative 本地路径误判为 SSRF 风险，这个问题已经解决并补了回归测试。

所以这一阶段的工作重点不是“补一个大洞”，而是:

- 补入口
- 补文案
- 补契约
- 补一致性
- 补真实执行路径

## 3. 执行顺序

按这个顺序推进，不要乱跳:

1. **Group A + Group E**
2. **Group B + Group C**
3. **Group D + Group F**

原因很简单:

- A + E 决定陌生用户能不能开始。
- B + C 决定 AI agent 能不能稳定读懂输出。
- D + F 决定能不能在扩展和真实 demo 上形成分发与执行置信度。

## 4. Group A - 入口与 Onboarding

**负责面**:
- `src/visual_agent/cli.py`
- `src/visual_agent/workspace.py`
- `src/visual_agent/preflight.py`
- `src/visual_agent/model_credentials.py`

**完成标准**:
- 新用户跑 3 条命令能看到第一次绿。

### 4.1 任务

1. `init` 完成后打印 next steps
2. 路径报错时附加 Hint
3. `model-credentials-inspect` 增加 fallback 提示
4. 修复 `env-check` 项目类型误检
5. `verify-impl` 支持零配置模式

### 4.2 任务说明

#### 4.2.1 `init` 完成后打印 next steps

当前 `init` 跑完就退出，陌生用户不知道下一步做什么。

要求:

- 输出 3 步以内
- 能直接复制执行
- 不要只给抽象说明

建议 next steps:

1. `workspace-status`
2. `verify-impl`
3. `show-status` 或 `context-snapshot`

#### 4.2.2 路径报错 -> 指向解法

当出现 `No such file or directory`、`FileNotFoundError`、工作区路径不存在等错误时，要给用户可执行建议。

要求:

- JSON 格式带 `suggestion`
- markdown 格式带 `Hint:` 或 `Try:`
- 例子里尽量带具体命令，不要只写“检查路径”

#### 4.2.3 `model-credentials-inspect` fallback 提示

如果当前模型不可用，但检测到 Anthropic key，应提示:

- `Anthropic key detected, use --model claude-...`

反过来，如果是 OpenAI / Gemini 但当前后端不支持，也要明确提示当前可用模型名。

#### 4.2.4 `env-check` 项目类型误检修复

当前要防止 Python 项目根目录被误判成 `vue` 或其他前端框架。

要求:

- 项目类型识别要更保守
- 不要因为有 `package.json` 就把整个项目错误归类
- Python 项目优先保持为 `python` / `unknown`

#### 4.2.5 `verify-impl` 零配置模式

这是 A 组最重要的任务。

要求:

- 无 `--base-url` 时自动推断
- 能从 `package.json` / `vite.config.*` / `next.config.*` / `manifest.json` 推断端口和基础 URL
- 能对本地 workspace-relative fixture 做正确处理
- 不要再把本地路径误判成 SSRF

### 4.3 验收标准

陌生用户在一个空 workspace 中，能跑通以下路径:

```powershell
visual-agent init
visual-agent verify-impl --workspace-root .agent-workspace ...
visual-agent show-status
```

输出要满足:

- 不靠猜
- 不靠手工翻文档
- 不出现无意义的硬错误

## 5. Group E - 重新定位与文档

**负责面**:
- `README.md`
- `docs/`
- 用户可见字符串

**完成标准**:
- 新用户 30 秒内理解这是什么、为什么有价值、第一步做什么。

### 5.1 任务

1. README 改写
2. 去掉 visual 的首要感
3. Quick Start 改成 3 条命令跑通
4. MCP 接入说明独立成页并在 README 有一级入口

### 5.2 任务说明

#### 5.2.1 README 改写

首屏要从“工具清单”变成“运行时定位”。

建议定位语言:

- `AI agent 验证运行时`
- `本地工作流执行层`
- `结构化失败输出`

不要再让首屏像“一个视觉 demo 工具”。

#### 5.2.2 去掉 visual 的首要感

对外语言不要让“视觉”成为第一印象。

原则:

- 先说验证运行时
- 再说结构化输出
- 最后才说视觉兜底

#### 5.2.3 Quick Start: 3 条命令跑通

必须是真实项目可跑，不要只在 demo 上成立。

推荐结构:

1. `init`
2. `verify-impl`
3. `show-status` / `context-snapshot`

#### 5.2.4 MCP 接入说明独立成页

现在 MCP 说明散在多个文档里，应该在 README 有一级入口。

要求:

- 有复制即用的 Cursor / Claude Code / VS Code 示例
- 明确说明要先读哪个状态文件
- 明确说明哪些工具是推荐的起步工具

### 5.3 验收标准

- README 第一屏能告诉陌生用户这是干什么的
- Quick Start 不依赖上下文记忆
- MCP 接入步骤可复制

## 6. Group B - MCP 与结构化输出

**负责面**:
- `src/visual_agent/mcp_server.py`
- `src/visual_agent/failure_summary.py`
- `src/visual_agent/session.py`
- `src/visual_agent/state.py`

**完成标准**:
- Codex / Claude Code 消费输出不会因字段漂移静默报错。

### 6.1 任务

1. `structured_failure` schema 锁 v1
2. MCP 新增 `verify_workflow` 工具
3. `context-snapshot` 输出格式稳定
4. MCP 接入文档

### 6.2 任务说明

#### 6.2.1 `structured_failure` schema 锁 v1

要求:

- 加 `schema_version`
- shape 固定
- 字段名固定
- 必须有回归测试

这是整个 AI 消费链的契约基线。

#### 6.2.2 MCP 新增 `verify_workflow` 工具

当前 MCP 侧有读状态、读失败、生成 workflow 的工具，但缺一个直接触发 verify 的入口。

要求:

- 能直接跑 workflow 验证
- 返回结构化结果
- 失败时返回统一失败对象

#### 6.2.3 `context-snapshot` 输出格式稳定

要求:

- 字段顺序稳定
- key 名稳定
- 不要因为新增信息就改掉旧 key

最好做成回归测试锁死。

#### 6.2.4 MCP 接入文档

要有能复制粘贴的配置示例，至少覆盖:

- Cursor
- Claude Code

### 6.3 验收标准

- AI agent 不会因为字段漂移而误判状态
- `structured_failure` 可以直接用于自动修复建议
- `context-snapshot` 可作为稳定入口

## 7. Group C - CLI 一致性

**负责面**:
- `src/visual_agent/cli.py`

**完成标准**:
- 所有工作流相关命令参数名一致，每个 exit-1 都有可操作提示。

### 7.1 任务

1. 统一 workflow 参数名
2. 所有 exit-1 路径加 suggestion
3. `verify-impl` help text 补充示例

### 7.2 任务说明

#### 7.2.1 统一 workflow 参数名

目标是减少用户记忆负担。

建议策略:

- 统一成一种主参数写法
- 旧写法保留兼容一段时间
- help 中明确标记推荐写法

#### 7.2.2 所有 exit-1 路径加 suggestion

要求:

- JSON 格式加 `suggestion`
- markdown 格式加建议行
- 错误信息不要只报错，要给下一步动作

#### 7.2.3 `verify-impl` help text 补充示例

必须给真实例子:

- `--task-description`
- `--base-url`

避免用户第一次就因为参数语义不明而卡住。

### 7.3 验收标准

- 同一类命令不要出现三种不同的参数风格
- 常见报错都有下一步建议

## 8. Group D - VS Code 扩展

**负责面**:
- `vscode-extension/`

**完成标准**:
- 扩展能走完 `init -> verify` 一轮，不需要手敲 CLI。

### 8.1 任务

1. 命令面板加 `x-agent: Verify Implementation`
2. 状态栏读取 `show-status` 真实数据
3. onboarding walkthrough
4. 打包测试

### 8.2 任务说明

#### 8.2.1 命令面板入口

主入口要明显，不要埋得太深。

#### 8.2.2 状态栏真实数据

状态栏不能是静态文本，必须读实际状态文件或 CLI 输出。

#### 8.2.3 onboarding walkthrough

首次安装后应引导用户:

1. init
2. verify
3. 看结果

#### 8.2.4 打包测试

要确认打包后没有路径假设和本地开发目录依赖。

### 8.3 验收标准

- 不打开终端也能完成最小闭环
- 扩展里能看到真实状态

## 9. Group F - 真实执行路径

**负责面**:
- `src/visual_agent/workflow.py`
- `src/visual_agent/vision.py`
- `examples/`

**完成标准**:
- 非 dry-run 的 click 在 demo app 上跑通，有截图证据。

### 9.1 任务

1. `minimal_testable_workflow` 去掉 dry-run 跑一遍
2. Next.js hydration mismatch 处理
3. `run-profile: supervised` 加入 demo 程序入口

### 9.2 任务说明

#### 9.2.1 `minimal_testable_workflow` 真实执行

当前这类 workflow 主要验证过 dry-run。

要补一轮真实执行，至少要确认:

- click 真正触发
- 截图真实生成
- 失败上下文真实可读

#### 9.2.2 Next.js hydration mismatch 处理

有两个选择:

- 修 demo
- 把这个错误标记为 known issue，并在结构化输出里显式标注

不能让它以“莫名其妙失败”的形式留在主故事里。

#### 9.2.3 `supervised` demo 入口

要有一个用户能直接试的 supervised 路径，便于他们从 dry-run 过渡到真实执行。

### 9.3 验收标准

- 至少一个 demo 的真实 click 路径通过
- 失败时有证据，不是黑盒

## 10. 里程碑建议

### 第一批

- Group A
- Group E

目标: 让陌生用户能开始，并且理解产品是什么。

### 第二批

- Group B
- Group C

目标: 让 AI agent 和 CLI 输出稳定可消费。

### 第三批

- Group D
- Group F

目标: 让扩展和真实 demo 都能形成推广证据。

## 11. 预计完成定义

达到推广状态前，至少满足:

- 新用户能在 3 条命令内看到第一次绿灯
- `verify-impl` 默认模式能在真实项目上工作
- `structured_failure` 输出稳定
- README / docs / 扩展入口一致
- 至少一个 demo 有真实执行证据

## 12. 不要做的事

这一阶段不要把时间继续花在下面这些地方:

- 继续扩展新动作而不收口入口
- 继续堆云端 spec 而不补本地体验
- 继续优化生成质量但不固化契约
- 继续改大规模架构而不先让陌生用户跑通

## 13. 新对话框起步建议

新开对话框后，建议先看:

1. [docs/audit_report.md](audit_report.md)
2. [docs/next_phase_plan.md](next_phase_plan.md)
3. [README.md](../README.md)

然后按顺序做:

1. Group A
2. Group E
3. Group B
4. Group C
5. Group D
6. Group F

## 14. 最后一句

这阶段的关键不是“再做很多功能”，而是把用户真正带到第一次绿灯，把 AI 消费链路稳定住，把对外语言统一成一个可以推广的产品叙事。
