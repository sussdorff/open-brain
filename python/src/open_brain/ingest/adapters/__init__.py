"""Ingest adapters package.

Note on ADAPTERS registry
--------------------------
``ADAPTERS`` is populated lazily at *registration time*, not at import time.
Each adapter module must call ``register(MyAdapter())`` at its own import time
to appear in the registry. The imports of ``MacWhisperConnector`` and
``TranscriptIngestor`` below do NOT auto-register those adapters — registration
is intentionally deferred to each adapter's own module per ADR-0001. Until an
adapter module calls ``register()``, ``ADAPTERS`` will be empty (or contain only
adapters registered by previously imported modules).
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
