from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .pacer_verification import classify_verification_step
from .subprocess_window import hidden_subprocess_kwargs


TASK_REVIEW_SCHEMA_VERSION = 1
TASK_CONTRACT_SCHEMA_VERSION = 1
SOURCE_BASELINE_SCHEMA_VERSION = 1
MAX_BASELINE_FILES = 10_000
MAX_HASH_BYTES = 4 * 1024 * 1024
_SUBPROCESS_RUN = subprocess.run
RESULT_KINDS = frozenset({"change", "configuration", "review", "research", "test"})
CLAIM_KINDS = frozenset({"change", "configuration", "review", "research", "test"})
FILE_STATES = frozenset({"created", "modified", "deleted"})
ACCEPTANCE_STEP_CLASSES = frozenset({"test", "build", "analyze"})
GENERIC_SUMMARIES = frozenset(
    {
        "done",
        "complete",
        "completed",
        "finished",
        "implemented",
        "fixed",
        "已完成",
        "完成",
        "已修复",
        "修复完成",
        "已实现",
    }
)
_CLAUSE_SPLIT = re.compile(r"[\r\n。！？!?；;]+")
_GOAL_CONNECTOR_SPLIT = re.compile(
    r"\b(?:and then|and|then|also|plus|as well as)\b|&|"
    r"(?:并且|以及|同时|然后|还要|还需|另外|此外)|"
    r"(?:并|与|和|后)(?=[修增添实完运补更删配接创检验优重支])|"
    r"[,，、](?=\s*(?:修|增|添|实|完|运|补|更|删|配|接|创|检|验|优|重|支|"
    r"add\b|build\b|fix\b|implement\b|run\b|test\b|update\b|write\b))",
    flags=re.IGNORECASE,
)
_LATIN_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_CJK_SEQUENCE = re.compile(r"[\u3400-\u9fff]+")
_STOP_TOKENS = {
    "a",
    "an",
    "and",
    "for",
    "of",
    "the",
    "to",
    "with",
    "任务",
    "项目",
    "完成",
    "进行",
    "开发",
    "代码",
    "功能",
    "这个",
    "一个",
    "本次",
}
_BASELINE_IGNORED_DIRS = {
    ".agent-workspace",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
_HASHABLE_SOURCE_SUFFIXES = {
    ".bat",
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".dart",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".kts",
    ".md",
    ".ps1",
    ".py",
    ".rs",
    ".scss",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}


def capture_task_source_baseline(repo_root: str | Path) -> dict[str, Any]:
    """Capture bounded, content-free attribution evidence before Codex starts."""

    repo = Path(repo_root).expanduser().resolve()
    captured_at = datetime.now(timezone.utc).isoformat()
    git_changes = _git_changed_files(repo)
    if git_changes is not None:
        paths = sorted(git_changes)
        selected = paths[:MAX_BASELINE_FILES]
        entries = {
            _path_key(path): _file_fingerprint(repo / Path(path))
            for path in selected
        }
        return {
            "schema_version": SOURCE_BASELINE_SCHEMA_VERSION,
            "kind": "git",
            "repo_root": str(repo),
            "captured_at": captured_at,
            "complete": len(paths) <= MAX_BASELINE_FILES,
            "head": str(_run_git(repo, ["rev-parse", "HEAD"]) or "").strip(),
            "git_prefix": str(_run_git(repo, ["rev-parse", "--show-prefix"]) or "")
            .strip()
            .replace("\\", "/"),
            "initial_changes": [_path_key(path) for path in selected],
            "entries": entries,
            "file_count": len(paths),
        }

    entries: dict[str, str] = {}
    complete = True
    visited = 0
    try:
        for current, dirs, files in os.walk(repo):
            dirs[:] = [name for name in dirs if name.lower() not in _BASELINE_IGNORED_DIRS]
            current_path = Path(current)
            for name in files:
                visited += 1
                if visited > MAX_BASELINE_FILES:
                    complete = False
                    break
                path = current_path / name
                try:
                    relative = path.resolve().relative_to(repo).as_posix()
                except (OSError, ValueError):
                    continue
                entries[_path_key(relative)] = _file_fingerprint(path)
            if not complete:
                break
    except OSError:
        complete = False
    return {
        "schema_version": SOURCE_BASELINE_SCHEMA_VERSION,
        "kind": "filesystem",
        "repo_root": str(repo),
        "captured_at": captured_at,
        "complete": complete,
        "head": "",
        "initial_changes": [],
        "entries": entries,
        "file_count": visited,
    }


def build_task_contract(goal: str) -> dict[str, Any]:
    raw_goal = str(goal or "").strip()
    if len(raw_goal) > 2000:
        raise ValueError("task goal exceeds the 2000 character contract limit")
    clean_goal = raw_goal
    requirements: list[dict[str, Any]] = []
    clauses = _goal_requirement_clauses(clean_goal)
    if len(clauses) > 20:
        raise ValueError("task goal contains more than 20 independently verifiable requirements")
    for clause in clauses:
        digest = hashlib.sha256(_normalize_text(clause).encode("utf-8")).hexdigest()[:8]
        intent, requires_source_change, artifact_role = _goal_intent(clause)
        requirements.append(
            {
                "id": f"R{len(requirements) + 1:02d}-{digest}",
                "text": clause[:500],
                "intent": intent,
                "requires_source_change": requires_source_change,
                "required_artifact_role": artifact_role,
            }
        )
    if not requirements and clean_goal:
        digest = hashlib.sha256(_normalize_text(clean_goal).encode("utf-8")).hexdigest()[:8]
        intent, requires_source_change, artifact_role = _goal_intent(clean_goal)
        requirements.append(
            {
                "id": f"R01-{digest}",
                "text": clean_goal[:500],
                "intent": intent,
                "requires_source_change": requires_source_change,
                "required_artifact_role": artifact_role,
            }
        )
    intents = {str(item.get("intent") or "") for item in requirements if item.get("intent")}
    roles = {
        str(item.get("required_artifact_role") or "")
        for item in requirements
        if item.get("required_artifact_role")
    }
    intent = next(iter(intents)) if len(intents) == 1 else "mixed"
    requires_source_change = any(bool(item.get("requires_source_change")) for item in requirements)
    artifact_role = next(iter(roles)) if len(roles) == 1 else "mixed" if roles else ""
    return {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "goal_digest": hashlib.sha256(_normalize_text(clean_goal).encode("utf-8")).hexdigest(),
        "intent": intent,
        "requires_source_change": requires_source_change,
        "required_artifact_role": artifact_role,
        "requirements": requirements[:20],
    }


def _goal_requirement_clauses(goal: str) -> list[str]:
    clauses: list[str] = []
    for sentence in _CLAUSE_SPLIT.split(str(goal or "")):
        for item in _GOAL_CONNECTOR_SPLIT.split(sentence):
            clean = item.strip(" \t\r\n,，、")
            if clean and _normalize_text(clean):
                clauses.append(clean[:500])
    return _unique_strings(clauses, limit=21, chars=500)


def _goal_intent(goal: str) -> tuple[str, bool, str]:
    normalized = _normalize_text(goal)
    if not normalized:
        return "implementation", True, "implementation"

    chinese_mutation_markers = (
        "修复", "实现", "新增", "添加", "编写", "重构", "优化", "删除", "接入",
        "配置", "创建", "制作", "完善", "升级", "调整", "开发", "更新", "改造", "支持", "补充",
    )
    english_words = set(re.findall(r"[a-z][a-z0-9_-]*", normalized))
    english_mutation_words = {
        "add", "build", "change", "configure", "create", "delete", "develop", "fix",
        "implement", "integrate", "modify", "optimize", "refactor", "support", "update", "write",
    }
    has_mutation = any(marker in normalized for marker in chinese_mutation_markers) or bool(
        english_words & english_mutation_words
    )
    has_test = any(marker in normalized for marker in ("测试", "用例", "回归", "覆盖率")) or bool(
        english_words & {"test", "tests", "spec", "coverage", "regression"}
    )
    add_test_markers = (
        "新增测试", "添加测试", "补充测试", "编写测试", "增加测试", "新增用例", "添加用例",
        "add test", "add tests", "write test", "write tests", "test coverage",
    )
    has_documentation = any(
        marker in normalized for marker in ("文档", "说明书", "使用说明")
    ) or bool(english_words & {"readme", "changelog", "documentation", "docs"})
    has_read_only = any(
        marker in normalized
        for marker in ("只读", "不要修改", "不修改", "无需修改", "不改代码", "审查", "审计", "分析", "调查", "评估", "解释", "给出意见")
    ) or bool(english_words & {"review", "audit", "analyze", "analyse", "investigate", "assess", "explain", "read-only"}) or any(
        phrase in normalized for phrase in ("do not modify", "without changes")
    )
    run_test_markers = (
        "运行现有测试", "执行现有测试", "只运行测试", "只执行测试", "跑现有测试", "运行测试",
        "run existing tests", "execute existing tests", "run tests", "run the tests",
    )

    test_change_action = any(
        marker in normalized
        for marker in (
            "修复", "新增", "添加", "补充", "编写", "增加", "更新", "调整",
            "fix", "add", "write", "update", "extend",
        )
    )
    if any(marker in normalized for marker in add_test_markers) or (has_test and test_change_action):
        return "test_change", True, "test"
    if has_documentation and has_mutation:
        return "documentation_change", True, "documentation"
    if any(marker in normalized for marker in run_test_markers) and not has_mutation:
        return "test_run", False, ""
    if has_read_only and not has_mutation:
        return "read_only", False, ""
    # Unknown natural-language tasks are deliberately treated as implementation work. This
    # prevents an unrecognized verb such as "support"/"支持" from bypassing source evidence.
    if has_test and not has_mutation:
        return "test_run", False, ""
    return "implementation", True, "implementation"


def _artifact_role_for_path(path: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip("/").lower()
    parts = [part for part in normalized.split("/") if part]
    name = parts[-1] if parts else ""
    stem = Path(name).stem.lower()
    suffix = Path(name).suffix.lower()
    if (
        any(part in {"test", "tests", "__tests__"} for part in parts[:-1])
        or name.startswith("test_")
        or stem.endswith(("_test", "_tests"))
        or ".test." in name
        or ".spec." in name
    ):
        return "test"
    if (
        any(part in {"doc", "docs", "documentation"} for part in parts[:-1])
        or name.startswith(("readme", "changelog", "contributing", "license"))
        or suffix in {".md", ".mdx", ".rst", ".adoc"}
    ):
        return "documentation"
    if any(part in {"example", "examples", "fixture", "fixtures", "snapshot", "snapshots", "demo", "demos"} for part in parts[:-1]):
        return "auxiliary"
    return "implementation"


def audit_task_completion(
    *,
    launch_goal: str,
    submitted_goal: str,
    summary: str,
    completion_evidence: Any,
    requested_steps: Any,
    repo_root: str | Path,
    task_contract: Any = None,
    source_baseline: Any = None,
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic review that binds claims to files and verification."""

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    completed_items: list[str] = []
    evidence_items: list[str] = []

    pinned_goal = str(launch_goal or "").strip()[:2000]
    goal = str(submitted_goal or "").strip()[:2000]
    clean_summary = str(summary or "").strip()
    if pinned_goal:
        if _normalize_text(pinned_goal) != _normalize_text(goal):
            _issue(
                errors,
                "goal_mismatch",
                "完成报告中的目标与任务启动时锁定的原目标不一致，Pacer 已阻止按新目标交付。",
            )
    else:
        _issue(
            errors,
            "launch_goal_unavailable",
            "本次启动没有可用的锁定目标，无法机械证明目标未被替换，Pacer 已阻止交付。",
        )
    if not clean_summary or _is_generic_summary(clean_summary):
        _issue(
            errors,
            "summary_generic",
            "结论过于空泛，必须说明实际完成了什么，不能只写“已完成”或“已实现”。",
        )

    expected_contract = build_task_contract(pinned_goal or goal)
    supplied_contract = task_contract if isinstance(task_contract, dict) else None
    if supplied_contract is not None and supplied_contract != expected_contract:
        _issue(
            errors,
            "task_contract_mismatch",
            "任务契约与启动时锁定的原目标不一致，可能已被修改，Pacer 已阻止交付。",
        )
    contract = expected_contract
    contract_requirements = (
        contract.get("requirements") if isinstance(contract.get("requirements"), list) else []
    )
    requirement_index = {
        str(item.get("id") or ""): item
        for item in contract_requirements
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    if not requirement_index:
        _issue(errors, "task_contract_empty", "任务契约没有可验收的需求项。")

    raw_evidence = completion_evidence if isinstance(completion_evidence, dict) else {}
    if not raw_evidence:
        _issue(errors, "completion_evidence_missing", "缺少结构化交付证据。")
    result_kind = str(raw_evidence.get("result_kind") or "").strip().lower()
    if result_kind not in RESULT_KINDS:
        _issue(errors, "result_kind_invalid", "交付类型无效。")
    source_change_required = bool(contract.get("requires_source_change"))
    required_roles = {
        str(item.get("required_artifact_role") or "")
        for item in requirement_index.values()
        if bool(item.get("requires_source_change"))
    }
    test_only_goal = bool(required_roles) and required_roles == {"test"}
    allowed_change_kinds = {"change", "configuration", "test"} if test_only_goal else {"change", "configuration"}
    if source_change_required and result_kind not in allowed_change_kinds:
        _issue(
            errors,
            "result_kind_conflicts_goal",
            "原目标要求修改项目，但交付被声明成只读审查或研究，Pacer 已阻止用类型伪装绕开文件证据。",
        )
    if not source_change_required:
        intent = str(contract.get("intent") or "")
        allowed_read_only_kinds = {"test", "review"} if intent == "test_run" else {"review", "research"}
        if result_kind not in allowed_read_only_kinds:
            _issue(
                errors,
                "result_kind_conflicts_goal",
                "原目标是只读审查或只运行现有测试，交付类型却声称修改了项目。",
            )

    unresolved = _bounded_strings(raw_evidence.get("unresolved_items"), limit=20, chars=400)
    known_risks = _bounded_strings(raw_evidence.get("known_risks"), limit=20, chars=400)
    if unresolved:
        _issue(
            errors,
            "unresolved_items_present",
            "仍有明确未完成事项，任务不能标记为完成。",
            count=len(unresolved),
        )

    step_index, duplicate_steps = _requested_step_index(requested_steps)
    for name in duplicate_steps:
        _issue(
            errors,
            "duplicate_verification_step",
            f"验收步骤名称重复，证据无法唯一绑定：{name}",
            step=name,
        )
    if not step_index:
        _issue(errors, "verification_steps_missing", "没有可供交付声明绑定的验收步骤。")

    verification_index: dict[str, dict[str, Any]] = {}
    final_phase = isinstance(verification, dict)
    verification_status = ""
    if final_phase:
        verification_status = str(verification.get("status") or "")
        records = verification.get("records") if isinstance(verification.get("records"), list) else []
        for record in records:
            if not isinstance(record, dict):
                continue
            name = _step_name(record.get("name"))
            if name and name not in verification_index:
                verification_index[name] = record
        if verification_status != "passed":
            _issue(
                errors,
                "verification_batch_not_passed",
                "最终验收没有全部通过，Pacer 不会认可交付。",
                status=verification_status or "missing",
            )

    repo = Path(repo_root).expanduser().resolve()
    baseline = source_baseline if isinstance(source_baseline, dict) else {}
    baseline_matches_repo = _baseline_matches_repo(baseline, repo)
    baseline_complete = bool(baseline.get("complete")) if baseline_matches_repo else False
    if source_change_required and not baseline_matches_repo:
        _issue(
            errors,
            "source_baseline_missing",
            "缺少任务启动前的源码基线，无法区分本轮修改与用户原有改动，Pacer 已阻止交付。",
        )
    elif source_change_required and not baseline_complete:
        _issue(
            errors,
            "source_baseline_incomplete",
            "任务启动前的源码基线不完整，无法可靠归属本轮修改，Pacer 已阻止交付。",
        )

    raw_claims = raw_evidence.get("claims")
    claims = raw_claims if isinstance(raw_claims, list) else []
    if not claims:
        _issue(errors, "claims_missing", "至少需要一条可核对的交付声明。")
    covered_requirement_ids: set[str] = set()
    role_evidence_by_requirement: dict[str, set[str]] = {
        requirement_id: set() for requirement_id in requirement_index
    }
    observed_file_count = 0
    attributed_file_count = 0
    attributed_implementation_file_count = 0
    observed_step_count = 0

    for index, raw_claim in enumerate(claims[:20]):
        claim_number = index + 1
        claim = raw_claim if isinstance(raw_claim, dict) else {}
        if not claim:
            _issue(errors, "claim_invalid", f"第 {claim_number} 条交付声明格式无效。", claim=claim_number)
            continue
        kind = str(claim.get("kind") or "").strip().lower()
        requirement = str(claim.get("requirement") or "").strip()[:500]
        result = str(claim.get("result") or "").strip()[:1000]
        requirement_ids = _bounded_strings(claim.get("requirement_ids"), limit=20, chars=80)
        valid_requirement_ids: list[str] = []
        if not requirement_ids:
            _issue(
                errors,
                "claim_requirement_ids_missing",
                f"第 {claim_number} 条声明没有引用启动时锁定的需求 ID。",
                claim=claim_number,
            )
        for requirement_id in requirement_ids:
            if requirement_id not in requirement_index:
                _issue(
                    errors,
                    "unknown_requirement_id",
                    f"第 {claim_number} 条声明引用了未知需求 ID：{requirement_id}",
                    claim=claim_number,
                    requirement_id=requirement_id,
                )
                continue
            if requirement_id not in valid_requirement_ids:
                valid_requirement_ids.append(requirement_id)
                covered_requirement_ids.add(requirement_id)
        if len(requirement_ids) != 1 or len(valid_requirement_ids) > 1:
            _issue(
                errors,
                "claim_requirement_id_count_invalid",
                f"第 {claim_number} 条声明必须且只能对应一个锁定需求 ID。",
                claim=claim_number,
            )
        if kind not in CLAIM_KINDS:
            _issue(errors, "claim_kind_invalid", f"第 {claim_number} 条交付声明类型无效。", claim=claim_number)
        if not requirement:
            _issue(errors, "claim_requirement_missing", f"第 {claim_number} 条声明没有对应原目标。", claim=claim_number)
        else:
            referenced_text = str(
                requirement_index[valid_requirement_ids[0]].get("text") or ""
            ) if len(valid_requirement_ids) == 1 else ""
            if referenced_text and _normalize_text(requirement) != _normalize_text(referenced_text):
                _issue(
                    errors,
                    "claim_requirement_mismatch",
                    f"第 {claim_number} 条声明文字与引用的需求 ID 不一致。",
                    claim=claim_number,
                )
        if not result or _is_generic_summary(result):
            _issue(
                errors,
                "claim_result_generic",
                f"第 {claim_number} 条声明没有说明实际结果。",
                claim=claim_number,
            )
        else:
            completed_items.append(result)
            if requirement and not _result_matches_requirement(result, requirement):
                _issue(
                    warnings,
                    "claim_result_low_overlap",
                    f"第 {claim_number} 条结果与对应目标缺少明显文字关联，需要人工关注其语义是否一致。",
                    claim=claim_number,
                )

        raw_files = claim.get("files")
        files = raw_files if isinstance(raw_files, list) else []
        claim_paths_by_role: dict[str, list[str]] = {}
        claim_requires_change = any(
            bool(requirement_index[item].get("requires_source_change"))
            for item in valid_requirement_ids
        )
        if claim_requires_change and not files:
            _issue(
                errors,
                "change_without_files",
                f"第 {claim_number} 条修改声明没有文件证据。",
                claim=claim_number,
            )
        for raw_file in files[:20]:
            item = raw_file if isinstance(raw_file, dict) else {}
            path = str(item.get("path") or "").strip()
            state = str(item.get("state") or "").strip().lower()
            normalized_path, path_error = _safe_evidence_path(repo, path)
            if path_error:
                _issue(
                    errors,
                    path_error,
                    f"第 {claim_number} 条声明包含无效或越界文件路径：{path or '<empty>'}",
                    claim=claim_number,
                    path=path[:300],
                )
                continue
            if state not in FILE_STATES:
                _issue(
                    errors,
                    "file_state_invalid",
                    f"文件 {normalized_path} 的状态无效。",
                    claim=claim_number,
                    path=normalized_path,
                )
                continue
            candidate = (repo / normalized_path).resolve()
            if state in {"created", "modified"} and not candidate.is_file():
                _issue(
                    errors,
                    "evidence_file_missing",
                    f"声明的交付文件不存在：{normalized_path}",
                    claim=claim_number,
                    path=normalized_path,
                )
                continue
            if state == "deleted" and candidate.exists():
                _issue(
                    errors,
                    "deleted_file_still_exists",
                    f"声明已删除的文件仍然存在：{normalized_path}",
                    claim=claim_number,
                    path=normalized_path,
                )
                continue
            observed_file_count += 1
            evidence_items.append(f"文件 {normalized_path}（{_state_label(state)}）")
            artifact_role = _artifact_role_for_path(normalized_path)
            claim_paths_by_role.setdefault(artifact_role, []).append(normalized_path)
            if claim_requires_change:
                changed, attribution_reason = _source_change_observed(
                    repo,
                    normalized_path,
                    state=state,
                    baseline=baseline,
                )
                if changed:
                    attributed_file_count += 1
                    for requirement_id in valid_requirement_ids:
                        role_evidence_by_requirement[requirement_id].add(artifact_role)
                    if artifact_role == "implementation":
                        attributed_implementation_file_count += 1
                else:
                    _issue(
                        errors,
                        "file_change_not_attributed",
                        f"无法证明文件由本轮任务修改：{normalized_path}",
                        claim=claim_number,
                        path=normalized_path,
                        reason=attribution_reason,
                    )
            elif baseline_matches_repo:
                changed, _ = _source_change_observed(
                    repo,
                    normalized_path,
                    state=state,
                    baseline=baseline,
                )
                if changed:
                    attributed_file_count += 1
            elif state in {"created", "modified", "deleted"}:
                _issue(
                    warnings,
                    "read_only_file_attribution_limited",
                    f"只读声明中的文件没有启动基线，不能归属本轮操作：{normalized_path}",
                    claim=claim_number,
                    path=normalized_path,
                )

        verification_refs = _bounded_strings(claim.get("verification_steps"), limit=20, chars=120)
        relevant_acceptance_bound = False
        acceptance_step_seen = False
        if not verification_refs:
            _issue(
                errors,
                "claim_without_verification",
                f"第 {claim_number} 条声明没有绑定验收步骤。",
                claim=claim_number,
            )
        for raw_name in verification_refs:
            name = _step_name(raw_name)
            step = step_index.get(name)
            if step is None:
                _issue(
                    errors,
                    "unknown_verification_step",
                    f"第 {claim_number} 条声明引用了不存在的验收步骤：{raw_name}",
                    claim=claim_number,
                    step=raw_name,
                )
                continue
            if step["step_class"] in ACCEPTANCE_STEP_CLASSES:
                acceptance_step_seen = True
                locked_requirement = (
                    str(requirement_index[valid_requirement_ids[0]].get("text") or "")
                    if len(valid_requirement_ids) == 1
                    else ""
                )
                required_role = (
                    str(requirement_index[valid_requirement_ids[0]].get("required_artifact_role") or "")
                    if len(valid_requirement_ids) == 1
                    else ""
                )
                relevance_files = claim_paths_by_role.get(required_role, []) if required_role else []
                if _verification_step_covers_claim(
                    step,
                    locked_requirement=locked_requirement,
                    files=relevance_files,
                ):
                    relevant_acceptance_bound = True
            claim_requires_test = kind == "test" or any(
                str(requirement_index[item].get("required_artifact_role") or "") == "test"
                for item in valid_requirement_ids
            )
            if claim_requires_test and step["step_class"] != "test":
                _issue(
                    errors,
                    "test_claim_without_test_step",
                    f"第 {claim_number} 条测试声明没有绑定真正的测试步骤。",
                    claim=claim_number,
                    step=raw_name,
                )
            if final_phase:
                record = verification_index.get(name)
                if record is None or str(record.get("status") or "") != "passed":
                    _issue(
                        errors,
                        "verification_step_not_passed",
                        f"声明引用的验收步骤没有通过：{raw_name}",
                        claim=claim_number,
                        step=raw_name,
                    )
                    continue
                observed_step_count += 1
                evidence_items.append(
                    f"验收 {raw_name}：通过，退出码 {record.get('exit_code')}，"
                    f"耗时 {float(record.get('elapsed_seconds') or 0.0):.2f}s"
                )
        if verification_refs and not relevant_acceptance_bound:
            _issue(
                errors,
                "claim_without_relevant_acceptance" if acceptance_step_seen else "claim_without_acceptance",
                (
                    f"第 {claim_number} 条声明绑定的聚焦验收与其文件或需求无明显关联。"
                    if acceptance_step_seen
                    else f"第 {claim_number} 条声明没有绑定 test/build/analyze 验收步骤。"
                ),
                claim=claim_number,
            )

    uncovered_ids = [item for item in requirement_index if item not in covered_requirement_ids]
    if uncovered_ids:
        _issue(
            errors,
            "goal_items_uncovered",
            "原目标仍有内容没有任何交付声明对应。",
            requirement_ids=uncovered_ids[:20],
        )
    for requirement_id, requirement_item in requirement_index.items():
        if not bool(requirement_item.get("requires_source_change")):
            continue
        observed_roles = role_evidence_by_requirement.get(requirement_id) or set()
        required_role = str(requirement_item.get("required_artifact_role") or "implementation")
        if not observed_roles:
            _issue(
                errors,
                "requirement_change_not_proven",
                f"需求 {requirement_id} 没有可归属到本轮的文件变化。",
                requirement_id=requirement_id,
            )
        elif required_role not in observed_roles:
            _issue(
                errors,
                "required_artifact_role_missing",
                f"需求 {requirement_id} 必须有{_artifact_role_label(required_role)}证据，现有文件类型不符合。",
                requirement_id=requirement_id,
                required_role=required_role,
                observed_roles=sorted(observed_roles),
            )
    if source_change_required and attributed_file_count <= 0:
        _issue(
            errors,
            "source_change_not_proven",
            "原目标要求修改项目，但没有任何文件被证明是在本轮启动后发生变化。",
        )
    if source_change_required and not test_only_goal and attributed_implementation_file_count <= 0:
        _issue(
            errors,
            "implementation_change_not_proven",
            "原目标要求实现或修复产品，但证据只有测试/审查文件，没有任何产品实现文件可归属本轮。",
        )

    errors = _dedupe_issues(errors)
    warnings = _dedupe_issues(warnings)
    valid = not errors
    trust = "no"
    if valid and final_phase:
        trust = "with_limits" if warnings else "yes"
    elif valid:
        trust = "with_limits"
    verdict = "approved" if valid else "rejected"
    headline = (
        "审查通过，可以按已列出的证据交付。"
        if trust == "yes"
        else "审查通过，但存在需要用户知道的证据边界。"
        if valid
        else "审查未通过，Pacer 不会把本次任务标记为完成。"
    )
    next_action = (
        "可以交付；后续结论不得超出下列已验证内容。"
        if valid and final_phase
        else "补齐或修正阻断项后重新验收，不要绕过审查。"
    )
    review = {
        "schema_version": TASK_REVIEW_SCHEMA_VERSION,
        "kind": "pacer_task_review",
        "phase": "final" if final_phase else "preflight",
        "valid": valid,
        "verdict": verdict,
        "trust": trust,
        "goal_binding": {
            "pinned": bool(pinned_goal),
            "matches": bool(pinned_goal and _normalize_text(pinned_goal) == _normalize_text(goal)),
        },
        "task_contract": contract,
        "result_kind": result_kind,
        "claim_count": min(len(claims), 20),
        "observed_file_count": observed_file_count,
        "attributed_file_count": attributed_file_count,
        "attributed_implementation_file_count": attributed_implementation_file_count,
        "observed_verification_step_count": observed_step_count,
        "errors": errors[:12],
        "warnings": warnings[:12],
        "user_report": {
            "headline": headline,
            "goal": (pinned_goal or goal)[:1000],
            "completed": _unique_strings(completed_items, limit=10, chars=300),
            "not_completed": unresolved[:10],
            "evidence": _unique_strings(evidence_items, limit=16, chars=300),
            "blocking_issues": _unique_strings(
                [str(item.get("message") or "") for item in errors],
                limit=12,
                chars=300,
            ),
            "risks": _unique_strings(
                [*known_risks, *[str(item.get("message") or "") for item in warnings]],
                limit=12,
                chars=300,
            ),
            "can_trust": trust,
            "next_action": next_action,
        },
    }
    review["user_report_markdown"] = task_review_to_markdown(review)
    return review


def task_review_to_markdown(review: dict[str, Any]) -> str:
    report = review.get("user_report") if isinstance(review.get("user_report"), dict) else {}
    trust = str(report.get("can_trust") or review.get("trust") or "no")
    trust_label = {"yes": "可信", "with_limits": "有限可信", "no": "不可信"}.get(trust, "不可信")
    lines = [
        f"Pacer 任务审查：{str(report.get('headline') or '没有可用结论')}",
        f"目标：{str(report.get('goal') or '未记录')}",
        "实际完成：",
        *_markdown_items(report.get("completed"), empty="无可核对的完成项"),
        "未完成：",
        *_markdown_items(report.get("not_completed"), empty="无已知未完成项"),
        "证据：",
        *_markdown_items(report.get("evidence"), empty="无可核对证据"),
        "阻断原因：",
        *_markdown_items(report.get("blocking_issues"), empty="无阻断项"),
        "风险与边界：",
        *_markdown_items(report.get("risks"), empty="无已知风险"),
        f"可信结论：{trust_label}",
        f"下一步：{str(report.get('next_action') or '继续核对任务证据。')}",
    ]
    return "\n".join(lines)


def task_review_error(review: dict[str, Any]) -> str:
    errors = review.get("errors") if isinstance(review.get("errors"), list) else []
    messages = [str(item.get("message") or item.get("code") or "") for item in errors if isinstance(item, dict)]
    return "completion audit rejected: " + " | ".join(messages[:6])


def _requested_step_index(raw_steps: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    steps = raw_steps if isinstance(raw_steps, list) else []
    result: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    for index, raw_step in enumerate(steps[:20]):
        step = raw_step if isinstance(raw_step, dict) else {}
        display_name = str(step.get("name") or f"step-{index + 1}").strip()
        name = _step_name(display_name)
        argv = [str(value) for value in step.get("argv") or []] if isinstance(step.get("argv"), list) else []
        if name in result:
            duplicates.append(display_name)
            continue
        result[name] = {
            "name": display_name,
            "argv": argv,
            "step_class": classify_verification_step(argv),
        }
    return result, duplicates


def _verification_step_covers_claim(
    step: dict[str, Any],
    *,
    locked_requirement: str,
    files: list[str],
) -> bool:
    argv = [str(value) for value in step.get("argv") or []]
    lowered = [value.strip().lower().replace("\\", "/") for value in argv[1:]]
    if not lowered:
        return True
    if "discover" in lowered or any(value in {".", "./...", "--all"} for value in lowered):
        return True

    command_words = {
        "-m", "analyze", "build", "check", "lint", "pytest", "run", "test", "unittest",
    }
    focus_values: list[str] = []
    capture_next = False
    after_separator = False
    for value in lowered:
        if capture_next:
            focus_values.append(value)
            capture_next = False
            continue
        if value == "--":
            after_separator = True
            continue
        if value in {"-k", "--filter", "--test-name-pattern", "--tests"}:
            capture_next = True
            continue
        if any(value.startswith(f"{flag}=") for flag in ("-k", "--filter", "--test-name-pattern", "--tests")):
            focus_values.append(value.split("=", 1)[1])
            continue
        if value.startswith("-"):
            continue
        if value in command_words:
            continue
        if after_separator or value:
            focus_values.append(value)
    if not focus_values:
        return True

    focus_terms = _relevance_terms(focus_values)
    if not focus_terms:
        # A target such as the whole tests/ directory is broad even though its only term is generic.
        return True
    claim_terms = _relevance_terms([locked_requirement, *files])
    return bool(focus_terms & claim_terms)


def _relevance_terms(values: list[str]) -> set[str]:
    ignored = {
        "app", "build", "check", "dart", "go", "lib", "main", "package", "project", "py",
        "pytest", "src", "test", "tests", "testing", "unittest", "verify", "verification",
    }
    terms: set[str] = set()
    for value in values:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]{2,}", normalized):
            if token in ignored or token.isdigit() or len(token) < 2:
                continue
            terms.add(token)
    return terms


def _safe_evidence_path(repo: Path, raw_path: str) -> tuple[str, str]:
    value = str(raw_path or "").strip().replace("\\", "/")
    if not value:
        return "", "file_path_missing"
    candidate_path = Path(value)
    if candidate_path.is_absolute() or candidate_path.drive or value.startswith(("//", "\\\\")):
        return value[:300], "file_path_outside_repo"
    if any(part == ".." for part in candidate_path.parts):
        return value[:300], "file_path_outside_repo"
    try:
        resolved = (repo / candidate_path).resolve()
        relative = resolved.relative_to(repo)
    except (OSError, ValueError):
        return value[:300], "file_path_outside_repo"
    return relative.as_posix(), ""


def _git_changed_files(repo: Path) -> set[str] | None:
    probe = _run_git(repo, ["rev-parse", "--is-inside-work-tree"])
    if probe is None or probe.strip() != "true":
        return None
    changed: set[str] = set()
    for args in (
        ["diff", "--relative", "--name-only", "-z"],
        ["diff", "--relative", "--cached", "--name-only", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        output = _run_git(repo, args)
        if output is None:
            return None
        changed.update(_path_key(value) for value in output.split("\0") if value)
    return changed


def _baseline_matches_repo(baseline: dict[str, Any], repo: Path) -> bool:
    if int(baseline.get("schema_version") or 0) != SOURCE_BASELINE_SCHEMA_VERSION:
        return False
    if str(baseline.get("kind") or "") not in {"git", "filesystem"}:
        return False
    raw_root = str(baseline.get("repo_root") or "").strip()
    if not raw_root:
        return False
    try:
        recorded = Path(raw_root).expanduser().resolve()
    except OSError:
        return False
    return os.path.normcase(str(recorded)) == os.path.normcase(str(repo))


def _source_change_observed(
    repo: Path,
    path: str,
    *,
    state: str,
    baseline: dict[str, Any],
) -> tuple[bool, str]:
    if not _baseline_matches_repo(baseline, repo):
        return False, "baseline_missing"
    if not bool(baseline.get("complete")):
        return False, "baseline_incomplete"
    key = _path_key(path)
    entries = baseline.get("entries") if isinstance(baseline.get("entries"), dict) else {}
    baseline_fingerprint = str(entries.get(key) or "")
    current_fingerprint = _file_fingerprint(repo / Path(path))

    if str(baseline.get("kind") or "") == "filesystem":
        existed = bool(baseline_fingerprint and baseline_fingerprint != "missing")
        return _fingerprint_change_matches_state(
            baseline_fingerprint=baseline_fingerprint,
            current_fingerprint=current_fingerprint,
            existed_at_launch=existed,
            state=state,
        )

    if baseline_fingerprint:
        existed = baseline_fingerprint != "missing"
        return _fingerprint_change_matches_state(
            baseline_fingerprint=baseline_fingerprint,
            current_fingerprint=current_fingerprint,
            existed_at_launch=existed,
            state=state,
        )

    current_changes = _git_changed_files(repo)
    if current_changes is None:
        return False, "git_changes_unavailable"
    committed_changes = _git_committed_changes(repo, str(baseline.get("head") or ""))
    if key not in current_changes and key not in committed_changes:
        return False, "not_changed_since_launch"
    existed_at_launch = _git_path_existed_at_head(
        repo,
        str(baseline.get("head") or ""),
        path,
        git_prefix=str(baseline.get("git_prefix") or ""),
    )
    return _state_matches_existence(
        existed_at_launch=existed_at_launch,
        exists_now=current_fingerprint != "missing",
        state=state,
    )


def _fingerprint_change_matches_state(
    *,
    baseline_fingerprint: str,
    current_fingerprint: str,
    existed_at_launch: bool,
    state: str,
) -> tuple[bool, str]:
    if baseline_fingerprint == current_fingerprint:
        return False, "content_unchanged_since_launch"
    matches, reason = _state_matches_existence(
        existed_at_launch=existed_at_launch,
        exists_now=current_fingerprint != "missing",
        state=state,
    )
    return (True, "changed_since_launch") if matches else (False, reason)


def _state_matches_existence(*, existed_at_launch: bool, exists_now: bool, state: str) -> tuple[bool, str]:
    expected = (
        "created"
        if not existed_at_launch and exists_now
        else "deleted"
        if existed_at_launch and not exists_now
        else "modified"
        if existed_at_launch and exists_now
        else "none"
    )
    if state != expected:
        return False, f"declared_{state}_but_observed_{expected}"
    return True, "changed_since_launch"


def _git_committed_changes(repo: Path, baseline_head: str) -> set[str]:
    head = str(baseline_head or "").strip()
    if not head:
        return set()
    current_head = str(_run_git(repo, ["rev-parse", "HEAD"]) or "").strip()
    if not current_head or current_head == head:
        return set()
    output = _run_git(repo, ["diff", "--relative", "--name-only", "-z", f"{head}..{current_head}"])
    if output is None:
        return set()
    return {_path_key(value) for value in output.split("\0") if value}


def _git_path_existed_at_head(
    repo: Path,
    baseline_head: str,
    path: str,
    *,
    git_prefix: str = "",
) -> bool:
    head = str(baseline_head or "").strip()
    if not head:
        return False
    normalized = str(path or "").replace("\\", "/")
    prefix = str(git_prefix or "").strip().replace("\\", "/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return _run_git(repo, ["cat-file", "-e", f"{head}:{prefix}{normalized}"]) is not None


def _file_fingerprint(path: Path) -> str:
    try:
        if not path.exists():
            return "missing"
        if path.is_symlink():
            stat = path.lstat()
            return f"symlink:{os.readlink(path)}:{stat.st_mtime_ns}"
        if not path.is_file():
            stat = path.stat()
            return f"other:{stat.st_size}:{stat.st_mtime_ns}"
        stat = path.stat()
        if path.suffix.lower() in _HASHABLE_SOURCE_SUFFIXES and stat.st_size <= MAX_HASH_BYTES:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return f"sha256:{digest.hexdigest()}"
        return f"stat:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return "unavailable"


def _goal_requires_source_change(goal: str) -> bool:
    normalized = _normalize_text(goal)
    read_only_markers = (
        "只读",
        "不要修改",
        "不修改",
        "无需修改",
        "不改代码",
        "read-only",
        "do not modify",
        "without changes",
    )
    strong_markers = (
        "修复",
        "实现",
        "新增",
        "添加",
        "重构",
        "优化",
        "删除",
        "接入",
        "配置",
        "创建",
        "制作",
        "完善",
        "升级",
        "调整",
        "开发",
    )
    english_change = re.search(
        r"\b(add|build|change|configure|create|delete|develop|fix|implement|integrate|modify|optimi[sz]e|refactor|update)\b",
        normalized,
    )
    strong = any(marker in normalized for marker in strong_markers) or english_change is not None
    if any(marker in normalized for marker in read_only_markers) and not strong:
        return False
    return strong


def _goal_is_test_only(goal: str) -> bool:
    normalized = _normalize_text(goal)
    test_markers = ("测试", "验收", "回归", "覆盖率", "test", "tests", "coverage", "regression")
    implementation_markers = (
        "修复",
        "实现",
        "开发",
        "重构",
        "优化",
        "配置",
        "接入",
        "页面",
        "功能",
        "fix",
        "implement",
        "develop",
        "refactor",
        "optimize",
        "configure",
        "integrate",
        "feature",
    )
    has_test = any(marker in normalized for marker in test_markers)
    has_implementation = any(marker in normalized for marker in implementation_markers)
    return has_test and not has_implementation


def _run_git(repo: Path, args: list[str]) -> str | None:
    try:
        completed = _SUBPROCESS_RUN(
            ["git", "-C", str(repo), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _uncovered_goal_clauses(goal: str, requirements: list[str]) -> list[str]:
    clauses = [item.strip() for item in _CLAUSE_SPLIT.split(str(goal or "")) if item.strip()]
    uncovered: list[str] = []
    for clause in clauses[:20]:
        if len(_normalize_text(clause)) < 4:
            continue
        if not any(_text_related(clause, requirement) for requirement in requirements):
            uncovered.append(clause[:240])
    return uncovered


def _text_related(left: str, right: str) -> bool:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    if len(normalized_left) >= 4 and normalized_left in normalized_right:
        return True
    left_tokens = _semantic_tokens(normalized_left)
    right_tokens = _semantic_tokens(normalized_right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    if len(left_tokens) == 1 and len(right_tokens) > 2:
        return False
    required = 1 if len(left_tokens) == 1 else max(2, (len(left_tokens) + 1) // 2)
    return overlap >= required


def _result_matches_requirement(result: str, requirement: str) -> bool:
    normalized_result = _normalize_text(result)
    normalized_requirement = _normalize_text(requirement)
    if len(normalized_requirement) >= 4 and normalized_requirement in normalized_result:
        return True
    return _text_related(result, requirement)


def _semantic_tokens(value: str) -> set[str]:
    tokens = {
        token
        for token in _LATIN_TOKEN.findall(value)
        if len(token) >= 2 and token not in _STOP_TOKENS
    }
    for sequence in _CJK_SEQUENCE.findall(value):
        if len(sequence) == 1:
            continue
        tokens.update(
            sequence[index : index + 2]
            for index in range(len(sequence) - 1)
            if sequence[index : index + 2] not in _STOP_TOKENS
        )
    return tokens


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return " ".join(normalized.split())


def _is_generic_summary(value: str) -> bool:
    normalized = _normalize_text(value)
    compact = re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)
    return normalized in GENERIC_SUMMARIES or compact in GENERIC_SUMMARIES


def _bounded_strings(value: Any, *, limit: int, chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:chars] for item in value[:limit] if str(item).strip()]


def _unique_strings(values: list[str], *, limit: int, chars: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()[:chars]
        identity = _normalize_text(clean)
        if not clean or identity in seen:
            continue
        seen.add(identity)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def _markdown_items(value: Any, *, empty: str) -> list[str]:
    items = value if isinstance(value, list) else []
    cleaned = [str(item).strip().replace("\n", " ") for item in items if str(item).strip()]
    return [f"- {item}" for item in cleaned] if cleaned else [f"- {empty}"]


def _step_name(value: Any) -> str:
    return _normalize_text(str(value or ""))


def _path_key(value: str) -> str:
    return os.path.normcase(str(value or "").replace("\\", "/")).casefold()


def _state_label(state: str) -> str:
    return {"created": "新增", "modified": "修改", "deleted": "删除"}.get(state, state)


def _artifact_role_label(role: str) -> str:
    return {
        "implementation": "产品实现文件",
        "test": "测试文件",
        "documentation": "文档文件",
        "auxiliary": "辅助文件",
    }.get(role, "指定类型文件")


def _issue(target: list[dict[str, Any]], code: str, message: str, **details: Any) -> None:
    target.append({"code": code, "message": message, **details})


def _dedupe_issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        identity = (str(item.get("code") or ""), str(item.get("message") or ""))
        if identity in seen:
            continue
        seen.add(identity)
        result.append(item)
    return result
