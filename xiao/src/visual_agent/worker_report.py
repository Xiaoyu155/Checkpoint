"""Structured worker report — the worker's channel back to Pacer.

Pacer's three existing gates (test-file tampering, scope violation, acceptance
chain protection) all detect *cheating*. None of them detect *drift*: a worker
that silently implements a self-contradictory spec, or that cannot actually do
what was asked and papers over it, passes every one of them.

This module is the return channel. The worker is told to end its turn with one
machine-readable block; Pacer parses it, never acts on it automatically, and
puts it in front of the human. The intent is explicit: Pacer is not qualified to
judge whether an objection is right. It is only qualified to make sure the
objection is not lost.

Block format (deliberately line-oriented so it survives log tails and any
agent's markdown habits)::

    PACER-REPORT
    objection: spec_contradiction
    summary: The skeleton and the failure banner cannot both be visible.
    detail: The spec asks for a loading skeleton while the request is in
    detail: flight and a failure banner in the same slot...
    END-PACER-REPORT
"""

from __future__ import annotations

import re
from typing import Any


BLOCK_START = "PACER-REPORT"
BLOCK_END = "END-PACER-REPORT"

#: What a worker is allowed to say back. ``none`` is the required "I looked and
#: found nothing" answer — silence is not an acceptable substitute for it.
OBJECTION_KINDS: dict[str, str] = {
    "none": "No contradiction, no blocker, no materially better approach.",
    "spec_contradiction": "The requirements contradict each other or contradict the code.",
    "cannot_comply": "The task as written cannot be done here (missing capability, access, or dependency).",
    "better_approach": "It was implemented as asked, but a materially better approach exists.",
}

_REPORTABLE_KINDS = tuple(kind for kind in OBJECTION_KINDS if kind != "none")

_KEY_LINE = re.compile(
    r"^\s*(objection|summary|detail|scope|claim|evidence)\s*:\s*(.*)$", re.IGNORECASE
)

#: Refs that point at whatever the upstream project happens to contain today.
#: A citation to one of these proves nothing tomorrow, so it is not a citation.
_DRIFTING_REFS = {"main", "master", "head", "latest", "trunk", "develop", "dev"}
_PINNED_REF = re.compile(r"^(?:[0-9a-f]{7,40}|v?\d+[\w.\-+]*)$", re.IGNORECASE)


def worker_report_prompt_block() -> list[str]:
    """Instructions appended to every worker prompt."""
    return [
        "",
        "Mandatory final report — Pacer is your client, not your supervisor.",
        "You are expected to push back. If the requirements contradict each other, if the task",
        "cannot honestly be done here, or if you complied but a materially better approach exists,",
        "say so. Implementing a bad spec silently and passing the tests is the failure mode this",
        "channel exists to prevent. Pacer will not act on your objection automatically and will not",
        "penalize you for raising one — it is shown to the human who wrote the requirements.",
        "",
        "End your final message with exactly this block, even when you have nothing to object to:",
        "",
        BLOCK_START,
        "objection: none | spec_contradiction | cannot_comply | better_approach",
        "summary: one line, plain language",
        "detail: optional; repeat the 'detail:' prefix for each extra line",
        "scope: every file or directory you changed; repeat 'scope:' per entry",
        "claim: <a statement you made about how some external code behaves>",
        "evidence: <where you checked it; one 'evidence:' line after each 'claim:'>",
        BLOCK_END,
        "",
        "The 'scope' lines are your declared change surface. Changing a file you did not declare",
        "is allowed and will not be reverted — the repository decides the right surface, not your",
        "first guess. But an undeclared change is routed to a human instead of merged, so declare",
        "what you actually touched, and say in 'detail' why the surface grew if it did.",
        "",
        "The claim/evidence pairs are for statements about code you do not control — a library's",
        "behavior, an API's contract, a framework's lifecycle. If you relied on such a statement,",
        "cite where you checked it, best source first:",
        "  evidence: local:<path in this machine's installed source>#<text you found there>",
        "  evidence: probe:<command you ran> -> <what it printed>",
        "  evidence: upstream:<repo url>@<commit sha or release tag>",
        "An upstream citation must pin a commit or tag; a link to a moving branch proves nothing.",
        "Pacer checks these mechanically. A citation to a file that does not exist, or to a moving",
        "branch, is treated as no citation at all — do not guess a plausible-looking path.",
    ]


