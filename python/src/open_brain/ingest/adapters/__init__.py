"""Ingest adapters package."""

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
