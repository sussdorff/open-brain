"""Tests for entity extraction on save_memory (open-brain-90p)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from open_brain.data_layer.interface import SaveMemoryResult
from open_brain.server import save_memory


# ─── AK1: People and orgs extraction ─────────────────────────────────────────

class TestPeopleAndOrgsExtraction:
    @pytest.mark.asyncio
    async def test_sarah_from_acme_corp_extracts_people_and_orgs(self):
        """AK1: Text mentioning 'Sarah from Acme Corp' extracts people=[Sarah], orgs=[Acme Corp]."""
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=1, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=1, message="updated")

        llm_response = json.dumps({
            "people": ["Sarah"],
            "orgs": ["Acme Corp"],
            "tech": [],
            "locations": [],
            "dates": [],
        })

        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete", return_value=llm_response):
            await save_memory(text="Sarah from Acme Corp visited us today.")

        update_call = mock_dl.update_memory.call_args
        assert update_call is not None, "update_memory should have been called"
        params = update_call[0][0]
        entities = params.metadata.get("entities", {})
        assert "Sarah" in entities.get("people", [])
        assert "Acme Corp" in entities.get("orgs", [])


# ─── AK2: Tech extraction ─────────────────────────────────────────────────────

class TestTechExtraction:
    @pytest.mark.asyncio
    async def test_python_docker_kubernetes_extracts_tech(self):
        """AK2: Text mentioning 'Python, Docker, Kubernetes' extracts tech=[Python, Docker, Kubernetes]."""
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=2, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=2, message="updated")

        llm_response = json.dumps({
            "people": [],
            "orgs": [],
            "tech": ["Python", "Docker", "Kubernetes"],
            "locations": [],
            "dates": [],
        })

        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete", return_value=llm_response):
            await save_memory(text="We deployed our Python app using Docker and Kubernetes.")

        update_call = mock_dl.update_memory.call_args
        assert update_call is not None, "update_memory should have been called"
        params = update_call[0][0]
        entities = params.metadata.get("entities", {})
        assert "Python" in entities.get("tech", [])
        assert "Docker" in entities.get("tech", [])
        assert "Kubernetes" in entities.get("tech", [])


# ─── AK3: Location extraction ─────────────────────────────────────────────────

class TestLocationExtraction:
    @pytest.mark.asyncio
    async def test_berlin_office_extracts_location(self):
        """AK3: Text mentioning 'Berlin office' extracts locations=[Berlin]."""
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=3, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=3, message="updated")

        llm_response = json.dumps({
            "people": [],
            "orgs": [],
            "tech": [],
            "locations": ["Berlin"],
            "dates": [],
        })

        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete", return_value=llm_response):
            await save_memory(text="The meeting is at the Berlin office.")

        update_call = mock_dl.update_memory.call_args
        assert update_call is not None, "update_memory should have been called"
        params = update_call[0][0]
        entities = params.metadata.get("entities", {})
        assert "Berlin" in entities.get("locations", [])


# ─── AK4: Pre-provided entities not overwritten ───────────────────────────────

class TestPreProvidedEntitiesNotOverwritten:
    @pytest.mark.asyncio
    async def test_existing_entities_in_metadata_skips_extraction(self):
        """AK4: If metadata.entities already set, skip extraction (llm_complete not called)."""
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=4, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=4, message="updated")

        existing_entities = {"people": ["Alice"], "orgs": ["Wonderland Inc"], "tech": [], "locations": [], "dates": []}
        pre_set_metadata = {"entities": existing_entities}

        # classify_and_extract returns existing_metadata unchanged when capture_template is set
        # or when called with pre-structured metadata — here it should also not update
        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete") as mock_llm, \
             patch("open_brain.server.classify_and_extract", new=AsyncMock(return_value=pre_set_metadata)):
            await save_memory(
                text="Some text with Sarah from Acme Corp.",
                metadata=pre_set_metadata,
            )

        # LLM should NOT have been called since entities were pre-provided
        mock_llm.assert_not_called()
        # update_memory should NOT have been called for entity enrichment
        # (entities were already there, no need to update)
        mock_dl.update_memory.assert_not_called()


# ─── AK5: Empty text produces empty dict ─────────────────────────────────────

class TestEmptyTextProducesEmptyDict:
    @pytest.mark.asyncio
    async def test_empty_text_returns_empty_entities_no_error(self):
        """AK5: Empty/no entities produces empty dict (not error)."""
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=5, message="saved")
        mock_dl.update_memory.return_value = SaveMemoryResult(id=5, message="updated")

        llm_response = json.dumps({
            "people": [],
            "orgs": [],
            "tech": [],
            "locations": [],
            "dates": [],
        })

        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete", return_value=llm_response), \
             patch("open_brain.server.classify_and_extract", return_value={}):
            # Should not raise any error
            result = await save_memory(text="")

        # Should still return a valid response
        data = json.loads(result)
        assert "id" in data
        # update_memory should NOT be called since entities dict is empty
        mock_dl.update_memory.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_entities_found_does_not_call_update_memory(self):
        """AK5: When LLM returns all empty arrays, update_memory is not called."""
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=6, message="saved")

        llm_response = json.dumps({
            "people": [],
            "orgs": [],
            "tech": [],
            "locations": [],
            "dates": [],
        })

        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete", return_value=llm_response), \
             patch("open_brain.server.classify_and_extract", return_value={}):
            await save_memory(text="Nothing specific here.")

        mock_dl.update_memory.assert_not_called()


# ─── LLM failure graceful degradation (extra) ────────────────────────────────

class TestLlmFailureGracefulDegradation:
    @pytest.mark.asyncio
    async def test_llm_failure_still_saves_memory(self):
        """Extra: If llm_complete raises, entities={} and save_memory still succeeds."""
        mock_dl = AsyncMock()
        mock_dl.save_memory.return_value = SaveMemoryResult(id=8, message="saved")

        async def failing_llm(*args, **kwargs):
            raise RuntimeError("LLM API error")

        with patch("open_brain.server.get_dl", return_value=mock_dl), \
             patch("open_brain.server.llm_complete", side_effect=failing_llm), \
             patch("open_brain.server.classify_and_extract", return_value={}):
            result = await save_memory(text="Sarah from Acme Corp visited Berlin.")

        # Memory should still be saved despite LLM failure
        data = json.loads(result)
        assert data["id"] == 8
        # update_memory should NOT be called since entities extraction failed
        mock_dl.update_memory.assert_not_called()
