"""Compatibility wrapper for the packaged person merge utility.

The implementation lives in open_brain.people.merge so the server and installed
`ob` CLI can expose the same workflow without depending on checkout-only
scripts.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_PYTHON_SRC = Path(__file__).resolve().parents[1] / "python" / "src"
if _PYTHON_SRC.exists() and str(_PYTHON_SRC) not in sys.path:
    sys.path.insert(0, str(_PYTHON_SRC))

from open_brain.people.merge import *  # noqa: F401,F403,E402
from open_brain.people.merge import main  # noqa: E402


if __name__ == "__main__":
    asyncio.run(main())
