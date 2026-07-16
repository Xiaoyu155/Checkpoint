from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .dogfood_evidence import load_dogfood_evidence
from .dogfood_policy import load_dogfood_policy
from .dogfood_provider_check import (
    dogfood_provider_receipt_to_markdown,
    run_dogfood_provider_check,
)
from .dogfood_quality import DOGFOOD_TARGET_SCORE
from .models import to_jsonable
from .mcp_doctor import build_mcp_startup_doctor, mcp_startup_doctor_to_markdown
from .quality import (
    build_coding_agent_brief,
    build_install_check_plan,
    build_mcp_client_config,
    build_release_check_plan,
    coding_agent_brief_to_markdown,
    demo_workspace_check_to_markdown,
    install_check_plan_to_markdown,
    list_quality_gate_reports,
    load_quality_gate_index,
    mcp_client_config_to_markdown,
    mcp_smoke_check_to_markdown,
    quality_gate_index_to_markdown,
    quality_gate_reports_to_markdown,
    quality_gate_to_dict,
    quality_gate_to_junit_xml,
    release_check_plan_to_markdown,
    release_smoke_to_markdown,
    release_trial_to_markdown,
    run_demo_workspace_check,
    run_mcp_smoke_check,
    run_quality_gate,
    run_release_smoke,
    run_release_trial,
    write_quality_gate_step_summary,
)
from .run_profile import SAFE_RUN_PROFILE_CHOICES
from .release_gate import assess_release_manifest_file
from .release_evidence import run_release_evidence_bundle


QUALITY_COMMANDS = {
    "quality-gate",
    "quality-gate-reports",
    "quality-gate-index",
    "release-check",
    "release-smoke",
    "release-trial",
    "pacer-dogfood-check",
    "pacer-dogfood-provider-check",
    "pacer-dogfood-policy-check",
    "pacer-release-manifest-check",
    "pacer-release-check",
    "install-check",
    "mcp-doctor",
    "mcp-client-config",
    "coding-agent-brief",
    "mcp-smoke",
    "demo-workspace-check",
}


