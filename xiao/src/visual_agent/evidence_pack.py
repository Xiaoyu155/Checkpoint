"""Completion evidence a person who cannot read code can actually judge.

Pacer's proof of completion has always been an exit code. "The command passed"
is a true statement that tells its owner nothing: they cannot read the command,
the diff, or the stack trace, so their only options are to believe it or not.

But the evidence is already there, in a form they *can* read. A real acceptance
run prints the name of every behavior it checked::

    ok 3 - a pet without a usable birth date gets no card
    ok 8 - a birthday card never carries medical advice

Those are sentences about the product. The owner of a pet app can judge them
immediately — that is exactly the judgement they are qualified to make, and the
only one anybody actually needs from them. All this module does is dig those
sentences out of the output and put them in front of the person.

What it deliberately does not do is overclaim. A passing check proves the
behavior it names, and nothing else: not that the screen looks right, not that
the wording is good, not that this was the feature worth building. Those stay
the person's call, and the report says so out loud.
"""

from __future__ import annotations

import re
from typing import Any

MAX_LISTED = 15

#: `ok 3 - name` / `not ok 3 - name` — node --test, tap, perl, many others.
_TAP = re.compile(r"^\s*(not\s+ok|ok)\s+\d+\s*-?\s*(.+?)\s*$", re.IGNORECASE)
#: `path/to/test.py::test_name PASSED` — pytest -v.
_PYTEST = re.compile(r"^(?P<path>\S+?)::(?P<name>[\w\[\]\-.]+)\s+(?P<status>PASSED|FAILED|ERROR)\b")
#: `--- PASS: TestName (0.01s)` — go test -v.
_GO = re.compile(r"^\s*---\s+(PASS|FAIL):\s+(\S+)")
#: `✓ name` / `✗ name` / `× name` — jest, vitest, mocha.
_TICK = re.compile(r"^\s*(?:[✓✔])\s+(.+?)\s*(?:\(\d+\s*m?s\))?\s*$")
_CROSS = re.compile(r"^\s*(?:[✗✘×])\s+(.+?)\s*(?:\(\d+\s*m?s\))?\s*$")

_FILEISH = re.compile(r"\.(js|mjs|cjs|ts|tsx|jsx|py|go|rb|java|rs|php)\b|[\\/]")
_TRAILING_TIME = re.compile(r"\s*\(\d+(?:\.\d+)?\s*m?s\)\s*$")


def parse_checked_behaviors(output: str) -> list[dict[str, str]]:
    """Pull the named behaviors out of an acceptance run's output."""
    behaviors: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(name: str, passed: bool) -> None:
        cleaned = _clean(name)
        if not cleaned:
            return
        key = (cleaned, "pass" if passed else "fail")
        if key in seen:
            return
        seen.add(key)
        behaviors.append({"name": cleaned, "status": "pass" if passed else "fail"})

    for line in str(output or "").splitlines():
        tap = _TAP.match(line)
        if tap:
            add(tap.group(2), tap.group(1).lower() == "ok")
            continue
        pytest_match = _PYTEST.match(line)
        if pytest_match:
            add(pytest_match.group("name"), pytest_match.group("status") == "PASSED")
            continue
        go_match = _GO.match(line)
        if go_match:
            add(go_match.group(2), go_match.group(1) == "PASS")
            continue
        tick = _TICK.match(line)
        if tick:
            add(tick.group(1), True)
            continue
        cross = _CROSS.match(line)
        if cross:
            add(cross.group(1), False)
    return behaviors


def _clean(raw: str) -> str:
    name = _TRAILING_TIME.sub("", str(raw or "").strip())
    name = name.strip("`'\"")
    if not name:
        return ""
    # A runner reports the file as a result too. "test/birthdayCard.test.js"
    # is not a sentence about the product, so it is noise here.
    if _FILEISH.search(name):
        return ""
    if name.lower() in {"subtests", "todo", "skipped"}:
        return ""
    return humanize_behavior_name(name)


