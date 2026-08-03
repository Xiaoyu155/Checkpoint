# Pacer 最新核心功能审计与 YOLO 托管报告

日期：2026-07-29

## 结论

本轮最新改造已经补齐 Claude Code 真 YOLO 启动命令，并把权限模式从交互入口贯通到 Pacer 托管 worker。

现在可用命令：

```powershell
pacer yolo
pacer claude-yolo
pacer cc-yolo
pacer host yolo --goal "你的开发任务" --execute
```

旧写法也已兼容：

```powershell
pacer claude --dangerously-skip-permissions
```

Pacer 会把它规整为 Claude Code 当前明确支持的权限参数：

```powershell
claude --permission-mode bypassPermissions
```

## 本轮修复内容

1. 交互 YOLO 入口

新增 `pacer yolo`、`pacer claude-yolo`、`pacer cc-yolo`。这些入口会清理冲突权限参数，并强制追加：

```powershell
--permission-mode bypassPermissions
```

2. 旧参数兼容

`pacer claude --dangerously-skip-permissions` 不再原样透传旧 flag，而是自动转换成：

```powershell
--permission-mode bypassPermissions
```

这避免 Claude Code 新版本里旧 flag 表现不稳定，导致仍然弹权限确认。

3. 托管开发 YOLO 模式

新增 `host yolo` 模式。默认 agent 为 `claude-code`，并写入：

```json
{
  "permission_mode": "bypassPermissions",
  "tool_permissions": "default"
}
```

这意味着权限意图不只存在于当前 CLI 进程，而是会进入 mission/background worker 调度链。

4. Claude headless worker 权限覆盖

`chief_dispatch` 会读取 `execution_policy.permission_mode`。当模式是 `yolo/bypass/danger` 时，最终 Claude worker argv 会使用：

```powershell
--permission-mode bypassPermissions
```

并且不再追加窄化的 `--allowedTools Bash(...)` 白名单，避免托管开发时普通开发命令被拦截。

5. Agent profile 更新

`claude-code.yaml` 中 bypass profile 从旧写法：

```powershell
--dangerously-skip-permissions
```

更新为：

```powershell
--permission-mode bypassPermissions
```

## 涉及文件

- `src/visual_agent/cli.py`
- `src/visual_agent/cli_chief.py`
- `src/visual_agent/pacer_host.py`
- `src/visual_agent/chief_dispatch.py`
- `src/visual_agent/agent_profiles/claude-code.yaml`
- `tests/test_cli.py`
- `tests/test_chief_dispatch.py`
- `tests/test_pacer_host.py`

## 验证记录

已通过 focused regression：

```powershell
python -m pytest tests/test_agent_capabilities.py tests/test_cli.py tests/test_chief_dispatch.py tests/test_pacer_host.py tests/test_chief_background.py tests/test_chief_run.py -q
```

结果：

```text
272 passed
```

真实 CLI preview 验证：

```powershell
pacer host yolo --goal "noop smoke" --format json
```

确认输出包含：

```json
{
  "mode": "yolo",
  "agent": "claude-code",
  "execution_policy": {
    "permission_mode": "bypassPermissions",
    "tool_permissions": "default"
  }
}
```

## 当前核心功能状态

Routing：当前可用，模型/agent 路由已能在 Claude Code 托管任务中选择合适模型。更大的缺口是 Pacer 还没有完全自动决定“Codex 还是 Claude Code 还是 Gemini”作为开发通道。

Memory：当前可用，但从用户反馈看，长窗口/下午开发记忆丢失仍是严重体验问题。需要继续强化任务恢复、空消息过滤、上下文摘要和 mission journey 的连接。

Managed：当前可用，但这是 Pacer 最需要继续打磨的核心。YOLO 权限链已补上，接下来要重点验证长时间托管、自动 resume、worker 卡死识别、后台日志和用户可读进度。

Acceptance：当前可用，focused tests 和真实 preview 已验证。但全仓 release matrix 和当前 Dogfood OIDC 证据还没有完整跑通。

Dogfood：历史 GitHub Dogfood 有成功记录，但当前源码工作区没有新的完整 Dogfood release 证据。当前只能说“本轮 YOLO 改造已本地验证”，不能宣称 Pacer 五核心功能已当前全量闭环。

## “没有用户消息”的现象

你看到 Code 输出类似：

```text
Read 1 file, ran 1 shell command
让我检查一下更近期的 Pacer 开发记录。
Thought for 9s
（没有用户消息，忽略系统提醒。）
```

这通常不是用户真的发了空消息，而是长会话里的恢复事件、工具执行后的空输入、上下文切换、系统提醒或自动续跑事件被模型看到。问题在于 worker 没有把它稳定识别成“非用户任务事件”。

Pacer 后续应补：

1. 空消息过滤：空内容、纯系统标签、纯恢复事件不能进入 task intake。
2. 任务粘性：没有新用户任务时，worker 必须继续当前 mission，而不是重新解释上下文。
3. 事件分层：区分 user message、system reminder、tool event、resume event。
4. 交付报告约束：final report 只能基于最新真实用户任务和 mission state。

## Code 审核重点

1. `pacer yolo` 默认进入 Claude Code 是否符合产品预期。
2. `host yolo` 默认 `allow_test_edits=True`、`merge=True` 是否太激进。
3. `execution_policy` 是否在所有 background/resume/repair 路径都没有丢失。
4. Claude headless 下 `--tools default` 是否会引入过宽工具面。
5. 旧版 Claude Code 是否兼容 `--permission-mode bypassPermissions`。本机 `claude --help` 已显示支持，但跨机器仍需 doctor 检测。

## 下一步建议

1. 增加 `pacer doctor claude-yolo` 或 `pacer host doctor --mode yolo`，真实检测当前 Claude CLI 是否支持 `bypassPermissions`。
2. 增加空消息过滤和 resume event 分类，解决“没有用户消息”导致的误判。
3. 做一次小型真实 Dogfood：用 `pacer host yolo` 跑一个一次性小改动，要求 mission journey 证明 Routing/Memory/Managed/Acceptance/Dogfood 连起来。
4. 再做长时间托管压测，重点观察卡死、自动恢复、上下文摘要和任务记忆丢失。
