"""provenance.py — Build provenance metadata updates for code-referencing memories.

This module provides pure functions for checking whether memory content references
file paths or code artifacts that exist on disk, and computing a metadata patch with
confidence score, verification timestamp, and stale reference list.

Usage as a library:
    from provenance import build_provenance_update
    patch = build_provenance_update(
        memory_id="abc123",
        memory_type="observation",
        content="See python/src/open_brain/server.py for details.",
        metadata=None,
        base_path="/Users/malte/code/myrepo",
    )
    # patch is None if no code references found, else dict with metadata_patch
"""

# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import datetime
import json
import re
import sys
from pathlib import Path
from typing import Optional


# Patterns that suggest a code artifact reference in memory content.
# Matches things like "src/foo/bar.py", "lib/utils.ts", "scripts/deploy.sh", etc.
_CODE_REF_PATTERN = re.compile(
    r"""
    (?:^|[\s,('"`])                     # word boundary or start
    (
        (?:[\w.-]+/)+                   # at least one directory component
        [\w.-]+                         # filename
        \.(?:py|ts|js|sh|go|rs|rb|java|kt|swift|yaml|yml|toml|json|md)
                                        # recognisable extension
    )
    (?:$|[\s,)'"`:#])                   # word boundary or end
    """,
    re.VERBOSE | re.MULTILINE,
)


def _extract_code_refs(content: str) -> list[str]:
    """Return all file-path-like code references found in content."""
    return [m.group(1) for m in _CODE_REF_PATTERN.finditer(content)]


def _resolve_ref(ref: str, base_path: str) -> Optional[Path]:
    """Resolve a reference against base_path. Returns Path if resolvable."""
    p = Path(ref)
    if p.is_absolute():
        return p
    if base_path:
        return Path(base_path) / p
    return None


def build_provenance_update(
    memory_id: str,
    memory_type: Optional[str],
    content: str,
    metadata: Optional[dict],
    base_path: str,
) -> Optional[dict]:
    """Compute a provenance metadata patch for a memory.

    Checks whether the memory content references file paths or code artifacts
    that exist on disk. Returns None if no code references are found.

    Args:
        memory_id: Unique identifier of the memory (used for logging only).
        memory_type: Type label of the memory (e.g. "observation"). May be None.
        content: Full text content of the memory.
        metadata: Existing metadata dict of the memory. May be None.
        base_path: Absolute path to resolve relative code references against.

    Returns:
        A dict with a "metadata_patch" key when code refs are found, e.g.:
            {
                "metadata_patch": {
                    "confidence_score": "high" | "medium" | "low",
                    "last_verified": "<ISO 8601 timestamp>",
                    "stale_refs": ["path/that/no/longer/exists.py", ...],
                }
            }
        Returns None if no code references are detected in content.
    """
    refs = _extract_code_refs(content)
    if not refs:
        return None

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stale_refs: list[str] = []
    found_refs: list[str] = []

    for ref in refs:
        resolved = _resolve_ref(ref, base_path)
        if resolved is None:
            # Cannot resolve — treat as stale to be safe
            stale_refs.append(ref)
            continue
        if resolved.exists():
            found_refs.append(ref)
        else:
            stale_refs.append(ref)

    total = len(refs)
    stale_count = len(stale_refs)

    if stale_count == 0:
        confidence = "high"
    elif stale_count < total:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "metadata_patch": {
            "confidence_score": confidence,
            "last_verified": now_iso,
            "stale_refs": stale_refs,
        }
    }