def parse_worker_report(text: str) -> dict[str, Any]:
    """Parse the last report block out of worker output.

    Returns ``{"present": False}`` when the worker never emitted one. The last
    block wins: a resumed or repaired session appends, and the final word is the
    worker's current position.
    """
    body = _last_block_body(str(text or ""))
    if body is None:
        return {"present": False, "objection": "", "summary": "", "detail": "", "scope": [], "claims": []}
    objection = ""
    summary = ""
    details: list[str] = []
    scope: list[str] = []
    claims: list[dict[str, str]] = []
    for line in body:
        match = _KEY_LINE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        value = match.group(2).strip()
        if key == "objection" and not objection:
            objection = _normalized_kind(value)
        elif key == "summary" and not summary:
            summary = value
        elif key == "detail" and value:
            details.append(value)
        elif key == "scope" and value:
            scope.extend(_normalized_scope_entries(value))
        elif key == "claim" and value and not _is_template_placeholder(value):
            claims.append({"claim": value, "evidence": ""})
        elif key == "evidence" and value and not _is_template_placeholder(value):
            # Evidence attaches to the claim above it. Evidence with no claim is
            # kept as its own row so it still gets checked rather than dropped.
            if claims and not claims[-1]["evidence"]:
                claims[-1]["evidence"] = value
            else:
                claims.append({"claim": "", "evidence": value})
    return {
        "present": True,
        "objection": objection,
        "summary": summary,
        "detail": "\n".join(details),
        "scope": scope,
        "claims": claims,
    }


def _is_template_placeholder(value: str) -> bool:
    # Models routinely echo the block template back. Only the angle-bracket
    # placeholders are treated as noise: a real claim may well contain a "|",
    # and dropping it would turn an honest report into a missing one.
    return value.strip().startswith("<")


def _normalized_scope_entries(value: str) -> list[str]:
    entries: list[str] = []
    for raw in re.split(r"[,;]", value):
        candidate = raw.strip().strip("`").strip().replace("\\", "/")
        while candidate.startswith("./"):
            # Strip a "./" prefix without eating the leading dot of ".github/".
            candidate = candidate[2:]
        if not candidate or candidate.startswith("<") or " " in candidate.strip():
            # Prose ("all files under src") is not a declaration we can check.
            continue
        entries.append(candidate.rstrip("/") + ("/" if raw.strip().endswith(("/", "\\")) else ""))
    return entries


def _last_block_body(text: str) -> list[str] | None:
    lines = text.splitlines()
    start = -1
    body: list[str] | None = None
    for index, line in enumerate(lines):
        stripped = line.strip().strip("`").strip()
        if stripped == BLOCK_START:
            start = index
        elif stripped == BLOCK_END and start >= 0:
            body = lines[start + 1 : index]
            start = -1
    if body is None and start >= 0:
        # Output was truncated mid-block (log tail, timeout). A partial block
        # still carries the objection line, which is the part that matters.
        body = lines[start + 1 :]
    return body


def _normalized_kind(value: str) -> str:
    candidate = value.strip().strip("`").strip().lower()
    # Models echo the prompt's option list when they have nothing to say.
    if "|" in candidate:
        return ""
    for kind in OBJECTION_KINDS:
        if candidate == kind:
            return kind
    return ""


