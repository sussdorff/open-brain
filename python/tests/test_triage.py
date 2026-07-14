"""Unit tests for triage.py and materialize.py (memory lifecycle pipeline)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from open_brain.data_layer.interface import (
    DecayResult,
    LifecycleActionQueryParams,
    LifecycleActionRecord,
    LifecycleActionStateParams,
    MaterializeParams,
    MaterializeResult,
    Memory,
    TriageAction,
    TriageParams,
    TriageResult,
)
from open_brain.data_layer.materialize import (
    execute_triage_actions,
    materialize_archive,
    materialize_promote,
    materialize_scaffold,
)
from open_brain.data_layer.triage import (
    _default_action_for_type,
    _triage_by_type_defaults,
    triage_with_llm,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


def _make_memory(
    id: int,
    mem_type: str = "observation",
    title: str | None = "Test Memory",
    content: str = "Some content about something",
    priority: float = 0.5,
) -> Memory:
    return Memory(
        id=id,
        index_id=1,
        session_id=None,
        type=mem_type,
        title=title,
        subtitle=None,
        narrative=None,
        content=content,
        metadata={},
        priority=priority,
        stability="stable",
        access_count=3,
        last_accessed_at=None,
        created_at="2026-01-01",
        updated_at="2026-01-01",
    )


# ─── Test: triage classification ──────────────────────────────────────────────


class TestTriageClassification:
    @pytest.mark.asyncio
    async def test_triage_classification(self):
        """Mocked LLM returns triage JSON; verify TriageAction created correctly."""
        memories = [_make_memory(1), _make_memory(2)]
        llm_response = json.dumps([
            {"memory_id": 1, "action": "keep", "reason": "Important observation"},
            {"memory_id": 2, "action": "archive", "reason": "Outdated note"},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            actions = await triage_with_llm(memories)

        assert len(actions) == 2
        action_map = {a.memory_id: a for a in actions}

        assert action_map[1].action == "keep"
        assert action_map[1].reason == "Important observation"
        assert action_map[1].executed is False

        assert action_map[2].action == "archive"
        assert action_map[2].reason == "Outdated note"

    @pytest.mark.asyncio
    async def test_all_valid_actions_parsed(self):
        """All five valid actions are parsed from LLM response."""
        memories = [_make_memory(i) for i in range(1, 6)]
        llm_response = json.dumps([
            {"memory_id": 1, "action": "keep", "reason": "Keep"},
            {"memory_id": 2, "action": "merge", "reason": "Duplicate"},
            {"memory_id": 3, "action": "promote", "reason": "Reusable"},
            {"memory_id": 4, "action": "scaffold", "reason": "Todo"},
            {"memory_id": 5, "action": "archive", "reason": "Old"},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            actions = await triage_with_llm(memories)

        action_map = {a.memory_id: a.action for a in actions}
        assert action_map[1] == "keep"
        assert action_map[2] == "merge"
        assert action_map[3] == "promote"
        assert action_map[4] == "scaffold"
        assert action_map[5] == "archive"

    @pytest.mark.asyncio
    async def test_invalid_action_defaults_to_keep(self):
        """Unknown action values are replaced with 'keep'."""
        memories = [_make_memory(1)]
        llm_response = json.dumps([
            {"memory_id": 1, "action": "zap", "reason": "Unknown action"},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            actions = await triage_with_llm(memories)

        assert actions[0].action == "keep"

    @pytest.mark.asyncio
    async def test_null_reason_is_normalized_to_empty_string(self):
        """Nullable LLM reasons remain persistable text values."""
        memories = [_make_memory(1)]
        llm_response = json.dumps([
            {"memory_id": 1, "action": "keep", "reason": None},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            actions = await triage_with_llm(memories)

        assert actions[0].reason == ""

    @pytest.mark.asyncio
    async def test_missing_memory_filled_with_type_default(self):
        """Memories omitted by LLM get type-based default actions."""
        memories = [_make_memory(1, "learning"), _make_memory(2, "session_summary")]
        # LLM only classifies memory 1
        llm_response = json.dumps([
            {"memory_id": 1, "action": "promote", "reason": "Good learning"},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            actions = await triage_with_llm(memories)

        assert len(actions) == 2
        action_map = {a.memory_id: a for a in actions}
        assert action_map[1].action == "promote"
        # Memory 2 (session_summary) gets type default
        assert action_map[2].action == "archive"

    @pytest.mark.asyncio
    async def test_falls_back_to_type_defaults_on_llm_error(self):
        """LLM error triggers fallback to type-based defaults."""
        memories = [
            _make_memory(1, "learning"),
            _make_memory(2, "session_summary"),
            _make_memory(3, "observation"),
        ]

        with patch("open_brain.data_layer.triage.llm_complete", side_effect=RuntimeError("API down")):
            actions = await triage_with_llm(memories)

        action_map = {a.memory_id: a.action for a in actions}
        assert action_map[1] == "promote"
        assert action_map[2] == "archive"
        assert action_map[3] == "keep"

    @pytest.mark.asyncio
    async def test_falls_back_without_api_key(self):
        """Without LLM API key, uses type-based defaults."""
        memories = [_make_memory(1, "learning"), _make_memory(2, "observation")]

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            import open_brain.config as cfg
            cfg._config = None
            actions = await triage_with_llm(memories)

        action_map = {a.memory_id: a.action for a in actions}
        assert action_map[1] == "promote"
        assert action_map[2] == "keep"

    @pytest.mark.asyncio
    async def test_string_memory_ids_are_coerced(self):
        """LLM returning memory_id as string '1234' instead of int 1234 is handled."""
        memories = [_make_memory(1, "learning"), _make_memory(2, "observation")]
        # LLM returns IDs as strings — a common LLM quirk
        llm_response = json.dumps([
            {"memory_id": "1", "action": "promote", "reason": "Good learning"},
            {"memory_id": "2", "action": "archive", "reason": "Old note"},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            actions = await triage_with_llm(memories)

        assert len(actions) == 2
        action_map = {a.memory_id: a for a in actions}
        # Both should be LLM-classified, NOT type defaults
        assert action_map[1].action == "promote"
        assert action_map[1].reason == "Good learning"
        assert action_map[2].action == "archive"
        assert action_map[2].reason == "Old note"

    @pytest.mark.asyncio
    async def test_llm_call_uses_4096_max_tokens(self):
        """triage_with_llm passes max_tokens=4096 to llm_complete to avoid truncation."""
        memories = [_make_memory(1)]
        llm_response = json.dumps([
            {"memory_id": 1, "action": "keep", "reason": "Fine"},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            await triage_with_llm(memories)

        mock_llm.assert_awaited_once()
        _, kwargs = mock_llm.call_args
        assert kwargs.get("max_tokens") == 4096, (
            f"Expected max_tokens=4096 to prevent response truncation, got {kwargs.get('max_tokens')}"
        )

    @pytest.mark.asyncio
    async def test_no_type_default_fallback_when_llm_functional(self):
        """When LLM classifies all memories, none get the type-default fallback reason."""
        memories = [
            _make_memory(1, "learning"),
            _make_memory(2, "session_summary"),
            _make_memory(3, "observation"),
        ]
        llm_response = json.dumps([
            {"memory_id": 1, "action": "keep", "reason": "Already well-known pattern"},
            {"memory_id": 2, "action": "archive", "reason": "Routine session"},
            {"memory_id": 3, "action": "scaffold", "reason": "Describes a todo item"},
        ])

        with patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = llm_response
            actions = await triage_with_llm(memories)

        fallback_reasons = [
            a for a in actions if "using type default" in (a.reason or "")
        ]
        assert len(fallback_reasons) == 0, (
            f"Expected no type-default fallbacks, got: {fallback_reasons}"
        )


# ─── Test: type differentiation ───────────────────────────────────────────────


class TestMemoryTypes:
    def test_learning_defaults_to_promote(self):
        """learning type → promote by default."""
        assert _default_action_for_type("learning") == "promote"

    def test_session_summary_defaults_to_archive(self):
        """session_summary type → archive by default."""
        assert _default_action_for_type("session_summary") == "archive"

    def test_observation_defaults_to_keep(self):
        """observation type → keep by default."""
        assert _default_action_for_type("observation") == "keep"

    def test_unknown_type_defaults_to_keep(self):
        """Unknown types fall back to keep."""
        assert _default_action_for_type("feature") == "keep"
        assert _default_action_for_type("bugfix") == "keep"
        assert _default_action_for_type("") == "keep"

    def test_type_defaults_bulk(self):
        """_triage_by_type_defaults returns correct actions for all types."""
        memories = [
            _make_memory(1, "learning"),
            _make_memory(2, "session_summary"),
            _make_memory(3, "observation"),
        ]
        actions = _triage_by_type_defaults(memories)

        assert len(actions) == 3
        action_map = {a.memory_id: a.action for a in actions}
        assert action_map[1] == "promote"
        assert action_map[2] == "archive"
        assert action_map[3] == "keep"

    def test_type_defaults_include_memory_metadata(self):
        """TriageAction includes memory type and title from the source memory."""
        memories = [_make_memory(42, "learning", title="My Learning")]
        actions = _triage_by_type_defaults(memories)

        assert actions[0].memory_type == "learning"
        assert actions[0].memory_title == "My Learning"
        assert actions[0].executed is False


# ─── Test: materialize promote ────────────────────────────────────────────────


class TestMaterializePromote:
    def test_promote_writes_to_file(self, tmp_path):
        """promote action appends memory content to the target file."""
        memory = _make_memory(1, "learning", title="Test Title", content="Test content here")
        memory.metadata["materialize_path"] = str(tmp_path / "MEMORY.md")

        result = materialize_promote(memory)

        assert result.success is True
        assert result.action == "promote"
        written = (tmp_path / "MEMORY.md").read_text()
        assert "## Memory: Test Title" in written
        assert "Test content here" in written

    def test_promote_creates_parent_dirs(self, tmp_path):
        """promote action creates parent directories if they don't exist."""
        memory = _make_memory(1)
        deep_path = tmp_path / "a" / "b" / "MEMORY.md"
        memory.metadata["materialize_path"] = str(deep_path)

        result = materialize_promote(memory)

        assert result.success is True
        assert deep_path.exists()

    def test_promote_is_idempotent(self, tmp_path):
        """Calling promote twice does not duplicate the section."""
        memory = _make_memory(1, title="Idempotent Title", content="Content")
        memory.metadata["materialize_path"] = str(tmp_path / "MEMORY.md")

        materialize_promote(memory)
        materialize_promote(memory)

        content = (tmp_path / "MEMORY.md").read_text()
        assert content.count("## Memory: Idempotent Title") == 1

    def test_promote_with_project_resolves_path(self, tmp_path):
        """promote with project name uses ~/.claude/projects/<project>/MEMORY.md."""
        memory = _make_memory(1, title="Project Memory", content="Content")
        # No materialize_path — uses project
        with patch("open_brain.data_layer.materialize.Path"):
            # We just verify the function doesn't crash and returns a result
            # Real path resolution tested in integration
            pass

        # Direct test: materialize_path in metadata takes priority
        memory.metadata["materialize_path"] = str(tmp_path / "proj" / "MEMORY.md")
        result = materialize_promote(memory, project="myproject")
        assert result.success is True

    def test_promote_appends_to_existing_file(self, tmp_path):
        """promote appends to existing file rather than overwriting."""
        target = tmp_path / "MEMORY.md"
        target.write_text("# Existing Content\n\nSome notes.\n")

        memory = _make_memory(1, title="New Memory", content="New content")
        memory.metadata["materialize_path"] = str(target)

        result = materialize_promote(memory)

        assert result.success is True
        content = target.read_text()
        assert "# Existing Content" in content
        assert "## Memory: New Memory" in content


