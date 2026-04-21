"""AST-based allowlist enforcer for session_summary writers.

The behavioral test in test_session_summary_sources.py only verifies that
the documented source values flow through without error. It does NOT catch
a regression that introduces a new rogue literal (e.g. someone adding
source="session-finale" to a future writer).

This test walks the AST of every module under src/open_brain/ and collects
every LITERAL `source` value used in a session_summary write site. The set
of candidate write sites is:

1. Direct SaveMemoryParams(type="session_summary", metadata={"source": ...})
2. Calls to summarize_transcript_turns(source=...)

Non-literal values (variables, expressions) are ignored — the static scan
cannot evaluate them and they are covered by other tests.

Discovery context: Bead open-brain-d8x (Phase 6.5 integration-verification
of open-brain-d4n).
"""

from __future__ import annotations

import ast
from pathlib import Path

from open_brain.session_summary import ALLOWED_SESSION_SUMMARY_SOURCES

SRC_ROOT = Path(__file__).parent.parent / "src" / "open_brain"
REPO_ROOT = SRC_ROOT.parent.parent.parent


class _NotLiteral:
    """Sentinel: a Call argument that is not a simple constant literal."""

    __slots__ = ()


_NOT_LITERAL = _NotLiteral()


def _literal_value(node: ast.AST) -> object:
    """Return the Python value of an ast.Constant node, or the _NOT_LITERAL sentinel."""
    if isinstance(node, ast.Constant):
        return node.value
    return _NOT_LITERAL


def _find_kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _iter_python_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _is_session_summary_save_memory_params(call: ast.Call) -> bool:
    """Detect SaveMemoryParams(type="session_summary", ...) constructor calls."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "SaveMemoryParams":
        pass
    elif isinstance(func, ast.Attribute) and func.attr == "SaveMemoryParams":
        pass
    else:
        return False
    type_arg = _find_kwarg(call, "type")
    if type_arg is None:
        return False
    return _literal_value(type_arg) == "session_summary"


def _is_summarize_transcript_turns(call: ast.Call) -> bool:
    """Detect calls to summarize_transcript_turns(...)."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "summarize_transcript_turns":
        return True
    if isinstance(func, ast.Attribute) and func.attr == "summarize_transcript_turns":
        return True
    return False


def _source_from_metadata_literal(call: ast.Call) -> tuple[bool, object]:
    """Extract the literal value of metadata['source'] from a SaveMemoryParams call.

    Returns (has_source_key, literal_value). If metadata is not a dict literal,
    or has no "source" key, returns (False, None). If the key exists but the
    value is not a constant, literal_value is the _NOT_LITERAL sentinel.
    """
    md = _find_kwarg(call, "metadata")
    if md is None or not isinstance(md, ast.Dict):
        return False, None
    for key, value in zip(md.keys, md.values, strict=False):
        if key is None:  # {**spread}
            continue
        if _literal_value(key) == "source":
            return True, _literal_value(value)
    return False, None


def _collect_source_literals():
    """Walk every module under SRC_ROOT and yield literal source values.

    Yields tuples: (path, lineno, literal_value, kind)
      - path: Path to the file
      - lineno: line number of the Call node
      - literal_value: the literal Python value, or the _NOT_LITERAL sentinel
      - kind: "save_memory_params" or "summarize_transcript_turns"
    """
    for path in _iter_python_files(SRC_ROOT):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _is_session_summary_save_memory_params(node):
                has_source, literal = _source_from_metadata_literal(node)
                if has_source:
                    yield path, node.lineno, literal, "save_memory_params"
            elif _is_summarize_transcript_turns(node):
                src_arg = _find_kwarg(node, "source")
                if src_arg is not None:
                    yield path, node.lineno, _literal_value(src_arg), "summarize_transcript_turns"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def test_session_summary_source_literal_allowlist():
    """Every LITERAL source value in the src tree must be in the canonical allowlist.

    This is the exhaustive regression guard. A rogue literal (e.g. a new
    writer with source="session-finale") will trip this test even if it
    flows through save_memory without runtime error.
    """
    violations: list[str] = []
    literal_hits = 0
    nonliteral_hits = 0

    for path, lineno, literal, kind in _collect_source_literals():
        if isinstance(literal, _NotLiteral):
            nonliteral_hits += 1
            continue
        literal_hits += 1
        if literal not in ALLOWED_SESSION_SUMMARY_SOURCES:
            violations.append(
                f"{_rel(path)}:{lineno} ({kind}) — source={literal!r} "
                f"not in allowlist {sorted(str(x) for x in ALLOWED_SESSION_SUMMARY_SOURCES)}"
            )

    # Scanner health check: if we matched zero call sites the AST walker is
    # probably broken (imports moved, constructor renamed, etc). Known
    # writers at time of writing: server.py (session-end-hook), regenerate.py
    # (transcript-backfill). At least one literal must exist.
    assert literal_hits > 0, (
        "AST scan found zero literal source values across session_summary "
        "writers — the scanner is likely broken. Known writers: "
        "server.py (session-end-hook), regenerate.py (transcript-backfill)."
    )

    assert not violations, (
        "Rogue session_summary source literals detected. "
        "Either add them to ALLOWED_SESSION_SUMMARY_SOURCES in "
        "src/open_brain/session_summary.py (and update "
        "docs/standards/session-summary-writers.md), or rename them to an "
        "existing allowlisted marker.\n  "
        + "\n  ".join(violations)
    )


def test_known_writers_detected():
    """Regression guard: the two current in-tree literal writers are detected.

    If either of these goes silent, the scanner has drifted — e.g. the
    constructor was renamed or a call site moved behind a helper that the
    AST walker no longer recognizes.
    """
    literals = {
        literal
        for _, _, literal, _ in _collect_source_literals()
        if not isinstance(literal, _NotLiteral)
    }
    assert "session-end-hook" in literals, (
        "Expected to find source='session-end-hook' literal in src — did "
        "server.py stop calling summarize_transcript_turns with an explicit "
        "source kwarg?"
    )
    assert "transcript-backfill" in literals, (
        "Expected to find source='transcript-backfill' literal in src — did "
        "regenerate.py stop calling summarize_transcript_turns with an "
        "explicit source kwarg?"
    )
