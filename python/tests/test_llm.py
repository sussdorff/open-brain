"""Unit tests for LLM provider abstraction (Anthropic + OpenRouter)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from open_brain.data_layer.llm import LlmMessage, llm_complete, _call_anthropic, _call_openrouter


def _make_anthropic_response(text: str) -> httpx.Response:
    """Build a mock Anthropic API response."""
    body = json.dumps({"content": [{"type": "text", "text": text}]})
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


def _make_openrouter_response(text: str) -> httpx.Response:
    """Build a mock OpenRouter API response."""
    body = json.dumps({"choices": [{"message": {"content": text}}]})
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


def _patch_client(response: httpx.Response):
    """Context manager that patches httpx.AsyncClient to return a given response."""
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)

    class FakeClient:
        async def __aenter__(self):
            return mock_client
        async def __aexit__(self, *args):
            pass

    return patch("httpx.AsyncClient", return_value=FakeClient())


class TestCallAnthropic:
    @pytest.mark.asyncio
    async def test_returns_text_response(self):
        response = _make_anthropic_response("Hello from Claude!")
        with _patch_client(response):
            result = await _call_anthropic(
                [LlmMessage(role="user", content="Hi")],
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
            )
        assert result == "Hello from Claude!"

    @pytest.mark.asyncio
    async def test_raises_without_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        # Reset singleton so config is reloaded
        import open_brain.config as config_module
        config_module._config = None
        # Recreate config with no anthropic key
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            import open_brain.config as cfg_mod
            cfg_mod._config = None
            with pytest.raises((ValueError, Exception)):
                await _call_anthropic(
                    [LlmMessage(role="user", content="test")],
                    "model",
                    100,
                )

    @pytest.mark.asyncio
    async def test_raises_on_api_error(self):
        error_response = httpx.Response(500, content=b"Internal Server Error")
        with _patch_client(error_response):
            with pytest.raises(RuntimeError, match="Anthropic API error 500"):
                await _call_anthropic(
                    [LlmMessage(role="user", content="test")],
                    "model",
                    100,
                )

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_string(self):
        body = json.dumps({"content": []})
        response = httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})
        with _patch_client(response):
            result = await _call_anthropic(
                [LlmMessage(role="user", content="test")],
                "model",
                100,
            )
        assert result == ""


class TestCallOpenRouter:
    @pytest.mark.asyncio
    async def test_returns_text_response(self):
        response = _make_openrouter_response("OpenRouter says hi")
        with (
            _patch_client(response),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-openrouter-key"}),
        ):
            import open_brain.config as cfg_mod
            cfg_mod._config = None
            result = await _call_openrouter(
                [LlmMessage(role="user", content="Hi")],
                model="some-model",
                max_tokens=100,
            )
        assert result == "OpenRouter says hi"

    @pytest.mark.asyncio
    async def test_raises_on_api_error(self):
        error_response = httpx.Response(401, content=b"Unauthorized")
        with (
            _patch_client(error_response),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
        ):
            import open_brain.config as cfg_mod
            cfg_mod._config = None
            with pytest.raises(RuntimeError, match="OpenRouter API error 401"):
                await _call_openrouter(
                    [LlmMessage(role="user", content="test")],
                    "model",
                    100,
                )

    @pytest.mark.asyncio
    async def test_empty_choices_returns_empty_string(self):
        body = json.dumps({"choices": []})
        response = httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})
        with (
            _patch_client(response),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
        ):
            import open_brain.config as cfg_mod
            cfg_mod._config = None
            result = await _call_openrouter(
                [LlmMessage(role="user", content="test")],
                "model",
                100,
            )
        assert result == ""


class TestLlmComplete:
    @pytest.mark.asyncio
    async def test_uses_anthropic_by_default(self):
        response = _make_anthropic_response("Anthropic response")
        with (
            _patch_client(response),
            patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}),
        ):
            import open_brain.config as cfg_mod
            cfg_mod._config = None
            result = await llm_complete([LlmMessage(role="user", content="test")])
            assert result == "Anthropic response"

    @pytest.mark.asyncio
    async def test_routes_to_openrouter(self):
        response = _make_openrouter_response("OpenRouter response")
        with (
            _patch_client(response),
            patch.dict("os.environ", {
                "LLM_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "test-key",
            }),
        ):
            import open_brain.config as cfg_mod
            cfg_mod._config = None
            result = await llm_complete([LlmMessage(role="user", content="test")])
            assert result == "OpenRouter response"

    @pytest.mark.asyncio
    async def test_uses_custom_model(self):
        """llm_complete respects model override."""
        captured = {}
        mock_client_instance = AsyncMock()

        async def capture_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _make_anthropic_response("ok")

        mock_client_instance.post = capture_post

        class FakeClient:
            async def __aenter__(self): return mock_client_instance
            async def __aexit__(self, *args): pass

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await llm_complete(
                [LlmMessage(role="user", content="test")],
                model="custom-model",
            )
        assert captured["json"]["model"] == "custom-model"

    @pytest.mark.asyncio
    async def test_uses_default_max_tokens(self):
        captured = {}
        mock_client_instance = AsyncMock()

        async def capture_post(url, **kwargs):
            captured["json"] = kwargs.get("json", {})
            return _make_anthropic_response("ok")

        mock_client_instance.post = capture_post

        class FakeClient:
            async def __aenter__(self): return mock_client_instance
            async def __aexit__(self, *args): pass

        with patch("httpx.AsyncClient", return_value=FakeClient()):
            await llm_complete([LlmMessage(role="user", content="test")])
        assert captured["json"]["max_tokens"] == 1024
