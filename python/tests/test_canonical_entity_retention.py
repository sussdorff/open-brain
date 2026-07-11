"""Tests for canonical entity retention during automated maintenance."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import (
    CompactParams,
    DecayParams,
    RefineAction,
    TriageParams,
    Memory,
)
from open_brain.data_layer.postgres import PostgresDataLayer


def _pool(conn: AsyncMock) -> MagicMock:
    @asynccontextmanager
    async def fake_acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = fake_acquire
    return pool


def _row(
    memory_id: int,
    *,
    access_count: int = 0,
    metadata: dict | None = None,
    memory_type: str = "observation",
) -> MagicMock:
    data = {
        "id": memory_id,
        "index_id": 1,
        "session_id": None,
        "type": memory_type,
        "title": f"Memory {memory_id}",
        "subtitle": None,
        "narrative": None,
        "content": "shared content",
        "metadata": metadata or {},
        "priority": 0.5,
        "stability": "stable",
        "access_count": access_count,
        "last_accessed_at": None,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "importance": "medium",
    }
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    row.get = lambda key, default=None: data.get(key, default)
    return row


def _id_row(memory_id: int) -> MagicMock:
    row = MagicMock()
    row.__getitem__ = lambda self, key: memory_id if key == "id" else None
    return row


def _sim_row(id1: int, id2: int, similarity: float = 0.95) -> MagicMock:
    data = {"id1": id1, "id2": id2, "similarity": similarity}
    row = MagicMock()
    row.__getitem__ = lambda self, key: data[key]
    return row


def _memory(memory_id: int, *, metadata: dict | None = None) -> Memory:
    return Memory(
        id=memory_id,
        index_id=1,
        session_id=None,
        type="observation",
        title="Duplicate title",
        subtitle=None,
        narrative=None,
        content="Duplicate content",
        metadata=metadata or {},
        priority=0.5,
        stability="stable",
        access_count=0,
        last_accessed_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        user_id=None,
        importance="medium",
    )


def _canonical_metadata(kind: str = "concept") -> dict:
    return {"canonical_entity": True, "canonical_kind": kind}


def test_shared_sql_guard_uses_metadata_canonical_entity_marker() -> None:
    """All SQL sites should reuse the canonical metadata protection predicate."""
    from open_brain.data_layer.postgres import canonical_entity_protection_predicate

    assert canonical_entity_protection_predicate() == (
        "(metadata->>'canonical_entity') IS DISTINCT FROM 'true'"
    )
    assert canonical_entity_protection_predicate("m") == (
        "(m.metadata->>'canonical_entity') IS DISTINCT FROM 'true'"
    )


@pytest.mark.asyncio
async def test_decay_dry_run_excludes_and_counts_protected_canonical_entities() -> None:
    """Decay dry-runs exclude canonical entities and report skipped candidates."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=[1, 0, 0, 2])
    dl = PostgresDataLayer()

    with patch("open_brain.data_layer.postgres.get_pool", return_value=_pool(conn)):
        result = await dl.decay_memories(DecayParams(dry_run=True))

    decay_sql = conn.fetchval.call_args_list[0].args[0]
    protected_sql = conn.fetchval.call_args_list[3].args[0]
    assert "canonical_entity" in decay_sql
    assert "canonical_entity" in protected_sql
    assert result.protected_canonical_entities == 2


@pytest.mark.asyncio
async def test_recall_decay_update_has_canonical_mutation_guard() -> None:
    """Recall-triggered decay must guard the UPDATE mutation site."""
    conn = AsyncMock()
    dl = PostgresDataLayer()

    await dl._apply_recall_decay(conn, memory_id=42, importance="medium", access_count=0)

    sql = conn.execute.call_args.args[0]
    assert "canonical_entity" in sql


@pytest.mark.asyncio
async def test_compact_dry_run_excludes_protected_entities_from_delete_plan() -> None:
    """Dry-run compaction plans must never list canonical entities in to_delete."""
    protected = _row(10, access_count=1, metadata=_canonical_metadata("project"))
    ordinary = _row(11, access_count=9)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetch = AsyncMock(side_effect=[[protected, ordinary], [_sim_row(10, 11)]])
    dl = PostgresDataLayer()

    with patch("open_brain.data_layer.postgres.get_pool", return_value=_pool(conn)):
        result = await dl.compact_memories(CompactParams(dry_run=True))

    assert result.protected_canonical_entities == 1
    assert all(10 not in plan.to_delete for plan in result.plan)
    assert 10 not in result.deleted_ids


