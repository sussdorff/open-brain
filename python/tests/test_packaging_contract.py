"""Regression tests for the installed Open Brain console entrypoint."""

import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_regression_python_package_declares_ob_console_script() -> None:
    pyproject = tomllib.loads(
        (REPO_ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert pyproject["project"]["scripts"]["ob"] == "open_brain.cli.main:main"


def test_regression_docker_builder_installs_project_console_script() -> None:
    """The runtime virtualenv must contain the declared `ob` console script."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    source_copy = dockerfile.index("COPY python/src ./src")
    project_install = dockerfile.find("RUN uv sync --frozen --no-dev", source_copy)

    assert project_install > source_copy
    assert "--no-install-project" not in dockerfile[project_install:].splitlines()[0]
