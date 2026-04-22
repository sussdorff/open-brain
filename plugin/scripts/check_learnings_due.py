"""check_learnings_due.py — Rate-limit check for periodic learnings extraction.

Usage:
    uv run python check_learnings_due.py

Output (execution-result envelope):
    {
        "status": "ok",
        "summary": "due|skip",
        "data": {
            "result": "due|skip",
            "last_run": "<ISO timestamp or empty>",
            "next_eligible_at": "<ISO timestamp or empty>"
        },
        ...
    }

Logic:
    Reads ~/.claude/learnings/processing-state.json and checks
    whether at least 4 hours have elapsed since the last learnings run.
    Returns "due" when extraction should proceed, "skip" otherwise.

Note:
    The 4-hour interval and "last_learnings_run" key name are shared with
    open_brain/learnings_state.py — keep both in sync when changing either.
"""

# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import datetime
import json
import pathlib
import sys

INTERVAL_SECONDS = 14400  # 4 hours


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _make_envelope(
    status: str,
    summary: str,
    result: str,
    last_run: str,
    next_eligible_at: str,
) -> dict:
    return {
        "status": status,
        "summary": summary,
        "data": {
            "result": result,
            "last_run": last_run,
            "next_eligible_at": next_eligible_at,
        },
        "errors": [],
        "next_steps": [],
        "open_items": [],
        "meta": {
            "contract_version": "1",
            "producer": "check_learnings_due",
            "generated_at": _now_utc().isoformat(),
            "schema": "core/contracts/execution-result.schema.json",
        },
    }


def check_due() -> dict:
    """Check whether learnings extraction is due.

    Returns:
        Execution-result envelope with data.result set to "due" or "skip".
    """
    state_file = pathlib.Path.home() / ".claude/learnings/processing-state.json"

    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except Exception:
        state = {}

    last_run_str = state.get("last_learnings_run", "")

    if not last_run_str:
        return _make_envelope(
            status="ok",
            summary="due",
            result="due",
            last_run="",
            next_eligible_at="",
        )

    try:
        last_run_dt = datetime.datetime.fromisoformat(last_run_str)
        now = _now_utc()
        delta = now - last_run_dt
        if delta.total_seconds() >= INTERVAL_SECONDS:
            next_eligible_at = ""
            result = "due"
            summary = "due"
        else:
            remaining = INTERVAL_SECONDS - delta.total_seconds()
            next_eligible_at = (now + datetime.timedelta(seconds=remaining)).isoformat()
            result = "skip"
            summary = "skip"
    except ValueError:
        # Unparseable timestamp — treat as due
        return _make_envelope(
            status="ok",
            summary="due",
            result="due",
            last_run=last_run_str,
            next_eligible_at="",
        )

    return _make_envelope(
        status="ok",
        summary=summary,
        result=result,
        last_run=last_run_str,
        next_eligible_at=next_eligible_at,
    )


def main() -> None:
    result = check_due()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
