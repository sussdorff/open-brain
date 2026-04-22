"""smart_search.py — AST-basierte Symbolsuche fuer Python, grep-Fallback fuer andere Sprachen.

Usage:
    uv run python smart_search.py <query> [--path=.] [--pattern=*.py] [--max-results=20]
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
        """Fuehrt einen Befehl aus.

        Args:
            cmd: Befehlsliste

        Returns:
            (returncode, stdout, stderr)
        """
        ...


@dataclass
class DefaultCommandRunner:
    """Echter subprocess-Runner mit sicherer Umgebung."""

    _SAFE_ENV: dict[str, str] = field(default_factory=lambda: {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", ""),
    })

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Fuehrt einen Befehl aus und gibt (returncode, stdout, stderr) zurueck."""
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                env=self._SAFE_ENV,
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
        """Gibt konfigurierte Antwort oder Default zurueck."""
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
class SymbolResult:
    """Gefundenes Symbol."""

    file: str
    line: int
    type: str
    name: str
    signature: str
    context: str = ""


# ─── Python AST Analyse ───────────────────────────────────────────────────────


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Baut eine lesbare Funktionssignatur aus einem AST-Knoten."""
    args = []
    func_args = node.args

    # Positionsargumente
    for arg in func_args.args:
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation)}"
        args.append(arg_str)

    # *args
    if func_args.vararg:
        args.append(f"*{func_args.vararg.arg}")

    # **kwargs
    if func_args.kwarg:
        args.append(f"**{func_args.kwarg.arg}")

    sig = f"{node.name}({', '.join(args)})"
    if node.returns:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _extract_python_symbols(
    file_path: Path, parent_class: str | None = None
) -> list[SymbolResult]:
    """Extrahiert alle Symbole aus einer Python-Datei via ast-Modul."""
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []

    results: list[SymbolResult] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            results.append(SymbolResult(
                file=str(file_path),
                line=node.lineno,
                type="class",
                name=node.name,
                signature=node.name,
            ))
            # Methoden der Klasse
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    results.append(SymbolResult(
                        file=str(file_path),
                        line=item.lineno,
                        type="method",
                        name=item.name,
                        signature=_build_signature(item),
                        context=f"class {node.name}",
                    ))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Nur top-level Funktionen (Methoden werden oben erfasst)
            # Pruefe ob es eine Methode ist (parent ist ClassDef)
            is_method = False
            for potential_parent in ast.walk(tree):
                if isinstance(potential_parent, ast.ClassDef):
                    for child in potential_parent.body:
                        if child is node:
                            is_method = True
                            break

            if not is_method:
                results.append(SymbolResult(
                    file=str(file_path),
                    line=node.lineno,
                    type="function",
                    name=node.name,
                    signature=_build_signature(node),
                ))

    return results


# ─── Grep-Fallback fuer nicht-Python Dateien ─────────────────────────────────


_SYMBOL_PATTERNS = [
    # TypeScript/JavaScript
    (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
    (r"(?:export\s+)?class\s+(\w+)", "class"),
    (r"(?:export\s+)?interface\s+(\w+)", "interface"),
    (r"const\s+(\w+)\s*=\s*(?:async\s*)?\(", "arrow_function"),
    # Go
    (r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(", "function"),
    # Rust
    (r"(?:pub\s+)?fn\s+(\w+)\s*[(<]", "function"),
    (r"(?:pub\s+)?struct\s+(\w+)", "struct"),
]


def _extract_grep_symbols(file_path: Path) -> list[SymbolResult]:
    """Extrahiert Symbole via regex-Muster (Fallback fuer nicht-Python Dateien)."""
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []

    results: list[SymbolResult] = []

    for line_num, line in enumerate(lines, start=1):
        for pattern, sym_type in _SYMBOL_PATTERNS:
            match = re.search(pattern, line)
            if match:
                name = match.group(1)
                results.append(SymbolResult(
                    file=str(file_path),
                    line=line_num,
                    type=sym_type,
                    name=name,
                    signature=line.strip()[:100],
                    context=line.strip(),
                ))
                break  # Nur erste Uebereinstimmung pro Zeile

    return results


# ─── Ranking ──────────────────────────────────────────────────────────────────


def _rank_score(name: str, query: str) -> int:
    """Berechnet einen Ranking-Score: hoeher ist besser.

    Exact match > prefix match > substring match
    """
    name_lower = name.lower()
    query_lower = query.lower().replace("_", "").replace("-", "")
    name_normalized = name_lower.replace("_", "").replace("-", "")

    if name_lower == query.lower():
        return 100
    if name_normalized == query_lower:
        return 90
    if name_lower.startswith(query.lower()):
        return 70
    if name_normalized.startswith(query_lower):
        return 60
    if query.lower() in name_lower:
        return 40
    if query_lower in name_normalized:
        return 30
    return 0


def rank_results(
    results: list[dict], query: str
) -> list[dict]:
    """Sortiert Ergebnisse nach Relevanz zum Query.

    Args:
        results: Liste von Symbol-Dicts
        query: Suchbegriff

    Returns:
        Sortierte Liste (beste Uebereinstimmung zuerst)
    """
    return sorted(results, key=lambda r: _rank_score(r["name"], query), reverse=True)


# ─── Hauptfunktion ────────────────────────────────────────────────────────────


def search_files(
    query: str,
    search_path: Path = Path("."),
    pattern: str = "**/*.py",
    max_results: int = 20,
    command_runner: CommandRunner | None = None,
) -> list[dict]:
    """Sucht nach Symbolen in Dateien.

    Args:
        query: Symbolname oder Teilname
        search_path: Verzeichnis fuer die rekursive Suche
        pattern: Glob-Muster fuer Dateifilter
        max_results: Maximale Anzahl Ergebnisse
        command_runner: CommandRunner-Instanz (None = Default)

    Returns:
        JSON-serialisierbare Liste von Symbol-Dicts
    """
    runner = command_runner or get_default_runner()
    search_path = Path(search_path)

    # Alle passenden Dateien finden
    if "," in pattern:
        patterns = [p.strip() for p in pattern.split(",")]
    else:
        patterns = [pattern]

    files: list[Path] = []
    for pat in patterns:
        # Sicherstellen dass recursive glob genutzt wird
        if not pat.startswith("**/"):
            pat = f"**/{pat}"
        files.extend(search_path.glob(pat))

    # Deduplizieren und sortieren
    files = sorted(set(files))

    all_symbols: list[SymbolResult] = []

    for file_path in files:
        if file_path.suffix == ".py":
            symbols = _extract_python_symbols(file_path)
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
                    signature = sym.source_code.splitlines()[0][:100] if sym.source_code else sym.name
                    symbols.append(SymbolResult(
                        file=str(file_path),
                        line=sym.line_start,
                        type=sym.type,
                        name=sym.name,
                        signature=signature,
                        context=signature,
                    ))
                if not symbols:
                    symbols = _extract_grep_symbols(file_path)
            else:
                # Fallback: try tree-sitter CLI, then grep
                rc, stdout, stderr = runner.run(["ts", "parse", str(file_path)])
                if rc == 0 and stdout.strip():
                    symbols = _parse_treesitter_output(file_path, stdout)
                else:
                    symbols = _extract_grep_symbols(file_path)

        all_symbols.extend(symbols)

    # Filtern: nur Symbole die den Query enthalten
    matching = [s for s in all_symbols if query.lower() in s.name.lower()]

    # Ranking
    result_dicts = [
        {
            "file": s.file,
            "line": s.line,
            "type": s.type,
            "name": s.name,
            "signature": s.signature,
            "context": s.context,
        }
        for s in matching
    ]
    ranked = rank_results(result_dicts, query)

    return ranked[:max_results]


def _parse_treesitter_output(file_path: Path, output: str) -> list[SymbolResult]:
    """Parse symbols using tree-sitter library (real AST) with grep fallback.

    The 'output' parameter is kept for backward compatibility but is no longer
    used — the tree-sitter library parses the file directly.
    """
    try:
        from tree_sitter_utils import parse_file_with_treesitter
    except ImportError:
        return _extract_grep_symbols(file_path)

    ast_symbols = parse_file_with_treesitter(file_path)
    if ast_symbols is None:
        return _extract_grep_symbols(file_path)

    results: list[SymbolResult] = []
    for sym in ast_symbols:
        signature = sym.source_code.splitlines()[0][:100] if sym.source_code else sym.name
        results.append(SymbolResult(
            file=str(file_path),
            line=sym.line_start,
            type=sym.type,
            name=sym.name,
            signature=signature,
            context=signature,
        ))

    if not results:
        results = _extract_grep_symbols(file_path)

    return results


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> tuple[str, Path, str, int]:
    """Parst Kommandozeilen-Argumente."""
    if not argv:
        print("Usage: smart_search.py <query> [--path=.] [--pattern=*.py] [--max-results=20]")
        sys.exit(1)

    query = argv[0]
    search_path = Path(".")
    pattern = "**/*.py"
    max_results = 20

    for arg in argv[1:]:
        if arg.startswith("--path="):
            search_path = Path(arg.split("=", 1)[1])
        elif arg.startswith("--pattern="):
            pattern = arg.split("=", 1)[1]
        elif arg.startswith("--max-results="):
            max_results = int(arg.split("=", 1)[1])

    return query, search_path, pattern, max_results


if __name__ == "__main__":
    query, search_path, pattern, max_results = _parse_args(sys.argv[1:])
    results = search_files(query, search_path=search_path, pattern=pattern, max_results=max_results)
    print(json.dumps(results, indent=2))