def humanize_behavior_name(raw: str) -> str:
    """Turn an identifier-shaped test name back into the sentence it was."""
    name = str(raw or "").strip()
    if not name:
        return ""
    # pytest parametrisation: test_foo[case-1] -> test_foo (case-1)
    param = ""
    bracket = re.match(r"^(?P<base>[^\[]+)\[(?P<param>.+)\]$", name)
    if bracket:
        name = bracket.group("base")
        param = bracket.group("param")
    for prefix in ("test_", "Test", "test ", "it_", "should_"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    if "_" in name:
        name = name.replace("_", " ")
    elif re.search(r"[a-z][A-Z]", name):
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
        # "UserCanLogIn" splits to "User Can Log In", which reads like a title,
        # not like the sentence it was meant to be. Acronyms stay shouting.
        name = " ".join(
            word if (word.isupper() or any(ch.isdigit() for ch in word)) else word.lower()
            for word in name.split(" ")
        )
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return ""
    if name[0].islower() and name[0].isascii():
        name = name[0].upper() + name[1:]
    if param:
        name = f"{name}（{param}）"
    return name


def build_evidence_pack(
    *,
    objective: str,
    command: str,
    output: str,
    exit_code: Any = None,
    delivery_status: str = "",
    translator: Any = None,
) -> dict[str, Any]:
    """Assemble the readable evidence.

    ``translator`` takes the list of behavior names and returns the same list in
    the reader's language. It is injected rather than assumed: a test suite
    written in English is still a wall for someone who does not read English,
    but a translation that silently fails must not be able to invent behaviors
    that were never checked.
    """
    behaviors = parse_checked_behaviors(output)
    if translator is not None and behaviors:
        behaviors = _translated(behaviors, translator)
    confirmed = [item for item in behaviors if item["status"] == "pass"]
    failed = [item for item in behaviors if item["status"] == "fail"]
    return {
        "schema_version": 1,
        "objective": str(objective or "").strip(),
        "command": str(command or "").strip(),
        "exit_code": exit_code,
        "delivery_status": str(delivery_status or ""),
        "confirmed": confirmed,
        "failed": failed,
        # Without named behaviors there is nothing a non-reader can judge, and
        # saying so is more useful than dressing up an exit code.
        "readable": bool(confirmed or failed),
    }


def _translated(behaviors: list[dict[str, str]], translator: Any) -> list[dict[str, str]]:
    names = [item["name"] for item in behaviors]
    try:
        rendered = translator(names)
    except Exception:  # noqa: BLE001 - a translation outage must not lose the evidence
        return behaviors
    if not isinstance(rendered, list) or len(rendered) != len(names):
        # A mismatched list would pair a behavior with someone else's sentence,
        # which is worse than leaving it untranslated.
        return behaviors
    result: list[dict[str, str]] = []
    for item, text in zip(behaviors, rendered):
        translated = str(text or "").strip()
        result.append({**item, "name": translated or item["name"]})
    return result


def evidence_markdown_lines(pack: dict[str, Any] | None) -> list[str]:
    """The section a person reads instead of the diff."""
    if not isinstance(pack, dict):
        return []
    confirmed = pack.get("confirmed") if isinstance(pack.get("confirmed"), list) else []
    failed = pack.get("failed") if isinstance(pack.get("failed"), list) else []
    if not confirmed and not failed:
        if pack.get("command"):
            return [
                "",
                "### 我替你确认了什么",
                "",
                "这次的检查只回了一个「通过」，没有说清它具体检查了哪些事，",
                "所以我没法把它翻译成你能核对的清单——你只能选择信或不信，这不太好。",
                "想改善的话，让检查在输出里带上每一项的名字。",
            ]
        return []

    lines = ["", "### 我替你确认了什么", ""]
    if confirmed:
        lines.append(f"下面这 {len(confirmed)} 件事，我一件件试过，都是对的：")
        lines.append("")
        for item in confirmed[:MAX_LISTED]:
            lines.append(f"- ✅ {item.get('name')}")
        if len(confirmed) > MAX_LISTED:
            lines.append(f"- …另有 {len(confirmed) - MAX_LISTED} 项同样通过")
    if failed:
        lines.extend(["", f"下面这 {len(failed)} 件事**没通过**：", ""])
        for item in failed[:MAX_LISTED]:
            lines.append(f"- ❌ {item.get('name')}")
        if len(failed) > MAX_LISTED:
            lines.append(f"- …另有 {len(failed) - MAX_LISTED} 项同样没通过")

    lines.extend(
        [
            "",
            "**这些能说明什么、不能说明什么：**",
            "",
            "- 能说明：上面每一条描述的行为，程序确实做到了。",
            "- **不能说明**：界面好不好看、文案合不合适、这功能是不是你真正想要的那个。",
            "  这三件事得你自己看一眼，我判断不了，也不该替你判断。",
        ]
    )
    if str(pack.get("delivery_status") or "") == "delivered":
        lines.append("- 另外我确认过：这份成果拿到干净环境里也跑得起来，不是只在我这台机器上好使。")
    lines.extend(
        [
            "",
            "**如果上面有哪条不是你要的**，直接把那句话复述给我，加一句你想要的样子就行，",
            "不用去看代码。",
        ]
    )
    return lines
