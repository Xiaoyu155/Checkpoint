from __future__ import annotations

from pathlib import Path
from typing import Any

from .workflow import parse_workflow_file


PUBLIC_BENCHMARKS: tuple[dict[str, Any], ...] = (
    {
        "id": "browser_use_browser_tasks",
        "name": "browser-use browser tasks",
        "source": "https://github.com/browser-use/browser-use",
        "category": "ai_browser_automation",
        "status": "reference",
        "suggested_visual_agent_scenarios": [
            "multi-step login-style form fill with observation after each action",
            "task retry after page content changes",
            "extract visible page state into a structured report",
        ],
        "requires_live_account": False,
    },
    {
        "id": "stagehand_act_extract",
        "name": "Stagehand act/extract style flows",
        "source": "https://github.com/browserbase/stagehand",
        "category": "ai_browser_automation",
        "status": "reference",
        "suggested_visual_agent_scenarios": [
            "combine deterministic Playwright selectors with AI fallback suggestions",
            "cache successful element resolutions for later deterministic runs",
            "compare natural-language act steps against explicit workflow YAML",
        ],
        "requires_live_account": False,
    },
    {
        "id": "skyvern_workflows",
        "name": "Skyvern browser workflows",
        "source": "https://github.com/Skyvern-AI/skyvern",
        "category": "workflow_automation",
        "status": "reference",
        "suggested_visual_agent_scenarios": [
            "long-running browser workflow with durable artifacts",
            "workflow rerun after failed extraction or assertion",
            "business process verification with screenshots and logs",
        ],
        "requires_live_account": False,
    },
    {
        "id": "healenium_locator_repair",
        "name": "Healenium locator self-healing",
        "source": "https://github.com/healenium/healenium",
        "category": "self_healing_tests",
        "status": "reference",
        "suggested_visual_agent_scenarios": [
            "selector drift detection after DOM changes",
            "candidate selector scoring and cached repair",
            "report old selector, new candidate, confidence, and fallback path",
        ],
        "requires_live_account": False,
    },
)


def list_public_benchmarks(*, category: str | None = None) -> dict[str, Any]:
    items = [dict(item) for item in PUBLIC_BENCHMARKS if category is None or item["category"] == category]
    return {
        "schema_version": 1,
        "status": "ready",
        "benchmark_count": len(items),
        "benchmarks": items,
        "message": "These are public reference benchmarks. They are not executed automatically yet.",
    }


def build_benchmark_plan(*, category: str | None = None, benchmark_id: str | None = None) -> dict[str, Any]:
    benchmarks = [
        dict(item)
        for item in PUBLIC_BENCHMARKS
        if (category is None or item["category"] == category) and (benchmark_id is None or item["id"] == benchmark_id)
    ]
    scenarios = []
    for item in benchmarks:
        scenarios.extend(benchmark_scenarios(item))
    return {
        "schema_version": 1,
        "status": "ready" if benchmarks else "not_found",
        "benchmark_count": len(benchmarks),
        "scenario_count": len(scenarios),
        "category": category,
        "benchmark_id": benchmark_id,
        "benchmarks": benchmarks,
        "scenarios": scenarios,
        "acceptance": {
            "generate": "Create or generate each scenario workflow in the workspace workflows/ directory.",
            "validate": "visual-agent workspace-validate --root .agent-workspace",
            "run": "visual-agent verify --workspace-root .agent-workspace --tags benchmark --run-profile dry-run",
            "repair": "For failed benchmark runs, use auto-repair --dry-run before applying any patch.",
        },
    }


def build_benchmark_workflow_draft(
    *,
    scenario_id: str,
    workspace_root: str | Path,
    output_path: str | Path | None = None,
    dry_run: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    scenario = find_benchmark_scenario(scenario_id)
    if scenario is None:
        return {"schema_version": 1, "status": "not_found", "scenario_id": scenario_id}
    yaml_text = benchmark_workflow_yaml(scenario)
    workspace = Path(workspace_root).resolve()
    saved_to: str | None = None
    if not dry_run:
        path = Path(output_path).resolve() if output_path else workspace / "workflows" / f"{scenario['workflow_name']}.yaml"
        try:
            path.relative_to(workspace)
        except ValueError:
            return {"schema_version": 1, "status": "error", "message": f"output path escapes workspace: {path}", "scenario": scenario, "yaml": yaml_text}
        if path.exists() and not overwrite:
            return {"schema_version": 1, "status": "exists", "message": f"workflow already exists: {path}", "scenario": scenario, "yaml": yaml_text}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml_text.rstrip() + "\n", encoding="utf-8")
        parse_workflow_file(path)
        saved_to = str(path)
    return {
        "schema_version": 1,
        "status": "success",
        "scenario": scenario,
        "workflow_name": scenario["workflow_name"],
        "yaml": yaml_text,
        "saved_to": saved_to,
        "message": f"Saved to: {saved_to}" if saved_to else "Generated benchmark workflow draft.",
    }


def find_benchmark_scenario(scenario_id: str) -> dict[str, Any] | None:
    for benchmark in PUBLIC_BENCHMARKS:
        for scenario in benchmark_scenarios(dict(benchmark)):
            if scenario["id"] == scenario_id or scenario["workflow_name"] == scenario_id:
                return scenario
    return None


