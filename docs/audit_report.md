# Checkpoint 审计报告

**审计日期**: 2026-06-09  
**审计范围**: 仓库主线代码、CLI、MCP、工作流引擎、验证层、扩展、示例应用、文档、测试

## 1. 执行摘要

本次审计的结论很直接: 项目已经从“功能原型”推进到“可交付的本地工作流平台”，主链路是闭合的，当前没有发现会立刻阻断交付的未完成项。

刚刚确认并修复了一个真实问题: `verify-impl` 核心路径会把 workspace-relative 本地路径，例如 `fixtures/simple_form.html`，误判成 SSRF 风险，导致预检失败。这个问题属于实际阻塞项，不是文档层面的待办。修复后，验证链路恢复正常。

当前剩余项主要是两类:

1. 刻意保留的未来接口，例如 OpenAI / Gemini 适配、云端 marketplace API spec、部分导出语义的 TODO。
2. 非阻塞的产品扩展点，例如更完整的云端多租户、更多模型供应商、Playwright 导出语义增强。

这意味着当前的优先级不应该再放在“继续补功能”上，而是放在“稳态维护、文档收口、发布准备、真实用户试用反馈”上。

## 2. 审计方法

本次审计不是只看 README，而是把以下几层一起核对:

- 代码入口: `src/visual_agent/cli.py`, `src/visual_agent/workflow.py`, `src/visual_agent/mcp_server.py`
- 核心配套: `validation.py`, `preflight.py`, `quality.py`, `reports.py`, `security.py`, `vision.py`
- 对外面: `vscode-extension/`, `cloud_api/`, `examples/`
- 验证层: `tests/test_cli.py`, `tests/test_mcp_server.py`, `tests/test_validation.py`, `tests/test_demo_apps.py`
- 文档层: `README.md`, `docs/`, `examples/workflows/README.md`

审计时重点看了三件事:

1. 哪些能力已经是闭环。
2. 哪些地方仍然是“预留位”而不是缺陷。
3. 哪些地方是现在就会影响用户使用或验证结果的真实问题。

## 3. 当前架构状态

### 3.1 核心工作流链路

当前核心链路已经完整:

- 工作区初始化和识别
- workflow 生成
- workflow 校验
- preflight 环境检查
- 本地运行
- 失败归因
- 状态文件
- run history
- 报告输出
- CI 和质量门禁

这条链路不是纸面存在，而是有 CLI、MCP、测试和文档共同支撑。

### 3.2 CLI 层

CLI 已经覆盖了很多实际操作面:

- `init`
- `env-check`
- `generate-workflow`
- `verify`
- `verify-impl`
- `quality-gate`
- `generate-report`
- `generate-fixture`
- `generate-ci`
- `cloud-run`
- `usage`
- `activate`
- `stats`
- `export-runs`
- `workflow-lint`
- `context-snapshot`
- `show-status`

这说明项目已经不再只是一个单点工具，而是一个可直接操作的命令集合。

### 3.3 MCP 层

MCP 侧已经覆盖:

- 生成 workflow
- 读取 session 状态
- 读取失败详情
- 读取视觉状态
- 读写 workspace 上下文
- 诊断和修复 payload

这部分对 Codex / Claude Code / Cursor 的接入价值很高，因为它减少了“读文件拼上下文”的成本。

### 3.4 VS Code 扩展

扩展已经不是空壳:

- 侧边栏有 workflow 视图
- 状态栏有快捷入口
- 有集成测试
- 有发布清单
- 有命令和配置项

这是很关键的，因为扩展是低摩擦分发入口，也是 workflow 获取量的实际放大器之一。

### 3.5 示例应用

`examples/demo-app` 和 `examples/nextjs-demo` 都已经补齐:

- 可安装依赖
- 可构建
- 可启动
- 可用浏览器核对
- 配套 workflow 已接入索引

其中 Vue demo 之前有一个端口冲突问题，但已经改到 `4173`，避免了和本机其他项目冲突。

## 4. 已完成的关键工作

下面是审计里认为“已经落地且有实际价值”的部分。

### 4.1 内容种子和生成质量

已经补了 starter workflow 内容和生成提示策略:

- `workflows/examples/auth/`
- `workflows/examples/forms/`
- `workflows/examples/navigation/`
- `workflows/examples/ecommerce/`
- `workflows/examples/states/`
- `workflows/examples/admin/`
- `workflows/examples/mobile_h5/`

这些内容不是装饰，而是 workflow 生成器的 few-shot 资产。对生成准确率和后续 workflow 获取量有直接影响。

### 4.2 可靠性和安全

已经建立:

- preflight 检查
- 端口检测
- 构建新鲜度检查
- SSRF 防护
- workflow 注入防护
- 安全审计日志
- security workflow

这部分的意义是把“失败看起来像代码问题”的噪音降下来。

### 4.3 失败归因和报告

已经建立:

- structured failure
- root cause 分类
- Next.js hydration mismatch 这类已知框架噪音现在会被标成 `known_issue`
- related files 推断
- report HTML
- trend chart
- PR comment 失败摘要
- latest failure 渲染

这类能力对真实用户很重要，因为它把“失败”从纯文本变成了可以行动的上下文。

### 4.4 云端和货币化骨架

已经有:

- cloud API 目录
- worker 骨架
- storage 兼容层
- auth key 处理
- license 读写
- usage 统计
- cloud run quota

这些还不是完整云产品，但骨架已经在位，不会把未来路线堵死。

### 4.5 集成和分发

已经有:

- Cursor rules
- Copilot instructions
- Windsurf rules
- JetBrains spec
- Playwright export
- demo apps
- public workflow examples

这说明项目的分发路径已经不是单一 CLI，而是多个入口并行。

## 5. 真实发现的问题

