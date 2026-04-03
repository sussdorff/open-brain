"""Helper for periodic learnings extraction state management.

Manages the processing-state.json file that tracks when the last
periodic learnings extraction ran, enabling 4h rate-limiting.

NOTE: The rate-limit check logic (4h interval, last_learnings_run key) is
intentionally duplicated in memory-heartbeat/SKILL.md (inline Python snippet)
and here. Keep both in sync when changing the interval or key name.
"""

from __future__ import annotations

import json
import warnings
from datetime import datetime, timezone
from pathlib import Path


def load_state(path: Path | str) -> dict:
    """Lade processing-state.json, gibt leeres Dict bei fehlendem/korruptem File zurueck.

    Args:
        path: Pfad zur state JSON-Datei

    Returns:
        Geladenes State-Dict, oder {} bei Fehler
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception as e:
        warnings.warn(f"Failed to load learnings state from {p}: {e}", stacklevel=2)
        return {}


def save_state(path: Path | str, state: dict) -> None:
    """Speichert State atomar in path (write to .tmp dann rename).

    Args:
        path: Zielpfad fuer die state JSON-Datei
        state: State-Dict das gespeichert werden soll
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(p)


def is_extraction_due(state: dict, interval_hours: float = 4.0) -> bool:
    """Prueft ob die periodische Learnings-Extraktion faellig ist.

    Args:
        state: Geladenes State-Dict aus processing-state.json
        interval_hours: Mindestabstand zwischen Extraktionen in Stunden

    Returns:
        True wenn last_learnings_run fehlt oder aelter als interval_hours ist
    """
    last_run_str = state.get("last_learnings_run", "")
    if not last_run_str:
        return True
    try:
        last_run = datetime.fromisoformat(last_run_str)
        delta = datetime.now(timezone.utc) - last_run
        return delta.total_seconds() >= interval_hours * 3600
    except Exception:
        # Reaching here means last_run_str was non-empty but unparseable
        warnings.warn(f"Malformed last_learnings_run timestamp: {last_run_str!r}", stacklevel=2)
        return True


def mark_extraction_ran(state: dict) -> dict:
    """Gibt neues State-Dict mit gesetztem last_learnings_run Timestamp zurueck.

    Args:
        state: Existierendes State-Dict (wird nicht mutiert)

    Returns:
        Neues State-Dict mit last_learnings_run auf aktueller UTC-Zeit
    """
    new_state = dict(state)
    new_state["last_learnings_run"] = datetime.now(timezone.utc).isoformat()
    return new_state