@pytest.mark.asyncio
async def test_compact_execute_repoints_relationships_before_guarded_delete() -> None:
    """Edges incident to compacted losers are repointed to the survivor before delete."""
    loser = _row(20, access_count=1)
    survivor = _row(21, access_count=9)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    # 3rd fetch is the pre-repoint canonical re-check: id 20 is still safe.
    conn.fetch = AsyncMock(
        side_effect=[[loser, survivor], [_sim_row(20, 21)], [_id_row(20)]]
    )
    conn.execute = AsyncMock(
        side_effect=["DELETE 0", "DELETE 0", "DELETE 0", "UPDATE 1", "DELETE 0", "DELETE 1"]
    )
    dl = PostgresDataLayer()

    with patch("open_brain.data_layer.postgres.get_pool", return_value=_pool(conn)):
        result = await dl.compact_memories(CompactParams(dry_run=False))

    sql_calls = [call.args[0] for call in conn.execute.call_args_list]
    assert any("UPDATE memory_relationships" in sql and "source_id = CASE" in sql for sql in sql_calls)
    assert not any("WHERE source_id = ANY" in sql and "target_id = ANY" in sql for sql in sql_calls)
    assert "canonical_entity" in sql_calls[-1]
    assert result.memories_deleted == 1


@pytest.mark.asyncio
async def test_compact_execute_skips_loser_promoted_to_canonical_before_repoint() -> None:
    """A loser promoted to canonical between planning and repoint must not be
    repointed or deleted, and is reported as a protected skip.

    Simulates the TOCTOU window: the candidate plan includes non-canonical
    loser id 20, but the pre-repoint re-check reports it as newly protected
    (returns no safe ids). Its relationships must be left intact.
    """
    loser = _row(20, access_count=1)
    survivor = _row(21, access_count=9)
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=0)
    # 1: candidate rows, 2: pairwise similarity, 3: pre-repoint re-check ->
    #    EMPTY, i.e. id 20 became canonical, so nothing is safe to delete.
    conn.fetch = AsyncMock(side_effect=[[loser, survivor], [_sim_row(20, 21)], []])
    conn.execute = AsyncMock(side_effect=["DELETE 0", "DELETE 0"])
    dl = PostgresDataLayer()

    with (
        patch("open_brain.data_layer.postgres.get_pool", return_value=_pool(conn)),
        patch(
            "open_brain.data_layer.postgres._repoint_relationships",
            new_callable=AsyncMock,
        ) as repoint,
    ):
        result = await dl.compact_memories(CompactParams(dry_run=False))

    repoint.assert_not_awaited()
    assert result.deleted_ids == []
    assert result.memories_deleted == 0
    assert result.protected_canonical_entities == 1
    # The now-protected loser and its survivor are both retained.
    assert 20 in result.memories_kept
    assert 21 in result.memories_kept


@pytest.mark.asyncio
async def test_refine_merge_skips_loser_promoted_to_canonical_before_repoint() -> None:
    """A merge loser promoted to canonical after the LLM-merge window must not be
    repointed or deleted, and is counted in protected_skipped.

    Simulates the TOCTOU window: the mutation-site filter at function entry sees
    no protected ids, but the pre-repoint re-check reports loser id 42 as newly
    protected (returns no safe ids).
    """
    from open_brain.data_layer.postgres import _execute_refine_action

    conn = AsyncMock()
    # 1: mutation-site protected check -> none protected at entry.
    # 2: pre-repoint re-check -> EMPTY, i.e. id 42 became canonical.
    conn.fetch = AsyncMock(side_effect=[[], []])
    conn.execute = AsyncMock(return_value="DELETE 0")
    action = RefineAction(
        action="merge",
        memory_ids=[40, 42],
        reason="LLM suggested a merge",
        skip_llm_merge=True,
    )

    with patch(
        "open_brain.data_layer.postgres._repoint_relationships",
        new_callable=AsyncMock,
    ) as repoint:
        skipped = await _execute_refine_action(conn, action)

    repoint.assert_not_awaited()
    assert skipped == 1
    # The guarded DELETE ran against no ids (the loser was dropped by the re-check).
    assert conn.execute.call_args_list[-1].args[1:] == ([],)


@pytest.mark.asyncio
@pytest.mark.parametrize("action_name", ["demote", "delete"])
async def test_refine_filters_protected_ids_for_destructive_actions(action_name: str) -> None:
    """Refine demote/delete mutations skip canonical IDs at execution time."""
    from open_brain.data_layer.postgres import _execute_refine_action

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[_id_row(30)])
    conn.execute = AsyncMock(return_value="UPDATE 1" if action_name == "demote" else "DELETE 1")
    action = RefineAction(action=action_name, memory_ids=[30, 31], reason="LLM suggested it")

    skipped = await _execute_refine_action(conn, action)

    assert skipped == 1
    assert action.memory_ids == [31]
    sql = conn.execute.call_args.args[0]
    assert "canonical_entity" in sql
    assert 30 not in conn.execute.call_args.args[1:]


