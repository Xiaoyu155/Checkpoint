from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PACER_TYPED_TOOL_NAMES = frozenset(
    {"get_pacer_memory", "run_pacer_verification", "complete_pacer_task"}
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int = Field(ge=1)
    status: str | None = None
    error: str | None = None


class MemoryInput(_StrictInput):
    workspace_root: str
    repo_root: str
    goal: str = ""
    limit: int = Field(default=8, ge=1, le=50)
    detail: Literal["compact", "full"] = "compact"
    memory_budget_chars: int = Field(default=6000, ge=500, le=50_000)
    known_memory_receipt: str = ""
    memory_ids_used: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _bind_used_ids_to_receipt(self) -> MemoryInput:
        if self.memory_ids_used and not self.known_memory_receipt:
            raise ValueError("memory_ids_used requires known_memory_receipt")
        return self


class VerificationStep(_StrictInput):
    name: str = Field(min_length=1, max_length=120)
    argv: list[str] = Field(
        min_length=1,
        max_length=100,
        description=(
            "Verification argv as a string array; name is a display label only and argv must start a real "
            "allowlisted test/build/analyze command; do not pass a command field and do not use a command field "
            "or shell command string."
        ),
        examples=[["python", "-m", "pytest", "-q"]],
    )
    cwd: str = "."
    timeout_seconds: float = Field(default=600, gt=0, le=7200)


class VerificationInput(_StrictInput):
    workspace_root: str
    repo_root: str
    steps: list[VerificationStep] = Field(min_length=1, max_length=20)
    stop_on_failure: bool = False
    tail_chars: int = Field(default=2000, ge=200, le=2000)


class LegacyFileFact(_StrictInput):
    path: str = Field(min_length=1, max_length=500)
    state: Literal["created", "modified", "deleted"]


class CompletionClaim(_StrictInput):
    kind: Literal["change", "configuration", "review", "research", "test"] | None = Field(
        default=None,
        description="Legacy compatibility field. Pacer derives the canonical value.",
    )
    requirement_ids: list[str] = Field(
        min_length=1,
        max_length=20,
        description="IDs from the immutable task_contract returned by begin_pacer_task.",
    )
    requirement: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Legacy compatibility field. Pacer loads immutable text by requirement ID.",
    )
    result: str = Field(min_length=1, max_length=1000)
    files: list[LegacyFileFact] = Field(
        default_factory=list,
        max_length=200,
        description="Legacy compatibility field. Pacer ignores it and derives file facts.",
    )
    verification_steps: list[str] = Field(
        min_length=1,
        max_length=20,
        description=(
            "Names must exactly match steps[].name and must pass. Reuse the task's substantive "
            "test/build/analyze step; the name is only a label and steps[].argv must be the real command. "
            "Pacer derives file facts, so do not add Git inspection steps."
        ),
    )


class CompletionEvidence(_StrictInput):
    result_kind: Literal["change", "configuration", "review", "research", "test"] | None = Field(
        default=None,
        description="Legacy compatibility field. Pacer derives the canonical value.",
    )
    claims: list[CompletionClaim] = Field(min_length=1, max_length=20)
    unresolved_items: list[str] = Field(max_length=20)
    known_risks: list[str] = Field(max_length=20)


class CompletionInput(_StrictInput):
    workspace_root: str
    repo_root: str
    goal: str = Field(min_length=1, max_length=2000)
    summary: str = Field(min_length=1, max_length=4000)
    completion_evidence: CompletionEvidence
    steps: list[VerificationStep] = Field(min_length=1, max_length=20)
    stop_on_failure: bool = False
    tail_chars: int = Field(default=1200, ge=200, le=2000)


class MemoryOutput(_StructuredOutput):
    memory_receipt: str | None = None
    effective_memory: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _require_success_shape(self) -> MemoryOutput:
        if not self.error and (not self.status or self.effective_memory is None):
            raise ValueError("memory output requires status and effective_memory")
        return self


class VerificationOutput(_StructuredOutput):
    run_id: str | None = None
    records: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _require_success_shape(self) -> VerificationOutput:
        if not self.error and (not self.status or not self.run_id or self.records is None):
            raise ValueError("verification output requires status, run_id, and records")
        return self


class CompletionOutput(_StructuredOutput):
    launch_id: str | None = None
    task_review: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _require_success_shape(self) -> CompletionOutput:
        if not self.error and (not self.status or not self.launch_id or self.task_review is None):
            raise ValueError("completion output requires status, launch_id, and task_review")
        return self


_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "get_pacer_memory": MemoryInput,
    "run_pacer_verification": VerificationInput,
    "complete_pacer_task": CompletionInput,
}
_OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "get_pacer_memory": MemoryOutput,
    "run_pacer_verification": VerificationOutput,
    "complete_pacer_task": CompletionOutput,
}


def pacer_tool_input_schema(name: str) -> dict[str, Any]:
    return _inline_local_refs(_INPUT_MODELS[name].model_json_schema())


def pacer_tool_output_schema(name: str) -> dict[str, Any]:
    return _inline_local_refs(_OUTPUT_MODELS[name].model_json_schema())


def validate_pacer_tool_input(name: str, value: Any) -> dict[str, Any]:
    model = _INPUT_MODELS.get(name)
    if model is None:
        return dict(value) if isinstance(value, dict) else {}
    validated = model.model_validate(value)
    return validated.model_dump(mode="python", exclude_none=True)


def validate_pacer_tool_output(name: str, value: Any) -> dict[str, Any]:
    model = _OUTPUT_MODELS.get(name)
    if model is None:
        return dict(value) if isinstance(value, dict) else {}
    validated = model.model_validate(value)
    return validated.model_dump(mode="python", exclude_none=True)


def _inline_local_refs(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}

    def expand(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [expand(item, stack) for item in value]
        if not isinstance(value, dict):
            return value
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.rsplit("/", 1)[-1]
            if name in stack or name not in definitions:
                raise ValueError(f"recursive or missing Pacer MCP schema reference: {name}")
            merged = {**deepcopy(definitions[name]), **{key: item for key, item in value.items() if key != "$ref"}}
            return expand(merged, (*stack, name))
        return {key: expand(item, stack) for key, item in value.items() if key != "$defs"}

    return expand(schema)
