"""Structured logging configuration for open-brain."""

import json
import logging


class JsonFormatter(logging.Formatter):
    """Newline-delimited JSON log formatter.

    Each log record is serialized as a single JSON object on one line.
    Fields: ts, level, logger, msg, and optionally exc (on exceptions).
    """

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts": self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj)