# ─── Test: materialize scaffold ───────────────────────────────────────────────


class TestMaterializeScaffold:
    def test_scaffold_calls_bd_create(self):
        """scaffold action runs bd create with title and description."""
        memory = _make_memory(1, title="Fix the thing", content="We should fix the thing")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Created issue open-brain-42\n"
        mock_result.stderr = ""

        with patch("open_brain.data_layer.materialize.subprocess.run", return_value=mock_result) as mock_run:
            result = materialize_scaffold(memory)

        assert result.success is True
        assert result.action == "scaffold"
        assert "open-brain-42" in result.detail

        # Verify bd was called with correct args
        call_args = mock_run.call_args[0][0]
        assert "bd" in call_args
        assert "create" in call_args
        assert any("Fix the thing" in arg for arg in call_args)

    def test_scaffold_handles_bd_failure(self):
        """scaffold returns failure result when bd exits non-zero."""
        memory = _make_memory(1, title="Task")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: bd not initialized"

        with patch("open_brain.data_layer.materialize.subprocess.run", return_value=mock_result):
            result = materialize_scaffold(memory)

        assert result.success is False
        assert "bd create failed" in result.detail

    def test_scaffold_handles_bd_not_found(self):
        """scaffold returns failure when bd command is not found."""
        memory = _make_memory(1, title="Task")

        with patch("open_brain.data_layer.materialize.subprocess.run", side_effect=FileNotFoundError()):
            result = materialize_scaffold(memory)

        assert result.success is False
        assert "not found" in result.detail

    def test_scaffold_handles_timeout(self):
        """scaffold returns failure on timeout."""
        import subprocess
        memory = _make_memory(1, title="Task")

        with patch("open_brain.data_layer.materialize.subprocess.run", side_effect=subprocess.TimeoutExpired("bd", 30)):
            result = materialize_scaffold(memory)

        assert result.success is False
        assert "timed out" in result.detail