def collect_worker_objections(records: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Aggregate the reports of every worker attempt in one dispatch.

    Only attempts that actually ran are considered: a crashed or quota-blocked
    worker never got the chance to file a report, and counting that as "the
    worker had nothing to say" would be a lie.
    """
    objections: list[dict[str, Any]] = []
    reported = 0
    silent: list[str] = []
    declared_scope: list[str] = []
    claims: list[dict[str, str]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if str(record.get("status") or "") != "completed":
            continue
        report = parse_worker_report(_record_text(record))
        if not report.get("present"):
            silent.append(str(record.get("attempt") or record.get("track_id") or "worker"))
            continue
        reported += 1
        # Repair rounds legitimately widen the surface; the declaration is the
        # union of what every attempt owned up to.
        for entry in report.get("scope") or []:
            if entry not in declared_scope:
                declared_scope.append(entry)
        for entry in report.get("claims") or []:
            if entry not in claims:
                claims.append(entry)
        kind = str(report.get("objection") or "")
        if kind in _REPORTABLE_KINDS:
            objections.append(
                {
                    "attempt": str(record.get("attempt") or ""),
                    "agent": str(record.get("agent") or ""),
                    "kind": kind,
                    "summary": str(report.get("summary") or ""),
                    "detail": str(report.get("detail") or ""),
                }
            )
    return {
        "schema_version": 1,
        "objections": objections,
        "reports_present": reported,
        "silent_attempts": silent,
        "declared_scope": declared_scope,
        "claims": claims,
    }


def audit_declared_scope(
    *,
    declared: list[str] | None,
    changed: list[str] | None,
    reported: bool,
) -> dict[str, Any]:
    """Compare the worker's declared change surface against what it changed.

    This never rejects: the worker only sees the objective at dispatch time, so
    the first guess at the surface is expected to be wrong sometimes, and
    reverting a correct change because it was undeclared would be the same
    "false red" mistake the scope whitelist already made. The outcome is a
    routing decision — undeclared changes go to a human instead of auto-merging.
    """
    changed_paths = [str(item).replace("\\", "/").strip() for item in changed or [] if str(item).strip()]
    declared_paths = [str(item).replace("\\", "/").strip() for item in declared or [] if str(item).strip()]
    if not changed_paths:
        return {"schema_version": 1, "status": "no_changes", "declared": declared_paths, "undeclared": []}
    if not reported:
        return {
            "schema_version": 1,
            "status": "not_declared",
            "declared": [],
            "undeclared": [],
            "reason": "worker filed no report, so no change surface was declared",
        }
    if not declared_paths:
        return {
            "schema_version": 1,
            "status": "not_declared",
            "declared": [],
            "undeclared": [],
            "reason": "worker filed a report with no scope lines",
        }
    undeclared = [path for path in changed_paths if not _covered_by(path, declared_paths)]
    return {
        "schema_version": 1,
        "status": "undeclared_changes" if undeclared else "matched",
        "declared": declared_paths,
        "undeclared": undeclared,
    }


def _covered_by(path: str, declared: list[str]) -> bool:
    for entry in declared:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
            continue
        if path == entry:
            return True
        # A bare directory name is a common, reasonable declaration.
        if path.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def audit_evidence_claims(
    claims: list[dict[str, str]] | None,
    *,
    repo_root: Any = None,
) -> dict[str, Any]:
    """Mechanically check each claim's citation. Never judges the claim itself.

    Pacer cannot tell whether "BackgroundTasks run after the response" is true.
    It can tell whether the cited file exists, whether the cited text is in it,
    and whether an upstream link is pinned to something that will still say the
    same thing next week. That is the whole job: catch fabricated and drifting
    citations, and be explicit about everything it could not confirm.
    """
    rows: list[dict[str, Any]] = []
    for entry in claims or []:
        if not isinstance(entry, dict):
            continue
        claim = str(entry.get("claim") or "").strip()
        evidence = str(entry.get("evidence") or "").strip()
        rows.append(_audit_one_claim(claim=claim, evidence=evidence, repo_root=repo_root))
    return {
        "schema_version": 1,
        "claims": rows,
        "verified": [row for row in rows if row["grade"] == "verified"],
        "unverified": [row for row in rows if row["grade"] == "unverified"],
        "rejected": [row for row in rows if row["grade"] == "rejected"],
    }


def _audit_one_claim(*, claim: str, evidence: str, repo_root: Any) -> dict[str, Any]:
    row = {"claim": claim, "evidence": evidence, "grade": "rejected", "reason": ""}
    if not claim:
        row["reason"] = "证据没有对应的主张"
        return row
    if not evidence:
        row["reason"] = "主张没有给出处"
        return row
    scheme, _, rest = evidence.partition(":")
    scheme = scheme.strip().lower()
    rest = rest.strip().strip("`").strip()
    if scheme == "local":
        return {**row, **_audit_local_source(rest, repo_root=repo_root)}
    if scheme == "upstream":
        return {**row, **_audit_upstream_source(rest)}
    if scheme == "probe":
        return {**row, **_audit_probe_source(rest)}
    row["reason"] = f"出处格式无法解析（应为 local:/probe:/upstream:，收到 `{evidence[:60]}`）"
    return row


def _audit_local_source(rest: str, *, repo_root: Any) -> dict[str, str]:
    from pathlib import Path

    raw_path, _, pattern = rest.partition("#")
    raw_path = raw_path.strip()
    pattern = pattern.strip()
    if not raw_path:
        return {"grade": "rejected", "reason": "local 出处没有给路径"}
    candidate = Path(raw_path)
    candidates = [candidate]
    if repo_root and not candidate.is_absolute():
        candidates.insert(0, Path(repo_root) / raw_path)
    resolved = next((item for item in candidates if item.is_file()), None)
    if resolved is None:
        # A path that does not exist is the signature of a fabricated citation.
        return {"grade": "rejected", "reason": f"引用的本地文件不存在：{raw_path}"}
    if not pattern:
        return {"grade": "unverified", "reason": "文件存在，但没给可核对的原文片段"}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return {"grade": "unverified", "reason": f"文件读不出来：{error}"}
    if pattern in text:
        return {"grade": "verified", "reason": f"在 {raw_path} 里核对到引用的原文"}
    return {"grade": "rejected", "reason": f"{raw_path} 里找不到引用的原文：{pattern[:60]}"}


def _audit_upstream_source(rest: str) -> dict[str, str]:
    url, separator, ref = rest.rpartition("@")
    ref = ref.strip()
    if not separator or not url.strip():
        return {"grade": "rejected", "reason": "上游出处没有钉版本（需要 <url>@<commit 或 tag>）"}
    if ref.lower() in _DRIFTING_REFS:
        return {"grade": "rejected", "reason": f"上游出处钉在会漂移的分支上：{ref}"}
    if not _PINNED_REF.match(ref):
        return {"grade": "rejected", "reason": f"上游出处的版本号看不出是 commit 或 tag：{ref}"}
    # Pacer is deliberately offline here: it confirms the citation is pinned and
    # checkable by a human, not that the linked code says what the worker says.
    return {"grade": "unverified", "reason": f"出处已钉在 {ref}，但 Pacer 没有联网核对内容"}


def _audit_probe_source(rest: str) -> dict[str, str]:
    parts = re.split(r"->|=>", rest, maxsplit=1)
    if len(parts) != 2:
        return {"grade": "rejected", "reason": "探针出处只给了命令，没给实测到的输出"}
    command, observed = parts
    if not command.strip() or not observed.strip():
        return {"grade": "rejected", "reason": "探针出处的命令或输出是空的"}
    # Re-running a command a model wrote is not something Pacer will do behind
    # the user's back, so a probe is recorded, not confirmed.
    return {"grade": "unverified", "reason": "探针是助手自报的实测，Pacer 没有重跑"}


def claims_markdown_lines(audit: dict[str, Any] | None) -> list[str]:
    if not isinstance(audit, dict):
        return []
    rows = audit.get("claims") if isinstance(audit.get("claims"), list) else []
    if not rows:
        return []
    lines = ["", "### 外部行为主张的出处", "", "| 主张 | 出处核对 | 说明 |", "| --- | --- | --- |"]
    for row in rows:
        claim = str(row.get("claim") or "（无主张）").replace("|", "\\|")
        lines.append(
            f"| {claim[:120]} | {_grade_label(str(row.get('grade') or ''))} | {str(row.get('reason') or '')[:120]} |"
        )
    rejected = audit.get("rejected") if isinstance(audit.get("rejected"), list) else []
    if rejected:
        lines.extend(
            [
                "",
                "有出处核对不过的主张（引用的文件不存在、原文对不上，或钉在会漂移的分支上）。"
                "这类主张按「没有出处」处理，改动没有自动合并。",
            ]
        )
    return lines


_GRADE_LABELS = {
    "verified": "✅ 已核对",
    "unverified": "⚠️ 无法核对",
    "rejected": "❌ 出处不实",
}


def _grade_label(grade: str) -> str:
    return _GRADE_LABELS.get(grade, grade or "?")


def scope_markdown_lines(audit: dict[str, Any] | None) -> list[str]:
    if not isinstance(audit, dict):
        return []
    status = str(audit.get("status") or "")
    if status == "undeclared_changes":
        undeclared = audit.get("undeclared") if isinstance(audit.get("undeclared"), list) else []
        lines = [
            "",
            "### 改动面超出了助手自己的声明",
            "",
            "助手改了它没有事先声明的文件。这不一定是错的（仓库说了算，不是它的第一猜测说了算），",
            "但没有自动合并，等你看一眼：",
            "",
        ]
        lines.extend(f"- `{item}`" for item in undeclared[:30])
        if len(undeclared) > 30:
            lines.append(f"- …另有 {len(undeclared) - 30} 个文件")
        return lines
    if status == "not_declared":
        return [
            "",
            "### 改动面没有声明",
            "",
            f"助手没有交回改动面声明（{audit.get('reason') or ''}），所以这次改了哪些面无法和它的意图对账。",
        ]
    return []


def _record_text(record: dict[str, Any]) -> str:
    parts = [str(record.get("stdout_tail") or "")]
    log_path = str(record.get("log_path") or "").strip()
    if log_path:
        parts.append(_read_tail(log_path))
    return "\n".join(part for part in parts if part)


def _read_tail(path: str, *, limit: int = 200_000) -> str:
    from pathlib import Path

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    return raw[-limit:].decode("utf-8", errors="replace")


def objection_markdown_lines(summary: dict[str, Any] | None) -> list[str]:
    """Render objections for the dispatch report. Never silently empty."""
    if not isinstance(summary, dict):
        return []
    objections = summary.get("objections") if isinstance(summary.get("objections"), list) else []
    silent = summary.get("silent_attempts") if isinstance(summary.get("silent_attempts"), list) else []
    if not objections and not silent:
        return []
    lines = ["", "### 编程助手的异议", ""]
    if objections:
        lines.append("助手对这次的需求提出了意见。Pacer 没有替你采纳，请你自己判断：")
        lines.append("")
        for item in objections:
            kind = str(item.get("kind") or "")
            lines.append(f"- **{_kind_label(kind)}**（{item.get('agent') or 'worker'} / {item.get('attempt') or ''}）")
            if item.get("summary"):
                lines.append(f"  - {item['summary']}")
            for detail_line in str(item.get("detail") or "").splitlines():
                if detail_line.strip():
                    lines.append(f"  - {detail_line.strip()}")
    if silent:
        lines.append("")
        lines.append(
            "- 注意：以下这轮助手没有交回异议报告，所以「它没意见」这句话没有证据："
            + ", ".join(str(item) for item in silent)
        )
    return lines


_KIND_LABELS = {
    "spec_contradiction": "需求自相矛盾",
    "cannot_comply": "这件事在这里做不到",
    "better_approach": "照做了，但有更好的做法",
}


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind or "异议")
