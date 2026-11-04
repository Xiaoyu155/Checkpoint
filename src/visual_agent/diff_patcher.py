# =============================================================================
# src/visual_agent/diff_patcher.py
# Version: 1.0.0
#
# Functional Overview:
#   Fast Aider-style differential patch engine for the Pacer visual agent.
#   - Parses LLM-generated <<<<<<< SEARCH / ======= / >>>>>>> REPLACE blocks
#   - Applies edits to workspace files with strict security (path traversal,
#     symlink, and allow-list guards)
#   - Tiered matching (exact, whitespace-normalized, indentation-insensitive)
#   - Returns structured PatchBatchResult with per-file success/failure reports
#   - Core symbols: SearchReplaceBlock, parse_search_replace_blocks(),
#     apply_search_replace_blocks(), apply_search_replace_text()
# =============================================================================

"""Fast Aider-Style SEARCH/REPLACE Differential Patch Engine for Pacer.

Parses `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` blocks from LLM output and
applies them to workspace files with:
1. Strict path-traversal security checks (`..`, symlink escape, and allowed-file guards).
2. Tiered matching:
   - Tier 1: Exact substring match (normalized LF line endings).
   - Tier 2: Line-by-line right-stripped match (ignores trailing whitespace drift).
   - Tier 3: Indentation/leading-whitespace-insensitive block match with automatic
     relative indentation preservation.
3. New-file creation when the `SEARCH` section is empty.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from typing import Iterable


_FILE_HINT_RE = re.compile(
    r"(?:^|\s|`|\"|')(?:path\s*=\s*|file\s*=\s*|filepath\s*=\s*)?"
    r"([A-Za-z0-9_.\-/\\]+\.(?:py|ts|tsx|js|jsx|json|yaml|yml|toml|md|sh|txt|html|css))"
    r"(?:`|\"|'|:|\s|$)",
    re.IGNORECASE,
)


@dataclass
class SearchReplaceBlock:
    file_path: str
    search_text: str
    replace_text: str
    block_index: int = 0


@dataclass
class PatchOperationReport:
    file_path: str
    applied: bool
    match_strategy: str  # exact | rstrip | indent_tolerant | create_file | failed
    error: str = ""


@dataclass
class PatchBatchResult:
    success: bool
    applied_count: int
    failed_count: int
    modified_files: list[str] = field(default_factory=list)
    operations: list[PatchOperationReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize_rel_path(raw_path: str) -> str:
    cleaned = str(raw_path or "").strip().strip("`\"'").replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


FORBIDDEN_WORKSPACE_PREFIXES = (
    ".git",
    ".pacer",
    ".agent-workspace",
    ".vscode",
    ".idea",
    ".venv",
    "venv",
    "env",
    "node_modules",
)


def resolve_safe_workspace_path(
    rel_path: str,
    workspace_root: str | Path,
    *,
    allowed_files: Iterable[str] | None = None,
    allow_new_files: bool = True,
    forbidden_prefixes: Iterable[str] | None = None,
) -> Path:
    """Resolve `rel_path` inside `workspace_root`, raising ValueError on path traversal."""
    norm_rel = _normalize_rel_path(rel_path)
    if not norm_rel:
        raise ValueError("Empty target file path in SEARCH/REPLACE block.")

    raw_parts = Path(norm_rel).parts
    if ".." in raw_parts:
        raise ValueError(f"Path traversal ('..') is forbidden: {rel_path}")

    root = Path(workspace_root).expanduser().resolve()
    candidate = (root / norm_rel).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Target path '{rel_path}' escapes workspace root '{root}'.") from exc

    prefixes = tuple(forbidden_prefixes or FORBIDDEN_WORKSPACE_PREFIXES)
    if any(norm_rel == p or norm_rel.startswith(f"{p}/") for p in prefixes):
        raise ValueError(f"Access to protected path '{norm_rel}' is forbidden.")

    if allowed_files is not None:
        allowed_set = {_normalize_rel_path(p) for p in allowed_files if str(p).strip()}
        if norm_rel not in allowed_set:
            basename_matches = [p for p in allowed_set if Path(p).name == Path(norm_rel).name]
            if len(basename_matches) == 1:
                candidate = (root / basename_matches[0]).resolve()
            else:
                raise ValueError(
                    f"Target path '{norm_rel}' is not in the task's allowed target_files {sorted(allowed_set)}."
                )

    return candidate


def _extract_file_path_from_header(preceding_lines: list[str], default_file: str = "") -> str:
    """Inspect up to 4 lines immediately preceding `<<<<<<< SEARCH` to identify the target file."""
    for raw_line in reversed(preceding_lines[-4:]):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("```"):
            fence_rest = stripped.lstrip("`").strip()
            match = _FILE_HINT_RE.search(fence_rest)
            if match:
                return _normalize_rel_path(match.group(1))
            continue
        cleaned = re.sub(r"^(?:#+|//+|/\*+|\*+|--+)\s*", "", stripped).rstrip("*/: ")
        match = _FILE_HINT_RE.search(cleaned)
        if match:
            return _normalize_rel_path(match.group(1))
    return _normalize_rel_path(default_file)


def parse_search_replace_blocks(
    llm_output: str,
    *,
    default_file: str = "",
) -> list[SearchReplaceBlock]:
    """Extract all `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` blocks from `llm_output`."""
    text = str(llm_output or "").replace("\r\n", "\n")
    lines = text.split("\n")
    n = len(lines)

    blocks: list[SearchReplaceBlock] = []
    current_file = _normalize_rel_path(default_file)
    i = 0

    while i < n:
        line = lines[i]
        if not re.match(r"^[ \t]*<{5,9}\s*SEARCH\b", line):
            header_candidate = _extract_file_path_from_header([line], default_file="")
            if header_candidate and not line.lstrip().startswith(("import ", "from ", "def ", "class ", "return ")):
                current_file = header_candidate
            i += 1
            continue

        detected_file = _extract_file_path_from_header(lines[max(0, i - 4) : i], default_file=current_file)
        if detected_file:
            current_file = detected_file

        inline_match = _FILE_HINT_RE.search(line)
        if inline_match:
            current_file = _normalize_rel_path(inline_match.group(1))

        i += 1
        search_lines: list[str] = []
        while i < n and not re.match(r"^[ \t]*={5,9}[ \t]*$", lines[i]):
            search_lines.append(lines[i])
            i += 1

        if i >= n:
            break

        i += 1
        replace_lines: list[str] = []
        while i < n and not re.match(r"^[ \t]*>{5,9}\s*REPLACE\b", lines[i]):
            replace_lines.append(lines[i])
            i += 1

        if i >= n:
            break

        i += 1
        blocks.append(
            SearchReplaceBlock(
                file_path=current_file or _normalize_rel_path(default_file),
                search_text="\n".join(search_lines),
                replace_text="\n".join(replace_lines),
                block_index=len(blocks),
            )
        )

    return blocks


def _apply_single_block_to_content(
    original_content: str,
    search_text: str,
    replace_text: str,
) -> tuple[str | None, str]:
    norm_orig = original_content.replace("\r\n", "\n")
    norm_search = search_text.replace("\r\n", "\n")
    norm_replace = replace_text.replace("\r\n", "\n")

    if norm_search == "":
        if not norm_orig.strip():
            new_text = norm_replace if norm_replace.endswith("\n") else norm_replace + "\n"
            return new_text, "create_file"
        sep = "" if norm_orig.endswith("\n") else "\n"
        return norm_orig + sep + norm_replace + "\n", "exact"

    if norm_search in norm_orig:
        return norm_orig.replace(norm_search, norm_replace, 1), "exact"

    orig_lines = norm_orig.split("\n")
    search_lines = norm_search.split("\n")
    replace_lines = norm_replace.split("\n")

    if search_lines and search_lines[-1] == "":
        search_lines = search_lines[:-1]
    if replace_lines and replace_lines[-1] == "" and norm_replace.endswith("\n"):
        replace_lines = replace_lines[:-1]

    m = len(search_lines)
    if m == 0 or m > len(orig_lines):
        return None, "failed"

    search_rstripped = [ln.rstrip() for ln in search_lines]
    for start_idx in range(len(orig_lines) - m + 1):
        window = [orig_lines[start_idx + offset].rstrip() for offset in range(m)]
        if window == search_rstripped:
            updated_lines = orig_lines[:start_idx] + replace_lines + orig_lines[start_idx + m :]
            return "\n".join(updated_lines), "rstrip"

    search_stripped = [ln.strip() for ln in search_lines]
    non_empty_search = [ln for ln in search_stripped if ln]
    if not non_empty_search:
        return None, "failed"

    for start_idx in range(len(orig_lines) - m + 1):
        window_stripped = [orig_lines[start_idx + offset].strip() for offset in range(m)]
        if window_stripped == search_stripped:
            indent_delta = 0
            for offset in range(m):
                if search_lines[offset].strip():
                    orig_indent = len(orig_lines[start_idx + offset]) - len(orig_lines[start_idx + offset].lstrip(" "))
                    search_indent = len(search_lines[offset]) - len(search_lines[offset].lstrip(" "))
                    indent_delta = orig_indent - search_indent
                    break

            adjusted_replace: list[str] = []
            for r_line in replace_lines:
                if not r_line.strip():
                    adjusted_replace.append("")
                elif indent_delta > 0:
                    adjusted_replace.append((" " * indent_delta) + r_line)
                elif indent_delta < 0:
                    cur_indent = len(r_line) - len(r_line.lstrip(" "))
                    trim = min(cur_indent, -indent_delta)
                    adjusted_replace.append(r_line[trim:])
                else:
                    adjusted_replace.append(r_line)

            updated_lines = orig_lines[:start_idx] + adjusted_replace + orig_lines[start_idx + m :]
            return "\n".join(updated_lines), "indent_tolerant"

    orig_non_blank: list[tuple[int, str]] = [
        (idx, ln.strip()) for idx, ln in enumerate(orig_lines) if ln.strip()
    ]
    k = len(non_empty_search)
    if k <= len(orig_non_blank):
        for pos in range(len(orig_non_blank) - k + 1):
            candidate_slice = [orig_non_blank[pos + offset][1] for offset in range(k)]
            if candidate_slice == non_empty_search:
                first_line_idx = orig_non_blank[pos][0]
                last_line_idx = orig_non_blank[pos + k - 1][0]
                updated_lines = (
                    orig_lines[:first_line_idx]
                    + replace_lines
                    + orig_lines[last_line_idx + 1 :]
                )
                return "\n".join(updated_lines), "indent_tolerant"

    # Tier 4: Line-number prefix tolerant (e.g. LLM copied ` 12 | ` or `12:` from snippet)
    _line_prefix_re = re.compile(r"^\s*(?:>|\s)?\s*\d+\s*[:|]\s?")
    if any(_line_prefix_re.match(ln) for ln in search_lines):
        clean_search = [_line_prefix_re.sub("", ln) for ln in search_lines]
        clean_replace = [_line_prefix_re.sub("", ln) for ln in replace_lines]
        res, strat = _apply_single_block_to_content(
            original_content,
            "\n".join(clean_search),
            "\n".join(clean_replace),
        )
        if res is not None:
            return res, f"line_prefix_stripped_{strat}"

    return None, "failed"


def apply_search_replace_blocks(
    blocks: Iterable[SearchReplaceBlock],
    *,
    workspace_root: str | Path = ".",
    allowed_files: Iterable[str] | None = None,
) -> PatchBatchResult:
    """Apply parsed `SearchReplaceBlock` items atomically per file in `workspace_root`."""
    root = Path(workspace_root).expanduser().resolve()
    operations: list[PatchOperationReport] = []
    errors: list[str] = []
    modified_files: list[str] = []
    applied_count = 0
    failed_count = 0
    pending_writes: list[tuple[Path, str, list[PatchOperationReport], str]] = []

    # Group blocks by file preserving order
    grouped: dict[str, list[SearchReplaceBlock]] = {}
    for block in blocks:
        grouped.setdefault(block.file_path, []).append(block)

    for file_key, file_blocks in grouped.items():
        try:
            target_path = resolve_safe_workspace_path(
                file_key,
                root,
                allowed_files=allowed_files,
            )
            rel_norm = target_path.relative_to(root).as_posix()
        except ValueError as exc:
            for b in file_blocks:
                failed_count += 1
                err_msg = str(exc)
                errors.append(err_msg)
                operations.append(
                    PatchOperationReport(
                        file_path=b.file_path or "<unknown>",
                        applied=False,
                        match_strategy="failed",
                        error=err_msg,
                    )
                )
            continue

        file_exists = target_path.exists()
        current_text = target_path.read_text(encoding="utf-8", errors="replace") if file_exists else None

        file_operations: list[PatchOperationReport] = []
        file_errors: list[str] = []
        staged_text = current_text
        file_all_ok = True

        for block in file_blocks:
            if not file_exists and staged_text is None:
                content = block.replace_text.replace("\r\n", "\n")
                if content and not content.endswith("\n"):
                    content += "\n"
                staged_text = content
                file_operations.append(
                    PatchOperationReport(
                        file_path=rel_norm,
                        applied=True,
                        match_strategy="create_file",
                    )
                )
                continue

            updated_text, strategy = _apply_single_block_to_content(
                staged_text or "",
                block.search_text,
                block.replace_text,
            )

            if updated_text is None:
                file_all_ok = False
                snippet_preview = block.search_text.strip().splitlines()[0] if block.search_text.strip() else "<empty>"
                err_msg = f"SEARCH block #{block.block_index + 1} did not match `{rel_norm}` near: {snippet_preview[:80]}"
                file_errors.append(err_msg)
                file_operations.append(
                    PatchOperationReport(
                        file_path=rel_norm,
                        applied=False,
                        match_strategy="failed",
                        error=err_msg,
                    )
                )
                break

            if updated_text and not updated_text.endswith("\n"):
                updated_text += "\n"
            staged_text = updated_text
            file_operations.append(
                PatchOperationReport(
                    file_path=rel_norm,
                    applied=True,
                    match_strategy=strategy,
                )
            )

        if file_all_ok and staged_text is not None:
            pending_writes.append((target_path, staged_text, file_operations, rel_norm))
        else:
            failed_count += len(file_blocks)
            errors.extend(file_errors)
            for op in file_operations:
                if op.applied:
                    op.applied = False
                    op.match_strategy = "rolled_back"
                    op.error = "Rolled back because a subsequent block in the same file failed to match."
            start_idx = len(file_operations)
            for b in file_blocks[start_idx:]:
                file_operations.append(
                    PatchOperationReport(
                        file_path=rel_norm,
                        applied=False,
                        match_strategy="skipped",
                        error="Skipped because a previous block in the same file failed to match.",
                    )
                )
            operations.extend(file_operations)

    if failed_count:
        for _path, _text, file_operations, rel_norm in pending_writes:
            for op in file_operations:
                op.applied = False
                op.match_strategy = "rolled_back"
                op.error = "Rolled back because another file in the same batch failed."
            operations.extend(file_operations)
        return PatchBatchResult(
            success=False,
            applied_count=0,
            failed_count=failed_count,
            modified_files=[],
            operations=operations,
            errors=errors,
        )

    for target_path, staged_text, file_operations, rel_norm in pending_writes:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            try:
                mode = target_path.stat().st_mode
                if not (mode & stat.S_IWRITE):
                    target_path.chmod(mode | stat.S_IWRITE)
            except Exception:
                pass
        tmp_path = target_path.with_suffix(target_path.suffix + f".tmp.{os.getpid()}")
        tmp_path.write_text(staged_text, encoding="utf-8")
        try:
            os.replace(tmp_path, target_path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        applied_count += len(file_operations)
        if rel_norm not in modified_files:
            modified_files.append(rel_norm)
        operations.extend(file_operations)

    return PatchBatchResult(
        success=(applied_count > 0 and failed_count == 0),
        applied_count=applied_count,
        failed_count=failed_count,
        modified_files=modified_files,
        operations=operations,
        errors=errors,
    )


def apply_search_replace_text(
    llm_output: str,
    *,
    workspace_root: str | Path = ".",
    default_file: str = "",
    allowed_files: Iterable[str] | None = None,
) -> PatchBatchResult:
    """Convenience entrypoint: parse SEARCH/REPLACE blocks from `llm_output` and apply them."""
    blocks = parse_search_replace_blocks(llm_output, default_file=default_file)
    if not blocks:
        return PatchBatchResult(
            success=False,
            applied_count=0,
            failed_count=0,
            errors=["No SEARCH/REPLACE blocks found in model output."],
        )
    return apply_search_replace_blocks(
        blocks,
        workspace_root=workspace_root,
        allowed_files=allowed_files,
    )