这是本次审计最重要的部分。真正需要修的，不是“未来功能”，而是会影响当前验证闭环的问题。

### 5.1 预检把本地 fixture 路径误判为 SSRF

**状态**: 已修复  
**影响面**: `verify-impl`、MCP verify payload、CLI verify impl 回归

#### 现象

当 workflow 使用 `fixtures/simple_form.html` 这类 workspace-relative 本地路径时，`validate_workflow_url()` 会把它当成缺少 scheme 的 URL，并返回:

```text
Unsupported URL scheme: missing
```

然后 `run_preflight()` 失败，整个 `verify-impl` 链路中断。

#### 为什么这是问题

`verify-impl` 的目标就是对本地项目和本地 fixture 做自动验证。如果连本地 fixture 都不能通过预检，那这条核心链路就会在真实使用时断掉。

#### 修复方式

在 `validation.py` 中增加了本地路径识别逻辑，对以下形式直接放行:

- `fixtures/simple_form.html`
- `./fixtures/simple_form.html`
- `../fixtures/simple_form.html`
- 绝对本地路径

仍然保留对真正网络 URL 的 SSRF 检查。

#### 影响结果

现在 `verify-impl` 的相关测试恢复通过。

### 5.2 Vue demo 端口冲突

**状态**: 已修复  
**影响面**: `examples/demo-app`

#### 现象

`5173` 被本机上的另一个项目占用，导致 demo-app 的浏览器核对根本不是我这套 Vue demo，而是别的项目。

#### 修复方式

把 demo-app 默认端口改成:

- `4173`

同时同步更新了:

- `examples/demo-app/README.md`
- `examples/demo-app/workflows/*.yaml`

#### 影响结果

现在 Vue demo 和 Next.js demo 可以并行存在，不会互相踩端口。

## 6. 当前仍然保留的空位

下面这些是“刻意保留”，不是缺陷。审计上要明确区分，不然容易把路线图和 bug 混在一起。

### 6.1 模型适配的预留位

文件: [src/visual_agent/llm_providers.py](../src/visual_agent/llm_providers.py)

当前状态:

- `anthropic` 是当前默认可用后端
- `openai` 和 `gemini` 是保留位
- 调用这些后端会明确报 `NotImplementedError`

这不是漏做，而是把未来扩展接口先固定下来，避免后面大改生成链路。

### 6.2 Marketplace 只是 spec

文件: [docs/marketplace-api.md](../docs/marketplace-api.md)

当前状态:

- `GET /api/workflows`
- `GET /api/workflows/search`
- `POST /api/workflows/publish`

这些都是协议文档，不是本地实现。

这也不是缺陷，因为本地项目当前的目标是把 workflow 资产、验证、分享和分发的基础打好，不是马上上线完整 marketplace。

### 6.3 Playwright 导出不是 1:1 语义转换

文件: [src/visual_agent/integrations.py](../src/visual_agent/integrations.py)

当前状态:

- 能导出
- 能生成 Playwright Test 基础框架
- 不能对所有 Checkpoint 动作做完美 1:1 映射

这类 `TODO` 是合理的，因为 Checkpoint 的动作模型比 Playwright 更高层。强行做伪等价反而会误导用户。

### 6.4 回归草稿里有人工补强提示

文件: [src/visual_agent/workspace.py](../src/visual_agent/workspace.py)

当前状态:

- 生成回归草稿时有提示人工补 selector/assertion
- 它不是 runtime 故障
- 它是对“自动生成草稿 != 最终可执行回归”的现实提醒

这是设计上的保守处理，不是代码坏掉。

## 7. 验证结果

本次审计实际跑过的关键验证:

- `python -m pytest tests/test_cli.py tests/test_mcp_server.py tests/test_validation.py -q`
- 结果: `155 passed`

另外也验证了:

- `python -m compileall src/visual_agent`
- demo-app 和 nextjs-demo 都能安装、构建、启动
- 浏览器级核对通过

## 8. 风险评估

### 8.1 低风险

以下风险当前较低:

- 核心 workflow 执行
- CLI 基础命令
- MCP 核心读写
- 失败归因
- 报告输出
- demo app 的启动和视觉核对

### 8.2 中风险

以下属于中风险，不是因为有 bug，而是因为未来扩展面较大:

- 多模型接入
- 云端 marketplace 完整实现
- Playwright 导出语义增强
- 云端运行的规模化和多租户边界

### 8.3 需要持续关注

下面这些点要持续看，但当前不构成阻断:

- 规范文档和实际命令是否继续同步
- 示例 workflow 是否和 demo app 保持一致
- 扩展和 CLI 的参数是否继续分叉
- 未来新增动作是否被 validation、quality、mcp、export 全链路覆盖

## 9. 建议的下一步

不是“继续堆功能”，而是做收口。

建议优先级如下:

1. 发布准备
   - 把安装、验证、扩展、示例、报告入口做最终检查
   - 准备一个可对外发的版本说明

2. 用户试用
   - 让真实项目跑一轮 `verify-impl`
   - 看是否还有新的本地路径、端口、预检边界问题

3. 轻量补强
   - 如果用户反馈集中在某类 workflow，再补那一类示例和 few-shot
   - 如果导出或云端接口频繁被问，再补文档或适配层

4. 暂缓大扩展
   - OpenAI / Gemini adapter
   - 完整 marketplace
   - 云端发布闭环

这些都值得做，但不是现在最需要做的。

## 10. 最终判断

如果用一句话总结:

**当前项目已经没有明显的“必须立刻修”的空洞，真正应当优先的是发布收口、真实试用和边界稳定，而不是继续扩展功能面。**

刚才修掉的本地 fixture SSRF 问题，是审计中唯一明确的硬缺口。除此之外，剩下的主要是未来路线图上的预留位。

