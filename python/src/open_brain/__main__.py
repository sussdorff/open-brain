"""Entry point for `python -m open_brain`."""

from open_brain.runtime import configure_logging as _configure_logging


if __name__ == "__main__":
    from open_brain.runtime import run_server

    run_server()
