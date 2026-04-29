"""Structured logging configuration for open-brain."""

import json
import logging
from typing import Any


class RequestIdFilter(logging.Filter):
    """Inject request_id from ContextVar into every LogRecord.

    This bridges the _request_id ContextVar (set by logged_tool in server.py)
    into the logging system so that all log lines emitted during a tool call
    — including those from data_layer, ingest, and llm sub-modules — carry
    the same request_id without requiring callers to pass it explicitly.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Import here to avoid circular imports (logging_config is imported
        # before server in __main__, so top-level import would fail).
        from open_brain.server import _request_id
        record.request_id = _request_id.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Newline-delimited JSON log formatter.

    Each log record is serialized as a single JSON object on one line.
    Fields: ts, level, logger, msg, request_id, and optionally exc (on exceptions).
    """

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj)
