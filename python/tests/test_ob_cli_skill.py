from __future__ import annotations

from pathlib import Path

import yaml


SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "ob-cli" / "SKILL.md"


def test_ob_cli_skill_frontmatter_is_valid_yaml() -> None:
    text = SKILL_PATH.read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---\n", 2)[1]

    data = yaml.safe_load(frontmatter)

    assert data["name"] == "ob-cli"
    assert data["argument-hint"] == "[subcommand] [args]"
