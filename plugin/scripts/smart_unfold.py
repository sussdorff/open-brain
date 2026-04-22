"""smart_unfold.py — AST-bounded symbol extraction.

Usage:
    uv run python smart_unfold.py <file_path> <symbol_name>
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


# ─── Python AST Unfold ────────────────────────────────────────────────────────


def _find_python_symbol(
    tree: ast.AST, symbol_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None:
    """Find a symbol in the AST (top-level or class method)."""
    # Search at top level
    if isinstance(tree, ast.Module):
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == symbol_name:
                    return node
            # Search methods inside classes
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name == symbol_name:
                            return item

    return None


def _extract_python_symbol(file_path: Path, symbol_name: str) -> dict | None:
    """Extract a symbol from a Python file using the ast module.

    Args:
        file_path: Path to the Python file
        symbol_name: Name of the symbol to find

    Returns:
        Dict with {file, symbol, line_start, line_end, source_code} or None
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

    # Source extraction (1-based line numbers)
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


# ─── Grep fallback for non-Python files ──────────────────────────────────────

# Patterns that identify the start of a symbol definition
_START_PATTERNS = [
    # TypeScript/JavaScript functions
    r"(?:export\s+)?(?:async\s+)?function\s+{name}\s*[\(<]",
    # TypeScript/JavaScript arrow functions
    r"(?:export\s+)?(?:const|let|var)\s+{name}\s*=",
    # Classes
    r"(?:export\s+)?(?:abstract\s+)?class\s+{name}[\s{{<]",
    # Go functions
    r"func\s+(?:\(\w+\s+\*?\w+\)\s+)?{name}\s*\(",
    # Rust functions
    r"(?:pub\s+)?(?:async\s+)?fn\s+{name}\s*[(<]",
]


def _find_block_end(lines: list[str], start_idx: int) -> int:
    """Find the end of a { }-block or indented block.

    Args:
        lines: All lines of the file (0-indexed)
        start_idx: Start line index of the block (0-indexed)

    Returns:
        End line index (0-indexed, inclusive)
    """
    start_line = lines[start_idx]

    # Strategy 1: Count curly braces
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

    # Strategy 2: Track indentation (Python-style)
    if not found_opening:
        # Determine base indentation of the start block
        base_indent = len(start_line) - len(start_line.lstrip())

        for i in range(start_idx + 1, len(lines)):
            line = lines[i]
            if not line.strip():  # Skip blank lines
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                return i - 1

    return len(lines) - 1


def _extract_grep_symbol(file_path: Path, symbol_name: str) -> dict | None:
    """Extract a symbol via regex + indentation heuristic (fallback).

    Args:
        file_path: Path to the file
        symbol_name: Name of the symbol to find

    Returns:
        Dict or None
    """
    try:
        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
    except UnicodeDecodeError:
        return None

    # Search for symbol start
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
        "line_start": start_idx + 1,  # 1-based
        "line_end": end_idx + 1,      # 1-based
        "source_code": "\n".join(symbol_lines),
    }


# ─── Main function ────────────────────────────────────────────────────────────


def unfold_symbol(
    file_path: Path,
    symbol_name: str,
    command_runner: CommandRunner | None = None,
) -> dict | None:
    """Extract a complete symbol (AST-bounded) from a file.

    Args:
        file_path: Path to the file to analyse
        symbol_name: Name of the symbol to find
        command_runner: CommandRunner instance (unused, kept for API compatibility)

    Returns:
        Dict with {file, symbol, line_start, line_end, source_code} or None
    """
    file_path = Path(file_path)

    if not file_path.exists():
        return None

    if file_path.suffix == ".py":
        return _extract_python_symbol(file_path, symbol_name)

    # Try tree-sitter library (real AST, accurate line boundaries)
    try:
        from tree_sitter_utils import find_symbol_in_file
        sym = find_symbol_in_file(file_path, symbol_name)
        if sym is not None:
            return {
                "file": str(file_path),
                "symbol": symbol_name,
                "line_start": sym.line_start,
                "line_end": sym.line_end,
                "source_code": sym.source_code,
            }
    except ImportError:
        pass

    # Fallback: grep-based heuristic
    return _extract_grep_symbol(file_path, symbol_name)


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