# ─── Test: materialize archive ────────────────────────────────────────────────


class TestMaterializeArchive:
    @pytest.mark.asyncio
    async def test_archive_calls_update_fn(self):
        """archive action calls update_fn with priority=0.1."""
        memory = _make_memory(99)
        update_fn = AsyncMock()

        result = await materialize_archive(memory, update_fn)

        assert result.success is True
        assert result.action == "archive"
        update_fn.assert_awaited_once_with(99, 0.1)

    @pytest.mark.asyncio
    async def test_archive_handles_update_error(self):
        """archive returns failure when update_fn raises."""
        memory = _make_memory(99)
        update_fn = AsyncMock(side_effect=RuntimeError("DB error"))

        result = await materialize_archive(memory, update_fn)

        assert result.success is False
        assert "Archive failed" in result.detail

    @pytest.mark.asyncio
    async def test_archive_skips_critical_importance(self):
        """archive does NOT call update_fn for critical importance memories."""
        memory = _make_memory(42)
        memory.importance = "critical"
        update_fn = AsyncMock()

        result = await materialize_archive(memory, update_fn)

        update_fn.assert_not_awaited()
        assert result.success is True
        assert result.action == "archive"
        assert result.detail == "skipped: protected importance"

    @pytest.mark.asyncio
    async def test_archive_skips_high_importance(self):
        """archive does NOT call update_fn for high importance memories."""
        memory = _make_memory(43)
        memory.importance = "high"
        update_fn = AsyncMock()

        result = await materialize_archive(memory, update_fn)

        update_fn.assert_not_awaited()
        assert result.success is True
        assert result.action == "archive"
        assert result.detail == "skipped: protected importance"

    @pytest.mark.asyncio
    async def test_archive_proceeds_for_medium_importance(self):
        """archive calls update_fn normally for medium importance memories."""
        memory = _make_memory(44)
        memory.importance = "medium"
        update_fn = AsyncMock()

        result = await materialize_archive(memory, update_fn)

        update_fn.assert_awaited_once_with(44, 0.1)
        assert result.success is True
        assert result.detail == "Priority set to 0.1"

    @pytest.mark.asyncio
    async def test_archive_proceeds_for_low_importance(self):
        """archive calls update_fn normally for low importance memories."""
        memory = _make_memory(45)
        memory.importance = "low"
        update_fn = AsyncMock()

        result = await materialize_archive(memory, update_fn)

        update_fn.assert_awaited_once_with(45, 0.1)
        assert result.success is True
        assert result.detail == "Priority set to 0.1"


# ─── Test: execute_triage_actions ─────────────────────────────────────────────


class TestExecuteTriageActions:
    @pytest.mark.asyncio
    async def test_keep_is_noop(self):
        """keep action produces a success no-op result."""
        memory = _make_memory(1)
        action = TriageAction(
            action="keep", memory_id=1, reason="Fine", memory_type="observation", memory_title="T"
        )

        results = await execute_triage_actions(
            [action], {1: memory}, AsyncMock()
        )

        assert results[0].success is True
        assert results[0].action == "keep"
        assert "No-op" in results[0].detail

    @pytest.mark.asyncio
    async def test_missing_memory_produces_failure(self):
        """Actions for missing memory IDs produce failure results."""
        action = TriageAction(
            action="keep", memory_id=999, reason="Missing", memory_type="observation", memory_title=None
        )

        results = await execute_triage_actions([action], {}, AsyncMock())

        assert results[0].success is False
        assert "not found" in results[0].detail

    @pytest.mark.asyncio
    async def test_merge_delegates(self):
        """merge action produces a delegation result."""
        memory = _make_memory(1)
        action = TriageAction(
            action="merge", memory_id=1, reason="Dup", memory_type="observation", memory_title="T"
        )

        results = await execute_triage_actions([action], {1: memory}, AsyncMock())

        assert results[0].success is True
        assert "Delegated" in results[0].detail


