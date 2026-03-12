"""smart_unfold.py — AST-bounded Symbol-Extraktion.

Usage:
    uv run python smart_unfold.py <file_path> <symbol_name>
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


# ─── CommandRunner DI ─────────────────────────────────────────────────────────


class CommandRunner(Protocol):
    """Protokoll fuer subprocess-Aufrufe."""

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Fuehrt einen Befehl aus."""
        ...


@dataclass
class DefaultCommandRunner:
    """Echter subprocess-Runner mit sicherer Umgebung."""

    _SAFE_ENV: dict[str, str] = field(default_factory=lambda: {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", ""),
    })

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Fuehrt Befehl aus."""
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, env=self._SAFE_ENV
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "timeout"
        except FileNotFoundError:
            return 127, "", f"command not found: {cmd[0]}"


@dataclass
class MockCommandRunner:
    """Test-Runner mit konfigurierbaren Antworten."""

    responses: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    default_response: tuple[int, str, str] = (0, "", "")

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Gibt konfigurierte Antwort zurueck."""
        key = " ".join(cmd)
        for pattern, response in self.responses.items():
            if pattern in key:
                return response
        return self.default_response


def get_default_runner() -> CommandRunner:
    """Gibt den Standard-CommandRunner zurueck."""
    return DefaultCommandRunner()


# ─── Python AST Unfold ────────────────────────────────────────────────────────


def _find_python_symbol(
    tree: ast.AST, symbol_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None:
    """Findet ein Symbol im AST (top-level oder Klassenmethode)."""
    # Top-level suchen
    if isinstance(tree, ast.Module):
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol_name:
                    return node
            # Methoden in Klassen suchen
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name == symbol_name:
                            return item

    return None


def _extract_python_symbol(file_path: Path, symbol_name: str) -> dict | None:
    """Extrahiert ein Symbol aus einer Python-Datei via ast-Modul.

    Args:
        file_path: Pfad zur Python-Datei
        symbol_name: Name des gesuchten Symbols

    Returns:
        Dict mit {file, symbol, line_start, line_end, source_code} oder None
    """
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return None

    node = _find_python_symbol(tree, symbol_name)
    if node is None:
        return None

    line_start = node.lineno
    line_end = node.end_lineno or node.lineno

    # Quellcode-Extraktion (1-basiert)
    source_lines = source.splitlines()
    symbol_lines = source_lines[line_start - 1 : line_end]
    source_code = "\n".join(symbol_lines)

    return {
        "file": str(file_path),
        "symbol": symbol_name,
        "line_start": line_start,
        "line_end": line_end,
        "source_code": source_code,
    }


# ─── Grep-Fallback fuer nicht-Python Dateien ─────────────────────────────────

# Muster die einen Symbolstart erkennen
_START_PATTERNS = [
    # TypeScript/JavaScript Funktionen
    r"(?:export\s+)?(?:async\s+)?function\s+{name}\s*[\(<]",
    # TypeScript/JavaScript Arrow Functions
    r"(?:export\s+)?(?:const|let|var)\s+{name}\s*=",
    # Klassen
    r"(?:export\s+)?(?:abstract\s+)?class\s+{name}[\s{{<]",
    # Go Funktionen
    r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?{name}\s*\(",
    # Rust Funktionen
    r"(?:pub\s+)?(?:async\s+)?fn\s+{name}\s*[(<]",
]


def _find_block_end(lines: list[str], start_idx: int) -> int:
    """Findet das Ende eines { }-Blocks oder indentierten Blocks.

    Args:
        lines: Alle Zeilen der Datei (0-basiert)
        start_idx: Startzeile des Blocks (0-basiert)

    Returns:
        End-Zeilenindex (0-basiert, inklusiv)
    """
    start_line = lines[start_idx]

    # Strategie 1: Geschweifte Klammern zaehlen
    brace_count = 0
    found_opening = False

    for i in range(start_idx, len(lines)):
        line = lines[i]
        for char in line:
            if char == "{":
                brace_count += 1
                found_opening = True
            elif char == "}":
                brace_count -= 1
                if found_opening and brace_count == 0:
                    return i

    # Strategie 2: Indentierung verfolgen (Python-Stil)
    if not found_opening:
        # Basis-Einrueckung des Startblocks bestimmen
        base_indent = len(start_line) - len(start_line.lstrip())

        for i in range(start_idx + 1, len(lines)):
            line = lines[i]
            if not line.strip():  # Leerzeilen ueberspringen
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                return i - 1

    return len(lines) - 1


def _extract_grep_symbol(file_path: Path, symbol_name: str) -> dict | None:
    """Extrahiert ein Symbol via Regex + Indentierungs-Heuristik (Fallback).

    Args:
        file_path: Pfad zur Datei
        symbol_name: Name des gesuchten Symbols

    Returns:
        Dict oder None
    """
    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
    except UnicodeDecodeError:
        return None

    # Suche nach Symbolstart
    start_idx: int | None = None

    for pattern_template in _START_PATTERNS:
        pattern = pattern_template.replace("{name}", re.escape(symbol_name))
        for i, line in enumerate(lines):
            if re.search(pattern, line):
                start_idx = i
                break
        if start_idx is not None:
            break

    if start_idx is None:
        return None

    end_idx = _find_block_end(lines, start_idx)
    symbol_lines = lines[start_idx : end_idx + 1]

    return {
        "file": str(file_path),
        "symbol": symbol_name,
        "line_start": start_idx + 1,  # 1-basiert
        "line_end": end_idx + 1,  # 1-basiert
        "source_code": "\n".join(symbol_lines),
    }


# ─── Hauptfunktion ────────────────────────────────────────────────────────────


def unfold_symbol(
    file_path: Path,
    symbol_name: str,
    command_runner: CommandRunner | None = None,
) -> dict | None:
    """Extrahiert ein vollstaendiges Symbol AST-bounded aus einer Datei.

    Args:
        file_path: Pfad zur zu analysierenden Datei
        symbol_name: Name des gesuchten Symbols
        command_runner: CommandRunner-Instanz (None = Default)

    Returns:
        Dict mit {file, symbol, line_start, line_end, source_code} oder None
    """
    runner = command_runner or get_default_runner()
    file_path = Path(file_path)

    if not file_path.exists():
        return None

    if file_path.suffix == ".py":
        return _extract_python_symbol(file_path, symbol_name)

    # Versuche tree-sitter CLI
    rc, stdout, stderr = runner.run(["ts", "parse", str(file_path)])
    if rc == 0 and stdout.strip():
        result = _parse_treesitter_symbol(file_path, symbol_name, stdout)
        if result:
            return result

    # Grep-Fallback
    return _extract_grep_symbol(file_path, symbol_name)


def _parse_treesitter_symbol(
    file_path: Path, symbol_name: str, ts_output: str
) -> dict | None:
    """Parst tree-sitter Output und sucht nach einem bestimmten Symbol."""
    # Vereinfachte Implementierung — faellt auf grep-Fallback zurueck
    # wenn keine strukturierten Daten verfuegbar
    return None


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: smart_unfold.py <file_path> <symbol_name>")
        sys.exit(1)

    file_path = Path(sys.argv[1])
    symbol_name = sys.argv[2]

    result = unfold_symbol(file_path, symbol_name)
    if result is None:
        print(json.dumps({"error": f"Symbol '{symbol_name}' not found in {file_path}"}))
        sys.exit(1)
    else:
        print(json.dumps(result, indent=2))
