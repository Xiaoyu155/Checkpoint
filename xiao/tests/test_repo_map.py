from __future__ import annotations

import subprocess
from pathlib import Path

from visual_agent.chief_dispatch import build_worker_command
from visual_agent.repo_map import build_repo_map, render_repo_map, repo_map_cache_path


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "billing.py").write_text(
        "class Invoice:\n"
        "    def total(self):\n"
        "        return 0\n"
        "    def currency(self):\n"
        "        return 'USD'\n"
        "\n"
        "def apply_discount(invoice):\n"
        "    return invoice\n",
        encoding="utf-8",
    )
    (repo / "src" / "app.ts").write_text(
        "export function renderCheckout() {}\n"
        "export class CartStore {}\n"
        "export const TAX_RATE = 0.2;\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return repo


def test_build_repo_map_extracts_symbols(tmp_path):
    repo = _make_repo(tmp_path)
    payload = build_repo_map(repo_root=repo, cache_path=None)
    files = payload["files"]
    assert "src/billing.py" in files
    symbols = files["src/billing.py"]["symbols"]
    assert any(item.startswith("class Invoice(") for item in symbols)
    assert "def apply_discount" in symbols
    ts_symbols = files["src/app.ts"]["symbols"]
    assert "renderCheckout" in ts_symbols
    assert "CartStore" in ts_symbols
    assert "TAX_RATE" in ts_symbols


def test_build_repo_map_handles_non_ascii_git_paths(tmp_path):
    repo = tmp_path / "proj"
    source = repo / "支付宝" / "backend" / "src"
    source.mkdir(parents=True)
    (source / "service.js").write_text(
        "export function reviewMedicalPolicy() {}\n"
        "export class ServiceCase {}\n",
        encoding="utf-8",
    )
    _git(repo, "init")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")

    payload = build_repo_map(repo_root=repo, cache_path=None)

    rel = "支付宝/backend/src/service.js"
    assert rel in payload["files"]
    assert "reviewMedicalPolicy" in payload["files"][rel]["symbols"]
    text = render_repo_map(payload, goal="支付宝 医保 service", max_lines=20)
    assert rel in text


def test_build_repo_map_incremental_reuses_cache(tmp_path):
    repo = _make_repo(tmp_path)
    cache = repo_map_cache_path(tmp_path / ".agent-workspace")
    first = build_repo_map(repo_root=repo, cache_path=cache)
    assert first["parsed"] == first["file_count"]
    assert cache.exists()

    second = build_repo_map(repo_root=repo, cache_path=cache)
    assert second["parsed"] == 0
    assert second["reused"] == second["file_count"]

    # Touching one file re-parses only that file: memory updates are incremental.
    billing = repo / "src" / "billing.py"
    billing.write_text(billing.read_text(encoding="utf-8") + "\ndef refund(invoice):\n    return invoice\n", encoding="utf-8")
    third = build_repo_map(repo_root=repo, cache_path=cache)
    assert third["parsed"] == 1
    assert "def refund" in third["files"]["src/billing.py"]["symbols"]


def test_render_repo_map_focuses_relevant_files(tmp_path):
    repo = _make_repo(tmp_path)
    payload = build_repo_map(repo_root=repo, cache_path=None)
    text = render_repo_map(payload, goal="fix billing invoice total", focus_files=["src/billing.py"], max_lines=20)
    lines = text.splitlines()
    assert len(lines) <= 20
    # The focused file appears with symbol detail; unrelated files stay in the tree summary.
    assert any("src/billing.py" in line and "Invoice" in line for line in lines)
    assert any("README.md" in line for line in lines)


def test_render_repo_map_respects_budget(tmp_path):
    repo = _make_repo(tmp_path)
    for index in range(30):
        (repo / "src" / f"mod_{index}.py").write_text(f"def fn_{index}():\n    pass\n", encoding="utf-8")
    _git(repo, "add", ".")
    payload = build_repo_map(repo_root=repo, cache_path=None)
    text = render_repo_map(payload, goal="mod fn", max_lines=12)
    assert len(text.splitlines()) <= 12


def test_build_repo_map_without_git_falls_back_to_walk(tmp_path):
    root = tmp_path / "plain"
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("export function hidden() {}\n", encoding="utf-8")
    (root / "main.py").write_text("def entry():\n    pass\n", encoding="utf-8")
    payload = build_repo_map(repo_root=root, cache_path=None)
    assert "main.py" in payload["files"]
    assert not any("node_modules" in rel for rel in payload["files"])


def test_worker_prompt_includes_repo_map_excerpt(tmp_path):
    plan = {"objective": "Fix invoice total", "acceptance_criteria": [], "worker_tracks": []}
    track = {"id": "track_1_codex", "agent": "codex"}
    command = build_worker_command(
        plan=plan,
        track=track,
        worktree=tmp_path,
        verification_command="checkpoint codex-check",
        repo_map_text="- src/billing.py (8L): class Invoice(total, currency)",
    )
    # Codex prompts travel via stdin since the Windows argv length fix.
    prompt = command.get("stdin") or command["argv"][-1]
    assert "Repository map" in prompt
    assert "class Invoice(total, currency)" in prompt
    assert "verify the real paths you need" in prompt
    assert "trust it for orientation instead of scanning" not in prompt


def test_worker_prompt_without_map_is_unchanged(tmp_path):
    plan = {"objective": "Fix invoice total", "acceptance_criteria": [], "worker_tracks": []}
    track = {"id": "track_1_codex", "agent": "codex"}
    command = build_worker_command(
        plan=plan,
        track=track,
        worktree=tmp_path,
        verification_command="checkpoint codex-check",
    )
    prompt = command.get("stdin") or command["argv"][-1]
    assert "Repository map" not in prompt
