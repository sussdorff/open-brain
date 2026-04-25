"""Ingest adapters package.

Note on ADAPTERS registry
--------------------------
The submodule imports below ARE the auto-registration mechanism per ADR-0001.
Each adapter module is expected to call ``register(MyAdapter())`` at import time;
importing this package triggers those calls and populates ``ADAPTERS``.

``MacWhisperConnector`` registers a sentinel instance (``data_layer=None``) at
module import time for adapter discovery. The sentinel raises ``RuntimeError`` if
``ingest_entry`` or ``ingest`` is called without a real ``DataLayer``; callers that
need to ingest must construct their own instance with ``data_layer`` provided.
``TranscriptIngestor`` is a low-level helper, not a top-level adapter, and is not
registered. See ADR-0001 § "Helper Modules vs. Top-Level Adapters" for the full
distinction and rules.
"""

from open_brain.ingest.adapters.base import ADAPTERS, IngestAdapter, get_credentials, register
from open_brain.ingest.adapters.email_imap import IMAPEmailIngestor
from open_brain.ingest.adapters.macwhisper import MacWhisperConnector
from open_brain.ingest.adapters.transcript import TranscriptIngestor

__all__ = [
    "ADAPTERS",
    "IMAPEmailIngestor",
    "IngestAdapter",
    "MacWhisperConnector",
    "TranscriptIngestor",
    "get_credentials",
    "register",
]
