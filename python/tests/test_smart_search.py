"""Unit tests for smart_search.py — AST-based Python symbol search with fallback."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

PLUGIN_SCRIPTS = Path(__file__).parent.parent.parent / "plugin" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))


def _write_py_file(tmp_path: Path, name: str, content: str) -> Path:
    """Schreibt eine temporaere Python-Datei."""
    p = tmp_path / name
    p.write_text(content)
    return p


# ─── Test: Python AST search ──────────────────────────────────────────────────


class TestSmartSearchPython:
    def test_smart_search_python_finds_function(self, tmp_path):
        """Findet eine Python-Funktion per Name via ast-Modul."""
        from smart_search import search_files

        src = _write_py_file(
            tmp_path,
            "example.py",
            "def process_data(items: list) -> dict:\n    return {}\n",
        )

        results = search_files("process_data", search_path=tmp_path)

        assert len(results) >= 1
        found = results[0]
        assert found["name"] == "process_data"
        assert found["type"] == "function"
        assert found["line"] == 1
        assert "process_data" in found["signature"]

    def test_smart_search_python_finds_class(self, tmp_path):
        """Findet eine Python-Klasse per Name via ast-Modul."""
        from smart_search import search_files

        _write_py_file(
            tmp_path,
            "model.py",
            "class UserRepository:\n    def __init__(self): pass\n",
        )

        results = search_files("UserRepository", search_path=tmp_path)

        assert len(results) >= 1
        assert results[0]["name"] == "UserRepository"
        assert results[0]["type"] == "class"

    def test_smart_search_python_finds_method(self, tmp_path):
        """Findet eine Methode innerhalb einer Klasse."""
        from smart_search import search_files

        _write_py_file(
            tmp_path,
            "service.py",
            "class MyService:\n    def get_user(self, user_id: int) -> dict:\n        pass\n",
        )

        results = search_files("get_user", search_path=tmp_path)

        assert len(results) >= 1
        match = next(r for r in results if r["name"] == "get_user")
        assert match["type"] == "method"

    def test_smart_search_result_includes_file_path(self, tmp_path):
        """Ergebnisse enthalten den Dateipfad."""
        from smart_search import search_files

        src = _write_py_file(tmp_path, "mymod.py", "def my_func(): pass\n")

        results = search_files("my_func", search_path=tmp_path)

        assert len(results) >= 1
        assert "file" in results[0]
        assert "mymod.py" in results[0]["file"]


# ─── Test: Ranking ────────────────────────────────────────────────────────────


class TestSmartSearchRanking:
    def test_smart_search_ranking(self, tmp_path):
        """Exact match rankt hoher als Prefix, Prefix hoeher als Substring."""
        from smart_search import rank_results

        results = [
            {"name": "process_data_fast", "type": "function", "file": "a.py", "line": 10},
            {"name": "process_data", "type": "function", "file": "b.py", "line": 5},
            {"name": "pre_process_data_items", "type": "function", "file": "c.py", "line": 1},
        ]

        ranked = rank_results(results, query="process_data")

        # Exact match first
        assert ranked[0]["name"] == "process_data"
        # Prefix match second (starts with query)
        assert ranked[1]["name"] == "process_data_fast"
        # Substring last
        assert ranked[2]["name"] == "pre_process_data_items"

    def test_ranking_case_insensitive(self, tmp_path):
        """Ranking ist case-insensitive."""
        from smart_search import rank_results

        results = [
            {"name": "ProcessData", "type": "class", "file": "a.py", "line": 1},
            {"name": "process_data_helper", "type": "function", "file": "b.py", "line": 1},
        ]

        ranked = rank_results(results, query="processdata")

        assert ranked[0]["name"] == "ProcessData"


# ─── Test: Fallback fuer nicht-Python Dateien ─────────────────────────────────


class TestSmartSearchFallback:
    def test_smart_search_fallback_ts_no_treesitter(self, tmp_path):
        """Faellt bei .ts-Dateien ohne tree-sitter auf grep-Fallback zurueck."""
        from smart_search import MockCommandRunner, search_files

        ts_file = tmp_path / "api.ts"
        ts_file.write_text(
            "export function fetchUser(id: string): Promise<User> {\n  return api.get(id);\n}\n"
        )

        # MockCommandRunner mit non-zero exit simuliert fehlendes tree-sitter
        failing_runner = MockCommandRunner(default_response=(127, "", "command not found"))
        results = search_files(
            "fetchUser", search_path=tmp_path, pattern="*.ts", command_runner=failing_runner
        )

        assert len(results) >= 1
        assert results[0]["name"] == "fetchUser"

    def test_smart_search_fallback_returns_line_context(self, tmp_path):
        """Grep-Fallback liefert Zeile und Kontext."""
        from smart_search import MockCommandRunner, search_files

        js_file = tmp_path / "utils.js"
        js_file.write_text("function calculateTotal(items) {\n  return items.reduce(...);\n}\n")

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        results = search_files(
            "calculateTotal",
            search_path=tmp_path,
            pattern="*.js",
            command_runner=failing_runner,
        )

        assert len(results) >= 1
        assert results[0]["line"] >= 1

    def test_no_treesitter_installed(self, tmp_path):
        """Python-only Modus: Kein Absturz wenn tree-sitter CLI fehlt."""
        from smart_search import MockCommandRunner, search_files

        # Erstelle .py und .ts Dateien
        _write_py_file(tmp_path, "core.py", "def core_logic(): pass\n")
        (tmp_path / "extra.ts").write_text("function extraHelper() {}\n")

        failing_runner = MockCommandRunner(default_response=(127, "", "ts: command not found"))

        # Kein Exception, Python-Symbole werden gefunden
        results = search_files(
            "core_logic", search_path=tmp_path, command_runner=failing_runner
        )

        assert len(results) >= 1
        assert results[0]["name"] == "core_logic"
        # No exception raised


# ─── Test: Max-Results Limit ──────────────────────────────────────────────────


class TestSmartSearchMaxResults:
    def test_max_results_limits_output(self, tmp_path):
        """max_results begrenzt die Ausgabe."""
        from smart_search import search_files

        content = "\n".join(
            f"def func_{i}(): pass" for i in range(20)
        )
        _write_py_file(tmp_path, "many.py", content)

        results = search_files("func_", search_path=tmp_path, max_results=5)

        assert len(results) <= 5

    def test_search_multiple_files(self, tmp_path):
        """Sucht rekursiv in allen passenden Dateien."""
        from smart_search import search_files

        subdir = tmp_path / "sub"
        subdir.mkdir()
        _write_py_file(tmp_path, "root.py", "def target_fn(): pass\n")
        _write_py_file(subdir, "nested.py", "def target_fn(): pass\n")

        results = search_files("target_fn", search_path=tmp_path)

        assert len(results) == 2