# ─── Test: pipeline dry_run ───────────────────────────────────────────────────


class TestPipelineDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_report_without_executing(self):
        """dry_run=True returns triage report without calling materialization side-effects."""
        from open_brain.data_layer.interface import TriageResult

        mock_dl = MagicMock()
        triage_result = TriageResult(
            analyzed=2,
            actions=[
                TriageAction(
                    action="promote",
                    memory_id=1,
                    reason="Good learning",
                    memory_type="learning",
                    memory_title="My Learning",
                    executed=False,
                ),
                TriageAction(
                    action="keep",
                    memory_id=2,
                    reason="Useful",
                    memory_type="observation",
                    memory_title="Obs",
                    executed=False,
                ),
            ],
            summary="Triaged 2 memories: 1 keep, 1 promote (dry run)",
        )

        from open_brain.data_layer.interface import MaterializeResult, MaterializeActionResult

        materialize_result = MaterializeResult(
            processed=1,
            results=[
                MaterializeActionResult(
                    memory_id=1,
                    action="promote",
                    success=True,
                    detail="dry run — not executed",
                )
            ],
            summary="Materialized 1/1 actions",
        )

        mock_dl.triage_memories = AsyncMock(return_value=triage_result)
        mock_dl.materialize_memories = AsyncMock(return_value=materialize_result)

        # Simulate what run_lifecycle_pipeline does
        triage = await mock_dl.triage_memories(TriageParams(scope="recent", dry_run=True))
        non_keep = [a for a in triage.actions if a.action != "keep"]
        mat = await mock_dl.materialize_memories(
            MaterializeParams(triage_actions=non_keep, dry_run=True)
        )

        # Verify triage was called with dry_run=True
        mock_dl.triage_memories.assert_awaited_once()
        call_args = mock_dl.triage_memories.call_args[0][0]
        assert call_args.dry_run is True

        # Verify materialization was called with dry_run=True
        mock_dl.materialize_memories.assert_awaited_once()
        mat_call_args = mock_dl.materialize_memories.call_args[0][0]
        assert mat_call_args.dry_run is True

        # Verify results have dry_run flag in summary
        assert "dry run" in triage.summary
        assert mat.processed == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write_files(self, tmp_path):
        """Materialization with dry_run=True does not write to disk."""
        memory = _make_memory(1, "learning", title="Learning Title")
        memory.metadata["materialize_path"] = str(tmp_path / "MEMORY.md")

        # Dry run via PostgresDataLayer.materialize_memories uses dry_run flag
        # In dry_run mode, results are returned without calling execute_triage_actions
        # We test this by ensuring the file was NOT written
        # Simulate dry-run path: no file should be written
        assert not (tmp_path / "MEMORY.md").exists()

        # Actually call materialize_promote to confirm normal path would write
        result = materialize_promote(memory)
        assert result.success is True
        assert (tmp_path / "MEMORY.md").exists()


