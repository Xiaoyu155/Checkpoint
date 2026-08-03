from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .goal_intake import intake_to_markdown, refine_goal, resolve_cheap_backend
from .workbench_app import validate_launch


@dataclass(frozen=True)
class WorkbenchEntryRoundSpec:
    goal: str
    test_command: str
    agent: str = "codex"
    answer: str = ""


@dataclass
class WorkbenchEntryRoundResult:
    round_index: int
    spec: WorkbenchEntryRoundSpec
    validation: dict[str, Any]
    intake: dict[str, Any]
    intake_markdown: str

    @property
    def valid(self) -> bool:
        return bool(self.validation.get("ok"))


@dataclass
class WorkbenchEntryAuditReport:
    generated_at: str
    project_dir: str
    workspace_root: str
    intake_backend: dict[str, Any] | None
    rounds: list[WorkbenchEntryRoundResult]

    def summary(self) -> dict[str, Any]:
        warning_rounds = sum(1 for item in self.rounds if item.validation.get("warning"))
        clear_rounds = sum(1 for item in self.rounds if item.intake.get("already_clear"))
        model_rounds = sum(1 for item in self.rounds if item.intake.get("source") == "model")
        deterministic_rounds = sum(1 for item in self.rounds if item.intake.get("source") == "deterministic")
        clarifying_total = sum(len(item.intake.get("clarifying_questions") or []) for item in self.rounds)
        return {
            "round_count": len(self.rounds),
            "warning_rounds": warning_rounds,
            "clear_rounds": clear_rounds,
            "model_rounds": model_rounds,
            "deterministic_rounds": deterministic_rounds,
            "clarifying_question_count": clarifying_total,
        }


DEFAULT_ENTRY_AUDIT_ROUNDS: list[WorkbenchEntryRoundSpec] = [
    WorkbenchEntryRoundSpec(
        goal="先帮我审查一下这个产品的入口链路，告诉我最先该修哪一块。",
        test_command="",
    ),
    WorkbenchEntryRoundSpec(
        goal="把桌面工作台改得更像正式产品首页，不要像调试窗。",
        test_command="python -m pytest tests/test_workbench_app.py -q",
    ),
    WorkbenchEntryRoundSpec(
        goal="我只说一句‘让新用户能直接开始’，你替我补成可执行目标。",
        test_command="flutter test",
        answer="重点先放在目标接待和启动入口，不要改业务逻辑。",
    ),
    WorkbenchEntryRoundSpec(
        goal="帮我检查自然语言目标会不会被当成没有验收的任务。",
        test_command="python -m pytest tests/test_goal_intake.py -q",
    ),
    WorkbenchEntryRoundSpec(
        goal="把‘验证命令’这块的提示做得更像给非工程师看的。",
        test_command="",
    ),
    WorkbenchEntryRoundSpec(
        goal="给一个只改入口文案、不改业务逻辑的小任务。",
        test_command="python -m pytest tests/test_dashboard.py -q",
    ),
    WorkbenchEntryRoundSpec(
        goal="让我在不懂命令的情况下也能发起一次检查。",
        test_command="npm test",
    ),
    WorkbenchEntryRoundSpec(
        goal="把状态摘要里的信息再压缩一点，但不要丢关键状态。",
        test_command="flutter analyze",
    ),
    WorkbenchEntryRoundSpec(
        goal="给我一个关于 Mimo 认证/fallback 的排查任务。",
        test_command="python -m pytest tests/test_workbench_app.py -q",
    ),
    WorkbenchEntryRoundSpec(
        goal="最后做一次冒烟级审查，看看这条入口链路还能不能稳定发任务。",
        test_command="python -m visual_agent.cli codex-check --workspace-root .agent-workspace --repo-root . --run-profile supervised --format json",
    ),
]


def default_audit_output_path(*, workspace_root: str | Path, project_dir: str | Path) -> Path:
    workspace_path = Path(workspace_root).expanduser().resolve()
    project_name = Path(project_dir).expanduser().resolve().name or "project"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return workspace_path / "reports" / f"workbench_entry_audit_{project_name}_{stamp}.md"


def run_workbench_entry_audit(
    *,
    project_dir: str | Path,
    workspace_root: str | Path,
    rounds: list[WorkbenchEntryRoundSpec] | None = None,
    enable_model: bool = True,
    backend_order: tuple[str, ...] = ("mimo", "deepseek"),
) -> WorkbenchEntryAuditReport:
    project_path = Path(project_dir).expanduser().resolve()
    workspace_path = Path(workspace_root).expanduser().resolve()
    round_specs = list(rounds or DEFAULT_ENTRY_AUDIT_ROUNDS)
    backend = resolve_cheap_backend(backend_order) if enable_model else None
    results: list[WorkbenchEntryRoundResult] = []
    for idx, spec in enumerate(round_specs, start=1):
        goal = str(spec.goal or "").strip()
        test_command = str(spec.test_command or "").strip()
        answer = str(spec.answer or "").strip()
        validation = validate_launch(project_dir=str(project_path), goal=goal, test_command=test_command)
        intake = refine_goal(
            goal,
            answers=[answer] if answer else None,
            enable_model=enable_model,
            model_id=str(backend["model_id"]) if backend else None,
            api_key=str(backend["api_key"]) if backend else None,
            base_url=str(backend["base_url"]) if backend else None,
            endpoint=str(backend["endpoint"]) if backend else None,
            max_tokens=int(backend["max_tokens"]) if backend else 600,
        )
        result = WorkbenchEntryRoundResult(
            round_index=idx,
            spec=spec,
            validation=validation,
            intake=intake,
            intake_markdown=intake_to_markdown(intake),
        )
        results.append(result)
    return WorkbenchEntryAuditReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        project_dir=str(project_path),
        workspace_root=str(workspace_path),
        intake_backend=backend,
        rounds=results,
    )


