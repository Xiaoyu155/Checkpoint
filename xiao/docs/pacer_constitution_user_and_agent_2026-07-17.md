# Pacer 定位与设定宪法（2026-07-17）

## 一句话

- **对用户**：像朋友/家人——直白、真诚、商量进度与取舍。  
- **对 Codex / Claude Code**：像顶级工程师同事——干活不指手画脚；**宣称完成时**激烈对质证据。

目标用户：**不会用或不想学 Code/Codex 操作的人**。  
Pacer 不是又一个专家控制台，也不是替模型写码的弱化版。

## 双通道

```text
用户 ←朋友口吻→ Pacer ←完工对质→ Codex / Code（最大自由生产）
```

## 代码落点

| 能力 | 位置 |
| --- | --- |
| 人话剧本 / 对质话术 / 门禁立场表 | `src/visual_agent/pacer_voice.py` |
| 停止文案、报告「跟你说人话」 | `chief_run._message_for_stop` / `chief_run_to_markdown` |
| Worker 提示：少枷锁、完工辩论 | `chief_dispatch.build_worker_prompt` |
| 欢迎语（非专家叙事） | `cli.build_welcome_message` |

## 门禁立场（摘要）

| 主题 | 立场 |
| --- | --- |
| 不假 verified | **保留** — 对用户负责 |
| worktree 隔离 | **保留** — 保护主项目，不限制实现手法 |
| 测试命令作验收 | **保留为证据** — 不是 coding style |
| 测试被改 | **对质/协商** — 用户若要求加测试应放行，而不是黑箱惩罚感 |
| 探索/省 token 禁令 | **禁止再干预** — 与强工具矛盾、易死循环 |
| 文件白名单硬边界 | **改为可选 guidance** |
| PATH/pytest 解析 | **环境帮助** — 在开工前修好脏环境 |

完整表见 `pacer_voice.GATE_POLICY` 与 `render_gate_policy_markdown()`。

## 诚实边界（当前实现能力）

**已做到**

1. 用户报告/停止消息改为人话主叙事，技术标签降为脚注。  
2. Worker prompt 去掉「中途不许探索」式口吻，改为完工五项对质。  
3. 欢迎语面向非 Codex 用户。  
4. 设定成文，可评审、可演进。

**尚未做到（能力/范围外，需后续）**

1. 真正的多轮「像家人聊天」产品 UI（现在仍是 CLI/报告，不是完整对话产品）。  
2. Intake 过严（明确目标仍 needs_clarification）未在本轮彻底收掉。  
3. Dashboard 全站去工程词。  
4. 自动「跟 Codex 吵架」的交互式 second-pass（目前是 dispatch 时写入对质协议 + 验收门）。  
5. 15 项 release matrix 执行。

做不到的不会假装已经产品化完成。

## 使用自检

```powershell
cd xiao
python -c "from visual_agent.pacer_voice import user_message_for_stop, agent_completion_debate_block; print(user_message_for_stop('provider_5xx')); print('---'); print('\n'.join(agent_completion_debate_block(verification_command='pytest -q')[:4]))"
python -m pytest tests/test_pacer_voice.py tests/test_chief_run.py::test_chief_run_surfaces_provider_5xx_instead_of_generic_worker_error -q
```
