"""Tests for diff_summary.py — Layer 2 of the three-tier acceptance architecture."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from visual_agent.diff_summary import (
    _build_checklist,
    _extract_symbols,
    build_diff_summary,
    format_diff_summary,
)


# ── helper ───────────────────────────────────────────────────────────────────

def _make_stat_output(entries: list[tuple[int, int, str]]) -> str:
    return "\n".join(f"{a}\t{r}\t{p}" for a, r, p in entries)


def _make_numstat_side_effect(*runs: str):
    """Return successive stdout values for subprocess.run calls."""
    calls = iter(runs)

    def side_effect(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 0
        try:
            m.stdout = next(calls)
        except StopIteration:
            m.stdout = ""
        m.stderr = ""
        return m

    return side_effect


# ── build_diff_summary ───────────────────────────────────────────────────────

class TestBuildDiffSummary:
    def test_no_changes(self, tmp_path):
        with patch("visual_agent.diff_summary.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = build_diff_summary(repo_root=tmp_path, base_ref="HEAD")

        assert result["file_count"] == 0
        assert result["lines_added"] == 0
        assert result["lines_removed"] == 0
        assert result["large_diff"] is False
        assert result["changed_files"] == []
        assert result["user_checklist"]  # always has at least one fallback

    def test_python_changes(self, tmp_path):
        stat_out = _make_stat_output([(10, 3, "src/foo.py"), (2, 0, "tests/test_foo.py")])
        diff_text = (
            "+def new_function(x):\n"
            "+    return x + 1\n"
            "+class MyClass:\n"
            "+    pass\n"
        )
        name_status = "M\tsrc/foo.py\nA\ttests/test_foo.py"

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "--numstat" in cmd and "--cached" not in cmd:
                m.stdout = stat_out
            elif "--numstat" in cmd and "--cached" in cmd:
                m.stdout = ""
            elif "--name-status" in cmd:
                m.stdout = name_status
            else:
                m.stdout = diff_text
            m.stderr = ""
            return m

        with patch("visual_agent.diff_summary.subprocess.run", side_effect=fake_run):
            result = build_diff_summary(repo_root=tmp_path, base_ref="HEAD")

        assert result["file_count"] == 2
        assert result["lines_added"] == 12
        assert result["lines_removed"] == 3
        assert result["large_diff"] is False
        paths = [f["path"] for f in result["changed_files"]]
        assert "src/foo.py" in paths
        symbol_names = {s["name"] for s in result["functions_touched"]}
        assert "new_function" in symbol_names
        assert "MyClass" in symbol_names
        # Python hint in checklist
        assert any("pytest" in h or "API" in h or "模块" in h for h in result["user_checklist"])

    def test_large_diff_flag(self, tmp_path):
        # 45 files, each 1 line changed => large_diff
        entries = [(1, 0, f"src/module_{i}.py") for i in range(45)]
        stat_out = _make_stat_output(entries)

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "--numstat" in cmd and "--cached" not in cmd:
                m.stdout = stat_out
            else:
                m.stdout = ""
            m.stderr = ""
            return m

        with patch("visual_agent.diff_summary.subprocess.run", side_effect=fake_run):
            result = build_diff_summary(repo_root=tmp_path, base_ref="HEAD")

        assert result["large_diff"] is True
        assert result["file_count"] == 45

    def test_large_lines_flag(self, tmp_path):
        # 5 files, 500 lines each => total 2500 > 2000 threshold
        stat_out = _make_stat_output([(500, 0, f"src/big_{i}.py") for i in range(5)])

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "--numstat" in cmd and "--cached" not in cmd:
                m.stdout = stat_out
            else:
                m.stdout = ""
            m.stderr = ""
            return m

        with patch("visual_agent.diff_summary.subprocess.run", side_effect=fake_run):
            result = build_diff_summary(repo_root=tmp_path, base_ref="HEAD")

        assert result["large_diff"] is True

    def test_multi_language_checklist(self, tmp_path):
        stat_out = _make_stat_output([(5, 1, "lib/ui.dart"), (3, 0, "backend/api.py")])

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            if "--numstat" in cmd and "--cached" not in cmd:
                m.stdout = stat_out
            else:
                m.stdout = ""
            m.stderr = ""
            return m

        with patch("visual_agent.diff_summary.subprocess.run", side_effect=fake_run):
            result = build_diff_summary(repo_root=tmp_path)

        checklist = result["user_checklist"]
        # Should have hints for both flutter and python families
        assert len(checklist) >= 1
        joined = " ".join(checklist)
        assert "Flutter" in joined or "pytest" in joined or "API" in joined

    def test_subprocess_error_degrades_gracefully(self, tmp_path):
        with patch("visual_agent.diff_summary.subprocess.run", side_effect=OSError("git not found")):
            result = build_diff_summary(repo_root=tmp_path)

        assert result["file_count"] == 0
        assert result["large_diff"] is False
        assert isinstance(result["user_checklist"], list)

    def test_untracked_files_are_included(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "new.js").write_text("function added() {\n  return true;\n}\n", encoding="utf-8")

        result = build_diff_summary(repo_root=tmp_path, base_ref="HEAD")

        assert {
            "path": "src/new.js",
            "status": "A",
            "lines_added": 3,
            "lines_removed": 0,
        } in result["changed_files"]
        assert any(symbol["name"] == "added" for symbol in result["functions_touched"])

    def test_chinese_paths_are_not_quoted(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "devpacer@example.local"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "DevPacer"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)

        target = tmp_path / "快手" / "feature.js"
        target.parent.mkdir()
        target.write_text("export const feature = true;\n", encoding="utf-8")

        result = build_diff_summary(repo_root=tmp_path, base_ref="HEAD")
        paths = [item["path"] for item in result["changed_files"]]

        assert "快手/feature.js" in paths
        assert not any("\\345" in path or path.startswith('"') for path in paths)


# ── _extract_symbols ─────────────────────────────────────────────────────────

class TestExtractSymbols:
    def test_python_def(self):
        diff = "+def my_function(x):\n+    return x\n"
        symbols = _extract_symbols(diff)
        names = {s["name"] for s in symbols}
        assert "my_function" in names

    def test_python_class(self):
        diff = "+class MyModel(Base):\n+    pass\n"
        symbols = _extract_symbols(diff)
        names = {s["name"] for s in symbols}
        assert "MyModel" in names

    def test_go_func(self):
        diff = "+func HandleRequest(w http.ResponseWriter, r *http.Request) {\n"
        symbols = _extract_symbols(diff)
        names = {s["name"] for s in symbols}
        assert "HandleRequest" in names

    def test_dart_class(self):
        diff = "+class LoginPage extends StatefulWidget {\n"
        symbols = _extract_symbols(diff)
        names = {s["name"] for s in symbols}
        assert "LoginPage" in names

    def test_empty_diff(self):
        assert _extract_symbols("") == []

    def test_removed_lines_ignored(self):
        # Lines starting with "-" should not produce symbols
        diff = "-def old_function(x):\n-    return x\n"
        symbols = _extract_symbols(diff)
        assert not any(s["name"] == "old_function" for s in symbols)


# ── _build_checklist ─────────────────────────────────────────────────────────

class TestBuildChecklist:
    def test_dart_hint(self):
        checklist = _build_checklist({"dart"}, [])
        assert any("Flutter" in h for h in checklist)

    def test_python_hint(self):
        checklist = _build_checklist({"py"}, [])
        assert any("pytest" in h or "API" in h or "模块" in h for h in checklist)

    def test_unknown_ext_fallback(self):
        checklist = _build_checklist({"xyz"}, [])
        assert len(checklist) >= 1

    def test_migration_file_detected(self):
        file_stats = [{"path": "db/migrations/0001_initial.sql", "lines_added": 5, "lines_removed": 0}]
        checklist = _build_checklist({"sql"}, file_stats)
        assert any("迁移" in h or "migration" in h.lower() or "数据库" in h for h in checklist)


# ── format_diff_summary ───────────────────────────────────────────────────────

class TestFormatDiffSummary:
    def test_renders_markdown(self):
        summary = {
            "file_count": 2,
            "lines_added": 10,
            "lines_removed": 3,
            "large_diff": False,
            "changed_files": [{"path": "src/foo.py", "status": "M", "lines_added": 10, "lines_removed": 3}],
            "functions_touched": [{"name": "do_thing", "lang": "Python"}],
            "user_checklist": ["运行 pytest"],
            "summary_text": "改了 2 个文件。",
        }
        md = format_diff_summary(summary)
        assert "改动摘要" in md
        assert "src/foo.py" in md
        assert "do_thing" in md
        assert "pytest" in md

    def test_large_diff_warning_shown(self):
        summary = {
            "file_count": 50,
            "lines_added": 3000,
            "lines_removed": 0,
            "large_diff": True,
            "changed_files": [],
            "functions_touched": [],
            "user_checklist": [],
            "summary_text": "",
        }
        md = format_diff_summary(summary)
        assert "异常大" in md or "large" in md.lower()

    def test_no_changes_renders_cleanly(self):
        summary = {
            "file_count": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "large_diff": False,
            "changed_files": [],
            "functions_touched": [],
            "user_checklist": [],
            "summary_text": "",
        }
        md = format_diff_summary(summary)
        assert isinstance(md, str)
