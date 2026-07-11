"""Deterministic bulk importer for historical Second Brain vault notes.

This module intentionally does not implement the ingest adapter registry
Protocol. It is a standalone migration module for one-shot historical vault
imports, while ``open_brain.migrate`` keeps its Markdown/Obsidian parsing note
scoped to the interactive single-note capture skill. Bulk migration needs a
deterministic, fixture-tested path so dry runs and repeated applies are
auditable.
"""

from __future__ import annotations

