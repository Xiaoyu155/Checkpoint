"""Zero-token repository map: DevPacer's persistent memory of code architecture.

Every worker round starts a fresh model session. Without a map the worker
re-reads the repository to orient itself, burning the exact tokens DevPacer
exists to save — on every single round. This module builds that orientation
locally (git + ast/regex, no model calls), caches it with per-file signatures
so a refresh only re-parses files that actually changed, and renders a
budgeted excerpt for the worker prompt: files relevant to the objective get
symbol-level detail, the rest collapse into a directory summary.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Languages we can extract top-level symbols from. Everything else still shows
# up in the directory summary, so the worker knows the file exists.
_LANG_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".kt": "kotlin",
    ".swift": "swift",
}

_JS_PATTERNS = [
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE),
    re.compile(r"^\s*export\s+(?:const|let|var)\s+(\w+)", re.MULTILINE),
]
_GENERIC_PATTERNS = {
    "go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", re.MULTILINE), re.compile(r"^type\s+(\w+)", re.MULTILINE)],
    "rust": [re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)", re.MULTILINE), re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)", re.MULTILINE)],
    "java": [re.compile(r"^\s*(?:public|protected|private)?\s*(?:abstract\s+|final\s+)?(?:class|interface|enum)\s+(\w+)", re.MULTILINE)],
    "ruby": [re.compile(r"^\s*(?:class|module)\s+(\w+)", re.MULTILINE), re.compile(r"^\s*def\s+(\w+)", re.MULTILINE)],
    "csharp": [re.compile(r"^\s*(?:public|internal|private|protected)?\s*(?:static\s+|sealed\s+|abstract\s+)?(?:class|interface|struct|enum)\s+(\w+)", re.MULTILINE)],
    "kotlin": [re.compile(r"^\s*(?:open\s+|data\s+|sealed\s+)?(?:class|object|interface)\s+(\w+)", re.MULTILINE), re.compile(r"^\s*fun\s+(\w+)", re.MULTILINE)],
    "swift": [re.compile(r"^\s*(?:public\s+|open\s+)?(?:class|struct|enum|protocol)\s+(\w+)", re.MULTILINE), re.compile(r"^\s*(?:public\s+)?func\s+(\w+)", re.MULTILINE)],
    "c": [re.compile(r"^\w[\w\s\*]*?\b(\w+)\s*\([^;]*\)\s*\{", re.MULTILINE)],
    "cpp": [re.compile(r"^\s*(?:class|struct)\s+(\w+)", re.MULTILINE)],
    "php": [re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", re.MULTILINE), re.compile(r"^\s*(?:public\s+|private\s+|protected\s+|static\s+)*function\s+(\w+)", re.MULTILINE)],
}

# Vendored/generated trees that would bloat the map without helping orientation
# (git ls-files already respects .gitignore; these guard the non-git fallback
# and repos that commit their dependencies).
_SKIP_DIR_NAMES = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".agent-workspace", ".tox", ".mypy_cache", ".pytest_cache", "vendor", "target",
}

_MAX_FILES = 8000
_MAX_PARSE_BYTES = 300_000
_MAX_SYMBOLS_PER_FILE = 24


def repo_map_cache_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / "repo_map.json"


def build_repo_map(*, repo_root: str | Path, cache_path: str | Path | None = None) -> dict[str, Any]:
    """Build or incrementally refresh the map; only changed files are re-parsed."""
    root = Path(repo_root).expanduser().resolve()
    cached_files = _load_cache(cache_path)
    tracked = _list_files(root)

    files: dict[str, Any] = {}
    parsed = 0
    reused = 0
    for rel in tracked[:_MAX_FILES]:
        path = root / rel
        try:
            stat = path.stat()
        except OSError:
            continue
        sig = [stat.st_size, stat.st_mtime_ns]
        previous = cached_files.get(rel)
        if isinstance(previous, dict) and previous.get("sig") == sig:
            files[rel] = previous
            reused += 1
            continue
        files[rel] = _index_file(path, rel, sig)
        parsed += 1

    payload = {
        "schema_version": 1,
        "repo_root": str(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "file_count": len(files),
        "truncated": len(tracked) > _MAX_FILES,
        "parsed": parsed,
        "reused": reused,
        "files": files,
    }
    if cache_path is not None:
        target = Path(cache_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


def render_repo_map(
    payload: dict[str, Any],
    *,
    goal: str = "",
    focus_files: list[str] | None = None,
    max_lines: int = 60,
) -> str:
    """Render a budgeted excerpt: relevant files in symbol detail, rest as a tree."""
    files = payload.get("files") if isinstance(payload.get("files"), dict) else {}
    if not files:
        return ""
    focus = {_norm(item) for item in (focus_files or []) if str(item).strip()}
    goal_tokens = _goal_tokens(goal)

    scored: list[tuple[int, str]] = []
    for rel in files:
        score = _relevance(rel, files[rel], focus=focus, goal_tokens=goal_tokens)
        if score > 0:
            scored.append((score, rel))
    scored.sort(key=lambda item: (-item[0], item[1]))

    lines = [f"Files indexed: {payload.get('file_count')} (local index, no model calls)."]
    detail_budget = max(4, (max_lines * 2) // 3)
    detailed: set[str] = set()
    for _, rel in scored:
        if len(lines) >= detail_budget:
            break
        lines.append(_detail_line(rel, files[rel]))
        detailed.add(rel)

    for line in _directory_summary(files, exclude=detailed):
        if len(lines) >= max_lines:
            break
        lines.append(line)
    return "\n".join(lines[:max_lines])


def repo_map_to_markdown(payload: dict[str, Any], *, goal: str = "", max_lines: int = 60) -> str:
    header = [
        "## DevPacer Repository Map",
        "",
        f"Repo: {payload.get('repo_root')}",
        f"Files: {payload.get('file_count')} (parsed {payload.get('parsed')}, reused {payload.get('reused')} from cache)",
        "",
    ]
    return "\n".join(header) + render_repo_map(payload, goal=goal, max_lines=max_lines)


def payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _load_cache(cache_path: str | Path | None) -> dict[str, Any]:
    if cache_path is None:
        return {}
    try:
        raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = raw.get("files") if isinstance(raw, dict) else None
    return files if isinstance(files, dict) else {}


def _list_files(root: Path) -> list[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files"],
            capture_output=True,
            text=True,
            timeout=30.0,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode == 0:
            return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Non-git fallback: walk with pruning so vendored trees do not flood the map.
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIP_DIR_NAMES for part in rel_parts[:-1]):
            continue
        found.append("/".join(rel_parts))
        if len(found) > _MAX_FILES:
            break
    return found


def _index_file(path: Path, rel: str, sig: list[int]) -> dict[str, Any]:
    lang = _LANG_BY_SUFFIX.get(path.suffix.lower(), "")
    entry: dict[str, Any] = {"sig": sig, "lang": lang, "lines": 0, "symbols": []}
    if not lang or sig[0] > _MAX_PARSE_BYTES:
        return entry
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entry
    entry["lines"] = text.count("\n") + 1
    if lang == "python":
        entry["symbols"] = _python_symbols(text)
    elif lang in {"javascript", "typescript"}:
        entry["symbols"] = _regex_symbols(text, _JS_PATTERNS)
    else:
        entry["symbols"] = _regex_symbols(text, _GENERIC_PATTERNS.get(lang, []))
    return entry


def _python_symbols(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    symbols: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            methods = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
            shown = ", ".join(methods[:6]) + (", …" if len(methods) > 6 else "")
            symbols.append(f"class {node.name}({shown})" if methods else f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(f"def {node.name}")
    return symbols[:_MAX_SYMBOLS_PER_FILE]


def _regex_symbols(text: str, patterns: list[re.Pattern[str]]) -> list[str]:
    seen: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            name = match.group(1)
            if name and name not in seen:
                seen.append(name)
            if len(seen) >= _MAX_SYMBOLS_PER_FILE:
                return seen
    return seen


def _norm(value: str) -> str:
    return str(value).replace("\\", "/").strip().lower()


def _goal_tokens(goal: str) -> set[str]:
    tokens = {tok.lower() for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", str(goal or ""))}
    # CJK goals rarely overlap with ASCII paths; CJK bigrams still catch files
    # or symbols named in Chinese.
    cjk = re.findall(r"[一-鿿]{2,}", str(goal or ""))
    for chunk in cjk:
        tokens.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


def _relevance(rel: str, entry: dict[str, Any], *, focus: set[str], goal_tokens: set[str]) -> int:
    rel_norm = _norm(rel)
    score = 0
    if rel_norm in focus or any(item.endswith(rel_norm) or rel_norm.endswith(item) for item in focus):
        score += 100
    for token in goal_tokens:
        if token in rel_norm:
            score += 10
    symbols = entry.get("symbols") if isinstance(entry.get("symbols"), list) else []
    joined = " ".join(str(item).lower() for item in symbols)
    for token in goal_tokens:
        if token in joined:
            score += 4
    return score


def _detail_line(rel: str, entry: dict[str, Any]) -> str:
    symbols = entry.get("symbols") if isinstance(entry.get("symbols"), list) else []
    lines = int(entry.get("lines") or 0)
    suffix = f" ({lines}L)" if lines else ""
    if not symbols:
        return f"- {rel}{suffix}"
    return f"- {rel}{suffix}: " + "; ".join(str(item) for item in symbols[:10])


def _directory_summary(files: dict[str, Any], *, exclude: set[str]) -> list[str]:
    groups: dict[str, list[str]] = {}
    for rel in files:
        if rel in exclude:
            continue
        parts = rel.replace("\\", "/").split("/")
        group = "/".join(parts[:-1]) or "."
        groups.setdefault(group, []).append(parts[-1])
    lines: list[str] = []
    for group in sorted(groups, key=lambda item: (-len(groups[item]), item)):
        names = groups[group]
        shown = ", ".join(sorted(names)[:6]) + (", …" if len(names) > 6 else "")
        lines.append(f"- {group}/ — {len(names)} files: {shown}")
    return lines
