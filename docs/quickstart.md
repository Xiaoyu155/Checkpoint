# Pacer 快速上手使用手册 (Quickstart Guide)

[English](#english-version) | [简体中文](#简体中文版)

---

## 简体中文版

欢迎使用 **Pacer (Speed-of-Light Multi-Model Accelerated Coding Protocol)**！
Pacer 是一款专为开发者设计的**极速多模型协同调度与省额度编码引擎**。通过无状态画布与精准差量补丁技术，为您在日常使用 Claude Code、Cursor、终端命令行写代码时，**节省 95% 以上的 Token 消耗**，并在 3 秒内极速生成代码与执行测试验证。

### 📦 1. 一键下载与安装

无论是在 Windows、macOS 还是 Linux 上，仅需通过 Git 与 Python 环境即可在 1 分钟内完成安装：

```bash
# 1. 克隆代码仓库
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint/xiao

# 2. 本地可编辑模式安装（自动注册 pacer 命令）
pip install -e .
```

安装完成后，在终端验证是否成功安装：
```bash
pacer --version
```
*(屏幕将输出 `pacer 0.1.2` 及运行环境)*

---

### 🔍 2. 零配置与自动嗅探 (无需繁琐配置)

Pacer 内置了强大的**本地与云端 AI 自动嗅探器**。您甚至不需要立即配置复杂的 Key，Pacer 会自动寻找您电脑上已有的工具：

- **桌面环境**：自动识别已安装的 VS Code、Cursor、Claude Desktop、WorkBuddy。
- **CLI 命令行**：自动识别 `codex`、`claude`、`gemini`、`gh`、`git` 等。
- **本地模型服务端点**：自动检测 `127.0.0.1:11434` (Ollama)、`127.0.0.1:1234` (LM Studio)、`127.0.0.1:8000` (WorkBuddy)。

#### 查看当前电脑嗅探到的模型状态：
```bash
pacer backends
```

---

### 🔑 3. 配置您的云端模型渠道 (URL + Key)

若您希望享受 **3 秒极速出码** 的体验，推荐配置 xAI Grok (通过 Sub2API 高速网关或 xAI 官方) 或其他云端 Key。

在您的用户家目录下创建或编辑 `~/.pacer/credentials.json`：
```json
{
  "grok_api_key": "您的_Sub2API_或_xAI_Key",
  "sub2api_base": "https://direct.aiboxz.com/v1",
  "grok_model": "grok-4.7-latest"
}
```

*同时支持在系统环境变量中直接设置：*
- `OPENAI_API_KEY`: 自动激活 OpenAI 官方通道
- `GEMINI_API_KEY`: 自动激活 Google Gemini 免费大上下文通道
- `ANTHROPIC_API_KEY`: 自动激活 Claude API 通道
- `DEEPSEEK_API_KEY`: 自动激活 DeepSeek 官方通道

---

### 🚀 4. 核心工作模式与常用命令

#### 4.1 📊 启动现代化中英双语控制面板
想要直观看到自己省下了多少 Token、多少等待时间、嗅探到了哪些工具？直接启动：
```bash
pacer dashboard
```
> 控制台将自动在浏览器中打开 `http://127.0.0.1:8765`。
> - 查看 Token 节省率（95%+）、累计节省的等待分钟数与预估美元。
> - 审查五大核心技术支柱的即时状态。
> - 切换中英双语，浏览执行留痕时间线。

#### 4.2 ⚡ 极速闭环开发 (Pacer Auto)
让 Pacer 自动为您检索当前工程符号、调度最优模型生成原子补丁，并运行单元测试验证：
```bash
# 在您的任何工程根目录下执行
pacer auto "优化用户登录模块的密码强度校验逻辑，并运行 pytest 单测"
```

#### 4.3 🧠 查看多角色 DAG 规划 (不实际执行)
```bash
pacer plan "将数据库连接池重构为异步连接"
```

#### 4.4 📜 跨窗口持久化留痕审查
所有通过 Pacer 执行的改动都会被结构化记录到 `~/.pacer/traces.jsonl`：
```bash
pacer trace --limit 10
```

---

### 🔌 5. 无感集成 Claude Code (极力推荐)

如果您习惯使用 Claude Code 作为主对话界面，只需将以下协议加入到您的全局 `~/.claude/CLAUDE.md` 文件中，Claude Code 将会在接到编码/修 Bug 任务时，**完全无感地自动委托给 Pacer**：

```markdown
# Pacer Multi-Model Accelerated Coding Protocol (Global)

When the user asks you to write code, modify files, fix a bug, or run/fix tests:
- DO NOT read multiple large files into the conversation context.
- DO NOT write large multi-file diffs manually line-by-line.
- Immediately delegate to Pacer Auto using Bash:
  `pacer auto "<user_task>" --workspace-root "<current_directory>"`
- Display the Pacer execution log and summarize modified files and test results.
```

---

<a name="english-version"></a>
## English Version

Welcome to **Pacer (Speed-of-Light Multi-Model Accelerated Coding Protocol)**!
Pacer is an ultra-fast multi-model orchestration engine built for developers. With its Stateless Canvas and AST diff patching architecture, it saves **95%+ of prompt tokens** and delivers **3-second code patches** while automatically running verification tests.

### 📦 1. Installation

Works seamlessly on Windows, macOS, and Linux:

```bash
# 1. Clone the repository
git clone https://github.com/Xiaoyu155/Checkpoint.git
cd Checkpoint/xiao

# 2. Install locally in editable mode
pip install -e .
```

Verify the installation:
```bash
pacer --version
```

---

### 🔍 2. Zero-Config Local AI Tool Sniffing

Pacer includes an automated **AI Channel & Tool Sniffer** that runs out of the box:
- **Desktop Apps**: Auto-detects Cursor, VS Code, Claude Desktop, WorkBuddy.
- **CLI Tools**: Auto-detects `codex`, `claude`, `gemini`, `gh`, `git`, `ollama`.
- **Local LLM Endpoints**: Auto-probes `127.0.0.1:11434` (Ollama), `127.0.0.1:1234` (LM Studio), `127.0.0.1:8000` (WorkBuddy).

Inspect all detected engines:
```bash
pacer backends
```

---

### 🔑 3. Cloud Provider Configuration (URL + Key)

To unlock 3-second streaming generation, configure xAI Grok (via Sub2API or xAI directly) in `~/.pacer/credentials.json`:
```json
{
  "grok_api_key": "your_api_key_here",
  "sub2api_base": "https://direct.aiboxz.com/v1",
  "grok_model": "grok-4.7-latest"
}
```

Standard environment variables are also supported (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`).

---

### 🚀 4. Usage & Commands

#### 📊 4.1 Launch Web Dashboard & Control Center
```bash
pacer dashboard
```
Opens `http://127.0.0.1:8765` featuring:
- Real-time token & wait-time savings counters.
- Five Core Pillars interactive breakdown.
- Auto-sniffer channel discovery.
- Persistent cross-session trace ledger.
- Bilingual toggle (English / 简体中文).

#### ⚡ 4.2 Autonomous Coding (Auto Mode)
```bash
pacer auto "Fix edge case in payment calculation and run pytest"
```

#### 📜 4.3 Audit Traces
```bash
pacer trace --limit 10
```

---

### 🛡️ Five Core Pillars

1. **Local AST Symbol Indexer**: Extracts class/method signatures in `< 20ms`.
2. **Stateless Canvas**: Keeps single-round prompts `<= 1,500 tokens` (95%+ savings).
3. **Multi-Model Failover Router**: Grok 4.7 (3s) → Codex Plus → Gemini 3.8 Flash.
4. **Atomic SEARCH/REPLACE Patcher**: Precision Aider-style diffs with auto-rollback.
5. **Tamper Guard & Verification Gate**: Automatically protects tests from being modified.
