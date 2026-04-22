"""Unit tests for smart_unfold.py — AST-bounded symbol extraction."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_SCRIPTS = Path(__file__).parent.parent.parent / "plugin" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))


# ─── Test: Python AST unfold ──────────────────────────────────────────────────


class TestSmartUnfoldPython:
    def test_smart_unfold_exact_function(self, tmp_path):
        """Extrahiert eine vollstaendige Funktion AST-bounded."""
        from smart_unfold import unfold_symbol

        src = tmp_path / "module.py"
        src.write_text(
            "def helper():\n"
            "    return 42\n"
            "\n"
            "def main_logic(x: int, y: int) -> int:\n"
            "    total = x + y\n"
            "    result = total * 2\n"
            "    if result > 100:\n"
            "        return result\n"
            "    return 0\n"
            "\n"
            "def after(): pass\n"
        )

        result = unfold_symbol(src, "main_logic")

        assert result["symbol"] == "main_logic"
        assert result["file"] == str(src)
        assert result["line_start"] == 4
        assert result["line_end"] >= 9
        assert "main_logic" in result["source_code"]
        assert "total = x + y" in result["source_code"]
        # Garantiert vollstaendig — kein mid-function cut
        assert "return 0" in result["source_code"]

    def test_smart_unfold_exact_class(self, tmp_path):
        """Extrahiert eine vollstaendige Klasse mit allen Methoden."""
        from smart_unfold import unfold_symbol

        src = tmp_path / "repo.py"
        src.write_text(
            "class UserRepository:\n"
            "    def __init__(self, db):\n"
            "        self.db = db\n"
            "\n"
            "    def find_by_id(self, user_id: int):\n"
            "        return self.db.query(user_id)\n"
            "\n"
            "    def save(self, user):\n"
            "        self.db.insert(user)\n"
            "\n"
            "class OtherClass:\n"
            "    pass\n"
        )

        result = unfold_symbol(src, "UserRepository")

        assert "UserRepository" in result["source_code"]
        assert "find_by_id" in result["source_code"]
        assert "save" in result["source_code"]
        # Stellt sicher, dass OtherClass nicht enthalten ist
        assert "OtherClass" not in result["source_code"]

    def test_smart_unfold_symbol_not_found_returns_none(self, tmp_path):
        """Gibt None zurueck wenn Symbol nicht gefunden."""
        from smart_unfold import unfold_symbol

        src = tmp_path / "mod.py"
        src.write_text("def existing(): pass\n")

        result = unfold_symbol(src, "nonexistent_symbol")

        assert result is None

    def test_smart_unfold_nested_method(self, tmp_path):
        """Findet eine Methode innerhalb einer Klasse."""
        from smart_unfold import unfold_symbol

        src = tmp_path / "service.py"
        src.write_text(
            "class DataService:\n"
            "    def process(self, data: list) -> dict:\n"
            "        cleaned = [x for x in data if x]\n"
            "        return {'count': len(cleaned)}\n"
            "\n"
            "    def validate(self, item): pass\n"
        )

        result = unfold_symbol(src, "process")

        assert result is not None
        assert "cleaned = [x for x in data if x]" in result["source_code"]
        assert "validate" not in result["source_code"]


# ─── Test: Fallback fuer nicht-Python Dateien ─────────────────────────────────


class TestSmartUnfoldFallback:
    def test_smart_unfold_fallback_ts(self, tmp_path):
        """Faellt auf indentierungs-basierten Fallback zurueck fuer .ts Dateien."""
        from smart_unfold import MockCommandRunner, unfold_symbol

        ts_file = tmp_path / "handler.ts"
        ts_file.write_text(
            "function beforeTarget() {\n"
            "  return 1;\n"
            "}\n"
            "\n"
            "function targetFunction(req: Request): Response {\n"
            "  const data = req.body;\n"
            "  const processed = transform(data);\n"
            "  return { status: 200, data: processed };\n"
            "}\n"
            "\n"
            "function afterTarget() {\n"
            "  return 2;\n"
            "}\n"
        )

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = unfold_symbol(ts_file, "targetFunction", command_runner=failing_runner)

        assert result is not None
        assert "targetFunction" in result["source_code"]
        assert "processed = transform" in result["source_code"]
        # Kein mid-function cut
        assert "afterTarget" not in result["source_code"]

    def test_smart_unfold_fallback_not_found_returns_none(self, tmp_path):
        """Fallback gibt None zurueck wenn Symbol nicht in der Datei."""
        from smart_unfold import MockCommandRunner, unfold_symbol

        ts_file = tmp_path / "empty.ts"
        ts_file.write_text("function otherFunc() {}\n")

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = unfold_symbol(ts_file, "missingSymbol", command_runner=failing_runner)

        assert result is None


# ─── Test: Ausgabe-Format ─────────────────────────────────────────────────────


class TestSmartUnfoldOutputFormat:
    def test_smart_unfold_output_fields(self, tmp_path):
        """Ausgabe enthaelt alle erwarteten Felder."""
        from smart_unfold import unfold_symbol

        src = tmp_path / "fmt.py"
        src.write_text("def my_func(a, b):\n    return a + b\n")

        result = unfold_symbol(src, "my_func")

        assert result is not None
        assert "file" in result
        assert "symbol" in result
        assert "line_start" in result
        assert "line_end" in result
        assert "source_code" in result

    def test_smart_unfold_line_start_is_one_based(self, tmp_path):
        """Zeilennummern sind 1-basiert."""
        from smart_unfold import unfold_symbol

        src = tmp_path / "lines.py"
        src.write_text("def first(): pass\n\ndef second(): pass\n")

        result = unfold_symbol(src, "first")

        assert result["line_start"] == 1

    def test_smart_unfold_source_is_complete(self, tmp_path):
        """source_code ist vollstaendig und nicht abgeschnitten."""
        from smart_unfold import unfold_symbol

        src = tmp_path / "complete.py"
        # Funktion mit 10 Zeilen
        body = "\n".join(f"    line_{i} = {i}" for i in range(8))
        src.write_text(f"def big_function():\n{body}\n    return line_7\n")

        result = unfold_symbol(src, "big_function")

        assert result is not None
        assert "return line_7" in result["source_code"]


# ─── Test: TypeScript tree-sitter AST unfold ─────────────────────────────────


class TestSmartUnfoldTypeScriptAST:
    """Tests for real tree-sitter AST parsing of TypeScript files.

    These tests verify the tree-sitter LIBRARY path (not CLI runner).
    They use a MockCommandRunner that returns failure to simulate missing ts CLI,
    but the tree-sitter library should still extract accurate boundaries.
    """

    def test_ts_unfold_function_accurate_boundaries_via_library(self, tmp_path):
        """tree-sitter library unfolds a TS function without needing CLI runner.

        With ts CLI disabled, grep fallback cannot determine end_line accurately
        for nested constructs. Tree-sitter library path must handle this via AST.
        """
        from smart_unfold import MockCommandRunner, unfold_symbol

        ts_file = tmp_path / "handler.ts"
        # Design: a class method that grep cannot end-line correctly,
        # but tree-sitter library can via accurate AST end_point
        ts_file.write_text(
            "class Helper {\n"
            "  static compute(): number { return 1; }\n"
            "}\n"
            "\n"
            "function targetFunction(req: string): string {\n"
            "  const data = req.trim();\n"
            "  const processed = data.toUpperCase();\n"
            "  return processed;\n"
            "}\n"
            "\n"
            "function afterTarget(): void {\n"
            "  console.log('after');\n"
            "}\n"
        )

        # Even with failing CLI runner, library path must work
        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = unfold_symbol(ts_file, "targetFunction", command_runner=failing_runner)

        assert result is not None
        assert result["symbol"] == "targetFunction"
        assert result["line_start"] == 5
        assert result["line_end"] == 9
        assert "targetFunction" in result["source_code"]
        assert "processed = data.toUpperCase" in result["source_code"]
        assert "afterTarget" not in result["source_code"]

    def test_ts_unfold_class_accurate_boundaries_via_library(self, tmp_path):
        """tree-sitter library extracts a TS class with accurate end_line."""
        from smart_unfold import MockCommandRunner, unfold_symbol

        ts_file = tmp_path / "service.ts"
        ts_file.write_text(
            "class UserService {\n"
            "  private users: string[] = [];\n"
            "\n"
            "  getUser(id: string): string {\n"
            "    return this.users.find(u => u === id) || '';\n"
            "  }\n"
            "\n"
            "  addUser(user: string): void {\n"
            "    this.users.push(user);\n"
            "  }\n"
            "}\n"
            "\n"
            "class OtherClass {}\n"
        )

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = unfold_symbol(ts_file, "UserService", command_runner=failing_runner)

        assert result is not None
        assert result["line_start"] == 1
        assert result["line_end"] == 11
        assert "getUser" in result["source_code"]
        assert "addUser" in result["source_code"]
        assert "OtherClass" not in result["source_code"]

    def test_ts_unfold_inline_arrow_function_accurate_end_line(self, tmp_path):
        """tree-sitter library gives accurate end_line for single-line arrow functions.

        Grep fallback _find_block_end uses brace counting, which fails for arrow
        functions without braces (e.g. 'const f = () => value;'). Tree-sitter
        gives the correct end_line via node.end_point.
        """
        from smart_unfold import MockCommandRunner, unfold_symbol

        ts_file = tmp_path / "api.ts"
        ts_file.write_text(
            "const helperFn = (): string => 'helper';\n"
            "\n"
            "const handleRequest = async (req: string): Promise<string> => {\n"
            "  const body = req.trim();\n"
            "  return body.toUpperCase();\n"
            "};\n"
            "\n"
            "const otherFn = (): string => 'other';\n"
        )

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = unfold_symbol(ts_file, "helperFn", command_runner=failing_runner)

        assert result is not None
        # tree-sitter must know helperFn is only line 1, not 6
        assert result["line_start"] == 1
        assert result["line_end"] == 1
        assert "helperFn" in result["source_code"]
        assert "handleRequest" not in result["source_code"]

    def test_ts_unfold_not_found_returns_none(self, tmp_path):
        """tree-sitter library returns None for missing symbol in TS file."""
        from smart_unfold import MockCommandRunner, unfold_symbol

        ts_file = tmp_path / "module.ts"
        ts_file.write_text("function existingFn(): void {}\n")

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = unfold_symbol(ts_file, "missingSymbol", command_runner=failing_runner)

        assert result is None

    def test_ts_unfold_output_fields(self, tmp_path):
        """tree-sitter library unfold output includes all required fields."""
        from smart_unfold import MockCommandRunner, unfold_symbol

        ts_file = tmp_path / "fmt.ts"
        ts_file.write_text(
            "function myTsFunc(a: number, b: number): number {\n"
            "  return a + b;\n"
            "}\n"
        )

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = unfold_symbol(ts_file, "myTsFunc", command_runner=failing_runner)

        assert result is not None
        assert "file" in result
        assert "symbol" in result
        assert "line_start" in result
        assert "line_end" in result
        assert "source_code" in result
        assert result["line_start"] == 1
        assert result["line_end"] == 3
