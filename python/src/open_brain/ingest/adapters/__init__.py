"""Ingest adapters package.

Note on ADAPTERS registry
--------------------------
The submodule imports below ARE the auto-registration mechanism per ADR-0001.
Each adapter module is expected to call ``register(MyAdapter())`` at import time;
importing this package triggers those calls and populates ``ADAPTERS``.

``MacWhisperConnector`` and ``TranscriptIngestor`` have not yet been retrofitted
to call ``register()`` at module bottom, so ``ADAPTERS`` is currently empty after
importing this package. Once each adapter module adds its ``register()`` call,
it will appear in ``ADAPTERS`` automatically.
"""

from open_brain.ingest.adapters.base import ADAPTERS, IngestAdapter, get_credentials, register
from open_brain.ingest.adapters.macwhisper import MacWhisperConnector
from open_brain.ingest.adapters.transcript import TranscriptIngestor

__all__ = [
    "ADAPTERS",
    "IngestAdapter",
    "MacWhisperConnector",
    "TranscriptIngestor",
    "get_credentials",
    "register",
]
