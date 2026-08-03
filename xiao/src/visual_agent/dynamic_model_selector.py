from __future__ import annotations

import json
import os
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_capabilities import canonical_agent_name, load_agent_profile
from .hourly_budget import quota_used_percentage
from .model_credentials import inspect_model_credentials
from .model_router import route_task


DEFAULT_SELECTOR_CONFIG = "model_pool.json"
ROUTING_POLICY_VERSION = 3


@dataclass(frozen=True)
class ModelCandidate:
    id: str
    provider: str
    model: str
    capability: float = 0.5
    cost: float = 0.5
    latency: float = 0.5
    reliability: float = 0.8
    max_context: int = 0
    modes: tuple[str, ...] = ()
    notes: str = ""
    agent_backend: str = "codex"
    capability_known: bool = True
    cost_known: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelSelection:
    status: str
    objective: str
    required_tier: str
    selected: ModelCandidate | None
    candidates: tuple[dict[str, Any], ...]
    reason: str
    quota_used_percentage: float
    route_signals: dict[str, Any]
    decision_id: str
    agent_backend: str


def model_pool_path(workspace_root: str | Path | None = None) -> Path:
    override = os.environ.get("CHECKPOINT_MODEL_POOL", "").strip()
    if override:
        return Path(override).expanduser()
    if workspace_root:
        return Path(workspace_root).expanduser().resolve() / DEFAULT_SELECTOR_CONFIG
    return Path(DEFAULT_SELECTOR_CONFIG)


def load_model_candidates(
    *,
    workspace_root: str | Path | None = None,
    config_path: str | Path | None = None,
    credential_source: str | Path | None = None,
    agent_backend: str = "codex",
) -> list[ModelCandidate]:
    path = Path(config_path).expanduser() if config_path else model_pool_path(workspace_root)
    configured = _read_configured_candidates(path)
    if configured:
        return configured
    backend = canonical_agent_name(agent_backend)
    profile_candidates = _candidates_from_agent_profiles(agent_backend=backend)
    credential_candidates = (
        _candidates_from_credentials(credential_source=credential_source)
        if backend == "codex"
        else []
    )
    return [*profile_candidates, *credential_candidates]


def select_model_for_task(
    *,
    objective: str,
    changed_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    repeated_failure: bool = False,
    quota_snapshot: dict[str, Any] | None = None,
    workspace_root: str | Path | None = None,
    config_path: str | Path | None = None,
    credential_source: str | Path | None = None,
    agent_backend: str = "codex",
) -> ModelSelection:
    route = route_task(
        objective=objective,
        changed_files=changed_files,
        acceptance_criteria=acceptance_criteria,
        repeated_failure=repeated_failure,
    )
    backend = canonical_agent_name(agent_backend)
    candidates = load_model_candidates(
        workspace_root=workspace_root,
        config_path=config_path,
        credential_source=credential_source,
        agent_backend=backend,
    )
    used = quota_used_percentage(quota_snapshot)
    scored = [
        _score_candidate(
            item,
            required_tier=route.tier,
            quota_used=used,
            agent_backend=backend,
        )
        for item in candidates
    ]
    scored.sort(key=lambda item: (-item["score"], item["cost"], -item["capability"], item["id"]))
    viable = [item for item in scored if item["viable"]]
    selected_payload = viable[0] if viable else None
    selected = next((item for item in candidates if item.id == selected_payload["id"]), None) if selected_payload else None
    if selected is None:
        decision_id = _routing_decision_id(objective, route.tier, backend, None, scored)
        return ModelSelection(
            status="blocked",
            objective=str(objective or ""),
            required_tier=route.tier,
            selected=None,
            candidates=tuple(scored),
            reason="No configured model candidate can satisfy the task tier.",
            quota_used_percentage=used,
            route_signals=route.signals,
            decision_id=decision_id,
            agent_backend=backend,
        )
    decision_id = _routing_decision_id(objective, route.tier, backend, selected, scored)
    return ModelSelection(
        status="selected",
        objective=str(objective or ""),
        required_tier=route.tier,
        selected=selected,
        candidates=tuple(scored),
        reason=_selection_reason(selected_payload, route.tier, used),
        quota_used_percentage=used,
        route_signals=route.signals,
        decision_id=decision_id,
        agent_backend=backend,
    )


def selection_to_dict(selection: ModelSelection) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "policy_version": ROUTING_POLICY_VERSION,
        "decision_id": selection.decision_id,
        "agent_backend": selection.agent_backend,
        "status": selection.status,
        "objective": selection.objective,
        "required_tier": selection.required_tier,
        "selected": _candidate_to_dict(selection.selected) if selection.selected else None,
        "reason": selection.reason,
        "quota_used_percentage": selection.quota_used_percentage,
        "route_signals": selection.route_signals,
        "candidates": list(selection.candidates),
    }


