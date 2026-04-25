"""Base IngestAdapter Protocol — per ADR-0001."""

from typing import Any, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from open_brain.ingest.models import IngestResult


class IngestAdapter(Protocol):
    """Protocol for all ingest adapters.

    Any adapter that can ingest content into open-brain must implement this
    interface. Adapters use structural subtyping (duck-typing) — no ABC
    inheritance is required.

    Attributes:
        name: Unique snake_case identifier for this adapter. Used as the key
            in the ``ADAPTERS`` registry.

    Notes:
        ``credentials()`` is optional per ADR-0001. Adapters that require no
        credentials do not need to implement it. Use the module-level helper
        ``get_credentials(adapter)`` rather than calling ``adapter.credentials()``
        directly — it falls back to ``{}`` when the method is absent.
    """

    name: str

    async def list_recent(self, n: int) -> list[Any]:
        """Return the N most recent items from the source.

        Args:
            n: Maximum number of items to return.

        Returns:
            A list of source-specific opaque references (``Ref``). The type is
            intentionally ``Any`` in the Protocol; individual adapters narrow it.
        """
        ...

    async def ingest(self, ref: Any, run_id: str) -> "IngestResult":
        """Ingest a single item identified by *ref*.

        Args:
            ref: Source-specific opaque reference to the item to ingest.
            run_id: UUID string created by the orchestrator for this ingest run.
                Must be embedded in the returned ``IngestResult``.

        Returns:
            An ``IngestResult`` with all created memory IDs and the ``run_id``.
        """
        ...

    def credentials(self) -> dict:
        """Return credential requirements for this adapter.

        The returned dict maps credential key names (env-var names or
        1Password op-paths) to human-readable descriptions.  Adapters that
        need no credentials do not have to implement this method — the
        module-level helper ``get_credentials()`` handles the missing-method
        case gracefully.

        Returns:
            A dict mapping credential key → description.
        """
        ...


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

ADAPTERS: dict[str, "IngestAdapter"] = {}


def register(adapter: "IngestAdapter") -> None:
    """Register *adapter* in the global ``ADAPTERS`` registry.

    Each adapter module should call ``register(MyAdapter())`` at import time.
    The package ``__init__`` imports every adapter submodule, which triggers
    all registrations automatically.

    Args:
        adapter: An object that satisfies the ``IngestAdapter`` Protocol.

    Raises:
        ValueError: If an adapter with the same ``name`` is already registered.
    """
    if adapter.name in ADAPTERS:
        raise ValueError(
            f"Adapter name collision: '{adapter.name}' is already registered. "
            "Each adapter must have a unique snake_case name."
        )
    ADAPTERS[adapter.name] = adapter


def get_credentials(adapter: "IngestAdapter") -> dict:
    """Return the credential requirements for *adapter*.

    This helper calls ``adapter.credentials()`` when the method exists and
    returns ``{}`` for adapters that have not implemented it, matching the
    ADR-0001 convention that ``credentials()`` is optional.

    Args:
        adapter: A registered (or unregistered) ``IngestAdapter`` instance.

    Returns:
        A dict mapping credential key → description, or ``{}`` if the adapter
        declares no credentials.
    """
    credentials_fn = getattr(adapter, "credentials", None)
    if callable(credentials_fn):
        return credentials_fn()
    return {}