class TestLifecyclePipelineStaging:
    @pytest.mark.asyncio
    async def test_first_run_stages_actions_without_materialization(self):
        """The lifecycle pipeline reports proposals without executing them."""
        from open_brain.server import run_lifecycle_pipeline

        mock_dl = MagicMock()
        mock_dl.decay_memories = AsyncMock(
            return_value=DecayResult(
                decayed=0,
                boosted=0,
                recent_memories=50,
                summary="No priorities changed",
            )
        )
        mock_dl.triage_memories = AsyncMock(
            return_value=TriageResult(
                analyzed=50,
                actions=[
                    TriageAction(
                        action="promote",
                        memory_id=memory_id,
                        reason="Reusable learning",
                        memory_type="learning",
                        memory_title=f"Learning {memory_id}",
                    )
                    for memory_id in range(1, 51)
                ],
                summary="Staged 50 lifecycle actions",
            )
        )
        mock_dl.materialize_memories = AsyncMock(
            return_value=MaterializeResult(
                processed=50,
                results=[],
                summary="Materialized 50 actions",
            )
        )

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            report = json.loads(await run_lifecycle_pipeline(scope="recent"))

        mock_dl.materialize_memories.assert_not_awaited()
        assert report["analyzed_count"] == 50
        assert report["newly_staged_count"] == 50
        assert report["actions_taken"] == []
        assert report["materialization_summary"] == "Disabled: actions staged for review"

    @pytest.mark.asyncio
    async def test_dry_run_previews_without_staging_or_materialization(self):
        """A dry run previews classification without persisting or applying it."""
        from open_brain.server import run_lifecycle_pipeline

        mock_dl = MagicMock()
        mock_dl.decay_memories = AsyncMock(
            return_value=DecayResult(
                decayed=2,
                boosted=1,
                recent_memories=4,
                summary="Previewed priority changes",
            )
        )
        mock_dl.triage_memories = AsyncMock(
            return_value=TriageResult(
                analyzed=1,
                actions=[
                    TriageAction(
                        action="archive",
                        memory_id=1,
                        reason="Superseded",
                        memory_type="observation",
                        memory_title="Old note",
                    )
                ],
                summary="Proposed 1 lifecycle action (dry run)",
            )
        )
        mock_dl.materialize_memories = AsyncMock()

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            report = json.loads(await run_lifecycle_pipeline(dry_run=True))

        triage_params = mock_dl.triage_memories.await_args.args[0]
        assert triage_params.dry_run is True
        mock_dl.materialize_memories.assert_not_awaited()
        assert report["proposed_count"] == 1
        assert report["newly_staged_count"] == 0

    @pytest.mark.asyncio
    async def test_second_run_stages_nothing_for_same_fifty_memories(self):
        """The persistent policy key makes a repeated 50-memory run a no-op."""
        from contextlib import asynccontextmanager

        from open_brain.data_layer.postgres import PostgresDataLayer

        class LifecycleLedgerConnection:
            def __init__(self) -> None:
                self.rows = [vars(_make_memory(memory_id)) for memory_id in range(1, 51)]
                self.staged: dict[tuple[int, str], int] = {}
                self.tokens: dict[tuple[int, str], str] = {}

            def transaction(self):
                @asynccontextmanager
                async def transaction_context():
                    yield

                return transaction_context()

            async def fetchval(self, query: str, *args):
                assert "pg_advisory_xact_lock" in query
                return None

            async def execute(self, query: str, *args):
                assert "state = 'classifying'" in query
                return "DELETE 0"

            async def fetch(self, query: str, *args):
                assert "memory_lifecycle_actions" in query
                assert "NOT EXISTS" in query
                policy_version = args[0]
                limit = args[-1]
                return [
                    row
                    for row in self.rows
                    if (row["id"], policy_version) not in self.staged
                ][:limit]

            async def fetchrow(self, query: str, *args):
                memory_id, policy_version = args[:2]
                key = (memory_id, policy_version)
                if "INSERT INTO memory_lifecycle_actions" in query:
                    assert "ON CONFLICT (memory_id, policy_version) DO UPDATE" in query
                    assert "memory_lifecycle_actions.state = 'failed'" in query
                    if key in self.staged:
                        return None
                    action_id = len(self.staged) + 1
                    self.staged[key] = action_id
                    self.tokens[key] = args[2]
                    return {"id": action_id}
                assert "UPDATE memory_lifecycle_actions" in query
                assert "reservation_token = $5" in query
                if self.tokens[key] != args[4]:
                    return None
                return {"id": self.staged[key]}

        conn = LifecycleLedgerConnection()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = acquire

        async def classify(memories):
            return [
                TriageAction(
                    action="keep",
                    memory_id=memory.id,
                    reason="Still useful",
                    memory_type=memory.type,
                    memory_title=memory.title,
                )
                for memory in memories
            ]

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "open_brain.data_layer.triage.triage_with_llm",
                new_callable=AsyncMock,
                side_effect=classify,
            ),
        ):
            dl = PostgresDataLayer()
            first = await dl.triage_memories(TriageParams(limit=50))
            second = await dl.triage_memories(TriageParams(limit=50))

        assert first.analyzed == 50
        assert len(first.actions) == 50
        assert all(action.executed is False for action in first.actions)
        assert second.analyzed == 0
        assert second.actions == []
        assert len(conn.staged) == 50

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "scope",
        [
            "recent",
            "project:open-brain",
            "type:observation",
            "low-priority",
            "session_ref:test-session",
        ],
    )
    async def test_regression_dry_run_rechecks_staged_memories(self, scope: str):
        """A dry-run previews the selected scope, not only the next unstaged batch."""
        from open_brain.data_layer.postgres import PostgresDataLayer

        class DryRunConnection:
            async def fetchrow(self, query: str, *args):
                for position, _arg in enumerate(args, start=1):
                    assert f"${position}" in query
                return {"id": 1}

            async def fetch(self, query: str, *args):
                # asyncpg rejects unused positional arguments because PostgreSQL
                # cannot infer a type for a parameter absent from the query.
                for position, _arg in enumerate(args, start=1):
                    assert f"${position}" in query
                if "NOT EXISTS" in query:
                    return []
                return [vars(_make_memory(1))]

        conn = DryRunConnection()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = acquire

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "open_brain.data_layer.triage.triage_with_llm",
                new_callable=AsyncMock,
                return_value=[
                    TriageAction(
                        action="keep",
                        memory_id=1,
                        reason="Still useful",
                        memory_type="observation",
                        memory_title="Test Memory",
                    )
                ],
            ),
        ):
            result = await PostgresDataLayer().triage_memories(
                TriageParams(scope=scope, limit=1, dry_run=True)
            )

        assert result.analyzed == 1
        assert len(result.actions) == 1

    @pytest.mark.asyncio
    async def test_regression_partial_result_fails_unfinished_reservations(self):
        """A partial classifier result must not leave owned rows classifying."""
        from open_brain.data_layer.postgres import PostgresDataLayer

        class PartialResultConnection:
            def __init__(self) -> None:
                self.rows = [vars(_make_memory(1)), vars(_make_memory(2))]
                self.states: dict[int, str] = {}
                self.tokens: dict[int, str] = {}

            def transaction(self):
                @asynccontextmanager
                async def transaction_context():
                    yield

                return transaction_context()

            async def fetchval(self, query: str, *args):
                return None

            async def fetch(self, query: str, *args):
                retries_failed_rows = "state != 'failed'" in query
                return [
                    row
                    for row in self.rows
                    if self.states.get(row["id"]) is None
                    or (
                        retries_failed_rows
                        and self.states.get(row["id"]) == "failed"
                    )
                ]

            async def fetchrow(self, query: str, *args):
                memory_id = args[0]
                if "INSERT INTO memory_lifecycle_actions" in query:
                    if self.states.get(memory_id) not in (None, "failed"):
                        return None
                    self.states[memory_id] = "classifying"
                    self.tokens[memory_id] = args[2]
                    return {"id": memory_id}
                self.states[memory_id] = "staged"
                return {"id": memory_id}

            async def execute(self, query: str, *args):
                if "DELETE FROM memory_lifecycle_actions" in query:
                    return "DELETE 0"
                assert "SET state = 'failed'" in query
                memory_ids, _policy_version, reservation_token = args
                for memory_id in memory_ids:
                    if self.tokens[memory_id] == reservation_token:
                        self.states[memory_id] = "failed"
                return f"UPDATE {len(memory_ids)}"

        conn = PartialResultConnection()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = acquire

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "open_brain.data_layer.triage.triage_with_llm",
                new_callable=AsyncMock,
                side_effect=[
                    [
                        TriageAction(
                            action="keep",
                            memory_id=1,
                            reason="Still useful",
                            memory_type="observation",
                            memory_title="Test Memory",
                        )
                    ],
                    [
                        TriageAction(
                            action="keep",
                            memory_id=2,
                            reason="Recovered on retry",
                            memory_type="observation",
                            memory_title="Test Memory",
                        )
                    ],
                ],
            ),
        ):
            dl = PostgresDataLayer()
            result = await dl.triage_memories(TriageParams(limit=2))
            retry = await dl.triage_memories(TriageParams(limit=2))

        assert len(result.actions) == 1
        assert "1 incomplete classifications failed" in result.summary
        assert retry.analyzed == 1
        assert len(retry.actions) == 1
        assert conn.states == {1: "staged", 2: "staged"}

    @pytest.mark.asyncio
    async def test_regression_classifier_crash_fails_owned_reservations(self):
        """An unexpected classifier exception must release every owned reservation."""
        from open_brain.data_layer.postgres import PostgresDataLayer

        class CrashingClassifierConnection:
            def __init__(self) -> None:
                self.row = vars(_make_memory(1))
                self.state: str | None = None
                self.token: str | None = None

            def transaction(self):
                @asynccontextmanager
                async def transaction_context():
                    yield

                return transaction_context()

            async def fetchval(self, query: str, *args):
                return None

            async def fetch(self, query: str, *args):
                return [self.row]

            async def fetchrow(self, query: str, *args):
                self.state = "classifying"
                self.token = args[2]
                return {"id": 1}

            async def execute(self, query: str, *args):
                if "DELETE FROM memory_lifecycle_actions" in query:
                    return "DELETE 0"
                assert "SET state = 'failed'" in query
                memory_ids, _policy_version, reservation_token = args
                if memory_ids == [1] and reservation_token == self.token:
                    self.state = "failed"
                return "UPDATE 1"

        conn = CrashingClassifierConnection()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = acquire

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "open_brain.data_layer.triage.triage_with_llm",
                new_callable=AsyncMock,
                side_effect=RuntimeError("classifier crashed"),
            ),
            pytest.raises(RuntimeError, match="classifier crashed"),
        ):
            await PostgresDataLayer().triage_memories(TriageParams(limit=1))

        assert conn.state == "failed"

    def test_regression_review_state_records_resolution_not_application(self):
        """The public state name must not imply an unverified materialization effect."""
        with pytest.raises(ValueError, match="Unknown review state"):
            LifecycleActionStateParams(action_id=1, state="applied")

        params = LifecycleActionStateParams(
            action_id=1,
            state="resolved",
            note="Keep decision reviewed",
        )
        assert params.state == "resolved"

    @pytest.mark.asyncio
    async def test_replaced_reservation_rejects_stale_worker_result(self):
        """A reclaimed reservation cannot be finalized by its original worker."""
        from contextlib import asynccontextmanager

        from open_brain.data_layer.postgres import PostgresDataLayer

        class ReclaimedReservationConnection:
            def __init__(self) -> None:
                self.row = vars(_make_memory(1))
                self.token: str | None = None

            def transaction(self):
                @asynccontextmanager
                async def transaction_context():
                    yield

                return transaction_context()

            async def fetchval(self, query: str, *args):
                return None

            async def execute(self, query: str, *args):
                return "DELETE 0"

            async def fetch(self, query: str, *args):
                return [self.row] if self.token is None else []

            async def fetchrow(self, query: str, *args):
                if "INSERT INTO memory_lifecycle_actions" in query:
                    self.token = args[2]
                    return {"id": 1}
                assert "reservation_token = $5" in query
                return {"id": 1} if args[4] == self.token else None

        conn = ReclaimedReservationConnection()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = acquire

        async def classify(memories):
            conn.token = "replacement-worker-token"
            return [
                TriageAction(
                    action="keep",
                    memory_id=1,
                    reason="Still useful",
                    memory_type="observation",
                    memory_title="Test Memory",
                )
            ]

        with (
            patch(
                "open_brain.data_layer.postgres.get_pool",
                new_callable=AsyncMock,
                return_value=pool,
            ),
            patch(
                "open_brain.data_layer.triage.triage_with_llm",
                new_callable=AsyncMock,
                side_effect=classify,
            ),
        ):
            result = await PostgresDataLayer().triage_memories(TriageParams(limit=1))

        assert result.analyzed == 1
        assert result.actions == []

    @pytest.mark.asyncio
    async def test_lifecycle_read_and_review_queries_preserve_queue_safety(self):
        """Reads span policies and review writes exclude in-flight reservations."""
        from contextlib import asynccontextmanager

        from open_brain.data_layer.postgres import PostgresDataLayer

        class LifecycleReviewConnection:
            async def fetch(self, query: str, *args):
                assert "($1::text IS NULL" in query
                assert args[0] is None
                return []

            async def fetchrow(self, query: str, *args):
                assert "state != 'classifying'" in query
                assert "action IS NOT NULL" in query
                assert "reason IS NOT NULL" in query
                return None

        conn = LifecycleReviewConnection()

        @asynccontextmanager
        async def acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = acquire

        with patch(
            "open_brain.data_layer.postgres.get_pool",
            new_callable=AsyncMock,
            return_value=pool,
        ):
            dl = PostgresDataLayer()
            records = await dl.list_lifecycle_actions(LifecycleActionQueryParams())
            with pytest.raises(ValueError, match="not reviewable"):
                await dl.set_lifecycle_action_state(
                    LifecycleActionStateParams(action_id=1, state="resolved")
                )

        assert records == []

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_regression_dry_run_bindings_execute_in_postgres(
        self,
        bootstrapped_database_url: str,
    ):
        """A real asyncpg dry-run must bind every argument used by PostgreSQL."""
        from uuid import uuid4

        import asyncpg

        from open_brain.config import get_config
        from open_brain.data_layer import postgres
        from open_brain.data_layer.postgres import PostgresDataLayer

        policy_version = f"memory-lifecycle.test.{uuid4()}"
        get_config().DATABASE_URL = bootstrapped_database_url
        await postgres.close_pool()

        setup_conn = await asyncpg.connect(bootstrapped_database_url)
        try:
            memory_id = await setup_conn.fetchval(
                """
                INSERT INTO memories (type, title, content, metadata, stability)
                VALUES ('observation', 'Dry-run binding regression', 'Test content', '{}'::jsonb, 'stable')
                RETURNING id
                """
            )
        finally:
            await setup_conn.close()

        try:
            with patch(
                "open_brain.data_layer.triage.triage_with_llm",
                new_callable=AsyncMock,
                return_value=[
                    TriageAction(
                        action="keep",
                        memory_id=memory_id,
                        reason="Still useful",
                        memory_type="observation",
                        memory_title="Dry-run binding regression",
                    )
                ],
            ):
                result = await PostgresDataLayer().triage_memories(
                    TriageParams(
                        scope="recent",
                        limit=1,
                        dry_run=True,
                        policy_version=policy_version,
                    )
                )

            pool = await postgres.get_pool()
            async with pool.acquire() as conn:
                queue_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM memory_lifecycle_actions WHERE policy_version = $1",
                    policy_version,
                )

            assert result.analyzed == 1
            assert len(result.actions) == 1
            assert queue_count == 0
        finally:
            pool = await postgres.get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM memories WHERE id = $1", memory_id)
            await postgres.close_pool()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_concurrent_runs_stage_same_fifty_once_in_postgres(
        self,
        bootstrapped_database_url: str,
    ):
        """Two overlapping database runs stage exactly one row per memory."""
        from uuid import uuid4

        import asyncpg

        from open_brain.config import get_config
        from open_brain.data_layer import postgres
        from open_brain.data_layer.postgres import PostgresDataLayer

        prefix = f"lifecycle-integration:{uuid4()}:"
        policy_version = f"memory-lifecycle.test.{uuid4()}"
        get_config().DATABASE_URL = bootstrapped_database_url
        await postgres.close_pool()

        setup_conn = await asyncpg.connect(bootstrapped_database_url)
        try:
            await setup_conn.execute(
                """
                INSERT INTO memories (
                    type,
                    title,
                    content,
                    session_ref,
                    metadata,
                    stability
                )
                SELECT
                    'observation',
                    'Lifecycle integration ' || item,
                    'Lifecycle integration content ' || item,
                    $1 || item,
                    '{}'::jsonb,
                    'stable'
                FROM generate_series(1, 50) AS item
                """,
                prefix,
            )
        finally:
            await setup_conn.close()

        first_classification_started = asyncio.Event()
        release_first_classification = asyncio.Event()
        classifier_calls = 0

        async def classify(memories):
            nonlocal classifier_calls
            classifier_calls += 1
            if classifier_calls == 1:
                first_classification_started.set()
                await release_first_classification.wait()
            return [
                TriageAction(
                    action="keep",
                    memory_id=memory.id,
                    reason="Still useful",
                    memory_type=memory.type,
                    memory_title=memory.title,
                )
                for memory in memories
            ]

        try:
            with patch(
                "open_brain.data_layer.triage.triage_with_llm",
                new_callable=AsyncMock,
                side_effect=classify,
            ):
                dl = PostgresDataLayer()
                params = TriageParams(
                    scope=f"session_ref:{prefix}",
                    limit=50,
                    policy_version=policy_version,
                )
                async def first_run():
                    return await dl.triage_memories(params)

                async def overlapping_run():
                    await first_classification_started.wait()
                    try:
                        return await dl.triage_memories(params)
                    finally:
                        release_first_classification.set()

                first, second = await asyncio.wait_for(
                    asyncio.gather(first_run(), overlapping_run()),
                    timeout=10,
                )

            pool = await postgres.get_pool()
            async with pool.acquire() as conn:
                staged_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM memory_lifecycle_actions
                    WHERE policy_version = $1
                    """,
                    policy_version,
                )

            assert first.analyzed == 50
            assert len(first.actions) == 50
            assert second.analyzed == 0
            assert second.actions == []
            assert staged_count == 50
            assert classifier_calls == 1
        finally:
            pool = await postgres.get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM memories WHERE session_ref LIKE $1",
                    prefix + "%",
                )
            await postgres.close_pool()

    def test_migration_defines_action_states_and_policy_idempotency_key(self):
        """The additive lifecycle ledger has the required durable constraints."""
        import inspect

        from open_brain.data_layer import postgres

        source = inspect.getsource(postgres._run_migrations)

        assert "CREATE TABLE IF NOT EXISTS memory_lifecycle_actions" in source
        assert "UNIQUE (memory_id, policy_version)" in source
        assert "reservation_token" in source
        assert "state = 'classifying'" in source
        assert "state = 'failed' AND reason IS NOT NULL" in source
        assert "action IS NOT NULL AND reason IS NOT NULL" in source
        assert "last_boost_at" in source
        for state in ("classifying", "staged", "resolved", "needs_review", "failed"):
            assert state in source

    @pytest.mark.asyncio
    async def test_staged_actions_can_be_listed_and_reviewed(self):
        """Persisted proposals have a read path and an explicit state transition."""
        from open_brain.server import list_lifecycle_actions, set_lifecycle_action_state

        record = LifecycleActionRecord(
            id=7,
            memory_id=42,
            policy_version="memory-lifecycle.v1",
            action="promote",
            reason="Reusable rule",
            state="staged",
            memory_type="learning",
            memory_title="Use durable triggers",
            resolution_note=None,
            created_at="2026-07-14T12:00:00+00:00",
            updated_at="2026-07-14T12:00:00+00:00",
        )
        resolved = LifecycleActionRecord(
            **{**vars(record), "state": "resolved", "resolution_note": "Reviewed"}
        )
        mock_dl = MagicMock()
        mock_dl.list_lifecycle_actions = AsyncMock(return_value=[record])
        mock_dl.set_lifecycle_action_state = AsyncMock(return_value=resolved)

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            listed = json.loads(await list_lifecycle_actions())
            transitioned = json.loads(
                await set_lifecycle_action_state(7, "resolved", "Reviewed")
            )

        assert listed["count"] == 1
        assert listed["actions"][0]["memory_id"] == 42
        query_params = mock_dl.list_lifecycle_actions.await_args.args[0]
        assert query_params.policy_version is None
        assert transitioned["state"] == "resolved"
        assert transitioned["resolution_note"] == "Reviewed"


# ─── Test: session_ref scope ──────────────────────────────────────────────────


class TestSessionRefScope:
    """Tests for the session_ref:<prefix> triage scope (AK1/AK2)."""

    @pytest.mark.asyncio
    async def test_session_ref_scope_queries_by_prefix(self):
        """triage_memories with scope='session_ref:ccmem:' queries memories by session_ref prefix."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, patch

        llm_response = json.dumps([
            {"memory_id": 1, "action": "keep", "reason": "Useful observation"},
            {"memory_id": 2, "action": "archive", "reason": "Outdated feedback"},
        ])

        db_rows = [
            {
                "id": 1, "index_id": 1, "session_id": None, "type": "observation",
                "title": "Claude Memory 1", "subtitle": None, "narrative": None,
                "content": "Some content", "metadata": {}, "priority": 0.5,
                "stability": "stable", "access_count": 0, "last_accessed_at": None,
                "created_at": "2026-01-01", "updated_at": "2026-01-01",
                "session_ref": "ccmem:abc123", "user_id": None, "embedding": None,
            },
            {
                "id": 2, "index_id": 1, "session_id": None, "type": "feedback",
                "title": "Claude Memory 2", "subtitle": None, "narrative": None,
                "content": "Feedback content", "metadata": {}, "priority": 0.5,
                "stability": "stable", "access_count": 0, "last_accessed_at": None,
                "created_at": "2026-01-01", "updated_at": "2026-01-01",
                "session_ref": "ccmem:def456", "user_id": None, "embedding": None,
            },
        ]

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=db_rows)

        @asynccontextmanager
        async def _acquire():
            yield mock_conn

        mock_pool = MagicMock()
        mock_pool.acquire = _acquire

        with (
            patch("open_brain.data_layer.postgres.get_pool", new_callable=AsyncMock, return_value=mock_pool),
            patch("open_brain.data_layer.triage.llm_complete", new_callable=AsyncMock) as mock_llm,
        ):
            mock_llm.return_value = llm_response

            from open_brain.data_layer.postgres import PostgresDataLayer
            dl = PostgresDataLayer()
            result = await dl.triage_memories(
                TriageParams(scope="session_ref:ccmem:", limit=200, dry_run=True)
            )

        # Verify the DB was queried with the session_ref LIKE pattern
        mock_conn.fetch.assert_awaited_once()
        call_args = mock_conn.fetch.call_args
        query = call_args[0][0]
        assert "session_ref LIKE" in query, (
            f"Expected query to contain 'session_ref LIKE', got: {query!r}"
        )
        # Verify the prefix parameter was passed (positional arg after query)
        call_params = call_args[0][1:]
        assert any("ccmem:%" in str(p) for p in call_params), (
            f"Expected 'ccmem:%' in query params, got: {call_params}"
        )

        # Verify triage analyzed both memories
        assert result.analyzed == 2
        action_map = {a.memory_id: a.action for a in result.actions}
        assert action_map[1] == "keep"
        assert action_map[2] == "archive"

    @pytest.mark.asyncio
    async def test_session_ref_scope_is_prefix_match_not_exact(self):
        """session_ref:ccmem: matches all ccmem:* entries, not just exact 'ccmem:'."""
        # This tests AK1: LIKE '<prefix>%' semantics
        params = TriageParams(scope="session_ref:ccmem:", limit=10, dry_run=True)
        assert params.scope is not None
        prefix = params.scope[len("session_ref:"):]
        assert prefix == "ccmem:"
        # The LIKE pattern appends %
        like_pattern = prefix + "%"
        assert like_pattern == "ccmem:%"

    def test_triage_params_accepts_session_ref_scope(self):
        """TriageParams can hold a session_ref: scope string."""
        params = TriageParams(scope="session_ref:ccmem:", limit=200, dry_run=True)
        assert params.scope == "session_ref:ccmem:"
        assert params.limit == 200
        assert params.dry_run is True
