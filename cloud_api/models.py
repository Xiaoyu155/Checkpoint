from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


RunState = Literal["queued", "running", "success", "failed", "cancelled"]


@dataclass(frozen=True)
class RunRequest:
    workflow_name: str = ""
    workflow_yaml: str = ""
    workflow_source: str = "workspace"
    workflow_id: str = ""
    workspace: str = ""
    org: str = ""
    user_id: str = ""
    run_profile: str = "dry-run"
    inputs: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    callback_url: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "RunRequest":
        inputs = payload.get("inputs")
        tags = payload.get("tags")
        return cls(
            workflow_name=str(payload.get("workflow_name") or payload.get("workflow") or ""),
            workflow_yaml=str(payload.get("workflow_yaml") or ""),
            workflow_source=str(payload.get("workflow_source") or "workspace"),
            workflow_id=str(payload.get("workflow_id") or ""),
            workspace=str(payload.get("workspace") or ""),
            org=str(payload.get("org") or payload.get("organization") or ""),
            user_id=str(payload.get("user_id") or payload.get("user") or ""),
            run_profile=str(payload.get("run_profile") or "dry-run"),
            inputs=inputs if isinstance(inputs, dict) else {},
            tags=[str(item) for item in tags] if isinstance(tags, list) else [],
            callback_url=str(payload.get("callback_url") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunStatus:
    id: str
    status: RunState
    workflow_name: str = ""
    workflow_source: str = "workspace"
    workflow_id: str = ""
    message: str = ""
    report_url: str = ""
    artifact_url: str = ""
    org: str = ""
    user_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunArtifact:
    name: str
    kind: str
    url: str
    content_type: str = ""
    size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunResult:
    id: str
    status: RunState
    workflow_name: str
    workflow_source: str = "workspace"
    workflow_id: str = ""
    passed: bool = False
    duration_ms: int = 0
    steps_total: int = 0
    steps_passed: int = 0
    failed_step: str = ""
    report_url: str = ""
    artifacts: list[RunArtifact] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    org: str = ""
    user_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifacts"] = [artifact.to_dict() for artifact in self.artifacts]
        return payload


def run_result_from_cloud_payload(payload: dict[str, Any]) -> RunResult:
    status = str(payload.get("status") or "failed")
    if status not in {"queued", "running", "success", "failed", "cancelled"}:
        status = "failed"
    artifacts = payload.get("artifacts")
    parsed_artifacts = [
        RunArtifact(
            name=str(item.get("name") or ""),
            kind=str(item.get("kind") or ""),
            url=str(item.get("url") or ""),
            content_type=str(item.get("content_type") or ""),
            size_bytes=int(item.get("size_bytes") or 0),
        )
        for item in artifacts
        if isinstance(item, dict)
    ] if isinstance(artifacts, list) else []
    return RunResult(
        id=str(payload.get("id") or payload.get("run_id") or ""),
        status=status,  # type: ignore[arg-type]
        workflow_name=str(payload.get("workflow_name") or ""),
        passed=status == "success",
        duration_ms=int(payload.get("duration_ms") or 0),
        steps_total=int(payload.get("steps_total") or 0),
        steps_passed=int(payload.get("steps_passed") or 0),
        failed_step=str(payload.get("failed_step") or ""),
        report_url=str(payload.get("report_url") or ""),
        artifacts=parsed_artifacts,
        raw=payload,
        workflow_source=str(payload.get("workflow_source") or "workspace"),
        workflow_id=str(payload.get("workflow_id") or ""),
        org=str(payload.get("org") or ""),
        user_id=str(payload.get("user_id") or payload.get("user") or ""),
    )
