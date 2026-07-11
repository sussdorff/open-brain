"""CLI and MCP tests for capture inbox operations."""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.cli.main import _build_parser
from open_brain.data_layer.interface import SaveMemoryParams, SaveMemoryResult, SearchResult


def parse(args: list[str]) -> Any:
    """Parse CLI args using the ob parser."""
    return _build_parser().parse_args(args)


class TestCaptureInboxCli:
    def test_inbox_parser_defaults(self) -> None:
        args = parse(["inbox"])

        assert args.command == "inbox"
        assert args.limit is None
        assert args.project is None

    @pytest.mark.asyncio
    async def test_inbox_calls_search_with_capture_status(self) -> None:
        from open_brain.cli.main import _cmd_inbox

        args = parse(["inbox", "--limit", "5", "--project", "proj"])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"total": 0, "results": []}
            await _cmd_inbox(args)

        mock_call.assert_called_once_with(
            "search",
            {"capture_status": "inbox", "limit": 5, "project": "proj"},
        )

    def test_capture_set_status_parser(self) -> None:
        args = parse(["capture", "set-status", "42", "processed"])

        assert args.command == "capture"
        assert args.capture_command == "set-status"
        assert args.memory_id == 42
        assert args.capture_status == "processed"
        assert args.lifecycle_status is None

    @pytest.mark.asyncio
    async def test_capture_set_status_calls_transition_tool(self) -> None:
        from open_brain.cli.main import _cmd_capture

        args = parse([
            "capture",
            "set-status",
            "42",
            "dismissed",
            "--lifecycle-status",
            "discarded",
        ])
        with patch("open_brain.cli.main.call_tool", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = {"id": 42, "message": "Capture status updated"}
            await _cmd_capture(args)

        mock_call.assert_called_once_with(
            "set_capture_status",
            {
                "memory_id": 42,
                "capture_status": "dismissed",
                "lifecycle_status": "discarded",
            },
        )


class TestCaptureInboxMcp:
    @pytest.mark.asyncio
    async def test_search_tool_forwards_capture_status(self) -> None:
        mock_dl = AsyncMock()
        mock_dl.search.return_value = SearchResult(results=[], total=0)

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import search

            result = await search(capture_status="inbox")

        data = json.loads(result)
        assert data == {"total": 0, "results": []}
        params = mock_dl.search.call_args[0][0]
        assert params.capture_status == "inbox"

    @pytest.mark.asyncio
    async def test_set_capture_status_tool_forwards_transition(self) -> None:
        mock_dl = AsyncMock()
        mock_dl.set_capture_status.return_value = SaveMemoryResult(
            id=42,
            message="Capture status updated",
        )

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            from open_brain.server import set_capture_status

            result = await set_capture_status(
                memory_id=42,
                capture_status="processed",
            )

        data = json.loads(result)
        assert data == {"id": 42, "message": "Capture status updated"}
        params = mock_dl.set_capture_status.call_args[0][0]
        assert params.memory_id == 42
        assert params.capture_status == "processed"
        assert params.lifecycle_status is None


@pytest.mark.integration
class TestCaptureInboxMcpRoundTrip:
    @pytest.mark.asyncio
    async def test_capture_status_round_trip_uses_real_data_layer(self) -> None:
        import os

        from open_brain.data_layer.postgres import PostgresDataLayer
        from open_brain.ingest.runs import ingest_run
        from open_brain.server import search, set_capture_status

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url or database_url.startswith("postgresql://test:test@"):
            pytest.skip("Requires real DATABASE_URL (not the CI test database)")

        dl = PostgresDataLayer()
        run_id: str | None = None
        try:
            with ingest_run() as current_run_id:
                run_id = current_run_id
                saved = await dl.save_memory(
                    SaveMemoryParams(
                        text=f"capture mcp round trip {run_id}",
                        capture_status="inbox",
                    )
                )

            inbox_payload = json.loads(
                await search(capture_status="inbox", metadata_filter={"run_id": run_id})
            )
            inbox_ids = {row["id"] for row in inbox_payload["results"]}
            assert saved.id in inbox_ids

            transition_payload = json.loads(
                await set_capture_status(
                    memory_id=saved.id,
                    capture_status="processed",
                )
            )
            assert transition_payload == {
                "id": saved.id,
                "message": "Capture status updated",
            }

            processed_payload = json.loads(
                await search(capture_status="inbox", metadata_filter={"run_id": run_id})
            )
            processed_ids = {row["id"] for row in processed_payload["results"]}
            assert saved.id not in processed_ids
        finally:
            if run_id is not None:
                await dl.delete_by_run_id(run_id)
