"""Run comprehensive Grok code review on Pacer core files."""

import os
from visual_agent.grok_bridge import invoke_grok

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
files = [
    "src/visual_agent/mcp_server.py",
    "src/visual_agent/subagent_orchestrator.py",
    "src/visual_agent/canvas_manager.py",
    "src/visual_agent/local_indexer.py",
    "src/visual_agent/diff_patcher.py",
    "examples/delta_action/core.py",
    "examples/delta_action/test_delta.py",
]

dumps = []
for rel in files:
    full = os.path.join(repo_root, rel)
    if os.path.isfile(full):
        with open(full, "r", encoding="utf-8", errors="ignore") as fp:
            dumps.append(f"### File: {rel}\n```python\n{fp.read()}\n```")

code_text = "\n\n".join(dumps)

prompt = f"""请你作为顶级代码审查和架构专家，对 Pacer 本次重构的核心模块以及 Delta Action 压力测试模块进行专业源码审查。

以下是相关模块的真实完整源码：

{code_text}

同时审查以下 Codex 审计提出的 4 个修改点：
1. workbuddy_bridge.py: psutil 未在依赖中声明，需要提供容错或补充依赖。
2. model_router.py: 否定词匹配在没有标点符号时可能误删后续正面诉求（如 'Do not refactor but secure payment'），需要以转折词截断。
3. game_2048/core.py: 默认初始化应自动生成起始方块。
4. cli.py: 写入 ~/.claude/commands/pacer.md 时如果文件已存在应保护或备份。

请给出你的深度源码审查报告（使用清晰结构化 Markdown）：
1. 架构与安全性真实评分（1-10分）
2. 对 MCP Server（路径安全、协议一致性、can_trust 凭证防伪造）、Subagent Orchestrator（DAG 编排、自我修复死循环防护、补丁原子回滚）与 Delta Action（干员状态机、穿甲计算、保险箱资产持久化）的源码级分析
3. 对 Codex 4 个发现的裁决与修复建议
4. 总体结论与上线建议
"""

print(f"Sending real source code ({len(code_text)} chars) to Grok...")
res = invoke_grok(prompt, timeout=180, max_tokens=4000)
print(f"Grok audit completed: success={res.success}, elapsed={res.elapsed_seconds:.2f}s")

if res.success:
    out_path = os.path.join(repo_root, "docs/grok_code_review_report.md")
    with open(out_path, "w", encoding="utf-8") as out_f:
        out_f.write(res.response)
    print(f"Review saved to: {out_path}")
    print("\n=== Grok Source Review Result ===\n")
    print(res.response)
else:
    print("Error:", res.error)
