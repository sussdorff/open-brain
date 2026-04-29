"""Entry point for `python -m open_brain`."""

import logging.config


def _configure_logging(log_level: str, log_format: str) -> None:
    """Install logging configuration via dictConfig before uvicorn starts."""
    handlers = {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json" if log_format == "json" else "human",
        }
    }
    formatters = {
        "human": {
            "format": "%(asctime)s %(levelname)-8s %(name)s %(message)s",
            "datefmt": "%Y-%m-%dT%H:%M:%S",
        },
        "json": {
            "()": "open_brain.logging_config.JsonFormatter",
        },
    }
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": formatters,
        "handlers": handlers,
        "root": {"level": log_level, "handlers": ["console"]},
        "loggers": {
            "uvicorn": {"handlers": ["console"], "propagate": False},
            "uvicorn.access": {"handlers": ["console"], "propagate": False},
            "fastmcp": {"handlers": ["console"], "propagate": False},
            "mcp": {"handlers": ["console"], "propagate": False},
        },
    })


if __name__ == "__main__":
    import uvicorn

    from open_brain.config import get_config
    from open_brain.server import app

    config = get_config()
    _configure_logging(config.LOG_LEVEL, config.LOG_FORMAT)
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
