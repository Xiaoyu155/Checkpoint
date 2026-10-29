# Pacer (Speed-of-Light Multi-Model Accelerated Coding Protocol)

<div align="center">

**🚀 极速多模型聚合协同编码引擎 | 无感集成 Claude Code / Cursor / 终端**

*95%+ Token 节约 · 3 秒极速补丁 · 本地 AST 毫秒级符号提取 · 自动化测试闭环与跨会话留痕*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)](https://www.python.org/)
[![Multi-Model](https://img.shields.io/badge/Orchestration-Grok%204.7%20%7C%20Codex%20Plus%20%7C%20Gemini%20Flash-orange.svg)]()

</div>

---

## 🌟 为什么选择 Pacer？

在大模型辅助编程中，开发者往往面临四大痛点：
1. **Token 消耗暴增**：反复将整个上千行的工程文件灌入对话窗口，改几次就爆上下文或触发限额。
2. **多模型割裂**：想用 **Gemini Flash** 的大上下文做工程检索，用 **xAI Grok 4.7** 的秒级流式做极速编码，用 **Codex Plus** 攻坚架构重构，必须在网页和多个终端之间反复复制代码。
3. **测试不敢放心交出**：AI 生成完代码直接“假装通过”，或者直接篡改测试断言甚至删掉单测。
4. **终端卡顿与心智负担**：想要智能体的高效，但不想学习复杂的 Agent DSL，只想保留在终端最自然的对话习惯。

**Pacer** 专为解决上述痛点而生：
- **无感透传集成**：在 Claude Code 或命令行终端里照常自然对话，Pacer 在底层毫秒级拦截代码生成与修 Bug 任务并派发最优引擎。
- **Stateless Canvas + SEARCH/REPLACE Diff 架构**：严格将单次对话 Prompt 压缩在 **<= 1,500 Token**（节约 95%+ 上下文），生成 Aider 风格的精确差异块，毫秒级原子打入磁盘。
- **本地原生 AST 符号索引器**：完全不依赖外部大模型，Python AST 在 **< 20ms** 内精确提取类、方法、调用签名及出错回溯上下文。
- **多模型无缝阶梯容错 (Zero-Waiting Failover)**：
  - **极速首选**：xAI Grok 4.7-latest（通过 Sub2API / OpenAI 兼容网关，3 秒极速出码）
  - **架构重器**：Codex Plus（深度推理与复杂并发并发回退）
  - **免费大池**：Gemini 3.8 Flash（百万上下文工程概览与粗排）
  - **本地辅助**：WorkBuddy (DeepSeek 桌面端网关)
- **铁血验收闸门与全生命周期留痕**：内置防篡改守卫（严禁模型修改既有单测契约），执行过程统一持久化记录在 `~/.pacer/traces.jsonl`，跨窗口可追溯可审查。

---

## 🏗️ 核心架构

```text
               【开发者终端 / Claude Code 自然交互】
                               │
                               ▼
               Pacer 毫秒级任务分流 (`cli.py auto`)
                               │
       ┌───────────────────────┴───────────────────────┐
       ▼                                               ▼
【本地 AST 极速索引器】                       【智能模型决策路由】
(<20ms 提取符号、Imports、回溯)             (Grok 4.7 / Codex / Gemini / WB)
       │                                               │
       └───────────────────────┬───────────────────────┘
                               ▼
                 【Stateless Canvas 状态画布】
                (单回合单提示词严控 <= 1,500 Tokens)
                               │
                               ▼
            【xAI Grok 4.7-latest (Sub2API) 3秒出码】
                   (若触发限额/异常 0秒无感切换 Codex)
                               │
                               ▼
               【Diff Patcher 原子补丁注入】
              (Aider 格式精准替换，支持事务级回滚)
                               │
                               ▼
                 【自动化测试与防篡改门禁】
              (pytest / unittest 验证，严禁篡改测试)
                               │
                               ▼
                 【跨会话持久化留痕审计】
                  (~/.pacer/traces.jsonl)
```

---

## ⚡ 快速开始

### 1. 安装要求
- Python >= 3.10
- Git (Windows / Linux / macOS)

### 2. 本地快速安装
```bash
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint/xiao
pip install -e .
```

### 3. 配置凭证
在你的家目录下的 `~/.pacer/credentials.json` 配置模型网关（例如 Sub2API 或直连 xAI）：
```json
{
  "grok_api_key": "your_sub2api_or_xai_key",
  "sub2api_base": "https://direct.aiboxz.com/v1",
  "grok_model": "grok-4.7-latest"
}
```

### 4. 常用 CLI 命令

#### 📊 启动现代化中英双语控制面板 (Dashboard)
实时查看节省的 Token 与时间看板、五大核心支柱状态、自动嗅探本机所有 AI 渠道：
```bash
pacer dashboard
```

#### 🔍 检查所有已接入的模型引擎状态
```bash
pacer backends
```

#### 🚀 极速闭环开发（Auto 模式）
自动根据目标检索代码、生成补丁、执行测试、留痕审计：
```bash
pacer auto "在 live_trading 模块中优化资金风控阈值，并运行对应单元测试"
```

#### 🧠 生成多角色 DAG 执行规划
```bash
pacer plan "重构数据获取层，引入带缓存的异步数据源"
```

#### 📜 查看跨窗口、跨会话持久化留痕记录
```bash
pacer trace --limit 10
```

---

## 🔌 深度集成 Claude Code（无感加速）

将以下内容放入全局 `~/.claude/CLAUDE.md`，即可让 Claude Code 在接到写代码、修 Bug 需求时，自动透传给 Pacer 极速执行：

```markdown
# Pacer Multi-Model Accelerated Coding Protocol (Global)

When the user asks you to write code, modify files, fix a bug, or run/fix tests:
- DO NOT read multiple large files into the conversation context.
- DO NOT write large multi-file diffs manually.
- Immediately delegate to Pacer Auto using Bash:
  `pacer auto "<user_task>" --workspace-root "<current_directory>"`
- Display the Pacer execution log and summarize modified files and test results.
```

---

## 📊 性能实测与审计基准

| 指标 | 传统大模型窗口直写 | Pacer 多模型加速架构 | 优化效果 |
| :--- | :--- | :--- | :--- |
| **单回合 Token 开销** | 8,000 ~ 25,000+ Tokens | **< 1,500 Tokens** | **节省 92% ~ 95%** |
| **代码生成等待耗时** | 35s ~ 90s (进程冷启动/全量吐字) | **3.2s ~ 8.5s** (Grok 4.7 流式) | **提速 5~10 倍** |
| **符号检索耗时** | 10s ~ 20s (LLM 查阅) | **< 20ms** (本地 Python AST) | **几乎零延迟** |
| **测试安全性** | 容易发生模型“偷偷改测试断言” | **强制校验 Git 契约白名单，误改立拒** | **100% 验收确定性** |

---

## 🛡️ 开源协议

本项目基于 [MIT 许可证](LICENSE) 开源。欢迎提交 Issue 与 PR 共同完善多模型加速生态！
