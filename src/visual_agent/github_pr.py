from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .quality import list_quality_gate_reports
from .reports import RunReport, list_run_summaries, load_run_report, run_report_to_dict
from .security import scrub_secrets


@dataclass(frozen=True)
class PullRequestContext:
    repository: str
    number: int
    event_name: str
    run_url: str = ""
    artifact_url: str = ""


def github_event_pr_number(event_path: str | Path | None) -> int | None:
    if not event_path:
        return None
    path = Path(event_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict):
        pull_request = payload.get("pull_request")
        if isinstance(pull_request, dict) and pull_request.get("number") is not None:
            try:
                return int(pull_request["number"])
            except (TypeError, ValueError):
                return None
        if payload.get("number") is not None and str(payload.get("event_name") or "") == "pull_request":
            try:
                return int(payload["number"])
            except (TypeError, ValueError):
                return None
    return None


def github_repository_from_env(repository: str | None = None) -> str:
    value = str(repository or os.environ.get("GITHUB_REPOSITORY") or "").strip()
    if not value or "/" not in value:
        raise ValueError("GitHub repository must be provided as owner/name.")
    return value


def github_run_url_from_env(run_url: str | None = None) -> str:
    value = str(run_url or "").strip()
    if value:
        return value
    server_url = str(os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    repository = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    if repository and run_id:
        return f"{server_url}/{repository}/actions/runs/{run_id}"
    return ""


def latest_failed_run_report(report_root: str | Path = ".runs") -> RunReport | None:
    summaries = list_run_summaries(report_root, limit=20)
    for summary in summaries:
        if summary.status == "failed":
            return load_run_report(summary.run_dir)
    if summaries:
        return load_run_report(summaries[0].run_dir)
    return None


def latest_quality_gate_entry(report_root: str | Path = ".runs/quality_gates") -> dict[str, Any] | None:
    entries = list_quality_gate_reports(report_root=report_root)
    return entries[0] if entries else None


def build_pr_failure_comment(
    run_report: RunReport | dict[str, Any] | None,
    *,
    quality_gate_entry: dict[str, Any] | None = None,
    artifact_url: str = "",
    run_url: str = "",
    max_screenshots: int = 3,
) -> dict[str, Any]:
    if run_report is None:
        return {
            "schema_version": 1,
            "status": "blocked",
            "message": "No failed run report was found.",
            "body": "",
        }
    report = run_report_to_dict(run_report) if isinstance(run_report, RunReport) else dict(run_report)
    report = scrub_secrets(report)
    steps = report.get("steps") if isinstance(report.get("steps"), list) else []
    failed_step = next((step for step in steps if isinstance(step, dict) and step.get("status") == "failed"), None)
    screenshots = collect_screenshots(steps, max_screenshots=max_screenshots)
    body_lines = [
        "## Checkpoint found a regression",
        "",
        f"- Workflow: `{report.get('workflow_name') or report.get('workflow') or ''}`",
        f"- Run ID: `{report.get('run_id') or ''}`",
        f"- Status: `{report.get('status') or 'unknown'}`",
        f"- Failed step: `{failed_step.get('id') if failed_step else report.get('failed_step') or 'unknown'}`",
    ]
    if run_url:
        body_lines.append(f"- Run: [{run_url}]({run_url})")
    if artifact_url:
        body_lines.append(f"- Uploaded artifacts: [{artifact_url}]({artifact_url})")
    if quality_gate_entry:
        body_lines.extend(
            [
                "",
                "### Quality Gate",
                "",
                f"- Profile: `{quality_gate_entry.get('profile') or ''}`",
                f"- Status: `{quality_gate_entry.get('status') or ''}`",
                f"- Report: `{quality_gate_entry.get('json_report') or ''}`",
            ]
        )
    if failed_step:
        diagnosis = failed_step.get("failure_diagnosis") if isinstance(failed_step.get("failure_diagnosis"), dict) else {}
        body_lines.extend(
            [
                "",
                "### Failure Details",
                "",
                f"- Action: `{failed_step.get('action') or ''}`",
            ]
        )
        if diagnosis.get("root_cause"):
            body_lines.append(f"- Root cause: `{diagnosis.get('root_cause')}`")
        if diagnosis.get("confidence") is not None:
            body_lines.append(f"- Confidence: `{diagnosis.get('confidence')}`")
        if diagnosis.get("suggested_fix"):
            body_lines.append(f"- Suggested fix: {diagnosis.get('suggested_fix')}")
        expected = diagnosis.get("expected")
        actual = diagnosis.get("actual")
        if expected or actual:
            body_lines.extend(["", "```text"])
            if expected:
                body_lines.append(f"expected: {expected}")
            if actual:
                body_lines.append(f"actual: {actual}")
            body_lines.append("```")
        screenshot = diagnosis.get("artifacts", {}).get("screenshot") if isinstance(diagnosis.get("artifacts"), dict) else None
        if screenshot:
            body_lines.append(f"- Failure screenshot: `{screenshot}`")
    if screenshots:
        body_lines.extend(["", "### Screenshots", ""])
        for screenshot in screenshots:
            body_lines.append(f"- `{screenshot}`")
    return {
        "schema_version": 1,
        "status": "ready",
        "body": "\n".join(body_lines).rstrip() + "\n",
        "report": report,
        "quality_gate": scrub_secrets(quality_gate_entry) if quality_gate_entry else None,
    }


def collect_screenshots(steps: list[dict[str, Any]], *, max_screenshots: int = 3) -> list[str]:
    screenshots: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for path in step.get("artifact_paths") if isinstance(step.get("artifact_paths"), list) else []:
            path_text = str(path)
            if path_text.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")) and path_text not in screenshots:
                screenshots.append(path_text)
        failure_artifacts = step.get("failure_artifacts") if isinstance(step.get("failure_artifacts"), dict) else {}
        screenshot = failure_artifacts.get("screenshot")
        if screenshot and str(screenshot) not in screenshots:
            screenshots.append(str(screenshot))
        if len(screenshots) >= max_screenshots:
            break
    return screenshots[:max_screenshots]


def github_api_request(
    url: str,
    *,
    token: str,
    method: str = "POST",
    payload: dict[str, Any] | None = None,
    opener: Any | None = None,
) -> dict[str, Any]:
    opener = opener or urlopen
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )
    try:
        with opener(request, timeout=30) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return {"status": "success", "http_status": getattr(response, "status", 200), "response": json.loads(raw or "{}") if raw.strip() else {}}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {
            "status": "failed",
            "http_status": exc.code,
            "message": str(scrub_secrets(raw))[:500],
        }
    except URLError as exc:
        return {"status": "failed", "http_status": 0, "message": str(scrub_secrets(str(exc)))[:500]}


def post_pr_comment(
    *,
    repository: str,
    number: int,
    token: str,
    body: str,
    opener: Any | None = None,
) -> dict[str, Any]:
    repo = github_repository_from_env(repository)
    url = f"https://api.github.com/repos/{repo}/issues/{int(number)}/comments"
    payload = github_api_request(url, token=token, method="POST", payload={"body": body}, opener=opener)
    payload["repository"] = repo
    payload["number"] = int(number)
    return payload


def pr_failure_comment_result(
    *,
    report_root: str | Path = ".runs",
    quality_gate_root: str | Path = ".runs/quality_gates",
    artifact_url: str = "",
    run_url: str = "",
    max_screenshots: int = 3,
) -> dict[str, Any]:
    run_report = latest_failed_run_report(report_root)
    quality_gate = latest_quality_gate_entry(quality_gate_root)
    return build_pr_failure_comment(
        run_report,
        quality_gate_entry=quality_gate,
        artifact_url=artifact_url,
        run_url=run_url,
        max_screenshots=max_screenshots,
    )

