from __future__ import annotations

from dataclasses import dataclass
from typing import Any


CURRENT_WORKFLOW_SCHEMA_VERSION = 1
CURRENT_CATALOG_SCHEMA_VERSION = 1
CURRENT_REPORT_SCHEMA_VERSION = 1
CURRENT_STRUCTURED_FAILURE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class UnsupportedSchemaVersionError(ValueError):
    schema_name: str
    version: int
    supported: int

    @property
    def migration_hint(self) -> str:
        return (
            f"Run {self.schema_name}-migrate to upgrade the payload to schema_version {self.supported}, "
            f"or regenerate it with a compatible writer."
        )

    def __str__(self) -> str:
        return f"Unsupported {self.schema_name} schema_version: {self.version}. Supported: {self.supported}. {self.migration_hint}"


def migrate_workflow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    schema_version = normalized.get("schema_version")
    if schema_version in (None, "", 0):
        normalized["schema_version"] = CURRENT_WORKFLOW_SCHEMA_VERSION
        normalized.setdefault("version", 1)
        return normalized
    parsed = int(schema_version)
    if parsed != CURRENT_WORKFLOW_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError("workflow", parsed, CURRENT_WORKFLOW_SCHEMA_VERSION)
    normalized["schema_version"] = parsed
    return normalized


def migrate_catalog_payload(payload: dict[str, Any], *, org: str = "") -> dict[str, Any]:
    normalized = dict(payload)
    schema_version = normalized.get("schema_version")
    if schema_version in (None, "", 0):
        normalized["schema_version"] = CURRENT_CATALOG_SCHEMA_VERSION
    else:
        parsed = int(schema_version)
        if parsed != CURRENT_CATALOG_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError("catalog", parsed, CURRENT_CATALOG_SCHEMA_VERSION)
        normalized["schema_version"] = parsed
    normalized.setdefault("org", org)
    workflows = normalized.get("workflows")
    if not isinstance(workflows, list):
        workflows = normalized.get("public_workflows")
    normalized["workflows"] = workflows if isinstance(workflows, list) else []
    withdrawn = normalized.get("withdrawn_workflows")
    if not isinstance(withdrawn, list):
        withdrawn = normalized.get("withdrawn")
    normalized["withdrawn_workflows"] = withdrawn if isinstance(withdrawn, list) else []
    normalized["next_id"] = int(normalized.get("next_id") or 1)
    return normalized


def migrate_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    schema_version = normalized.get("schema_version")
    if schema_version in (None, "", 0):
        normalized["schema_version"] = CURRENT_REPORT_SCHEMA_VERSION
        return normalized
    parsed = int(schema_version)
    if parsed != CURRENT_REPORT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError("report", parsed, CURRENT_REPORT_SCHEMA_VERSION)
    normalized["schema_version"] = parsed
    return normalized


def migrate_structured_failure_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    schema_version = normalized.get("schema_version")
    if schema_version in (None, "", 0):
        normalized["schema_version"] = CURRENT_STRUCTURED_FAILURE_SCHEMA_VERSION
        return normalized
    parsed = int(schema_version)
    if parsed != CURRENT_STRUCTURED_FAILURE_SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError("structured_failure", parsed, CURRENT_STRUCTURED_FAILURE_SCHEMA_VERSION)
    normalized["schema_version"] = parsed
    return normalized