def audit_report_to_dict(report: WorkbenchEntryAuditReport) -> dict[str, Any]:
    return {
        "generated_at": report.generated_at,
        "project_dir": report.project_dir,
        "workspace_root": report.workspace_root,
        "intake_backend": report.intake_backend,
        "summary": report.summary(),
        "rounds": [
            {
                "round_index": item.round_index,
                "spec": asdict(item.spec),
                "validation": item.validation,
                "intake": item.intake,
                "intake_markdown": item.intake_markdown,
            }
            for item in report.rounds
        ],
    }


def audit_report_to_markdown(report: WorkbenchEntryAuditReport) -> str:
    lines = [
        "# Workbench 入口审查文档",
        "",
        f"- 生成时间: `{report.generated_at}`",
        f"- 项目目录: `{report.project_dir}`",
        f"- 工作区: `{report.workspace_root}`",
    ]
    if report.intake_backend:
        lines.append(f"- 入口模型后端: `{report.intake_backend.get('model_id')}`")
    lines.extend([
        "",
        "## 审查摘要",
        "",
    ])
    summary = report.summary()
    lines.append(f"- 轮数: {summary['round_count']}")
    lines.append(f"- 有警告的轮次: {summary['warning_rounds']}")
    lines.append(f"- 目标已足够清晰的轮次: {summary['clear_rounds']}")
    lines.append(f"- 模型输出轮次: {summary['model_rounds']}")
    lines.append(f"- 本地规则轮次: {summary['deterministic_rounds']}")
    lines.append(f"- 澄清问题总数: {summary['clarifying_question_count']}")
    lines.extend(["", "## 轮次明细", ""])
    for item in report.rounds:
        lines.append(f"### Round {item.round_index}")
        lines.append("")
        lines.append(f"- 输入目标: {item.spec.goal}")
        lines.append(f"- 输入验收命令: {item.spec.test_command or '（空）'}")
        if item.spec.agent:
            lines.append(f"- 选择 agent: {item.spec.agent}")
        if item.spec.answer:
            lines.append(f"- 追问补充: {item.spec.answer}")
        lines.append(f"- 入口校验: {'通过' if item.validation.get('ok') else '失败'}")
        if item.validation.get("warning"):
            lines.append(f"- 校验警告: {item.validation['warning']}")
        if item.validation.get("error"):
            lines.append(f"- 校验错误: {item.validation['error']}")
        lines.append(f"- intake 来源: {item.intake.get('source')}")
        lines.append(f"- intake 已清晰: {'是' if item.intake.get('already_clear') else '否'}")
        qs = item.intake.get("clarifying_questions") or []
        if qs:
            lines.append("- 澄清问题:")
            lines.extend(f"  - {q}" for q in qs)
        suggested = str(item.intake.get("suggested_goal") or "").strip()
        if suggested:
            lines.append(f"- 建议改写: {suggested}")
        hint = str(item.intake.get("acceptance_hint") or "").strip()
        if hint:
            lines.append(f"- 建议验收: {hint}")
        if item.intake.get("model_error"):
            lines.append(f"- 模型错误: {item.intake['model_error']}")
        lines.extend(["", "```text", item.intake_markdown, "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def write_audit_report(report: WorkbenchEntryAuditReport, output: str | Path) -> Path:
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(audit_report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        path.write_text(audit_report_to_markdown(report), encoding="utf-8")
    return path


def handle_workbench_audit_command(args: Any) -> int:
    raw_backend_order = str(getattr(args, "backend_order", "") or "")
    report = run_workbench_entry_audit(
        project_dir=args.project_dir,
        workspace_root=args.workspace_root,
        enable_model=not args.no_model,
        backend_order=tuple(str(item).strip() for item in raw_backend_order.split(",") if str(item).strip()) or ("mimo", "deepseek"),
    )
    output_path = Path(args.output).expanduser() if args.output else default_audit_output_path(
        workspace_root=args.workspace_root,
        project_dir=args.project_dir,
    )
    output_path = write_audit_report(report, output_path)
    payload = audit_report_to_dict(report)
    payload["output_path"] = str(output_path)
    payload["status"] = "success"
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(audit_report_to_markdown(report))
        print(f"\n审查文档已写入: {output_path}")
    return 0
