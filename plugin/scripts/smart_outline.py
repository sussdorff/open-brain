"""smart_outline.py — AST-based structural skeleton for files.

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
                cmd, capture_output=True, text=True, timeout=10, env=self._SAFE_ENV
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
        """Return the configured response for the given command."""
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
class SymbolInfo:
    """A symbol found in a file."""

    name: str
    type: str
    line: int
    end_line: int
    signature: str


@dataclass
class OutlineResult:
    """Result of a file analysis."""

    file: str
    symbols: list[SymbolInfo]


# ─── Python AST Outline ───────────────────────────────────────────────────────


def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a readable function signature from an AST node."""
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
    """Extract all top-level and class-level symbols via the ast module."""
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
            # Class methods
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


# ─── Grep fallback for non-Python files ──────────────────────────────────────


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
    """Extract symbols via regex patterns (fallback)."""
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
                    end_line=line_num,  # End line unknown without AST
                    signature=line.strip()[:120],
                ))
                break

    return symbols


# ─── Main function ────────────────────────────────────────────────────────────


def outline_file(
    file_path: Path,
    command_runner: CommandRunner | None = None,
) -> dict:
    """Build a structural skeleton of a file.

    Args:
        file_path: Path to the file to analyse
        command_runner: CommandRunner instance (unused, kept for API compatibility)

    Returns:
        Dict with {file, symbols: [{name, type, line, end_line, signature}]}
    """
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
            # Fallback: grep-based heuristic
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


# ─── CLI Entry Point ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: smart_outline.py <file_path>")
        sys.exit(1)

    result = outline_file(Path(sys.argv[1]))
    print(json.dumps(result, indent=2))
