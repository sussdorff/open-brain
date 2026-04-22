"""Unit tests for smart_outline.py — AST-based file structure skeleton."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_SCRIPTS = Path(__file__).parent.parent.parent / "plugin" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))


# ─── Test: Python AST outline ─────────────────────────────────────────────────


class TestSmartOutlinePython:
    def test_smart_outline_classes_functions(self, tmp_path):
        """Gibt Klassen und Funktionen mit Zeilennummern zurueck."""
        from smart_outline import outline_file

        src = tmp_path / "module.py"
        src.write_text(
            "class MyClass:\n"
            "    def method_a(self): pass\n"
            "    def method_b(self): pass\n"
            "\n"
            "def standalone_func(x: int) -> str:\n"
            "    return str(x)\n"
        )

        result = outline_file(src)

        assert result["file"] == str(src)
        symbols = result["symbols"]

        names = [s["name"] for s in symbols]
        assert "MyClass" in names
        assert "method_a" in names
        assert "method_b" in names
        assert "standalone_func" in names

    def test_smart_outline_includes_line_numbers(self, tmp_path):
        """Symbole enthalten Zeilen-Start und Zeilen-Ende."""
        from smart_outline import outline_file

        src = tmp_path / "sample.py"
        src.write_text(
            "def alpha():\n"
            "    x = 1\n"
            "    return x\n"
            "\n"
            "def beta(): pass\n"
        )

        result = outline_file(src)

        symbols = {s["name"]: s for s in result["symbols"]}
        assert "alpha" in symbols
        assert symbols["alpha"]["line"] == 1
        assert symbols["alpha"]["end_line"] >= 3

    def test_smart_outline_includes_type(self, tmp_path):
        """Jedes Symbol hat einen Typ: class, function oder method."""
        from smart_outline import outline_file

        src = tmp_path / "typed.py"
        src.write_text(
            "class Foo:\n"
            "    def bar(self): pass\n"
            "\n"
            "def baz(): pass\n"
        )

        result = outline_file(src)

        sym_map = {s["name"]: s["type"] for s in result["symbols"]}
        assert sym_map["Foo"] == "class"
        assert sym_map["bar"] == "method"
        assert sym_map["baz"] == "function"

    def test_smart_outline_includes_signature(self, tmp_path):
        """Funktionen enthalten ihre Signatur."""
        from smart_outline import outline_file

        src = tmp_path / "sig.py"
        src.write_text("def greet(name: str, age: int = 0) -> str:\n    pass\n")

        result = outline_file(src)

        sym = next(s for s in result["symbols"] if s["name"] == "greet")
        assert "greet" in sym["signature"]
        assert "name" in sym["signature"]

    def test_smart_outline_empty_file(self, tmp_path):
        """Leere Datei liefert leere Symbolliste."""
        from smart_outline import outline_file

        src = tmp_path / "empty.py"
        src.write_text("")

        result = outline_file(src)

        assert result["symbols"] == []

    def test_smart_outline_is_shorter_than_full_file(self, tmp_path):
        """Outline ist deutlich kuerzer als die vollstaendige Datei."""
        from smart_outline import outline_file
        import json

        content = "\n".join(
            [
                f"def func_{i}(x: int, y: str) -> bool:\n"
                + "\n".join(f"    impl_line_{j} = {j} * 2 + {i}" for j in range(20))
                + "\n    return impl_line_19\n"
                for i in range(10)
            ]
        )
        src = tmp_path / "big.py"
        src.write_text(content)

        result = outline_file(src)
        # Symbolliste sollte wesentlich kompakter sein als der Quellcode
        # (nur Signaturen, keine Implementierungsdetails)
        symbol_count = len(result["symbols"])
        assert symbol_count == 10  # 10 Funktionen
        # Outline enthaelt keine Implementierungszeilen
        for sym in result["symbols"]:
            assert "impl_line" not in sym["signature"]


# ─── Test: Fallback fuer nicht-Python Dateien ─────────────────────────────────


class TestSmartOutlineFallback:
    def test_smart_outline_ts_fallback_no_treesitter(self, tmp_path):
        """Faellt auf grep-Fallback zurueck wenn tree-sitter nicht verfuegbar."""
        from smart_outline import MockCommandRunner, outline_file

        ts_file = tmp_path / "service.ts"
        ts_file.write_text(
            "interface UserService {\n"
            "  getUser(id: string): User;\n"
            "}\n"
            "\n"
            "class UserServiceImpl implements UserService {\n"
            "  getUser(id: string): User {\n"
            "    return db.find(id);\n"
            "  }\n"
            "}\n"
        )

        failing_runner = MockCommandRunner(default_response=(127, "", "command not found"))
        result = outline_file(ts_file, command_runner=failing_runner)

        assert result["file"] == str(ts_file)
        names = [s["name"] for s in result["symbols"]]
        assert len(names) >= 1

    def test_smart_outline_js_fallback_detects_functions(self, tmp_path):
        """Grep-Fallback erkennt JavaScript Funktionen."""
        from smart_outline import MockCommandRunner, outline_file

        js_file = tmp_path / "utils.js"
        js_file.write_text(
            "function processOrder(order) {\n"
            "  return order.total * 1.1;\n"
            "}\n"
            "\n"
            "class OrderManager {\n"
            "  constructor() {}\n"
            "}\n"
        )

        failing_runner = MockCommandRunner(default_response=(127, "", "not found"))
        result = outline_file(js_file, command_runner=failing_runner)

        names = [s["name"] for s in result["symbols"]]
        assert "processOrder" in names or len(names) >= 1


# ─── Test: TypeScript tree-sitter AST outline ────────────────────────────────


class TestSmartOutlineTypeScriptAST:
    """Tests for real tree-sitter AST parsing of TypeScript files."""

    def _make_ts_file(self, tmp_path, content: str, name: str = "module.ts"):
        f = tmp_path / name
        f.write_text(content)
        return f

    def test_ts_outline_function_declaration_accurate_lines(self, tmp_path):
        """tree-sitter outline gives accurate line numbers for function declarations."""
        from smart_outline import outline_file

        ts_file = self._make_ts_file(
            tmp_path,
            "function alpha(): void {\n"
            "  console.log('a');\n"
            "}\n"
            "\n"
            "function beta(x: number): number {\n"
            "  return x * 2;\n"
            "}\n",
        )

        result = outline_file(ts_file)

        symbols = {s["name"]: s for s in result["symbols"]}
        assert "alpha" in symbols, f"Expected 'alpha' in {list(symbols)}"
        assert symbols["alpha"]["line"] == 1
        assert symbols["alpha"]["end_line"] == 3
        assert symbols["beta"]["line"] == 5
        assert symbols["beta"]["end_line"] == 7

    def test_ts_outline_class_declaration_accurate_lines(self, tmp_path):
        """tree-sitter outline gives accurate line numbers for class declarations."""
        from smart_outline import outline_file

        ts_file = self._make_ts_file(
            tmp_path,
            "class MyService {\n"
            "  getUser(id: string): User {\n"
            "    return db.find(id);\n"
            "  }\n"
            "}\n",
        )

        result = outline_file(ts_file)

        symbols = {s["name"]: s for s in result["symbols"]}
        assert "MyService" in symbols
        assert symbols["MyService"]["line"] == 1
        assert symbols["MyService"]["end_line"] == 5
        assert symbols["MyService"]["type"] == "class"

    def test_ts_outline_interface_declaration(self, tmp_path):
        """tree-sitter outline detects interface declarations."""
        from smart_outline import outline_file

        ts_file = self._make_ts_file(
            tmp_path,
            "interface Config {\n"
            "  host: string;\n"
            "  port: number;\n"
            "}\n",
        )

        result = outline_file(ts_file)

        symbols = {s["name"]: s for s in result["symbols"]}
        assert "Config" in symbols
        assert symbols["Config"]["type"] == "interface"
        assert symbols["Config"]["line"] == 1
        assert symbols["Config"]["end_line"] == 4

    def test_ts_outline_arrow_function_variable(self, tmp_path):
        """tree-sitter outline detects arrow function variables."""
        from smart_outline import outline_file

        ts_file = self._make_ts_file(
            tmp_path,
            "const handler = async (req: Request): Promise<Response> => {\n"
            "  return new Response();\n"
            "};\n",
        )

        result = outline_file(ts_file)

        symbols = {s["name"]: s for s in result["symbols"]}
        assert "handler" in symbols
        assert symbols["handler"]["type"] == "arrow_function"
        assert symbols["handler"]["line"] == 1
        assert symbols["handler"]["end_line"] == 3

    def test_ts_outline_method_definition(self, tmp_path):
        """tree-sitter outline detects method definitions inside classes."""
        from smart_outline import outline_file

        ts_file = self._make_ts_file(
            tmp_path,
            "class Api {\n"
            "  fetchData(url: string): Promise<void> {\n"
            "    return fetch(url);\n"
            "  }\n"
            "\n"
            "  postData(url: string, body: object): Promise<void> {\n"
            "    return fetch(url, { method: 'POST', body });\n"
            "  }\n"
            "}\n",
        )

        result = outline_file(ts_file)

        names = [s["name"] for s in result["symbols"]]
        assert "fetchData" in names
        assert "postData" in names
        methods = {s["name"]: s for s in result["symbols"] if s["name"] in ("fetchData", "postData")}
        assert methods["fetchData"]["type"] == "method"
        assert methods["fetchData"]["line"] == 2
        assert methods["fetchData"]["end_line"] == 4

    def test_ts_outline_exported_function(self, tmp_path):
        """tree-sitter outline handles exported function declarations."""
        from smart_outline import outline_file

        ts_file = self._make_ts_file(
            tmp_path,
            "export function greet(name: string): string {\n"
            "  return 'Hello ' + name;\n"
            "}\n",
        )

        result = outline_file(ts_file)

        symbols = {s["name"]: s for s in result["symbols"]}
        assert "greet" in symbols
        assert symbols["greet"]["type"] == "function"
        assert symbols["greet"]["line"] == 1
        assert symbols["greet"]["end_line"] == 3
