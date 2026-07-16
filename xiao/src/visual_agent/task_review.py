from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .acceptance_contract import assess_acceptance_contract, build_acceptance_contract
from .pacer_verification import classify_verification_step
from .subprocess_window import hidden_subprocess_kwargs


TASK_REVIEW_SCHEMA_VERSION = 2
TASK_CONTRACT_SCHEMA_VERSION = 2
SOURCE_BASELINE_SCHEMA_VERSION = 1
MAX_BASELINE_FILES = 10_000
MAX_COMPLETION_FILES = 200
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
_EXPLICIT_REQUIREMENT_ITEM = re.compile(
    r"^\s*(?:[-*]|\d{1,2}[.)、])\s+(?P<text>.+?)\s*$"
)
_REQUIREMENT_HEADER = re.compile(
    r"^\s*(?:requirements?|acceptance criteria|任务要求|需求|要求)\s*[:：]?\s*$",
    flags=re.IGNORECASE,
)
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
_SOURCE_PATH_TOKEN = re.compile(
    r"(?<![\w.-])([\w@+./\\-]+\.(?:bat|c|cc|cpp|css|dart|go|h|hpp|html|java|js|json|jsx|kt|kts|"
    r"md|mdx|ps1|py|rs|rst|scss|sh|sql|toml|ts|tsx|yaml|yml))(?![\w.-])",
    flags=re.IGNORECASE,
)
_NEGATED_MUTATION_MARKERS = (
    "不要修改",
    "不得修改",
    "不能修改",
    "无需修改",
    "无须修改",
    "禁止修改",
    "切勿修改",
    "do not modify",
    "don't modify",
    "must not modify",
    "without modifying",
)


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


def build_task_contract(goal: str, *, repo_root: str | Path | None = None) -> dict[str, Any]:
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
    _inherit_requirement_artifact_context(requirements)
    intents = {str(item.get("intent") or "") for item in requirements if item.get("intent")}
    roles = {
        str(item.get("required_artifact_role") or "")
        for item in requirements
        if item.get("required_artifact_role")
    }
    intent = next(iter(intents)) if len(intents) == 1 else "mixed"
    requires_source_change = any(bool(item.get("requires_source_change")) for item in requirements)
    artifact_role = next(iter(roles)) if len(roles) == 1 else "mixed" if roles else ""
    protected_paths = _protected_paths_from_requirements(requirements)
    contract = {
        "schema_version": TASK_CONTRACT_SCHEMA_VERSION,
        "goal_digest": hashlib.sha256(_normalize_text(clean_goal).encode("utf-8")).hexdigest(),
        "intent": intent,
        "requires_source_change": requires_source_change,
        "required_artifact_role": artifact_role,
        "protected_paths": protected_paths,
        "requirements": requirements[:20],
    }
    acceptance_contract = build_acceptance_contract(
        goal=clean_goal,
        task_contract=contract,
        repo_root=repo_root,
    )
    acceptance_protected = [
        str(item) for item in acceptance_contract.get("protected_paths") or [] if str(item)
    ]
    source_path = str(acceptance_contract.get("source_path") or "")
    contract["protected_paths"] = list(
        dict.fromkeys([*protected_paths, *acceptance_protected, *([source_path] if source_path else [])])
    )[:20]
    contract["acceptance_contract"] = acceptance_contract
    return contract


def _inherit_requirement_artifact_context(requirements: list[dict[str, Any]]) -> None:
    """Preserve an explicit documentation target across adjacent content clauses."""

    previous_role = ""
    for requirement in requirements:
        role = str(requirement.get("required_artifact_role") or "")
        text = str(requirement.get("text") or "")
        if (
            previous_role == "documentation"
            and role == "implementation"
            and _looks_like_documentation_content(text)
        ):
            requirement["intent"] = "documentation_change"
            requirement["requires_source_change"] = True
            requirement["required_artifact_role"] = "documentation"
            role = "documentation"
        if bool(requirement.get("requires_source_change")):
            previous_role = role


