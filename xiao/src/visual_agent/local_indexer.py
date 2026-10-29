"""Sub-20ms Local AST & Regex Symbol Indexer for Pacer.

Replaces the slow network-bound Gemini Indexer subagent with deterministic,
in-process Python `ast` and regex analysis:
- Parses Python files in <5ms to extract classes, methods, functions, signatures,
  line ranges (`Lstart-Lend`), and top-level imports.
- Given a target line number (e.g. from a pytest traceback) or symbol query, extracts
  the enclosing AST node's exact code window with line numbers for `SEARCH/REPLACE` patching.
- Provides a lightweight regex fallback for TypeScript/JavaScript/Go/Rust files or
  Python files with syntax errors.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
import time
from typing import Any


_FALLBACK_DEF_RE = re.compile(
    r"^\s*(?:async\s+def|def|class|function|export\s+(?:async\s+)?function|export\s+class)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

_TRACEBACK_LOC_RE = re.compile(
    r'(?:File\s+"([^"]+\.py)",\s+line\s+(\d+))|([A-Za-z0-9_./\\\-]+\.py):(\d+):'
)


@dataclass
class SymbolSpan:
    name: str
    kind: str  # class | function | async_function | method
    signature: str
    start_line: int
    end_line: int
    parent: str = ""

    def format_compact(self) -> str:
        prefix = f"{self.parent}." if self.parent else ""
        if self.kind == "class":
            return f"class {prefix}{self.name} [L{self.start_line}-L{self.end_line}]"
        return f"def {prefix}{self.signature} [L{self.start_line}-L{self.end_line}]"


@dataclass
class FileIndexResult:
    file_path: str
    imports: list[str] = field(default_factory=list)
    symbols: list[SymbolSpan] = field(default_factory=list)
    focused_symbol: str = ""
    focused_snippet: str = ""
    elapsed_ms: float = 0.0
    error: str = ""

    def compact_symbols(self, max_items: int = 25) -> list[str]:
        return [s.format_compact() for s in self.symbols[:max_items]]

    def to_summary_text(self, *, include_snippet: bool = True) -> str:
        lines = [f"File: {self.file_path} (indexed in {self.elapsed_ms:.2f}ms)"]
        if self.imports:
            lines.append(f"Imports: {', '.join(self.imports[:10])}")
        if self.symbols:
            lines.append("Symbols:")
            for sym in self.symbols[:20]:
                lines.append(f"  - {sym.format_compact()}")
        if include_snippet and self.focused_snippet:
            header = f"Focused Excerpt ({self.focused_symbol}):" if self.focused_symbol else "Focused Excerpt:"
            lines.append(header)
            lines.append(self.focused_snippet)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _format_arg_list(args: ast.arguments) -> str:
    parts: list[str] = []
    all_pos = list(getattr(args, "posonlyargs", [])) + list(args.args)
    for arg in all_pos[:6]:
        if arg.annotation is not None:
            try:
                ann = ast.unparse(arg.annotation)
                parts.append(f"{arg.arg}: {ann}")
            except Exception:
                parts.append(arg.arg)
        else:
            parts.append(arg.arg)
    if len(all_pos) > 6:
        parts.append("...")
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for kw in args.kwonlyargs[:3]:
        parts.append(f"{kw.arg}=...")
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return ", ".join(parts)


def _extract_python_ast_info(source: str) -> tuple[list[str], list[SymbolSpan]]:
    tree = ast.parse(source)
    imports: list[str] = []
    symbols: list[SymbolSpan] = []

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in imports:
                    imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [a.name for a in node.names[:4]]
            entry = f"{mod}({', '.join(names)})" if mod else ", ".join(names)
            if entry and entry not in imports:
                imports.append(entry)
        elif isinstance(node, ast.ClassDef):
            end_ln = getattr(node, "end_lineno", node.lineno) or node.lineno
            bases = []
            for b in node.bases[:3]:
                try:
                    bases.append(ast.unparse(b))
                except Exception:
                    pass
            sig = f"{node.name}({', '.join(bases)})" if bases else node.name
            symbols.append(
                SymbolSpan(
                    name=node.name,
                    kind="class",
                    signature=sig,
                    start_line=node.lineno,
                    end_line=end_ln,
                )
            )
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    c_end = getattr(child, "end_lineno", child.lineno) or child.lineno
                    arg_str = _format_arg_list(child.args)
                    ret_str = ""
                    if child.returns is not None:
                        try:
                            ret_str = f" -> {ast.unparse(child.returns)}"
                        except Exception:
                            ret_str = ""
                    symbols.append(
                        SymbolSpan(
                            name=child.name,
                            kind="method",
                            signature=f"{child.name}({arg_str}){ret_str}",
                            start_line=child.lineno,
                            end_line=c_end,
                            parent=node.name,
                        )
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_ln = getattr(node, "end_lineno", node.lineno) or node.lineno
            arg_str = _format_arg_list(node.args)
            ret_str = ""
            if node.returns is not None:
                try:
                    ret_str = f" -> {ast.unparse(node.returns)}"
                except Exception:
                    ret_str = ""
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            symbols.append(
                SymbolSpan(
                    name=node.name,
                    kind=kind,
                    signature=f"{node.name}({arg_str}){ret_str}",
                    start_line=node.lineno,
                    end_line=end_ln,
                )
            )

    return imports, symbols


def _extract_regex_fallback_info(source: str) -> tuple[list[str], list[SymbolSpan]]:
    lines = source.splitlines()
    symbols: list[SymbolSpan] = []
    for idx, line in enumerate(lines, start=1):
        match = _FALLBACK_DEF_RE.match(line)
        if match:
            name = match.group(1)
            kind = "class" if "class " in line else "function"
            end_line = min(len(lines), idx + 25)
            symbols.append(
                SymbolSpan(
                    name=name,
                    kind=kind,
                    signature=f"{name}(...)",
                    start_line=idx,
                    end_line=end_line,
                )
            )
    return [], symbols


def extract_code_window(
    source: str,
    *,
    start_line: int,
    end_line: int,
    highlight_line: int | None = None,
    max_lines: int = 35,
) -> str:
    """Format a 1-indexed line window from `source` with line numbers."""
    lines = source.splitlines()
    if not lines:
        return ""
    total = len(lines)
    s_idx = max(1, min(total, start_line))
    e_idx = max(s_idx, min(total, end_line))

    if e_idx - s_idx + 1 > max_lines:
        if highlight_line and s_idx <= highlight_line <= e_idx:
            half = max_lines // 2
            s_idx = max(1, highlight_line - half)
            e_idx = min(total, s_idx + max_lines - 1)
        else:
            e_idx = s_idx + max_lines - 1

    out: list[str] = []
    for ln in range(s_idx, e_idx + 1):
        marker = ">" if (highlight_line is not None and ln == highlight_line) else " "
        out.append(f"{marker}{ln:4d} | {lines[ln - 1]}")
    return "\n".join(out)


def index_file_symbols(
    file_path: str | Path,
    *,
    workspace_root: str | Path | None = None,
    target_line: int | None = None,
    query: str = "",
    max_snippet_lines: int = 32,
) -> FileIndexResult:
    """Parse `file_path` in <20ms using native `ast` and extract symbols + focused snippet."""
    t0 = time.perf_counter()
    raw_path = Path(file_path)
    if workspace_root is not None:
        root = Path(workspace_root).expanduser().resolve()
        full_path = (root / raw_path).resolve() if not raw_path.is_absolute() else raw_path.resolve()
        try:
            rel_display = full_path.relative_to(root).as_posix()
        except ValueError:
            elapsed = (time.perf_counter() - t0) * 1000.0
            return FileIndexResult(
                file_path=str(raw_path),
                elapsed_ms=elapsed,
                error=f"Path traversal rejected: '{raw_path}' is outside workspace root",
            )
    else:
        full_path = raw_path.expanduser().resolve()
        rel_display = raw_path.as_posix()

    if not full_path.is_file():
        elapsed = (time.perf_counter() - t0) * 1000.0
        return FileIndexResult(
            file_path=rel_display,
            elapsed_ms=elapsed,
            error=f"File not found: {rel_display}",
        )

    try:
        source = full_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000.0
        return FileIndexResult(
            file_path=rel_display,
            elapsed_ms=elapsed,
            error=f"Read error: {exc}",
        )

    if full_path.suffix.lower() == ".py":
        try:
            imports, symbols = _extract_python_ast_info(source)
        except SyntaxError:
            imports, symbols = _extract_regex_fallback_info(source)
    else:
        imports, symbols = _extract_regex_fallback_info(source)

    focused_symbol = ""
    focused_snippet = ""

    if target_line is not None and target_line > 0:
        enclosing: SymbolSpan | None = None
        for sym in symbols:
            if sym.start_line <= target_line <= sym.end_line:
                if enclosing is None or (sym.end_line - sym.start_line) <= (enclosing.end_line - enclosing.start_line):
                    enclosing = sym
        if enclosing is not None:
            focused_symbol = enclosing.format_compact()
            focused_snippet = extract_code_window(
                source,
                start_line=enclosing.start_line,
                end_line=enclosing.end_line,
                highlight_line=target_line,
                max_lines=max_snippet_lines,
            )
        else:
            focused_symbol = f"L{target_line}"
            focused_snippet = extract_code_window(
                source,
                start_line=max(1, target_line - 10),
                end_line=target_line + 12,
                highlight_line=target_line,
                max_lines=max_snippet_lines,
            )
    elif query:
        q_lower = query.lower()
        matched_sym: SymbolSpan | None = None
        for sym in symbols:
            if sym.name.lower() in q_lower:
                matched_sym = sym
                if sym.kind != "class":
                    break
        if matched_sym is not None:
            focused_symbol = matched_sym.format_compact()
            focused_snippet = extract_code_window(
                source,
                start_line=matched_sym.start_line,
                end_line=matched_sym.end_line,
                max_lines=max_snippet_lines,
            )

    if not focused_snippet and source.strip():
        focused_snippet = extract_code_window(
            source,
            start_line=1,
            end_line=min(max_snippet_lines, len(source.splitlines())),
            max_lines=max_snippet_lines,
        )

    elapsed = (time.perf_counter() - t0) * 1000.0
    return FileIndexResult(
        file_path=rel_display,
        imports=imports,
        symbols=symbols,
        focused_symbol=focused_symbol,
        focused_snippet=focused_snippet,
        elapsed_ms=elapsed,
    )


def parse_error_location(error_output: str, workspace_root: str | Path | None = None) -> tuple[str, int | None]:
    """Extract the most relevant `(rel_file_path, line_number)` from a test/traceback output."""
    if not error_output:
        return ("", None)

    matches = _TRACEBACK_LOC_RE.findall(error_output)
    if not matches:
        return ("", None)

    root = Path(workspace_root).expanduser().resolve() if workspace_root else None
    candidates: list[tuple[str, int]] = []
    for m in matches:
        raw_file = m[0] or m[2]
        raw_line = m[1] or m[3]
        if not raw_file or not raw_line:
            continue
        try:
            line_no = int(raw_line)
        except ValueError:
            continue

        norm_path = raw_file.replace("\\", "/")
        if root is not None:
            p = Path(raw_file)
            full = p.resolve() if p.is_absolute() else (root / p).resolve()
            try:
                norm_path = full.relative_to(root).as_posix()
            except ValueError:
                continue
        candidates.append((norm_path.lstrip("/"), line_no))

    if not candidates:
        return ("", None)

    for fpath, lno in reversed(candidates):
        if not fpath.startswith(("tests/", "test_")) and "/test_" not in fpath:
            return (fpath, lno)
    return candidates[-1]