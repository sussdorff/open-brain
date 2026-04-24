"""Ingest run context management.

Provides a context manager that tracks the current ingest run_id
using Python's contextvars for async-safe propagation.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Generator

_current_run_id: ContextVar[str | None] = ContextVar("_current_run_id", default=None)


def get_current_run_id() -> str | None:
    """Return the run_id for the current ingest context, or None if outside a run."""
    return _current_run_id.get()


@contextmanager
def ingest_run() -> Generator[str, None, None]:
    """Context manager that generates a run_id for an ingest batch.

    All save_memory() and create_relationship() calls within this context
    automatically get run_id injected into their metadata.

    Yields:
        run_id: A UUID4 string identifying this ingest run.

    Example:
        with ingest_run() as run_id:
            await dl.save_memory(...)  # run_id injected automatically
            await dl.create_relationship(...)  # run_id injected automatically
    """
    run_id = str(uuid.uuid4())
    token = _current_run_id.set(run_id)
    try:
        yield run_id
    finally:
        _current_run_id.reset(token)