def _looks_like_documentation_content(text: str) -> bool:
    normalized = _normalize_text(text)
    chinese_markers = (
        "小节",
        "章节",
        "段落",
        "标题",
        "运行方式",
        "使用方式",
        "启动说明",
        "安装说明",
        "命令示例",
        "代码块",
        "说明文字",
    )
    if any(marker in normalized for marker in chinese_markers):
        return True
    words = set(re.findall(r"[a-z][a-z0-9_-]*", normalized))
    return bool(words & {"heading", "instructions", "paragraph", "quickstart", "section", "usage"})


def task_contract_allows_compile_only(task_contract: Any) -> bool:
    """Allow compile-only acceptance only for a source-changing documentation task."""

    contract = task_contract if isinstance(task_contract, dict) else {}
    requirements = contract.get("requirements") if isinstance(contract.get("requirements"), list) else []
    changed_roles = {
        str(item.get("required_artifact_role") or "")
        for item in requirements
        if isinstance(item, dict) and bool(item.get("requires_source_change"))
    }
    return bool(contract.get("requires_source_change")) and changed_roles == {"documentation"}


def _goal_requirement_clauses(goal: str) -> list[str]:
    raw_goal = str(goal or "")
    lines = raw_goal.splitlines()
    if any(_EXPLICIT_REQUIREMENT_ITEM.match(line) for line in lines):
        clauses: list[str] = []
        prose: list[str] = []

        def flush_prose() -> None:
            if not prose:
                return
            clauses.extend(_split_goal_prose(" ".join(prose)))
            prose.clear()

        for line in lines:
            item = _EXPLICIT_REQUIREMENT_ITEM.match(line)
            if item:
                flush_prose()
                text = item.group("text").strip(" \t\r\n,，、")
                if text and _normalize_text(text):
                    clauses.append(text[:500])
                continue
            if _REQUIREMENT_HEADER.match(line):
                flush_prose()
                continue
            if not line.strip():
                flush_prose()
                continue
            prose.append(line.strip())
        flush_prose()
        return _unique_strings(clauses, limit=21, chars=500)

    return _split_goal_prose(raw_goal)


def _split_goal_prose(goal: str) -> list[str]:
    clauses: list[str] = []
    for sentence in _CLAUSE_SPLIT.split(str(goal or "")):
        for item in _GOAL_CONNECTOR_SPLIT.split(sentence):
            clean = item.strip(" \t\r\n,，、")
            if clean and _normalize_text(clean):
                clauses.append(clean[:500])
    return _unique_strings(clauses, limit=21, chars=500)


def _protected_paths_from_requirements(requirements: list[dict[str, Any]]) -> list[str]:
    protected: list[str] = []
    for requirement in requirements:
        if str(requirement.get("intent") or "") != "read_only":
            continue
        text = str(requirement.get("text") or "")
        normalized = _normalize_text(text)
        if not any(marker in normalized for marker in _NEGATED_MUTATION_MARKERS):
            continue
        for match in _SOURCE_PATH_TOKEN.finditer(text):
            path = match.group(1).replace("\\", "/").strip("`'\"")
            if path and path not in protected:
                protected.append(path)
    return protected[:20]


