"""Entry point for `python -m open_brain`."""

from open_brain.server import app
from open_brain.config import get_config

if __name__ == "__main__":
    import uvicorn

    config = get_config()
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
