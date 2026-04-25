"""Ingest adapters package."""

from open_brain.ingest.adapters.macwhisper import MacWhisperConnector
from open_brain.ingest.adapters.transcript import TranscriptIngestor

__all__ = ["MacWhisperConnector", "TranscriptIngestor"]