def _goal_intent(goal: str) -> tuple[str, bool, str]:
    normalized = _normalize_text(goal)
    if not normalized:
        return "implementation", True, "implementation"

    mutation_text = normalized
    for negated_mutation in _NEGATED_MUTATION_MARKERS:
        mutation_text = mutation_text.replace(negated_mutation, "")

    chinese_mutation_markers = (
        "修复", "实现", "新增", "添加", "编写", "重构", "优化", "删除", "接入",
        "配置", "创建", "制作", "完善", "升级", "调整", "开发", "更新", "修改", "增加",
        "改造", "支持", "补充",
    )
    english_words = set(re.findall(r"[a-z][a-z0-9_-]*", normalized))
    mutation_english_words = set(re.findall(r"[a-z][a-z0-9_-]*", mutation_text))
    english_mutation_words = {
        "add", "build", "change", "configure", "create", "delete", "develop", "fix",
        "implement", "integrate", "modify", "optimize", "refactor", "support", "update", "write",
    }
    has_mutation = any(marker in mutation_text for marker in chinese_mutation_markers) or bool(
        mutation_english_words & english_mutation_words
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
    test_is_acceptance_target = bool(
        re.search(
            r"(?:使|让|确保|以便|直到).{0,80}(?:测试|用例|tests?|spec).{0,40}(?:通过|成功|pass(?:es|ed)?)",
            normalized,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\bso(?:\s+that)?\b.{0,80}\b(?:tests?|specs?)\b.{0,40}\bpass(?:es|ed)?\b",
            normalized,
            flags=re.IGNORECASE,
        )
    )
    if any(marker in normalized for marker in add_test_markers) or (
        has_test and test_change_action and not test_is_acceptance_target
    ):
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


def derive_task_completion_evidence(
    *,
    completion_evidence: Any,
    repo_root: str | Path,
    task_contract: Any,
    source_baseline: Any,
) -> dict[str, Any]:
    """Replace model-authored file facts with evidence derived from the launch baseline."""

    raw = completion_evidence if isinstance(completion_evidence, dict) else {}
    contract = task_contract if isinstance(task_contract, dict) else {}
    requirements = contract.get("requirements") if isinstance(contract.get("requirements"), list) else []
    requirement_index = {
        str(item.get("id") or ""): item
        for item in requirements
        if isinstance(item, dict) and str(item.get("id") or "")
    }
    change_set = derive_task_source_changes(
        repo_root=repo_root,
        source_baseline=source_baseline,
    )
    changes = change_set.get("changes") if isinstance(change_set.get("changes"), list) else []
    source_change_issues = [
        {
            "code": "source_change_detection_failed",
            "message": "Pacer 无法完整推导任务启动后的源码变化，已按失败关闭处理。",
            "reason": str(reason),
        }
        for reason in change_set.get("errors") or []
    ]
    source_change_issues.extend(_task_source_scope_issues(contract, changes))

    raw_claims = raw.get("claims") if isinstance(raw.get("claims"), list) else []
    claims: list[dict[str, Any]] = []
    legacy_fields_ignored: set[str] = set()
    if "result_kind" in raw:
        legacy_fields_ignored.add("result_kind")
    for raw_claim in raw_claims[:20]:
        claim = raw_claim if isinstance(raw_claim, dict) else {}
        requirement_ids = _bounded_strings(claim.get("requirement_ids"), limit=20, chars=80)
        requirement = requirement_index.get(requirement_ids[0]) if len(requirement_ids) == 1 else None
        if any(field in claim for field in ("kind", "requirement", "files")):
            legacy_fields_ignored.update(field for field in ("kind", "requirement", "files") if field in claim)
        files = _derived_files_for_requirement(requirement, changes, contract=contract)
        claims.append(
            {
                "kind": _derived_claim_kind(requirement, contract=contract),
                "requirement_ids": requirement_ids,
                "requirement": str(requirement.get("text") or "") if isinstance(requirement, dict) else "",
                "result": str(claim.get("result") or "").strip()[:1000],
                "files": files,
                "verification_steps": _bounded_strings(
                    claim.get("verification_steps"), limit=20, chars=120
                ),
            }
        )
    return {
        "schema_version": 2,
        "evidence_origin": "server_derived",
        "result_kind": _derived_result_kind(contract),
        "claims": claims,
        "unresolved_items": _bounded_strings(raw.get("unresolved_items"), limit=20, chars=400),
        "known_risks": _bounded_strings(raw.get("known_risks"), limit=20, chars=400),
        "source_changes": changes,
        "source_change_complete": bool(change_set.get("complete")),
        "source_change_issues": source_change_issues,
        "legacy_fields_ignored": sorted(legacy_fields_ignored),
    }


def derive_task_source_changes(
    *,
    repo_root: str | Path,
    source_baseline: Any,
) -> dict[str, Any]:
    """Return bounded launch-attributed file facts using Git's machine formats when available."""

    repo = Path(repo_root).expanduser().resolve()
    baseline = source_baseline if isinstance(source_baseline, dict) else {}
    if not _baseline_matches_repo(baseline, repo):
        return {"complete": False, "changes": [], "errors": ["baseline_missing"]}
    if not bool(baseline.get("complete")):
        return {"complete": False, "changes": [], "errors": ["baseline_incomplete"]}
    if str(baseline.get("kind") or "") == "git":
        return _derive_git_source_changes(repo, baseline)
    return _derive_filesystem_source_changes(repo, baseline)


def _derive_git_source_changes(repo: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    prefix = str(_run_git(repo, ["rev-parse", "--show-prefix"]) or "").strip().replace("\\", "/")
    status = _run_git(repo, ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--", "."])
    if status is None:
        return {"complete": False, "changes": [], "errors": ["git_status_unavailable"]}
    current_paths, rename_pairs, status_errors = _parse_git_porcelain_v2(status, git_prefix=prefix)
    errors.extend(status_errors)

    baseline_head = str(baseline.get("head") or "").strip()
    current_head = str(_run_git(repo, ["rev-parse", "HEAD"]) or "").strip()
    committed_paths: set[str] = set()
    if baseline_head and current_head and baseline_head != current_head:
        committed = _run_git(
            repo,
            ["diff", "--relative", "--name-status", "-z", "--find-renames", f"{baseline_head}..{current_head}", "--", "."],
        )
        if committed is None:
            errors.append("git_committed_diff_unavailable")
        else:
            parsed_paths, parsed_renames, parse_errors = _parse_git_name_status_z(committed)
            committed_paths.update(parsed_paths)
            rename_pairs.update(parsed_renames)
            errors.extend(parse_errors)

    entries = baseline.get("entries") if isinstance(baseline.get("entries"), dict) else {}
    candidates: dict[str, str] = {}
    for path in [*current_paths, *committed_paths, *entries.keys()]:
        normalized = str(path or "").replace("\\", "/").strip("/")
        if normalized and not _ignored_source_path(normalized):
            candidates.setdefault(_path_key(normalized), normalized)
    observed_keys = {_path_key(item) for item in current_paths | committed_paths}

    changes: list[dict[str, Any]] = []
    for key, path in candidates.items():
        baseline_fingerprint = str(entries.get(key) or "")
        current_fingerprint = _file_fingerprint(repo / Path(path))
        if baseline_fingerprint:
            if baseline_fingerprint == current_fingerprint:
                continue
            existed_at_launch = baseline_fingerprint != "missing"
        else:
            if key not in observed_keys:
                continue
            existed_at_launch = _git_path_existed_at_head(
                repo,
                baseline_head,
                path,
                git_prefix=str(baseline.get("git_prefix") or ""),
            )
        exists_now = current_fingerprint != "missing"
        state = _state_from_existence(existed_at_launch=existed_at_launch, exists_now=exists_now)
        if state == "none":
            continue
        change = {
            "path": path,
            "state": state,
            "artifact_role": _artifact_role_for_path(path),
        }
        for old_path, new_path in rename_pairs.items():
            if _path_key(path) == _path_key(old_path):
                change["renamed_to"] = new_path
            elif _path_key(path) == _path_key(new_path):
                change["renamed_from"] = old_path
        changes.append(change)

    changes.sort(key=lambda item: str(item.get("path") or ""))
    if len(changes) > MAX_COMPLETION_FILES:
        errors.append(f"source_change_limit_exceeded:{len(changes)}")
        changes = changes[:MAX_COMPLETION_FILES]
    return {"complete": not errors, "changes": changes, "errors": list(dict.fromkeys(errors))}


def _derive_filesystem_source_changes(repo: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    current_entries, current_paths, complete = _scan_filesystem_source(repo)
    baseline_entries = baseline.get("entries") if isinstance(baseline.get("entries"), dict) else {}
    changes: list[dict[str, Any]] = []
    for key in sorted(set(baseline_entries) | set(current_entries)):
        before = str(baseline_entries.get(key) or "missing")
        after = str(current_entries.get(key) or "missing")
        if before == after:
            continue
        path = str(current_paths.get(key) or key)
        state = _state_from_existence(
            existed_at_launch=before != "missing",
            exists_now=after != "missing",
        )
        if state == "none" or _ignored_source_path(path):
            continue
        changes.append(
            {"path": path, "state": state, "artifact_role": _artifact_role_for_path(path)}
        )
    errors: list[str] = []
    if not complete:
        errors.append("filesystem_scan_incomplete")
    if len(changes) > MAX_COMPLETION_FILES:
        errors.append(f"source_change_limit_exceeded:{len(changes)}")
        changes = changes[:MAX_COMPLETION_FILES]
    return {"complete": not errors, "changes": changes, "errors": errors}


def _scan_filesystem_source(repo: Path) -> tuple[dict[str, str], dict[str, str], bool]:
    entries: dict[str, str] = {}
    paths: dict[str, str] = {}
    visited = 0
    complete = True
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
                key = _path_key(relative)
                entries[key] = _file_fingerprint(path)
                paths[key] = relative
            if not complete:
                break
    except OSError:
        complete = False
    return entries, paths, complete


def _parse_git_porcelain_v2(
    output: str,
    *,
    git_prefix: str,
) -> tuple[set[str], dict[str, str], list[str]]:
    records = output.split("\0")
    paths: set[str] = set()
    renames: dict[str, str] = {}
    errors: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or record.startswith("# "):
            continue
        path = ""
        if record.startswith("1 "):
            fields = record.split(" ", 8)
            path = fields[8] if len(fields) == 9 else ""
        elif record.startswith("2 "):
            fields = record.split(" ", 9)
            path = fields[9] if len(fields) == 10 else ""
            original = records[index] if index < len(records) else ""
            index += 1
            new_path = _task_relative_git_path(path, git_prefix)
            old_path = _task_relative_git_path(original, git_prefix)
            if old_path and new_path:
                renames[old_path] = new_path
                paths.add(old_path)
        elif record.startswith("u "):
            fields = record.split(" ", 10)
            path = fields[10] if len(fields) == 11 else ""
            errors.append("git_unmerged_source_state")
        elif record.startswith("? "):
            path = record[2:]
        elif record.startswith("! "):
            continue
        else:
            errors.append("git_status_record_unrecognized")
            continue
        relative = _task_relative_git_path(path, git_prefix)
        if relative:
            paths.add(relative)
    return paths, renames, list(dict.fromkeys(errors))


def _parse_git_name_status_z(output: str) -> tuple[set[str], dict[str, str], list[str]]:
    fields = [field for field in output.split("\0") if field]
    paths: set[str] = set()
    renames: dict[str, str] = {}
    errors: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status or status[0] not in "ACDMRTUXB":
            errors.append("git_diff_status_unrecognized")
            break
        if status[0] in {"R", "C"}:
            if index + 1 >= len(fields):
                errors.append("git_diff_rename_incomplete")
                break
            old_path = fields[index].replace("\\", "/")
            new_path = fields[index + 1].replace("\\", "/")
            index += 2
            paths.update((old_path, new_path))
            if status[0] == "R":
                renames[old_path] = new_path
            continue
        if index >= len(fields):
            errors.append("git_diff_path_missing")
            break
        paths.add(fields[index].replace("\\", "/"))
        index += 1
    return paths, renames, list(dict.fromkeys(errors))


def _task_relative_git_path(path: str, git_prefix: str) -> str:
    normalized = str(path or "").replace("\\", "/").strip("/")
    prefix = str(git_prefix or "").replace("\\", "/").strip("/")
    if not normalized:
        return ""
    if not prefix:
        return normalized
    prefix_with_slash = prefix + "/"
    if normalized == prefix:
        return ""
    if normalized.startswith(prefix_with_slash):
        return normalized[len(prefix_with_slash) :]
    return normalized


def _ignored_source_path(path: str) -> bool:
    parts = [part.lower() for part in str(path or "").replace("\\", "/").split("/") if part]
    return any(part in _BASELINE_IGNORED_DIRS for part in parts)


def _state_from_existence(*, existed_at_launch: bool, exists_now: bool) -> str:
    if not existed_at_launch and exists_now:
        return "created"
    if existed_at_launch and not exists_now:
        return "deleted"
    if existed_at_launch and exists_now:
        return "modified"
    return "none"


def _derived_result_kind(contract: dict[str, Any]) -> str:
    requirements = contract.get("requirements") if isinstance(contract.get("requirements"), list) else []
    changed_roles = {
        str(item.get("required_artifact_role") or "")
        for item in requirements
        if isinstance(item, dict) and bool(item.get("requires_source_change"))
    }
    if bool(contract.get("requires_source_change")):
        return "test" if changed_roles == {"test"} else "change"
    return "test" if str(contract.get("intent") or "") == "test_run" else "review"


def _derived_claim_kind(requirement: Any, *, contract: dict[str, Any]) -> str:
    item = requirement if isinstance(requirement, dict) else {}
    if bool(item.get("requires_source_change")):
        return "test" if str(item.get("required_artifact_role") or "") == "test" else "change"
    if str(item.get("intent") or contract.get("intent") or "") == "test_run":
        return "test"
    return "review"


def _derived_files_for_requirement(
    requirement: Any,
    changes: list[dict[str, Any]],
    *,
    contract: dict[str, Any],
) -> list[dict[str, str]]:
    item = requirement if isinstance(requirement, dict) else {}
    if not bool(item.get("requires_source_change")):
        return []
    required_role = str(item.get("required_artifact_role") or "implementation")
    contract_roles = {
        str(requirement.get("required_artifact_role") or "")
        for requirement in contract.get("requirements") or []
        if isinstance(requirement, dict) and bool(requirement.get("requires_source_change"))
    }
    selected = changes
    if "implementation" not in contract_roles and required_role != "implementation":
        selected = [
            change
            for change in changes
            if str(change.get("artifact_role") or "") in {required_role, "auxiliary"}
        ]
    return [
        {"path": str(change.get("path") or ""), "state": str(change.get("state") or "")}
        for change in selected[:MAX_COMPLETION_FILES]
    ]


def _task_source_scope_issues(
    contract: dict[str, Any],
    changes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not changes:
        return []
    if not bool(contract.get("requires_source_change")):
        return [
            {
                "code": "read_only_source_changes_detected",
                "message": "原目标是只读或只运行现有测试，但任务启动后出现了源码变化。",
                "paths": [str(item.get("path") or "") for item in changes[:20]],
            }
        ]
    requirements = contract.get("requirements") if isinstance(contract.get("requirements"), list) else []
    required_roles = {
        str(item.get("required_artifact_role") or "")
        for item in requirements
        if isinstance(item, dict) and bool(item.get("requires_source_change"))
    }
    if "implementation" in required_roles:
        allowed_roles = {"implementation", "test", "documentation", "auxiliary"}
    else:
        allowed_roles = set(required_roles) | {"auxiliary"}
    issues: list[dict[str, Any]] = []
    out_of_scope = [
        str(change.get("path") or "")
        for change in changes
        if str(change.get("artifact_role") or "") not in allowed_roles
    ]
    if out_of_scope:
        issues.append(
            {
                "code": "source_change_out_of_scope",
                "message": "服务端检测到与任务契约文件类型不符的变化。",
                "paths": out_of_scope[:20],
                "allowed_roles": sorted(allowed_roles),
            }
        )
    protected = [str(item).replace("\\", "/").strip("/") for item in contract.get("protected_paths") or []]
    protected_changes = [
        str(change.get("path") or "")
        for change in changes
        if any(_path_matches_scope(str(change.get("path") or ""), path) for path in protected)
    ]
    if protected_changes:
        issues.append(
            {
                "code": "protected_path_changed",
                "message": "服务端检测到任务明确要求不得修改的文件发生了变化。",
                "paths": protected_changes[:20],
                "protected_paths": protected,
            }
        )
    return issues


def _path_matches_scope(path: str, scope: str) -> bool:
    normalized_path = _path_key(path).strip("/")
    normalized_scope = _path_key(scope).strip("/")
    return bool(
        normalized_scope
        and (normalized_path == normalized_scope or normalized_path.startswith(normalized_scope + "/"))
    )


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

    expected_contract = build_task_contract(pinned_goal or goal, repo_root=repo_root)
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
    allowed_acceptance_step_classes = (
        ACCEPTANCE_STEP_CLASSES | {"compile"}
        if task_contract_allows_compile_only(contract)
        else ACCEPTANCE_STEP_CLASSES
    )
    if not requirement_index:
        _issue(errors, "task_contract_empty", "任务契约没有可验收的需求项。")

    raw_evidence = completion_evidence if isinstance(completion_evidence, dict) else {}
    if not raw_evidence:
        _issue(errors, "completion_evidence_missing", "缺少结构化交付证据。")
    source_changes = (
        raw_evidence.get("source_changes")
        if isinstance(raw_evidence.get("source_changes"), list)
        else []
    )
    source_change_issues = (
        raw_evidence.get("source_change_issues")
        if isinstance(raw_evidence.get("source_change_issues"), list)
        else []
    )
    for raw_issue in source_change_issues:
        if not isinstance(raw_issue, dict):
            continue
        _issue(
            errors,
            str(raw_issue.get("code") or "source_change_issue"),
            str(raw_issue.get("message") or "源码变化证据无效。"),
            **{
                key: value
                for key, value in raw_issue.items()
                if key not in {"code", "message"}
            },
        )
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
            if step["step_class"] in allowed_acceptance_step_classes:
                acceptance_step_seen = True
                locked_requirement = (
                    str(requirement_index[valid_requirement_ids[0]].get("text") or "")
                    if len(valid_requirement_ids) == 1
                    else ""
                )
                requirement_item = (
                    requirement_index[valid_requirement_ids[0]]
                    if len(valid_requirement_ids) == 1
                    else {}
                )
                required_role = str(requirement_item.get("required_artifact_role") or "")
                requirement_intent = str(requirement_item.get("intent") or "")
                relevance_files = claim_paths_by_role.get(required_role, []) if required_role else []
                if (
                    required_role == "documentation"
                    or (requirement_intent == "test_run" and step["step_class"] == "test")
                    or requirement_intent == "read_only"
                    or _verification_step_covers_claim(
                        step, locked_requirement=locked_requirement, files=relevance_files
                    )
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
    if (
        source_change_required
        and "implementation" in required_roles
        and attributed_implementation_file_count <= 0
    ):
        _issue(
            errors,
            "implementation_change_not_proven",
            "原目标要求实现或修复产品，但证据只有测试/审查文件，没有任何产品实现文件可归属本轮。",
        )

    acceptance_assessment = assess_acceptance_contract(
        contract.get("acceptance_contract"),
        requested_steps=requested_steps,
        final_phase=final_phase,
    )
    if acceptance_assessment["adequacy"] != "sufficient":
        _issue(
            warnings,
            "acceptance_standard_insufficient",
            "验收证据可信，但锁定标准不足以证明产品业务结果，产品结论只能是不确定。",
            reason_codes=acceptance_assessment["reason_codes"],
        )

    errors = _dedupe_issues(errors)
    warnings = _dedupe_issues(warnings)
    valid = not errors
    evidence_integrity = "verified" if valid and final_phase else "pending" if valid else "rejected"
    acceptance_adequacy = str(acceptance_assessment.get("adequacy") or "insufficient")
    product_verdict = (
        "fail"
        if not valid
        else "pass"
        if final_phase and acceptance_adequacy == "sufficient"
        else "indeterminate"
    )
    trust = "no"
    if product_verdict == "pass":
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
        if product_verdict == "pass"
        else "补充仓库或用户确认的验收标准后重新判定产品结果；现有证据仍可保留。"
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
        "evidence_integrity": evidence_integrity,
        "acceptance_adequacy": acceptance_adequacy,
        "product_verdict": product_verdict,
        "acceptance_assessment": acceptance_assessment,
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
        "evidence_origin": str(raw_evidence.get("evidence_origin") or "client_declared"),
        "source_changes": source_changes[:MAX_COMPLETION_FILES],
        "source_change_complete": bool(raw_evidence.get("source_change_complete")),
        "legacy_fields_ignored": list(raw_evidence.get("legacy_fields_ignored") or [])[:10],
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
            "evidence_integrity": evidence_integrity,
            "acceptance_adequacy": acceptance_adequacy,
            "product_verdict": product_verdict,
            "next_action": next_action,
        },
    }
    review["user_report_markdown"] = task_review_to_markdown(review)
    return review


def task_review_to_markdown(review: dict[str, Any]) -> str:
    report = review.get("user_report") if isinstance(review.get("user_report"), dict) else {}
    trust = str(report.get("can_trust") or review.get("trust") or "no")
    trust_label = {"yes": "可信", "with_limits": "有限可信", "no": "不可信"}.get(trust, "不可信")
    evidence_label = {
        "verified": "已验证",
        "pending": "待验证",
        "rejected": "已拒绝",
    }.get(str(report.get("evidence_integrity") or review.get("evidence_integrity") or ""), "未知")
    adequacy_label = {
        "sufficient": "充分",
        "insufficient": "不足",
        "unknown": "未知",
    }.get(str(report.get("acceptance_adequacy") or review.get("acceptance_adequacy") or ""), "未知")
    product_label = {
        "pass": "通过",
        "fail": "失败",
        "indeterminate": "无法判定",
    }.get(str(report.get("product_verdict") or review.get("product_verdict") or ""), "无法判定")
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
        f"证据完整性：{evidence_label}",
        f"验收标准充分性：{adequacy_label}",
        f"产品结论：{product_label}",
        f"可信结论：{trust_label}",
        f"下一步：{str(report.get('next_action') or '继续核对任务证据。')}",
    ]
    return "\n".join(lines)


def task_review_error(
    review: dict[str, Any],
    *,
    retryable: bool = True,
    attempt: int | None = None,
    max_attempts: int | None = None,
) -> str:
    errors = review.get("errors") if isinstance(review.get("errors"), list) else []
    contract = review.get("task_contract") if isinstance(review.get("task_contract"), dict) else {}
    requirements = contract.get("requirements") if isinstance(contract.get("requirements"), list) else []
    correction = {
        "schema_version": 1,
        "kind": "pacer_completion_correction",
        "retryable": bool(retryable),
        "errors": [
            {
                "code": str(item.get("code") or "completion_rejected"),
                "message": str(item.get("message") or "完成证据无效。"),
                "correction": _completion_correction(str(item.get("code") or "")),
                **{
                    key: value
                    for key, value in item.items()
                    if key not in {"code", "message"}
                },
            }
            for item in errors[:6]
            if isinstance(item, dict)
        ],
        "requirements": [
            {"id": str(item.get("id") or ""), "text": str(item.get("text") or "")}
            for item in requirements[:20]
            if isinstance(item, dict)
        ],
        "server_derived_changes": list(review.get("source_changes") or [])[:20],
        "required_claim_fields": ["requirement_ids", "result", "verification_steps"],
    }
    if attempt is not None and max_attempts is not None:
        correction["completion_control"] = {
            "attempt": max(1, int(attempt)),
            "max_attempts": max(1, int(max_attempts)),
        }
    return "completion audit rejected: " + json.dumps(
        correction,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _completion_correction(code: str) -> str:
    return {
        "claim_requirement_ids_missing": "Use exactly one ID from requirements; do not rescan the repository.",
        "unknown_requirement_id": "Replace the ID with one returned in requirements.",
        "claim_requirement_id_count_invalid": "Split the claim so each claim has exactly one requirement ID.",
        "claim_result_generic": "Describe the concrete result for that requirement.",
        "claim_without_verification": "Reference one of the submitted verification step names.",
        "unknown_verification_step": "Use an exact name from the submitted verification steps.",
        "read_only_source_changes_detected": "Restore the listed source changes before retrying completion.",
        "source_change_out_of_scope": "Restore out-of-scope files or update only files allowed by the locked task contract.",
        "protected_path_changed": "Restore every listed protected path before retrying completion.",
        "source_change_detection_failed": "Repair the Git/filesystem state so Pacer can derive a complete change set.",
        "verification_steps_missing": "Submit at least one substantive test, build, or analysis step.",
    }.get(code, "Correct the listed field and retry complete_pacer_task with the same locked goal.")


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
    if _text_related(result, requirement):
        return True
    shared = _semantic_tokens(normalized_result) & _semantic_tokens(normalized_requirement)
    generic = {
        "add", "build", "change", "complete", "fix", "implement", "modify", "pass",
        "test", "tests", "update", "write", "修改", "修复", "实现", "完成", "测试", "通过", "验证",
    }
    if shared - generic:
        return True
    code_identifiers = set(
        re.findall(
            r"\b([a-z_][a-z0-9_.-]*)\b(?=\s*(?:函数|方法|类|模块|function\b|method\b|class\b|module\b))",
            normalized_requirement,
            flags=re.IGNORECASE,
        )
    )
    return bool(shared & code_identifiers)


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