def benchmark_workflow_yaml(scenario: dict[str, Any]) -> str:
    name = str(scenario["workflow_name"])
    description = str(scenario["description"])
    tags = [str(item) for item in scenario.get("tags", ["benchmark"])]
    capabilities = ", ".join(str(item) for item in scenario.get("capabilities", []))
    acceptance = str(scenario.get("acceptance") or "")
    fixture_path = "examples/fixtures/login_page_observation.json"
    expected_text = "客户管理系统" if "selector_repair" in scenario.get("capabilities", []) else "登录"
    return (
        'schema_version: 1\n'
        'min_runtime_version: "0.1.0"\n'
        f"name: {name}\n"
        "version: 1\n"
        f"description: {yaml_quote(description)}\n"
        "visibility: private\n"
        'author: ""\n'
        'license: ""\n'
        "tags:\n"
        + "".join(f"  - {yaml_quote(tag)}\n" for tag in tags)
        + "steps:\n"
        + "  - id: observe_reference\n"
        + "    action: observe_fixture\n"
        + f"    path: {fixture_path}\n"
        + "  - id: assert_reference_text\n"
        + "    action: assert_text\n"
        + f"    text: {yaml_quote(expected_text)}\n"
        + "  - id: assert_benchmark_contract\n"
        + "    action: assert_product_contract\n"
        + "    checks:\n"
        + f"      - {yaml_quote('capabilities: ' + capabilities)}\n"
        + f"      - {yaml_quote('acceptance: ' + acceptance)}\n"
    )


def benchmark_draft_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Benchmark Workflow Draft",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Workflow: `{payload.get('workflow_name') or ''}`",
    ]
    if payload.get("saved_to"):
        lines.append(f"- Saved to: `{payload.get('saved_to')}`")
    if payload.get("message"):
        lines.append(f"- Message: {payload.get('message')}")
    if payload.get("yaml"):
        lines.extend(["", "```yaml", str(payload.get("yaml")).rstrip(), "```"])
    return "\n".join(lines).rstrip() + "\n"


def benchmark_plan_to_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Benchmark Plan",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Benchmarks: {payload.get('benchmark_count')}",
        f"- Scenarios: {payload.get('scenario_count')}",
        "",
    ]
    scenarios = payload.get("scenarios") if isinstance(payload.get("scenarios"), list) else []
    if not scenarios:
        lines.append("No matching benchmark scenarios.")
        return "\n".join(lines).rstrip() + "\n"
    lines.append("| id | workflow | capabilities | acceptance |")
    lines.append("| --- | --- | --- | --- |")
    for scenario in scenarios:
        if not isinstance(scenario, dict):
            continue
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(scenario.get("id")),
                    markdown_cell(scenario.get("workflow_name")),
                    markdown_cell(", ".join(str(item) for item in scenario.get("capabilities", []))),
                    markdown_cell(scenario.get("acceptance")),
                ]
            )
            + " |"
        )
    acceptance = payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else {}
    if acceptance:
        lines.extend(["", "## Acceptance Commands", ""])
        for key, value in acceptance.items():
            lines.append(f"- `{key}`: {value}")
    return "\n".join(lines).rstrip() + "\n"


def benchmark_scenarios(benchmark: dict[str, Any]) -> list[dict[str, Any]]:
    benchmark_id = str(benchmark["id"])
    scenarios = list(benchmark.get("suggested_visual_agent_scenarios") or [])
    result = []
    for index, description in enumerate(scenarios, start=1):
        result.append(
            {
                "id": f"{benchmark_id}_{index}",
                "benchmark_id": benchmark_id,
                "benchmark_name": benchmark.get("name"),
                "workflow_name": f"benchmark_{benchmark_id}_{index}",
                "tags": ["benchmark", str(benchmark.get("category") or "reference")],
                "description": str(description),
                "capabilities": benchmark_capabilities(benchmark_id, str(description)),
                "requires_live_account": bool(benchmark.get("requires_live_account", False)),
                "recommended_run_profile": "dry-run",
                "acceptance": benchmark_acceptance(benchmark_id, str(description)),
            }
        )
    return result


def benchmark_capabilities(benchmark_id: str, description: str) -> list[str]:
    text = f"{benchmark_id} {description}".lower()
    capabilities = ["observe", "workflow_run", "report"]
    if any(token in text for token in ("selector", "locator", "dom")):
        capabilities.extend(["dom_inspection", "selector_repair"])
    if any(token in text for token in ("retry", "rerun", "long-running")):
        capabilities.extend(["retry", "queue_or_rerun"])
    if any(token in text for token in ("extract", "structured", "business")):
        capabilities.extend(["structured_assertion", "artifact_report"])
    if any(token in text for token in ("repair", "self-healing", "changes")):
        capabilities.extend(["failure_diagnosis", "auto_repair_preview"])
    return sorted(set(capabilities))


def benchmark_acceptance(benchmark_id: str, description: str) -> str:
    text = f"{benchmark_id} {description}".lower()
    if "selector" in text or "locator" in text:
        return "A failed selector produces repair candidates with confidence and no automatic model patch application."
    if "extract" in text or "structured" in text:
        return "The workflow report contains a structured assertion or extracted state artifact."
    if "rerun" in text or "retry" in text:
        return "A failed or changed page state can be rerun with durable artifacts and a clear latest failure summary."
    return "The workflow runs in dry-run mode, records observations, and produces an AI-readable report."


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")[:160]


def yaml_quote(value: str) -> str:
    import json

    return json.dumps(str(value), ensure_ascii=False)
