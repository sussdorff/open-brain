"""Operator docs name Forgejo as canonical origin and the Cognovis PyPI install."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]

GIT_DOC_PATHS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "docs" / "architecture.md",
)
DOC_PATHS = (
    *GIT_DOC_PATHS,
    REPO_ROOT / "standards" / "open-brain" / "cli-routing.md",
)

RECOMMENDED_INSTALL = (
    "uv tool install --python 3.14 "
    "--index-url https://git.cognovis.de/api/packages/cognovis/pypi/simple "
    "open-brain"
)
CANONICAL_CLONE_URL = "https://git.cognovis.de/cognovis/open-brain"
PUBLIC_MIRROR_URL = "https://github.com/sussdorff/open-brain"
CANONICAL_CLONE_COMMAND = "git clone https://git.cognovis.de/cognovis/open-brain.git"
MARKETPLACE_SOURCE = "source: https://git.cognovis.de/cognovis/open-brain"
DEPRECATED_GIT_INSTALL = (
    "git+https://github.com/sussdorff/open-brain.git#subdirectory=python"
)


def test_operator_docs_document_cognovis_pypi_install() -> None:
    """README, CONTRIBUTING, architecture, and cli-routing name the wheel install."""
    for path in DOC_PATHS:
        text = path.read_text(encoding="utf-8")
        assert RECOMMENDED_INSTALL in text, (
            f"{path.name} must document the Cognovis PyPI install command"
        )
        assert DEPRECATED_GIT_INSTALL not in text, (
            f"{path.name} must not recommend the GitHub git+subdirectory install"
        )


def test_git_docs_name_canonical_forgejo_and_github_mirror() -> None:
    """Git-facing docs name Forgejo as origin and GitHub as a public mirror only."""
    for path in GIT_DOC_PATHS:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert CANONICAL_CLONE_URL in text, (
            f"{path.name} must name the canonical Forgejo URL"
        )
        assert "canonical" in lowered, (
            f"{path.name} must describe Forgejo as the canonical repository"
        )
        assert PUBLIC_MIRROR_URL in text, (
            f"{path.name} must name the public GitHub mirror URL"
        )
        assert "mirror" in lowered, (
            f"{path.name} must describe GitHub as a public mirror"
        )


def _uncommented_text(text: str) -> str:
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_contributing_clone_uses_canonical_forgejo() -> None:
    """Development clone is Forgejo origin, not the GitHub mirror."""
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    working = _uncommented_text(text)
    assert CANONICAL_CLONE_COMMAND in working, (
        "CONTRIBUTING.md must document git clone from canonical Forgejo origin"
    )
    assert "git clone https://github.com/sussdorff/open-brain.git" not in working, (
        "CONTRIBUTING.md must not use the GitHub mirror as the working clone command"
    )


def test_readme_marketplace_source_is_canonical_forgejo() -> None:
    """Library marketplace source is the canonical Forgejo repository."""
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert MARKETPLACE_SOURCE in text, (
        "README.md marketplace source must be the canonical Forgejo repository"
    )
    assert "source: https://github.com/sussdorff/open-brain" not in text, (
        "README.md marketplace source must not be the GitHub mirror"
    )
