"""Shared test fakes and doubles for open-brain tests."""


class MockCommandRunner:
    """CommandRunner test double that returns pre-configured responses."""

    def __init__(
        self,
        responses: dict[str, tuple[int, str, str]] | None = None,
        default: tuple[int, str, str] = (0, "", ""),
    ) -> None:
        self._responses = responses or {}
        self._default = default

    def run(self, cmd: list[str]) -> tuple[int, str, str]:
        """Return the pre-configured response for the command, or the default."""
        key = " ".join(cmd)
        return self._responses.get(key, self._default)