def selection_to_markdown(selection: ModelSelection) -> str:
    lines = ["## Dynamic Model Selection", ""]
    lines.append(f"Objective: {selection.objective}")
    lines.append(f"Required tier: `{selection.required_tier}`")
    lines.append(f"Agent backend: `{selection.agent_backend}`")
    lines.append(f"Quota used: `{selection.quota_used_percentage:.1f}%`")
    if selection.selected:
        lines.append(f"Selected: `{selection.selected.id}` ({selection.selected.provider}:{selection.selected.model})")
    else:
        lines.append("Selected: none")
    lines.append(f"Reason: {selection.reason}")
    lines.extend(["", "### Candidates", ""])
    for item in selection.candidates:
        marker = "selected" if selection.selected and item["id"] == selection.selected.id else ""
        lines.append(
            f"- `{item['id']}` score={item['score']:.3f} "
            f"cap={item['capability']:.2f} cost={item['cost']:.2f} "
            f"viable={item['viable']} {marker}".rstrip()
        )
    return "\n".join(lines).rstrip()


def routing_request_evidence(
    selection: Any,
    *,
    requested_provider: str,
    requested_model: str,
) -> dict[str, Any]:
    decision = selection if isinstance(selection, dict) else {}
    selected = decision.get("selected") if isinstance(decision.get("selected"), dict) else {}
    decision_id = str(decision.get("decision_id") or "")
    if not decision_id or not selected:
        return {
            "schema_version": 1,
            "decision_id": decision_id,
            "policy_version": int(decision.get("policy_version") or 0),
            "verdict": "passthrough",
            "policy_match": None,
            "reason_codes": ["routing_decision_unavailable"],
            "request": {
                "provider": requested_provider,
                "model": requested_model,
            },
        }
    expected_provider = str(selected.get("provider") or "")
    expected_model = str(selected.get("model") or "")
    matched = expected_model.casefold() == str(requested_model or "").casefold() and (
        not expected_provider
        or expected_provider.casefold() == str(requested_provider or "").casefold()
    )
    return {
        "schema_version": 1,
        "decision_id": decision_id,
        "policy_version": int(decision.get("policy_version") or 0),
        "verdict": "matched" if matched else "mismatched",
        "policy_match": matched,
        "reason_codes": [] if matched else ["routing_request_mismatch"],
        "decision": {
            "required_tier": str(decision.get("required_tier") or ""),
            "provider": expected_provider,
            "model": expected_model,
        },
        "request": {
            "provider": requested_provider,
            "model": requested_model,
        },
    }


def _read_configured_candidates(path: Path) -> list[ModelCandidate]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    raw_items = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(raw_items, list):
        return []
    return [_candidate_from_dict(item) for item in raw_items if isinstance(item, dict) and item.get("id")]


def _candidates_from_credentials(*, credential_source: str | Path | None = None) -> list[ModelCandidate]:
    inspected = inspect_model_credentials(source=credential_source)
    providers = inspected.get("providers") if isinstance(inspected.get("providers"), list) else []
    candidates: list[ModelCandidate] = []
    for item in providers:
        if not isinstance(item, dict) or int(item.get("secret_count") or 0) <= 0:
            continue
        provider = str(item.get("provider") or "")
        defaults = _provider_defaults(provider)
        candidates.append(
            ModelCandidate(
                id=f"{provider}:auto",
                provider=provider,
                model=str(defaults["model"]),
                capability=float(defaults["capability"]),
                cost=float(defaults["cost"]),
                latency=float(defaults["latency"]),
                reliability=0.75,
                modes=tuple(defaults["modes"]),
                notes="Auto-discovered from configured credentials.",
                agent_backend="codex" if provider.lower() == "openai" else "unsupported",
            )
        )
    return candidates


def _candidates_from_agent_profiles(*, agent_backend: str) -> list[ModelCandidate]:
    candidates: list[ModelCandidate] = []
    for agent in (canonical_agent_name(agent_backend),):
        profile = load_agent_profile(agent) or {}
        models = profile.get("models") if isinstance(profile.get("models"), list) else []
        for entry in models:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            role = str(entry.get("role") or "").strip().lower()
            defaults = _profile_role_defaults(role)
            if not defaults:
                continue
            model = str(entry["id"])
            candidates.append(
                ModelCandidate(
                    id=f"{agent}:{model}",
                    provider="",
                    model=model,
                    capability=float(defaults["capability"]),
                    cost=float(defaults["cost"]),
                    latency=float(defaults["latency"]),
                    reliability=float(defaults["reliability"]),
                    modes=tuple(defaults["modes"]),
                    notes=f"Built in from {agent} agent profile role `{role}`.",
                    agent_backend=agent,
                    raw=dict(entry),
                )
            )
    return candidates


