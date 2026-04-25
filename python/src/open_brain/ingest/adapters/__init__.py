"""Ingest adapters package.

Note on ADAPTERS registry
--------------------------
The submodule imports below ARE the auto-registration mechanism per ADR-0001.
Each adapter module is expected to call ``register(MyAdapter())`` at import time;
importing this package triggers those calls and populates ``ADAPTERS``.

``MacWhisperConnector`` requires a ``DataLayer`` at construction time and therefore
cannot register itself at module import. Callers must instantiate the connector and
call ``register(connector)`` explicitly after construction. ``TranscriptIngestor``
is a low-level helper, not a top-level adapter, and is not registered.
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
