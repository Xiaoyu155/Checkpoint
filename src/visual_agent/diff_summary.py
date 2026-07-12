"""Layer 2 verification: diff summary always generated after every dispatch.

The goal is not to judge correctness — that is the test command's job.
The goal is to make every change *visible*: which files moved, which
functions were touched, and the minimum the user needs to manually verify.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any


# Per-extension checklist hints.  Keyed by normalised extension (no dot).
_EXT_CHECKLIST: dict[str, str] = {
    "dart": "在设备/模拟器上运行 Flutter app，操作涉及改动的页面或功能",
    "py": "运行受影响的模块或 API 端点，确认行为与预期一致",
    "ts": "在浏览器中打开相关页面，检查 TypeScript 编译产物是否正常",
    "tsx": "在浏览器中打开相关页面，检查 React 组件渲染是否正常",
    "js": "在浏览器中打开相关页面，确认 JS 逻辑正常",
    "jsx": "在浏览器中打开相关组件页面，确认渲染和交互正常",
    "html": "在浏览器中打开该 HTML 页面，检查布局和元素是否正确",
    "css": "刷新相关页面，检查样式是否符合预期（重点：响应式、颜色、间距）",
    "go": "运行 `go test ./...` 并手动调用修改的函数或 HTTP 路由",
    "rs": "运行 `cargo test` 并确认修改的模块编译通过",
    "sql": "检查迁移脚本是否已执行，数据库结构是否与预期一致",
    "yaml": "检查修改的配置文件格式正确，相关服务重启后行为正常",
    "json": "确认 JSON 格式合法，相关配置或数据加载正常",
    "md": "浏览修改的文档，确认内容准确且格式正常",
    "swift": "在 Xcode 中构建并在模拟器/真机上运行，操作相关功能",
    "kt": "在 Android Studio 中构建并运行，操作相关功能",
    "java": "构建并运行，确认修改的类或接口行为正确",
}

# Patterns applied to the *content* of added lines (leading "+" already stripped).
_SYMBOL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Python", re.compile(r"(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")),
    ("Python", re.compile(r"class\s+([A-Za-z_]\w*)\s*[:(]")),
    ("JS/TS",  re.compile(r"function\s+([A-Za-z_$]\w*)\s*\(")),
    ("JS/TS",  re.compile(r"(?:const|let|var)\s+([A-Za-z_$]\w*)\s*=\s*(?:async\s*)?\(")),
    ("JS/TS",  re.compile(r"class\s+([A-Za-z_$]\w*)\s*(?:extends|{)")),
    ("Dart",   re.compile(r"class\s+([A-Za-z_]\w*)\s*(?:extends|implements|with|{)")),
    ("Dart",   re.compile(r"(?:void|Widget|Future|Stream|String|int|bool)\s+([A-Za-z_]\w*)\s*\(")),
    ("Go",     re.compile(r"func\s+(?:\([^)]+\)\s+)?([A-Za-z_]\w*)\s*\(")),
    ("Rust",   re.compile(r"(?:pub\s+)?fn\s+([A-Za-z_]\w*)\s*\(")),
]

_MAX_DIFF_BYTES = 400_000  # stop reading huge diffs early
_MAX_SYMBOLS = 20          # cap the displayed symbol list


def build_diff_summary(
    *,
    repo_root: str | Path,
    base_ref: str | None = None,
) -> dict[str, Any]:
    """Always-generated summary of what the worker changed.

    Returns a dict with:
      changed_files   list[dict]  — path, status, lines_added, lines_removed
      functions_touched list[dict] — name, lang (best-effort, regex-based)
      user_checklist  list[str]   — "open X and verify Y" (one per ext family)
      summary_text    str         — one paragraph for the dashboard
      file_count      int
      lines_added     int
      lines_removed   int
      large_diff      bool        — True if volume seems unusually high
    """
    root = Path(repo_root).expanduser().resolve()
    base = (base_ref or "HEAD").strip() or "HEAD"

    file_stats = _git_diff_stat(root, base)
    diff_text = _git_diff_text(root, base)

    file_count = len(file_stats)
    total_added = sum(f["lines_added"] for f in file_stats)
    total_removed = sum(f["lines_removed"] for f in file_stats)
    large_diff = file_count > 40 or (total_added + total_removed) > 2000

    functions_touched = _extract_symbols(diff_text)
    exts = {_ext(f["path"]) for f in file_stats}
    user_checklist = _build_checklist(exts, file_stats)
    summary_text = _build_summary(file_count, total_added, total_removed, functions_touched, large_diff)

    return {
        "file_count": file_count,
        "lines_added": total_added,
        "lines_removed": total_removed,
        "large_diff": large_diff,
        "changed_files": file_stats,
        "functions_touched": functions_touched[:_MAX_SYMBOLS],
        "user_checklist": user_checklist,
        "summary_text": summary_text,
    }


def format_diff_summary(summary: dict[str, Any]) -> str:
    """Render a diff summary as Markdown for the dashboard or workbench."""
    lines: list[str] = ["### 本次改动摘要", ""]

    fc = summary.get("file_count", 0)
    la = summary.get("lines_added", 0)
    lr = summary.get("lines_removed", 0)
    lines.append(f"**文件数**: {fc}　**新增行**: +{la}　**删除行**: -{lr}")

    if summary.get("large_diff"):
        lines.append("")
        lines.append("> ⚠️ **改动体量异常大**：请仔细检查 diff，确认 worker 没有误改无关代码。")

    changed = summary.get("changed_files") or []
    if changed:
        lines += ["", "**改动文件**:", ""]
        for f in changed[:30]:
            status_tag = {"A": "新增", "D": "删除", "M": "修改", "R": "重命名"}.get(f.get("status", "M"), "改动")
            lines.append(f"- `{f['path']}` ({status_tag}, +{f['lines_added']}/-{f['lines_removed']})")
        if len(changed) > 30:
            lines.append(f"- … 还有 {len(changed) - 30} 个文件")

    symbols = summary.get("functions_touched") or []
    if symbols:
        lines += ["", "**涉及的函数/类**:", ""]
        for sym in symbols:
            lines.append(f"- `{sym['name']}` ({sym['lang']})")

    checklist = summary.get("user_checklist") or []
    if checklist:
        lines += ["", "**你需要手动确认的事项**:", ""]
        for item in checklist:
            lines.append(f"- {item}")

    return "\n".join(lines)


# ── internals ────────────────────────────────────────────────────────────────

def _git_diff_stat(root: Path, base: str) -> list[dict[str, Any]]:
    """Parse `git diff --numstat` for per-file line counts."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--numstat", base],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30.0,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    # Also check staged changes
    staged: dict[str, tuple[int, int]] = {}
    try:
        staged_result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--numstat", "--cached"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30.0,
            encoding="utf-8",
            errors="replace",
        )
        for line in staged_result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                staged[parts[2].strip()] = (int(parts[0]), int(parts[1]))
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Get name-status for A/M/D classification
    status_map: dict[str, str] = {}
    try:
        ns = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--name-status", base],
            cwd=str(root), capture_output=True, text=True, timeout=30.0,
            encoding="utf-8", errors="replace",
        )
        for line in ns.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                status_map[parts[1].strip()] = parts[0].strip()[0]
    except (OSError, subprocess.TimeoutExpired):
        pass

    files: dict[str, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_str, removed_str, path = parts
        # Binary files show "-"
        added = int(added_str) if added_str.isdigit() else 0
        removed = int(removed_str) if removed_str.isdigit() else 0
        path = path.strip()
        files[path] = {
            "path": path,
            "status": status_map.get(path, "M"),
            "lines_added": added,
            "lines_removed": removed,
        }

    for path, (added, removed) in staged.items():
        if path not in files:
            files[path] = {
                "path": path,
                "status": status_map.get(path, "M"),
                "lines_added": added,
                "lines_removed": removed,
            }

    for path in _git_untracked_files(root):
        if path in files:
            continue
        files[path] = {
            "path": path,
            "status": "A",
            "lines_added": _count_file_lines(root / path),
            "lines_removed": 0,
        }

    return sorted(files.values(), key=lambda f: f["path"])


def _git_diff_text(root: Path, base: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", base],
            cwd=str(root),
            capture_output=True,
            timeout=30.0,
            encoding="utf-8",
            errors="replace",
        )
        text = result.stdout or ""
        untracked_text = _untracked_diff_text(root)
        if untracked_text:
            text = text + "\n" + untracked_text
        return text[:_MAX_DIFF_BYTES]
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _git_untracked_files(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30.0,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    paths: list[str] = []
    for raw in result.stdout.splitlines():
        path = raw.strip().strip('"').replace("\\", "/")
        if not path:
            continue
        full = root / path
        if full.exists() and full.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _count_file_lines(path: Path) -> int:
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if b"\0" in data:
        return 0
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _untracked_diff_text(root: Path) -> str:
    chunks: list[str] = []
    remaining = _MAX_DIFF_BYTES
    for rel in _git_untracked_files(root):
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        header = f"diff --git a/{rel} b/{rel}\n--- /dev/null\n+++ b/{rel}\n"
        body = "\n".join(f"+{line}" for line in text.splitlines())
        chunk = header + body + "\n"
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
    return "\n".join(chunks)


def _extract_symbols(diff_text: str) -> list[dict[str, str]]:
    if not diff_text:
        return []
    # Work on added-lines only: strip the leading "+" so patterns don't need
    # to account for it, and skip diff-header lines ("+++").
    added_content = "\n".join(
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("++")
    )
    seen: set[str] = set()
    symbols: list[dict[str, str]] = []
    for lang, pattern in _SYMBOL_PATTERNS:
        for match in pattern.finditer(added_content):
            name = next((g for g in match.groups() if g), None)
            if name and name not in seen:
                seen.add(name)
                symbols.append({"name": name, "lang": lang})
    return symbols


def _ext(path: str) -> str:
    return Path(path).suffix.lstrip(".").lower()


def _build_checklist(exts: set[str], file_stats: list[dict[str, Any]]) -> list[str]:
    checklist: list[str] = []
    seen_hints: set[str] = set()

    # Group extensions into families to avoid duplicates
    families = {
        "frontend": {"ts", "tsx", "js", "jsx", "html", "css"},
        "flutter": {"dart"},
        "python": {"py"},
        "go": {"go"},
        "rust": {"rs"},
        "db": {"sql"},
        "config": {"yaml", "yml", "json", "toml"},
        "docs": {"md", "rst"},
        "mobile": {"swift", "kt", "java"},
    }

    for family, family_exts in families.items():
        matched = exts & family_exts
        if not matched:
            continue
        # Use the first matched ext's hint
        for ext in sorted(matched):
            hint = _EXT_CHECKLIST.get(ext)
            if hint and hint not in seen_hints:
                seen_hints.add(hint)
                checklist.append(hint)
                break

    if not checklist:
        checklist.append("运行应用并操作本次任务涉及的功能，确认行为符合预期")

    # Check for db migrations specifically
    has_migration = any(
        "migrat" in f["path"].lower() or "schema" in f["path"].lower()
        for f in file_stats
    )
    if has_migration and "sql" not in exts:
        checklist.append("运行数据库迁移脚本，确认表结构变更符合预期")

    return checklist


def _build_summary(
    file_count: int,
    lines_added: int,
    lines_removed: int,
    symbols: list[dict[str, str]],
    large_diff: bool,
) -> str:
    if file_count == 0:
        return "本次任务未检测到代码变更。"

    parts = [f"本次改动涉及 {file_count} 个文件，新增 {lines_added} 行，删除 {lines_removed} 行。"]

    if symbols:
        names = "、".join(f"`{s['name']}`" for s in symbols[:5])
        suffix = f"等 {len(symbols)} 处" if len(symbols) > 5 else ""
        parts.append(f"涉及函数/类：{names}{suffix}。")

    if large_diff:
        parts.append("改动体量较大，建议重点审查 diff，确认 worker 没有误改无关代码。")
    else:
        parts.append("体量正常，请按核查清单做最小限度的人工确认。")

    return "".join(parts)