def add_quality_parsers(subparsers: argparse._SubParsersAction[Any]) -> None:
    quality_gate = subparsers.add_parser("quality-gate", help="Show or run smoke/local/CI quality gates.")
    quality_gate.add_argument("--profile", choices=["smoke", "local", "ci"], default="local", help="Quality profile. Default: local.")
    quality_gate.add_argument("--workspace-root", help="Optional workspace root for workspace regression tests.")
    quality_gate.add_argument("--run", action="store_true", help="Execute the quality gate. Default only prints the plan.")
    quality_gate.add_argument("--timeout-seconds", type=float, default=300.0, help="Timeout per step. Default: 300.")
    quality_gate.add_argument("--report-root", help="Optional report output directory.")
    quality_gate.add_argument("--ci", action="store_true", help="Emit JUnit XML for CI consumption instead of JSON.")
    quality_gate.add_argument("--junit-output", default=None, help="Optional JUnit XML output path when --ci is set.")
    quality_gate.add_argument(
        "--fail-on-risk-policy-error",
        action="store_true",
        help="Fail executed gates when workspace risk policy validation has errors.",
    )
    quality_gate.add_argument(
        "--fail-on-secret-leak",
        action="store_true",
        help="Fail gates when reports/runs/artifacts contain possible secret leaks.",
    )

    quality_reports = subparsers.add_parser("quality-gate-reports", help="List quality gate JSON reports.")
    quality_reports.add_argument("--workspace-root", help="Optional workspace root containing reports/quality_gates.")
    quality_reports.add_argument("--report-root", help="Optional quality gate report directory.")
    quality_reports.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    quality_reports.add_argument("--profile", choices=["smoke", "local", "ci"], help="Filter by quality gate profile.")
    quality_reports.add_argument("--status", choices=["planned", "success", "failed"], help="Filter by report status.")
    quality_reports.add_argument(
        "--strict-policy-failed",
        choices=["true", "false"],
        help="Filter by strict policy gate failure state.",
    )

    quality_index = subparsers.add_parser("quality-gate-index", help="Build or query the quality gate report index.")
    quality_index.add_argument("--workspace-root", help="Optional workspace root containing reports/quality_gates.")
    quality_index.add_argument("--report-root", help="Optional quality gate report directory.")
    quality_index.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")
    quality_index.add_argument("--rebuild", action="store_true", help="Rebuild index.json before reading.")
    quality_index.add_argument("--profile", choices=["smoke", "local", "ci"], help="Filter by quality gate profile.")
    quality_index.add_argument("--status", choices=["planned", "success", "failed"], help="Filter by report status.")
    quality_index.add_argument(
        "--strict-policy-failed",
        choices=["true", "false"],
        help="Filter by strict policy gate failure state.",
    )

    release_check = subparsers.add_parser("release-check", help="Print the release readiness check plan.")
    release_check.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to use in generated commands.")
    release_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    release_smoke = subparsers.add_parser("release-smoke", help="Run or print the product release smoke gate.")
    release_smoke.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to use for smoke checks.")
    release_smoke.add_argument("--run", action="store_true", help="Execute the smoke gate. Default only prints the plan.")
    release_smoke.add_argument("--skip-vscode", action="store_true", help="Skip VS Code extension npm tests.")
    release_smoke.add_argument("--timeout-seconds", type=float, default=300.0, help="Timeout per command. Default: 300.")
    release_smoke.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    release_trial = subparsers.add_parser("release-trial", help="Run the real trial validation bundle on a workspace.")
    release_trial.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root to initialize and validate.")
    release_trial.add_argument("--overwrite", action="store_true", help="Overwrite demo files before running.")
    release_trial.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="supervised", help="Demo/cloud run profile. Default: supervised.")
    release_trial.add_argument("--cloud-org", default="team-a", help="Org header used for local cloud execution.")
    release_trial.add_argument("--cloud-user", default="release-trial", help="User header used for local cloud execution.")
    release_trial.add_argument("--cloud-api-key", default="release-trial-key", help="Bearer token used for local cloud execution.")
    release_trial.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    dogfood_check = subparsers.add_parser(
        "pacer-dogfood-check",
        help="Verify canonical Pacer-on-Pacer evidence and every referenced artifact.",
    )
    dogfood_check.add_argument("--repo-root", default=".", help="Pacer repository root. Default: current directory.")
    dogfood_check.add_argument(
        "--artifact-root",
        action="append",
        default=[],
        help="Explicit trusted artifact root outside the repository. Repeat for multiple roots.",
    )
    dogfood_check.add_argument(
        "--attestation-key-id",
        default=None,
        help="Attestation key id. The secret is read only from PACER_DOGFOOD_ATTESTATION_KEY.",
    )
    dogfood_check.add_argument(
        "--github-repository",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub owner/repository whose artifact attestations must verify.",
    )
    dogfood_check.add_argument(
        "--signer-workflow",
        default=os.environ.get("PACER_DOGFOOD_SIGNER_WORKFLOW", ""),
        help="Expected GitHub workflow identity passed to gh attestation verify.",
    )
    dogfood_check.add_argument(
        "--require-github-provenance",
        action="store_true",
        help="Fail unless both the evidence file and candidate wheel have verified GitHub attestations.",
    )
    dogfood_check.add_argument(
        "--minimum-score",
        type=int,
        default=DOGFOOD_TARGET_SCORE,
        help="Required mechanical Dogfood quality score. Default: 95.",
    )
    dogfood_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    dogfood_policy = subparsers.add_parser(
        "pacer-dogfood-policy-check",
        help="Validate the canonical three-lane Dogfood policy and pinned GitHub references.",
    )
    dogfood_policy.add_argument("--repo-root", default=".", help="Pacer repository root.")
    dogfood_policy.add_argument("--format", choices=["json", "markdown"], default="json")

    dogfood_provider = subparsers.add_parser(
        "pacer-dogfood-provider-check",
        help="Verify a custom Codex Responses provider without exposing its endpoint or credential.",
    )
    dogfood_provider.add_argument("--provider-id", required=True, help="Codex custom provider id.")
    dogfood_provider.add_argument("--base-url", required=True, help="Provider endpoint; never emitted in the receipt.")
    dogfood_provider.add_argument("--model", required=True, help="Model id to probe.")
    dogfood_provider.add_argument(
        "--key-env",
        required=True,
        help="Environment variable containing the provider credential; raw keys are not accepted.",
    )
    dogfood_provider.add_argument(
        "--timeout-seconds",
        type=float,
        default=60.0,
        help="Probe timeout, greater than zero and at most 300 seconds. Default: 60.",
    )
    dogfood_provider.add_argument("--format", choices=["json", "markdown"], default="json")

    release_manifest_check = subparsers.add_parser(
        "pacer-release-manifest-check",
        help="Validate a digest-locked Pacer release matrix manifest.",
    )
    release_manifest_check.add_argument("--manifest", default=".pacer/release.json", help="Release manifest path.")
    release_manifest_check.add_argument("--expected-digest", required=True, help="Expected canonical SHA-256 digest.")
    release_manifest_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    release_bundle_check = subparsers.add_parser(
        "pacer-release-check",
        help="Evaluate an externally attested, digest-locked Pacer release evidence bundle.",
    )
    release_bundle_check.add_argument("--repo-root", default=".", help="Pacer repository root.")
    release_bundle_check.add_argument("--manifest", default=".pacer/release.json", help="Canonical release manifest.")
    release_bundle_check.add_argument("--expected-digest", required=True, help="Pre-locked release manifest SHA-256.")
    release_bundle_check.add_argument("--evidence-root", default=".", help="Trusted root containing the bundle and case results.")
    release_bundle_check.add_argument("--bundle", default=".pacer/release-evidence.json", help="Bundle path relative to evidence-root.")
    release_bundle_check.add_argument(
        "--artifact-root",
        action="append",
        default=[],
        help="Additional trusted Dogfood artifact root. Repeat for multiple roots.",
    )
    release_bundle_check.add_argument(
        "--attestation-key-id",
        default=None,
        help="Release bundle key id. The secret is read from PACER_RELEASE_ATTESTATION_KEY.",
    )
    release_bundle_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    install_check = subparsers.add_parser("install-check", help="Print the local install/dependency check plan.")
    install_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    mcp_client_config = subparsers.add_parser("mcp-client-config", help="Generate MCP client configuration for this checkout.")
    mcp_client_config.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root passed to the MCP server.")
    mcp_client_config.add_argument("--client", choices=["cursor", "claude-desktop", "vscode"], default="cursor", help="Client config shape to generate.")
    mcp_client_config.add_argument("--python", default=None, help="Python executable used by the MCP client. Default: current Python.")
    mcp_client_config.add_argument("--repo-root", default=".", help="Repository root used for cwd and PYTHONPATH.")
    mcp_client_config.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    mcp_doctor = subparsers.add_parser("mcp-doctor", help="Diagnose MCP startup paths, Python, PYTHONPATH, and package imports.")
    mcp_doctor.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root expected by MCP tools.")
    mcp_doctor.add_argument("--repo-root", default=".", help="Repository root used for cwd and PYTHONPATH.")
    mcp_doctor.add_argument("--python", default=None, help="Python executable used by the MCP client. Default: current Python.")
    mcp_doctor.add_argument("--client", default="codex", help="Client label for the diagnostic report.")
    mcp_doctor.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    coding_agent_brief = subparsers.add_parser("coding-agent-brief", help="Generate a Codex/Claude Code/Cursor/VS Code onboarding brief.")
    coding_agent_brief.add_argument("--workspace-root", default=".agent-workspace", help="Workspace root passed to the MCP server.")
    coding_agent_brief.add_argument("--repo-root", default=".", help="Repository root used for cwd and PYTHONPATH.")
    coding_agent_brief.add_argument("--client", choices=["codex", "claude-code", "cursor", "vscode"], default="codex", help="Coding agent target.")
    coding_agent_brief.add_argument("--python", default=None, help="Python executable used by the MCP client. Default: current Python.")
    coding_agent_brief.add_argument("--format", choices=["json", "markdown"], default="markdown", help="Output format. Default: markdown.")

    mcp_smoke = subparsers.add_parser("mcp-smoke", help="Run local MCP tool smoke checks through the in-process MCP adapter.")
    mcp_smoke.add_argument("--workspace-root", required=True, help="Workspace root containing demo workflows.")
    mcp_smoke.add_argument("--workflow", default="local_html_form_workflow", help="Workflow to validate and run through MCP.")
    mcp_smoke.add_argument("--inputs-file", default="demo_login.json", help="Workspace inputs file used for the dry-run.")
    mcp_smoke.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")

    demo_workspace_check = subparsers.add_parser("demo-workspace-check", help="Initialize and dry-run the local demo workspace.")
    demo_workspace_check.add_argument("--root", default=".agent-workspace", help="Workspace root to initialize/check.")
    demo_workspace_check.add_argument("--overwrite", action="store_true", help="Overwrite demo assets before checking.")
    demo_workspace_check.add_argument("--run-profile", choices=SAFE_RUN_PROFILE_CHOICES, default="dry-run", help="Execution profile. Use supervised for the browser demo path.")
    demo_workspace_check.add_argument("--format", choices=["json", "markdown"], default="json", help="Output format. Default: json.")


