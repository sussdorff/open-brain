"""smart_search.py — AST-based symbol search for Python, grep fallback for other languages.

Usage:
    uv run python smart_search.py <query> [--path=.] [--pattern=*.py] [--max-results=20]
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter-language-pack>=1.6.2"]
# ///

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
    """Protocol for subprocess calls."""

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Execute a command and return (returncode, stdout, stderr)."""
        ...


@dataclass
class DefaultCommandRunner:
    """Real subprocess runner with a restricted environment."""

    _SAFE_ENV: dict[str, str] = field(default_factory=lambda: {
        "PATH": __import__("os").environ.get("PATH", ""),
        "HOME": __import__("os").environ.get("HOME", ""),
    })

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Execute a command and return (returncode, stdout, stderr)."""
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
    """Test runner with configurable responses."""

    responses: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    default_response: tuple[int, str, str] = (0, "", "")

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Return the configured response or the default."""
        key = " ".join(cmd)
        for pattern, response in self.responses.items():
            if pattern in key:
                return response
        return self.default_response


def get_default_runner() -> CommandRunner:
    """Return the default CommandRunner instance."""
    return DefaultCommandRunner()


# ─── Types ────────────────────────────────────────────────────────────────────


@dataclass
class SymbolResult:
    """A found symbol."""

    file: str
    line: int
    type: str
    name: str
    signature: str
    context: str = ""


# ─── Python AST analysis ──────────────────────────────────────────────────────


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a readable function signature from an AST node."""
    args = []
    func_args = node.args

    # Positional arguments
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
    """Extract all symbols from a Python file via the ast module."""
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
            # Class methods
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
            # Only top-level functions (methods are captured above)
            # Check whether this node is a method (parent is ClassDef)
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


# ─── Grep fallback for non-Python files ──────────────────────────────────────


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
    """Extract symbols via regex patterns (fallback for non-Python files)."""
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
                break  # Only first match per line

    return results


# ─── Ranking ──────────────────────────────────────────────────────────────────


def _rank_score(name: str, query: str) -> int:
    """Calculate a ranking score: higher is better.

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
    """Sort results by relevance to the query.

    Args:
        results: List of symbol dicts
        query: Search term

    Returns:
        Sorted list (best match first)
    """
    return sorted(results, key=lambda r: _rank_score(r["name"], query), reverse=True)


# ─── Main function ────────────────────────────────────────────────────────────


def search_files(
    query: str,
    search_path: Path = Path("."),
    pattern: str = "**/*.py",
    max_results: int = 20,
    command_runner: CommandRunner | None = None,
) -> list[dict]:
    """Search for symbols in files.

    Args:
        query: Symbol name or partial name
        search_path: Directory for recursive search
        pattern: Glob pattern for file filtering
        max_results: Maximum number of results
        command_runner: CommandRunner instance (unused, kept for API compatibility)

    Returns:
        JSON-serialisable list of symbol dicts
    """
    search_path = Path(search_path)

    # Collect all matching files
    if "," in pattern:
        patterns = [p.strip() for p in pattern.split(",")]
    else:
        patterns = [pattern]

    files: list[Path] = []
    for pat in patterns:
        # Ensure recursive glob is used
        if not pat.startswith("**/"):
            pat = f"**/{pat}"
        files.extend(search_path.glob(pat))

    # Deduplicate and sort
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
                # Fallback: grep-based heuristic
                symbols = _extract_grep_symbols(file_path)

        all_symbols.extend(symbols)

    # Filter: only symbols whose name contains the query
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


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> tuple[str, Path, str, int]:
    """Parse command-line arguments."""
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
