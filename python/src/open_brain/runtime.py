"""Runtime helpers for starting the open-brain server."""

import logging.config


def configure_logging(log_level: str, log_format: str) -> None:
    """Install logging configuration before uvicorn starts."""
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json" if log_format == "json" else "human",
            "filters": ["request_id"],
        }
    }
    formatters = {
        "human": {
            "format": "%(asctime)s %(levelname)-8s %(name)s [%(request_id)s] %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
        "json": {
            "()": "open_brain.logging_config.JsonFormatter",
        },
    }
    filters = {
        "request_id": {
            "()": "open_brain.logging_config.RequestIdFilter",
        },
    }
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatters,
        "filters": filters,
        "handlers": handlers,
        "root": {"level": log_level, "handlers": ["console"]},
        "loggers": {
            "uvicorn": {"handlers": ["console"], "propagate": False},
            "uvicorn.access": {"handlers": ["console"], "propagate": False},
            "fastmcp": {"handlers": ["console"], "propagate": False},
            "mcp": {"handlers": ["console"], "propagate": False},
        },
    })


def run_server(host: str = "0.0.0.0", port: int | None = None) -> None:
    """Start the FastAPI/MCP server via uvicorn."""
    import uvicorn

    from open_brain.config import get_config
    from open_brain.server import app

    config = get_config()
    configure_logging(config.LOG_LEVEL, config.LOG_FORMAT)
    # Pass log_config=None so uvicorn does not re-apply its default LOGGING_CONFIG,
    # which would override our pre-installed dictConfig for uvicorn/uvicorn.access.
    uvicorn.run(app, host=host, port=port or config.PORT, log_config=None)