def handle_quality_command(args: Any, *, release_trial_runner: Any = None) -> int:
    if args.command == "quality-gate":
        result = run_quality_gate(
            args.profile,
            workspace_root=args.workspace_root,
            execute=args.run,
            timeout_seconds=args.timeout_seconds,
            report_root=args.report_root,
            fail_on_risk_policy_error=args.fail_on_risk_policy_error,
            fail_on_secret_leak=args.fail_on_secret_leak,
        )
        if args.ci:
            junit_xml = quality_gate_to_junit_xml(result)
            if args.junit_output:
                output_path = Path(args.junit_output).expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(junit_xml, encoding="utf-8")
            write_quality_gate_step_summary(result, junit_output=args.junit_output)
            print(junit_xml)
        else:
            print(json.dumps(quality_gate_to_dict(result), ensure_ascii=False, indent=2))
        return 0 if result.status in {"planned", "success"} else 1
    if args.command == "quality-gate-reports":
        reports = list_quality_gate_reports(
            report_root=args.report_root,
            workspace_root=args.workspace_root,
            profile=args.profile,
            status=args.status,
            strict_policy_failed=parse_optional_bool(args.strict_policy_failed),
        )
        if args.format == "markdown":
            print(quality_gate_reports_to_markdown(reports))
        else:
            print(json.dumps(to_jsonable(reports), ensure_ascii=False, indent=2))
        return 0
    if args.command == "quality-gate-index":
        index = load_quality_gate_index(
            report_root=args.report_root,
            workspace_root=args.workspace_root,
            rebuild=args.rebuild,
            profile=args.profile,
            status=args.status,
            strict_policy_failed=parse_optional_bool(args.strict_policy_failed),
        )
        if args.format == "markdown":
            print(quality_gate_index_to_markdown(index))
        else:
            print(json.dumps(to_jsonable(index), ensure_ascii=False, indent=2))
        return 0
    if args.command == "release-check":
        plan = build_release_check_plan(workspace_root=args.workspace_root)
        if args.format == "markdown":
            print(release_check_plan_to_markdown(plan))
        else:
            print(json.dumps(to_jsonable(plan), ensure_ascii=False, indent=2))
        return 0
    if args.command == "release-smoke":
        if args.run:
            result = run_release_smoke(
                workspace_root=args.workspace_root,
                include_vscode=not args.skip_vscode,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            from .quality import build_release_smoke_plan

            result = build_release_smoke_plan(
                workspace_root=args.workspace_root,
                include_vscode=not args.skip_vscode,
            )
        if args.format == "markdown":
            print(release_smoke_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") in {"planned", "success"} else 1
    if args.command == "release-trial":
        runner = release_trial_runner or run_release_trial
        result = runner(
            workspace_root=args.workspace_root,
            overwrite=args.overwrite,
            run_profile=args.run_profile,
            cloud_org=args.cloud_org,
            cloud_user=args.cloud_user,
            cloud_api_key=args.cloud_api_key,
        )
        if args.format == "markdown":
            print(release_trial_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    if args.command == "pacer-dogfood-check":
        key_id = str(
            args.attestation_key_id
            or os.environ.get("PACER_DOGFOOD_ATTESTATION_KEY_ID")
            or ""
        ).strip()
        secret = os.environ.get("PACER_DOGFOOD_ATTESTATION_KEY", "")
        attestation_keys = {key_id: secret} if key_id and secret else {}
        try:
            result = load_dogfood_evidence(
                args.repo_root,
                artifact_roots=tuple(args.artifact_root or ()),
                attestation_keys=attestation_keys,
                github_repository=args.github_repository,
                github_signer_workflow=args.signer_workflow,
                github_run_id=os.environ.get("GITHUB_RUN_ID", ""),
                github_run_attempt=os.environ.get("GITHUB_RUN_ATTEMPT", ""),
                require_github_provenance=args.require_github_provenance,
                target_score=args.minimum_score,
            )
        except Exception as exc:  # noqa: BLE001 - CLI gates must fail closed
            result = {
                "schema_version": 1,
                "status": "failed",
                "passed": False,
                "reason_codes": ["dogfood_evidence_load_failed"],
                "error_type": type(exc).__name__,
            }
        quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
        if result.get("passed") is True and quality.get("meets_target") is not True:
            result["status"] = "failed"
            result["passed"] = False
            result["pacer_on_pacer"] = False
            result["reason_codes"] = list(
                dict.fromkeys(
                    [*result.get("reason_codes", []), "dogfood_quality_target_not_met"]
                )
            )
        if args.format == "markdown":
            print(_pacer_gate_to_markdown("Pacer Dogfood", result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("passed") is True else 1
    if args.command == "pacer-dogfood-policy-check":
        try:
            result = load_dogfood_policy(args.repo_root)
        except Exception as exc:  # noqa: BLE001 - policy gates fail closed
            result = {
                "schema_version": 1,
                "status": "failed",
                "passed": False,
                "reason_codes": ["dogfood_policy_load_failed"],
                "error_type": type(exc).__name__,
            }
        if args.format == "markdown":
            print(_pacer_gate_to_markdown("Pacer Dogfood Policy", result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("passed") is True else 1
    if args.command == "pacer-dogfood-provider-check":
        result = run_dogfood_provider_check(
            provider_id=args.provider_id,
            base_url=args.base_url,
            model=args.model,
            key_env=args.key_env,
            timeout_seconds=args.timeout_seconds,
        )
        if args.format == "markdown":
            print(dogfood_provider_receipt_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("passed") is True else 1
    if args.command == "pacer-release-manifest-check":
        result = assess_release_manifest_file(
            args.manifest,
            expected_manifest_digest=args.expected_digest,
        )
        if args.format == "markdown":
            print(_pacer_gate_to_markdown("Pacer Release Manifest", result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("passed") is True else 1
    if args.command == "pacer-release-check":
        try:
            manifest_path = _canonical_release_manifest_path(args.repo_root, args.manifest)
            manifest_assessment = assess_release_manifest_file(
                manifest_path,
                expected_manifest_digest=args.expected_digest,
            )
            if not manifest_assessment.get("passed"):
                result = manifest_assessment
            else:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                key_id = str(
                    args.attestation_key_id
                    or os.environ.get("PACER_RELEASE_ATTESTATION_KEY_ID")
                    or manifest.get("release_attestation_key_id")
                    or ""
                ).strip()
                release_secret = os.environ.get("PACER_RELEASE_ATTESTATION_KEY", "")
                dogfood_key_id = str(
                    os.environ.get("PACER_DOGFOOD_ATTESTATION_KEY_ID") or key_id
                ).strip()
                dogfood_secret = os.environ.get("PACER_DOGFOOD_ATTESTATION_KEY", "")
                release_keys = {key_id: release_secret} if key_id and release_secret else {}
                dogfood_keys = (
                    {dogfood_key_id: dogfood_secret}
                    if dogfood_key_id and dogfood_secret
                    else release_keys
                )
                result = run_release_evidence_bundle(
                    manifest=manifest,
                    expected_manifest_digest=args.expected_digest,
                    repo_root=args.repo_root,
                    evidence_root=args.evidence_root,
                    bundle_path=args.bundle,
                    release_attestation_keys=release_keys,
                    dogfood_attestation_keys=dogfood_keys,
                    dogfood_artifact_roots=tuple(args.artifact_root or ()),
                )
        except Exception as exc:  # noqa: BLE001 - release gates must fail closed
            result = {
                "schema_version": 1,
                "status": "failed",
                "passed": False,
                "reason_codes": ["release_evidence_load_failed"],
                "error_type": type(exc).__name__,
            }
        if args.format == "markdown":
            print(_pacer_gate_to_markdown("Pacer Release", result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("passed") is True else 1
    if args.command == "install-check":
        plan = build_install_check_plan()
        if args.format == "markdown":
            print(install_check_plan_to_markdown(plan))
        else:
            print(json.dumps(to_jsonable(plan), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mcp-client-config":
        payload = build_mcp_client_config(
            workspace_root=args.workspace_root,
            client=args.client,
            python=args.python,
            repo_root=args.repo_root,
        )
        if args.format == "markdown":
            print(mcp_client_config_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mcp-doctor":
        payload = build_mcp_startup_doctor(
            workspace_root=args.workspace_root,
            repo_root=args.repo_root,
            python=args.python,
            client=args.client,
        )
        if args.format == "markdown":
            print(mcp_startup_doctor_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0 if payload.get("status") in {"success", "warning"} else 1
    if args.command == "coding-agent-brief":
        payload = build_coding_agent_brief(
            workspace_root=args.workspace_root,
            repo_root=args.repo_root,
            client=args.client,
            python=args.python,
        )
        if args.format == "markdown":
            print(coding_agent_brief_to_markdown(payload))
        else:
            print(json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mcp-smoke":
        result = run_mcp_smoke_check(
            workspace_root=args.workspace_root,
            workflow=args.workflow,
            inputs_file=args.inputs_file,
        )
        if args.format == "markdown":
            print(mcp_smoke_check_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    if args.command == "demo-workspace-check":
        result = run_demo_workspace_check(root=args.root, overwrite=args.overwrite, run_profile=args.run_profile)
        if args.format == "markdown":
            print(demo_workspace_check_to_markdown(result))
        else:
            print(json.dumps(to_jsonable(result), ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "success" else 1
    raise ValueError(f"Unsupported quality command: {args.command}")


def _pacer_gate_to_markdown(title: str, result: dict[str, Any]) -> str:
    reasons = [str(item) for item in result.get("reason_codes") or [] if str(item)]
    lines = [f"## {title}", "", f"Status: `{result.get('status', 'failed')}`"]
    digest = str(result.get("manifest_digest") or result.get("evidence_digest") or "")
    if digest:
        lines.append(f"Digest: `{digest}`")
    if reasons:
        lines.extend(["", "Reasons:", *[f"- `{reason}`" for reason in reasons]])
    error = str(result.get("error") or "")
    if error:
        lines.extend(["", f"Error: {error}"])
    return "\n".join(lines)


def parse_optional_bool(value: str | None) -> bool | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected boolean value, got: {value}")


def _canonical_release_manifest_path(repo_root: str | Path, value: str | Path) -> Path:
    root = Path(repo_root).expanduser().resolve()
    expected = root / ".pacer" / "release.json"
    current = root
    for part in (".pacer", "release.json"):
        current /= part
        if current.is_symlink():
            raise ValueError("release manifest path cannot use symbolic links")
    supplied = Path(value).expanduser()
    if not supplied.is_absolute():
        supplied = root / supplied
    if supplied.resolve(strict=False) != expected.resolve(strict=False):
        raise ValueError("release manifest must be .pacer/release.json")
    return expected