def _profile_role_defaults(role: str) -> dict[str, Any]:
    if role == "fast":
        return {
            "capability": 0.50,
            "cost": 0.20,
            "latency": 0.35,
            "reliability": 0.80,
            "modes": ("cheap",),
        }
    if role == "balanced":
        return {
            "capability": 0.72,
            "cost": 0.35,
            "latency": 0.45,
            "reliability": 0.82,
            "modes": ("standard",),
        }
    if role == "default":
        return {
            "capability": 0.86,
            "cost": 0.70,
            "latency": 0.55,
            "reliability": 0.86,
            "modes": ("standard", "strong"),
        }
    return {}


def _provider_defaults(provider: str) -> dict[str, Any]:
    low = provider.lower()
    if low in {"deepseek", "xiaomimimo", "gemini"}:
        return {"model": "auto", "capability": 0.45, "cost": 0.15, "latency": 0.35, "modes": ("cheap", "standard")}
    if low in {"openai", "anthropic", "qwen", "kimi"}:
        return {"model": "auto", "capability": 0.72, "cost": 0.55, "latency": 0.45, "modes": ("standard", "strong")}
    return {"model": "auto", "capability": 0.55, "cost": 0.45, "latency": 0.5, "modes": ("standard",)}


def _candidate_from_dict(item: dict[str, Any]) -> ModelCandidate:
    modes = item.get("modes")
    capability_known = "capability" in item or "quality" in item
    cost_known = "cost" in item
    return ModelCandidate(
        id=str(item.get("id") or ""),
        provider=str(item.get("provider") or ""),
        model=str(item.get("model") or ""),
        capability=_clamp01(item.get("capability", item.get("quality", 0.0))),
        cost=_clamp01(item.get("cost", 1.0)),
        latency=_clamp01(item.get("latency", 0.5)),
        reliability=_clamp01(item.get("reliability", 0.8)),
        max_context=max(0, int(item.get("max_context") or 0)),
        modes=tuple(str(mode).strip() for mode in (modes or []) if str(mode).strip()),
        notes=str(item.get("notes") or ""),
        agent_backend=str(item.get("agent_backend") or "codex"),
        capability_known=capability_known,
        cost_known=cost_known,
        raw=dict(item),
    )


def _score_candidate(
    candidate: ModelCandidate,
    *,
    required_tier: str,
    quota_used: float,
    agent_backend: str,
) -> dict[str, Any]:
    required_capability = {"cheap": 0.2, "standard": 0.45, "strong": 0.7}.get(required_tier, 0.45)
    allowed_by_mode = not candidate.modes or required_tier in candidate.modes
    backend_compatible = canonical_agent_name(candidate.agent_backend) == canonical_agent_name(agent_backend)
    viable = (
        candidate.capability_known
        and candidate.cost_known
        and candidate.capability >= required_capability
        and allowed_by_mode
        and backend_compatible
    )
    quota_pressure = max(0.0, min(1.0, quota_used / 100.0))
    capability_fit = 1.0 - min(1.0, abs(candidate.capability - required_capability))
    # High quota pressure increases the cost penalty. Strong tasks still require
    # enough capability; cheap tasks heavily prefer low-cost models.
    cost_weight = 0.35 + (0.35 * quota_pressure)
    capability_weight = 0.45 if required_tier == "strong" else 0.30
    reliability_weight = 0.18
    latency_weight = 0.07
    score = (
        capability_fit * capability_weight
        + candidate.reliability * reliability_weight
        + (1.0 - candidate.cost) * cost_weight
        + (1.0 - candidate.latency) * latency_weight
    )
    if not viable:
        score -= 1.0
    return {
        **_candidate_to_dict(candidate),
        "required_capability": required_capability,
        "viable": viable,
        "backend_compatible": backend_compatible,
        "score": round(score, 6),
    }


def _selection_reason(candidate: dict[str, Any], required_tier: str, quota_used: float) -> str:
    return (
        f"Selected the best viable candidate for `{required_tier}` with quota at "
        f"{quota_used:.1f}% used; score balances capability, reliability, cost, and latency."
    )


def _candidate_to_dict(candidate: ModelCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "id": candidate.id,
        "provider": candidate.provider,
        "model": candidate.model,
        "capability": candidate.capability,
        "cost": candidate.cost,
        "latency": candidate.latency,
        "reliability": candidate.reliability,
        "max_context": candidate.max_context,
        "modes": list(candidate.modes),
        "notes": candidate.notes,
        "agent_backend": candidate.agent_backend,
        "capability_known": candidate.capability_known,
        "cost_known": candidate.cost_known,
    }


def _routing_decision_id(
    objective: str,
    required_tier: str,
    agent_backend: str,
    selected: ModelCandidate | None,
    candidates: list[dict[str, Any]],
) -> str:
    payload = {
        "policy_version": ROUTING_POLICY_VERSION,
        "objective": str(objective or ""),
        "required_tier": required_tier,
        "agent_backend": agent_backend,
        "selected": selected.id if selected else "",
        "candidates": [str(item.get("id") or "") for item in candidates],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _clamp01(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = 0.5
    return max(0.0, min(1.0, num))
