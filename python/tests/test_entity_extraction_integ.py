"""Integration tests for entity extraction — parallel execution (open-brain-90p, AK6)."""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryResult


# ─── AK6: Parallel execution ─────────────────────────────────────────────────

@pytest.mark.integration
class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_extraction_runs_parallel_with_save(self):
        """AK6: Entity extraction runs parallel with dl.save_memory (asyncio.gather)."""
        delay = 0.1  # 100ms each

        async def slow_save(params):
            await asyncio.sleep(delay)
            return SaveMemoryResult(id=7, message="saved")

        async def slow_llm(*args, **kwargs):
            await asyncio.sleep(delay)
            return json.dumps({"people": ["Alice"], "orgs": [], "tech": [], "locations": [], "dates": []})

        mock_dl = AsyncMock()
        mock_dl.save_memory.side_effect = slow_save
        mock_dl.update_memory.return_value = SaveMemoryResult(id=7, message="updated")

        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete", side_effect=slow_llm):
            from open_brain.server import save_memory
            start = time.monotonic()
            await save_memory(text="Alice worked on a project.")
            elapsed = time.monotonic() - start

        # If parallel: ~delay (max of the two). If sequential: ~2*delay.
        # Allow generous margin for CI but reject clearly sequential execution.
        assert elapsed < delay * 1.8, (
            f"Expected parallel execution (~{delay}s), but took {elapsed:.3f}s — likely sequential"
        )