@pytest.mark.asyncio
async def test_refine_merge_skips_protected_ids_and_repoints_remaining_edges() -> None:
    """Refine merge must not delete protected IDs and must preserve loser relationships."""
    from open_brain.data_layer.postgres import _execute_refine_action

    conn = AsyncMock()
    # 1st fetch: mutation-site protected check flags id 41.
    # 2nd fetch: pre-repoint re-check confirms id 42 is still safe to remove.
    conn.fetch = AsyncMock(side_effect=[[_id_row(41)], [_id_row(42)]])
    conn.execute = AsyncMock(
        side_effect=["DELETE 0", "DELETE 0", "DELETE 0", "UPDATE 1", "DELETE 1"]
    )
    action = RefineAction(
        action="merge",
        memory_ids=[40, 41, 42],
        reason="LLM suggested a mixed merge",
        skip_llm_merge=True,
    )

    skipped = await _execute_refine_action(conn, action)

    sql_calls = [call.args[0] for call in conn.execute.call_args_list]
    assert skipped == 1
    assert action.memory_ids == [40, 42]
    assert any("UPDATE memory_relationships" in sql and "source_id = CASE" in sql for sql in sql_calls)
    assert conn.execute.call_args_list[-1].args[1:] == ([42],)


@pytest.mark.asyncio
async def test_triage_candidate_filter_excludes_archived_and_canonical_entities() -> None:
    """Triage must use the shared lifecycle filter instead of a divergent copy."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    dl = PostgresDataLayer()

    with patch("open_brain.data_layer.postgres.get_pool", return_value=_pool(conn)):
        await dl.triage_memories(TriageParams(dry_run=True))

    sql = conn.fetch.call_args.args[0]
    assert "archived" in sql
    assert "canonical_entity" in sql


@pytest.mark.asyncio
async def test_refine_prompt_marks_canonical_entities_as_protected() -> None:
    """The refine LLM prompt should not invite destructive actions for canonical entities."""
    from open_brain.data_layer import refine

    canonical = _memory(50, metadata=_canonical_metadata("concept"))

    with patch("open_brain.data_layer.refine.llm_complete", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = "[]"
        await refine._analyze_batch([canonical])

    prompt = mock_llm.await_args.args[0][0].content
    assert "Canonical entity IDs" in prompt
    assert "merge, demote, or delete" in prompt


def test_refine_duplicate_fallback_ignores_canonical_entities() -> None:
    """No-LLM duplicate fallback must not merge protected canonical entities."""
    from open_brain.data_layer.refine import find_obvious_duplicates

    canonical = _memory(60, metadata=_canonical_metadata("person"))
    ordinary = _memory(61)

    assert find_obvious_duplicates([canonical, ordinary]) == []


@pytest.mark.asyncio
async def test_materialize_archive_skips_canonical_entity_at_mutation_site() -> None:
    """materialize_archive must guard canonical entities at the mutation site.

    A canonical entity (non-critical importance) must be skipped as a success
    without invoking the archive/update callback, while an ordinary,
    non-critical memory is archived (priority -> 0.1) as before.
    """
    from open_brain.data_layer.materialize import materialize_archive

    update_fn = AsyncMock()

    canonical = _memory(80, metadata=_canonical_metadata("concept"))
    canonical_result = await materialize_archive(canonical, update_fn)

    assert canonical_result.success is True
    assert canonical_result.detail == "skipped: protected canonical entity"
    update_fn.assert_not_awaited()

    ordinary = _memory(81)
    ordinary_result = await materialize_archive(ordinary, update_fn)

    assert ordinary_result.success is True
    assert ordinary_result.detail == "Priority set to 0.1"
    update_fn.assert_awaited_once_with(81, 0.1)


@pytest.mark.asyncio
async def test_triage_prompt_marks_canonical_entities_as_keep_only() -> None:
    """The triage LLM prompt should direct canonical entities to keep."""
    from open_brain.data_layer import triage

    canonical = _memory(70, metadata=_canonical_metadata("organization"))

    with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
        mock_llm.return_value = '[{"memory_id":70,"action":"archive","reason":"bad"}]'
        actions = await triage._triage_batch([canonical])

    prompt = mock_llm.await_args.args[0][0].content
    assert "Canonical entity IDs" in prompt
    assert "classify them as keep" in prompt
    assert actions[0].action == "keep"
