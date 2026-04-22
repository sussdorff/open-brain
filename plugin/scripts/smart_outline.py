"""smart_outline.py — AST-basiertes Struktur-Skelett fuer Dateien.

Usage:
    uv run python smart_outline.py <file_path>
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


# ─── Typen ────────────────────────────────────────────────────────────────────


@dataclass
class SymbolInfo:
    """Symbol in einer Datei."""

    name: str
    type: str
    line: int
    end_line: int
    signature: str


@dataclass
class OutlineResult:
    """Ergebnis der Datei-Analyse."""

    file: str
    symbols: list[SymbolInfo]


# ─── Python AST Outline ───────────────────────────────────────────────────────


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Baut eine lesbare Funktionssignatur aus einem AST-Knoten."""
    args = []
    func_args = node.args

    for arg in func_args.args:
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation)}"
        args.append(arg_str)

    if func_args.vararg:
        args.append(f"*{func_args.vararg.arg}")

    if func_args.kwarg:
        args.append(f"**{func_args.kwarg.arg}")

    sig = f"{node.name}({', '.join(args)})"
    if node.returns:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _outline_python(file_path: Path) -> list[SymbolInfo]:
    """Extrahiert alle top-level und class-level Symbole via ast-Modul."""
    try:
        source = file_path.read_text(encoding="utf-8")
        if not source.strip():
            return []
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []

    symbols: list[SymbolInfo] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            end_line = node.end_lineno or node.lineno
            symbols.append(SymbolInfo(
                name=node.name,
                type="class",
                line=node.lineno,
                end_line=end_line,
                signature=node.name,
            ))
            # Methoden der Klasse
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    item_end = item.end_lineno or item.lineno
                    symbols.append(SymbolInfo(
                        name=item.name,
                        type="method",
                        line=item.lineno,
                        end_line=item_end,
                        signature=_build_signature(item),
                    ))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = node.end_lineno or node.lineno
            symbols.append(SymbolInfo(
                name=node.name,
                type="function",
                line=node.lineno,
                end_line=end_line,
                signature=_build_signature(node),
            ))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                symbols.append(SymbolInfo(
                    name=alias.asname or alias.name,
                    type="import",
                    line=node.lineno,
                    end_line=node.lineno,
                    signature=f"import {alias.name}",
                ))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                symbols.append(SymbolInfo(
                    name=alias.asname or alias.name,
                    type="import",
                    line=node.lineno,
                    end_line=node.lineno,
                    signature=f"from {module} import {alias.name}",
                ))

    return symbols


# ─── Grep-Fallback fuer nicht-Python Dateien ─────────────────────────────────


_GREP_PATTERNS = [
    # TypeScript/JavaScript
    (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(<]", "function"),
    (r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
    (r"(?:export\s+)?interface\s+(\w+)", "interface"),
    (r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", "arrow_function"),
    # Go
    (r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", "function"),
    (r"^type\s+(\w+)\s+struct", "struct"),
    # Rust
    (r"^(?:pub\s+)?fn\s+(\w+)\s*[(<]", "function"),
    (r"^(?:pub\s+)?struct\s+(\w+)", "struct"),
    (r"^(?:pub\s+)?enum\s+(\w+)", "enum"),
]


def _outline_grep(file_path: Path) -> list[SymbolInfo]:
    """Extrahiert Symbole via Regex-Muster (Fallback)."""
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []

    symbols: list[SymbolInfo] = []

    for line_num, line in enumerate(lines, start=1):
        for pattern, sym_type in _GREP_PATTERNS:
            match = re.search(pattern, line)
            if match:
                name = match.group(1)
                symbols.append(SymbolInfo(
                    name=name,
                    type=sym_type,
                    line=line_num,
                    end_line=line_num,  # Ohne AST unbekannt
                    signature=line.strip()[:120],
                ))
                break

    return symbols


# ─── Hauptfunktion ────────────────────────────────────────────────────────────


def outline_file(
    file_path: Path,
    command_runner: CommandRunner | None = None,
) -> dict:
    """Erstellt ein Struktur-Skelett einer Datei.

    Args:
        file_path: Pfad zur zu analysierenden Datei
        command_runner: CommandRunner-Instanz (None = Default)

    Returns:
        Dict mit {file, symbols: [{name, type, line, end_line, signature}]}
    """
    runner = command_runner or get_default_runner()
    file_path = Path(file_path)

    if not file_path.exists():
        return {"file": str(file_path), "symbols": [], "error": "file not found"}

    if file_path.suffix == ".py":
        symbols = _outline_python(file_path)
    else:
        # Try tree-sitter library first (real AST, accurate line numbers)
        try:
            from tree_sitter_utils import parse_file_with_treesitter
            ast_symbols = parse_file_with_treesitter(file_path)
        except ImportError:
            ast_symbols = None

        if ast_symbols is not None:
            symbols = []
            for sym in ast_symbols:
                symbols.append(SymbolInfo(
                    name=sym.name,
                    type=sym.type,
                    line=sym.line_start,
                    end_line=sym.line_end,
                    signature=sym.source_code.splitlines()[0][:120] if sym.source_code else sym.name,
                ))
            if not symbols:
                symbols = _outline_grep(file_path)
        else:
            # Fallback: try tree-sitter CLI, then grep
            rc, stdout, stderr = runner.run(["ts", "parse", str(file_path)])
            if rc == 0 and stdout.strip():
                symbols = _parse_treesitter_symbols(file_path, stdout)
            else:
                symbols = _outline_grep(file_path)

    return {
        "file": str(file_path),
        "symbols": [
            {
                "name": s.name,
                "type": s.type,
                "line": s.line,
                "end_line": s.end_line,
                "signature": s.signature,
            }
            for s in symbols
        ],
    }


def _parse_treesitter_symbols(file_path: Path, output: str) -> list[SymbolInfo]:
    """Parse symbols using tree-sitter library (real AST) with grep fallback.

    The 'output' parameter is kept for backward compatibility but is no longer
    used — the tree-sitter library parses the file directly.
    """
    try:
        from tree_sitter_utils import parse_file_with_treesitter
    except ImportError:
        return _outline_grep(file_path)

    ast_symbols = parse_file_with_treesitter(file_path)
    if ast_symbols is None:
        return _outline_grep(file_path)

    results: list[SymbolInfo] = []
    for sym in ast_symbols:
        results.append(SymbolInfo(
            name=sym.name,
            type=sym.type,
            line=sym.line_start,
            end_line=sym.line_end,
            signature=sym.source_code.splitlines()[0][:120] if sym.source_code else sym.name,
        ))

    if not results:
        results = _outline_grep(file_path)

    return results


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: smart_outline.py <file_path>")
        sys.exit(1)

    result = outline_file(Path(sys.argv[1]))
    print(json.dumps(result, indent=2))
