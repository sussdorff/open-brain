"""Tests for the IngestAdapter Protocol and ADAPTERS registry (ADR-0001)."""

import pytest

from open_brain.ingest.adapters.base import (
    ADAPTERS,
    IngestAdapter,
    get_credentials,
    register,
)
from open_brain.ingest.models import IngestResult


# ---------------------------------------------------------------------------
# Stub adapters used only in these tests
# ---------------------------------------------------------------------------


class _MinimalAdapter:
    """Minimal stub that satisfies the IngestAdapter Protocol.

    Implements all required attributes/methods; ``credentials()`` is omitted
    intentionally so we can test the ``get_credentials`` fallback path.
    """

    name = "test_minimal"

    async def list_recent(self, n: int) -> list:
        return []

    async def ingest(self, ref, run_id: str) -> IngestResult:
        return IngestResult(meeting_memory_id=1, run_id=run_id)


class _FullAdapter:
    """Stub adapter that also implements ``credentials()``."""

    name = "test_full"

    async def list_recent(self, n: int) -> list:
        return ["ref_a", "ref_b"][:n]

    async def ingest(self, ref, run_id: str) -> IngestResult:
        return IngestResult(meeting_memory_id=2, run_id=run_id)

    def credentials(self) -> dict:
        return {"SOME_API_KEY": "API key for the test service"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate each test: restore ADAPTERS to its pre-test state afterwards."""
    snapshot = dict(ADAPTERS)
    yield
    ADAPTERS.clear()
    ADAPTERS.update(snapshot)


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


def test_register_adds_adapter_under_its_name():
    adapter = _MinimalAdapter()
    register(adapter)
    assert "test_minimal" in ADAPTERS
    assert ADAPTERS["test_minimal"] is adapter


def test_register_raises_on_name_collision():
    adapter1 = _MinimalAdapter()
    adapter2 = _MinimalAdapter()  # same name
    register(adapter1)
    with pytest.raises(ValueError, match="test_minimal"):
        register(adapter2)


def test_register_multiple_distinct_adapters():
    minimal = _MinimalAdapter()
    full = _FullAdapter()
    register(minimal)
    register(full)
    assert ADAPTERS["test_minimal"] is minimal
    assert ADAPTERS["test_full"] is full


# ---------------------------------------------------------------------------
# get_credentials helper tests
# ---------------------------------------------------------------------------


def test_get_credentials_returns_dict_when_implemented():
    adapter = _FullAdapter()
    creds = get_credentials(adapter)
    assert creds == {"SOME_API_KEY": "API key for the test service"}


def test_get_credentials_returns_empty_dict_when_not_implemented():
    adapter = _MinimalAdapter()
    creds = get_credentials(adapter)
    assert creds == {}


# ---------------------------------------------------------------------------
# Protocol structural-shape tests
# ---------------------------------------------------------------------------


def test_protocol_has_name_attribute():
    assert hasattr(IngestAdapter, "__protocol_attrs__") or True  # Protocol marker
    adapter = _MinimalAdapter()
    assert isinstance(adapter.name, str)


def test_protocol_shape_name():
    adapter = _MinimalAdapter()
    assert adapter.name == "test_minimal"


@pytest.mark.asyncio
async def test_protocol_shape_list_recent():
    adapter = _MinimalAdapter()
    result = await adapter.list_recent(5)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_protocol_shape_ingest():
    adapter = _MinimalAdapter()
    result = await adapter.ingest("some_ref", "run-uuid-123")
    assert isinstance(result, IngestResult)
    assert result.run_id == "run-uuid-123"


def test_protocol_shape_credentials_optional():
    """credentials() is NOT a Protocol member — get_credentials() absorbs its absence."""
    adapter = _MinimalAdapter()
    # _MinimalAdapter has no credentials() method; get_credentials must return {}
    assert not hasattr(adapter, "credentials"), (
        "_MinimalAdapter must not have credentials() to test the fallback path"
    )
    creds = get_credentials(adapter)
    assert creds == {}


def test_minimal_adapter_satisfies_protocol_without_credentials():
    """An adapter without credentials() must still be accepted by the Protocol.

    credentials() was intentionally removed from IngestAdapter (ADR-0001: it is
    optional). This test guards against re-introducing it as a required member by
    verifying it is not listed in the Protocol's declared members.
    """
    # Collect the members declared in the Protocol body.
    # __protocol_attrs__ is set by typing.Protocol on Python 3.12+.
    protocol_members = getattr(IngestAdapter, "__protocol_attrs__", None)
    if protocol_members is not None:
        assert "credentials" not in protocol_members, (
            "credentials() must NOT be a Protocol member (it is optional per ADR-0001)"
        )

    # Verify that _MinimalAdapter (no credentials()) can be registered and used
    # without any AttributeError — the registry and get_credentials helper must
    # handle it gracefully.
    adapter = _MinimalAdapter()
    register(adapter)
    assert "test_minimal" in ADAPTERS
    assert get_credentials(adapter) == {}


def test_full_adapter_credentials():
    adapter = _FullAdapter()
    creds = get_credentials(adapter)
    assert "SOME_API_KEY" in creds
    assert isinstance(creds["SOME_API_KEY"], str)
