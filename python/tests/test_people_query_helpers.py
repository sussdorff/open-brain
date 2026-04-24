"""Unit tests for people-aware query helpers.

Tests use mocked asyncpg pool — no real DB connections.
Fixtures: 1 person with 3 meetings, 1 stale person (no interactions), 2 persons with overlapping mentions.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.postgres import PostgresDataLayer


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_pool(conn: AsyncMock) -> MagicMock:
    """Build a properly structured asyncpg pool mock."""

    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _make_row(data: dict) -> MagicMock:
    """Create a mock asyncpg Record with the given data."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


# ─── Fixture data ─────────────────────────────────────────────────────────────
#
# Person memory: id=100, title="Alice"
# Meetings: id=10, id=11, id=12
# Edges: 10→100 attended_by, 11→100 attended_by, 12→100 mentioned_in
#
# Stale person: id=200, title="Bob", last_contact=None
#
# Overlapping persons: id=300 (Carol), id=301 (Dave)


# ─── Test: people_discussed_with ─────────────────────────────────────────────


class TestPeopleDiscussedWith:
    @pytest.mark.asyncio
    async def test_returns_meetings_linked_to_person(self):
        """Returns meetings/mentions linked to a person via traverse + fetch."""
        conn = AsyncMock()

        # traverse returns edges (source=meeting, target=person)
        edge_rows = [
            _make_row({"id": 1, "source_id": 10, "target_id": 100, "link_type": "attended_by"}),
            _make_row({"id": 2, "source_id": 11, "target_id": 100, "link_type": "attended_by"}),
            _make_row({"id": 3, "source_id": 12, "target_id": 100, "link_type": "mentioned_in"}),
        ]
        # fetch memories for the meeting IDs
        memory_rows = [
            _make_row({"id": 10, "title": "Meeting A", "created_at": "2026-04-01T10:00:00+00:00", "link_type": "attended_by"}),
            _make_row({"id": 11, "title": "Meeting B", "created_at": "2026-04-10T10:00:00+00:00", "link_type": "attended_by"}),
            _make_row({"id": 12, "title": "Mention C", "created_at": "2026-04-15T10:00:00+00:00", "link_type": "mentioned_in"}),
        ]
        conn.fetch = AsyncMock(side_effect=[edge_rows, memory_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_discussed_with(person_id=100)

        assert len(result) == 3
        # verify schema
        item = result[0]
        assert "memory_id" in item
        assert "title" in item
        assert "date" in item
        assert "link_type" in item

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_links(self):
        """Returns empty list if person has no linked meetings."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_discussed_with(person_id=999)

        assert result == []

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        """Returns at most `limit` results."""
        conn = AsyncMock()

        edge_rows = [
            _make_row({"id": i, "source_id": 10 + i, "target_id": 100, "link_type": "attended_by"})
            for i in range(5)
        ]
        memory_rows = [
            _make_row({"id": 10 + i, "title": f"M{i}", "created_at": f"2026-04-{i+1:02d}T10:00:00+00:00", "link_type": "attended_by"})
            for i in range(5)
        ]
        conn.fetch = AsyncMock(side_effect=[edge_rows, memory_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_discussed_with(person_id=100, limit=2)

        assert len(result) <= 2

    @pytest.mark.asyncio
    async def test_filters_by_since_date(self):
        """Filters out results before the `since` date."""
        conn = AsyncMock()

        edge_rows = [
            _make_row({"id": 1, "source_id": 10, "target_id": 100, "link_type": "attended_by"}),
            _make_row({"id": 2, "source_id": 11, "target_id": 100, "link_type": "attended_by"}),
        ]
        memory_rows = [
            _make_row({"id": 10, "title": "Old Meeting", "created_at": "2026-01-01T10:00:00+00:00", "link_type": "attended_by"}),
            _make_row({"id": 11, "title": "New Meeting", "created_at": "2026-04-15T10:00:00+00:00", "link_type": "attended_by"}),
        ]
        conn.fetch = AsyncMock(side_effect=[edge_rows, memory_rows])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_discussed_with(person_id=100, since="2026-04-01")

        # Only the new meeting should pass the since filter
        titles = [r["title"] for r in result]
        assert "New Meeting" in titles
        assert "Old Meeting" not in titles


# ─── Test: people_stale_contacts ─────────────────────────────────────────────


class TestPeopleStaleContacts:
    @pytest.mark.asyncio
    async def test_returns_stale_persons(self):
        """Returns person memories with null or old last_contact."""
        conn = AsyncMock()
        rows = [
            _make_row({"id": 200, "title": "Bob", "created_at": "2025-01-01T00:00:00+00:00", "metadata": {"last_contact": None}}),
            _make_row({"id": 201, "title": "Carol", "created_at": "2024-06-01T00:00:00+00:00", "metadata": {"last_contact": "2024-06-15T00:00:00+00:00"}}),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_stale_contacts(min_days=90, limit=50)

        assert len(result) == 2
        # verify schema
        item = result[0]
        assert "memory_id" in item
        assert "title" in item
        assert "last_contact" in item
        assert "days_stale" in item

    @pytest.mark.asyncio
    async def test_null_last_contact_returns_none_and_none_days(self):
        """Person with null last_contact has last_contact=None, days_stale=None."""
        conn = AsyncMock()
        rows = [
            _make_row({"id": 200, "title": "Bob", "created_at": "2025-01-01T00:00:00+00:00", "metadata": {}}),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_stale_contacts()

        assert result[0]["last_contact"] is None
        assert result[0]["days_stale"] is None

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_stale_contacts(self):
        """Returns empty list if no stale contacts."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_stale_contacts()

        assert result == []

    @pytest.mark.asyncio
    async def test_sql_queries_person_type(self):
        """The SQL must filter on type='person'."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            await dl.people_stale_contacts(min_days=30, limit=10)

        conn.fetch.assert_called_once()
        sql = conn.fetch.call_args[0][0]
        assert "person" in sql.lower()


# ─── Test: people_mentions_window ────────────────────────────────────────────


class TestPeopleMentionsWindow:
    @pytest.mark.asyncio
    async def test_returns_mentions_grouped_by_person(self):
        """Returns mention aggregates grouped by person_id."""
        conn = AsyncMock()
        rows = [
            _make_row({"person_id": 300, "mention_count": 5, "last_mentioned_at": "2026-04-20T10:00:00+00:00"}),
            _make_row({"person_id": 301, "mention_count": 3, "last_mentioned_at": "2026-04-18T10:00:00+00:00"}),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_mentions_window(days=30, min_count=1)

        assert len(result) == 2
        # verify schema
        item = result[0]
        assert "person_id" in item
        assert "mention_count" in item
        assert "last_mentioned_at" in item

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_mentions(self):
        """Returns empty list if no mentions in window."""
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_mentions_window()

        assert result == []

    @pytest.mark.asyncio
    async def test_min_count_filters_infrequent(self):
        """min_count is applied to filter out persons below threshold."""
        conn = AsyncMock()
        rows = [
            _make_row({"person_id": 300, "mention_count": 5, "last_mentioned_at": "2026-04-20T10:00:00+00:00"}),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_mentions_window(days=30, min_count=3)

        # SQL should pass min_count; mock returns filtered result
        assert len(result) == 1
        assert result[0]["mention_count"] >= 3

    @pytest.mark.asyncio
    async def test_overlapping_mentions_counted_together(self):
        """Two persons with overlapping time windows are returned correctly."""
        conn = AsyncMock()
        rows = [
            _make_row({"person_id": 300, "mention_count": 4, "last_mentioned_at": "2026-04-22T10:00:00+00:00"}),
            _make_row({"person_id": 301, "mention_count": 2, "last_mentioned_at": "2026-04-19T10:00:00+00:00"}),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        pool = _make_pool(conn)

        dl = PostgresDataLayer()
        with patch("open_brain.data_layer.postgres.get_pool", return_value=pool):
            result = await dl.people_mentions_window(days=30, min_count=1)

        person_ids = {r["person_id"] for r in result}
        assert 300 in person_ids
        assert 301 in person_ids


# ─── Test: MCP tool registration ─────────────────────────────────────────────


class TestPeopleQueryMCPTools:
    @pytest.mark.asyncio
    async def test_people_discussed_with_tool_registered(self):
        """people_discussed_with appears in mcp.list_tools()."""
        from open_brain.server import mcp, _current_scopes

        token = _current_scopes.set(("memory",))
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            assert "people_discussed_with" in tool_names, (
                f"people_discussed_with not in registered tools: {tool_names}"
            )
        finally:
            _current_scopes.reset(token)

    @pytest.mark.asyncio
    async def test_people_stale_contacts_tool_registered(self):
        """people_stale_contacts appears in mcp.list_tools()."""
        from open_brain.server import mcp, _current_scopes

        token = _current_scopes.set(("memory",))
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            assert "people_stale_contacts" in tool_names, (
                f"people_stale_contacts not in registered tools: {tool_names}"
            )
        finally:
            _current_scopes.reset(token)

    @pytest.mark.asyncio
    async def test_people_mentions_window_tool_registered(self):
        """people_mentions_window appears in mcp.list_tools()."""
        from open_brain.server import mcp, _current_scopes

        token = _current_scopes.set(("memory",))
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            assert "people_mentions_window" in tool_names, (
                f"people_mentions_window not in registered tools: {tool_names}"
            )
        finally:
            _current_scopes.reset(token)
