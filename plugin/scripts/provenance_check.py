"""provenance_check.py — Entry point for provenance check on a single memory.

Reads a memory JSON object from stdin, runs build_provenance_update,
and prints the result as JSON (or "null" if no code references found).

Usage:
    echo '<memory_json>' | uv run python plugin/scripts/provenance_check.py

Input (stdin):
    JSON object with keys: id, type (optional), content, metadata (optional)

Output (stdout):
    JSON object with "metadata_patch" key, or the string "null".

Example:
    echo '{"id":"abc","content":"See src/foo.py for details."}' \\
        | uv run python plugin/scripts/provenance_check.py
"""

# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow running without installation by adding the scripts directory to sys.path
_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from provenance import build_provenance_update


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print("null")
        return

    try:
        memory = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"null", file=sys.stderr)
        sys.stderr.write(f"provenance_check: invalid JSON on stdin: {exc}\n")
        print("null")
        return

    memory_id = memory.get("id", "")
    memory_type = memory.get("type")
    content = memory.get("content", "")
    metadata = memory.get("metadata")

    # Resolve base_path: use env var PROVENANCE_REPO_ROOT, else home directory
    base_path = os.environ.get("PROVENANCE_REPO_ROOT", str(Path.home()))

    result = build_provenance_update(
        memory_id=memory_id,
        memory_type=memory_type,
        content=content,
        metadata=metadata,
        base_path=base_path,
    )

    if result is None:
        print("null")
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
