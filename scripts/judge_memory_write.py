#!/usr/bin/env python3
"""Judge an OpenBrain memory-write proposal from JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "python" / "src"))

from open_brain.memory_write_judge import judge_memory_write_proposal  # noqa: E402


def load_json(path: str) -> dict[str, Any]:
    """Load a proposal JSON object from a file path or stdin."""
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("proposal must be a JSON object")
    return data


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(description="Judge an OpenBrain memory-write proposal.")
    parser.add_argument("proposal", help="Proposal JSON path, or '-' for stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the memory-write judge CLI."""
    args = build_parser().parse_args(argv)
    try:
        proposal = load_json(args.proposal)
    except Exception as exc:
        print(json.dumps({
            "decision": "ESCALATE",
            "reason": f"proposal parse error: {exc}",
            "reason_category": "schema",
            "policy_version": "memory-write-judge.v1",
            "provenance_refs": [],
            "escalation_target": "memory-policy-owner",
        }, indent=2))
        return 2

    outcome = judge_memory_write_proposal(proposal).to_payload()
    print(json.dumps(outcome, indent=2))
    return 0 if outcome["decision"] == "ALLOW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
