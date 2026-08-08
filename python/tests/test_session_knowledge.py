"""Session-knowledge producer uses the public memory-write proposal contract.

Full session-knowledge capture API belongs to open-brain-ekn.9. This suite only
proves structured producers can construct and validate proposals without
importing judge internals (open-brain-ekn.2 AC5).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from open_brain.memory_write_proposal import proposal_preflight_issues

PYTHON_ROOT = Path(__file__).resolve().parents[1]


def test_session_producer_cold_import_does_not_load_judge() -> None:
    script = r"""
import sys
from open_brain.memory_write_proposal import (
    build_memory_write_proposal,
    parse_memory_write_proposal,
    proposal_preflight_issues,
    raw_proposal_payload,
)

proposal = build_memory_write_proposal(
    intended_memory_content=(
        "Observed outcome: focused tests passed before the full non-integration suite."
    ),
    category="observation",
    source_citation={
        "ref": "agent-session:codex:session-knowledge-demo",
        "label": "observed",
    },
    authorization_basis={
        "ref": "policy://session/evidence-write",
        "label": "observed",
        "granted_by": "system",
    },
    expected_use="evidence",
    retention_scope="session",
    risk_flags=[],
)
payload = raw_proposal_payload(proposal)
parsed, errors = parse_memory_write_proposal(payload)
assert errors == []
assert parsed is not None
assert proposal_preflight_issues(payload) == []
assert "open_brain.memory_write_judge" not in sys.modules
print("OK")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PYTHON_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(PYTHON_ROOT),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_session_producer_flags_missing_authorization_without_judge_import() -> None:
    issues = proposal_preflight_issues(
        {
            "intended_memory_content": "Inferred lesson from the session.",
            "category": "lesson",
            "source_citation": {
                "ref": "agent://session-inference",
                "label": "inferred",
            },
            "authorization_basis": None,
            "expected_use": "evidence",
            "retention_scope": "project",
            "risk_flags": [],
        }
    )
    assert any(issue.code == "missing_authorization" for issue in issues)
