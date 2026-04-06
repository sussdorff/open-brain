"""Tests for scope-gated MCP tool list and runtime enforcement.

AK 1: MCP tools can be conditionally registered based on OAuth scopes.
AK 2: Evolution tools only available with `evolution` scope.
AK 3: Admin tools (if any) gated behind `admin` scope.
AK 4: Unauthenticated callers see only public tools — satisfied by existing
      BearerAuthMiddleware which returns 401 before MCP is reached; no extra
      tool-list filtering needed for unauthenticated paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from open_brain.server import (
    _EVOLUTION_TOOLS,
    _current_scopes,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_dl():
    """Minimal AsyncMock DataLayer for evolution tool calls."""
    dl = AsyncMock()
    return dl


# ─── AK 1+2+3: Tool list filters based on OAuth scopes ────────────────────────

class TestScopeGatedToolList:
    """AK 1+2+3: tool list filters based on OAuth scopes."""

    @pytest.mark.asyncio
    async def test_evolution_tools_visible_with_evolution_scope(self):
        """Evolution tools appear in list_tools() when caller has evolution scope."""
        from open_brain.server import mcp

        token = _current_scopes.set(["memory", "evolution"])
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            for evolution_tool in _EVOLUTION_TOOLS:
                assert evolution_tool in tool_names, (
                    f"Expected evolution tool '{evolution_tool}' to be visible "
                    f"with evolution scope, but it was not in {tool_names}"
                )
        finally:
            _current_scopes.reset(token)

    @pytest.mark.asyncio
    async def test_evolution_tools_hidden_without_evolution_scope(self):
        """Evolution tools are absent from list_tools() when caller lacks evolution scope."""
        from open_brain.server import mcp

        token = _current_scopes.set(["memory"])
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            for evolution_tool in _EVOLUTION_TOOLS:
                assert evolution_tool not in tool_names, (
                    f"Expected evolution tool '{evolution_tool}' to be hidden "
                    f"without evolution scope, but it appeared in {tool_names}"
                )
        finally:
            _current_scopes.reset(token)

    @pytest.mark.asyncio
    async def test_all_evolution_tools_gated(self):
        """Every tool in _EVOLUTION_TOOLS is absent without scope and present with scope."""
        from open_brain.server import mcp

        # Without evolution scope
        token = _current_scopes.set(["memory"])
        try:
            tools_without = await mcp.list_tools()
            names_without = {t.name for t in tools_without}
        finally:
            _current_scopes.reset(token)

        # With evolution scope
        token = _current_scopes.set(["memory", "evolution"])
        try:
            tools_with = await mcp.list_tools()
            names_with = {t.name for t in tools_with}
        finally:
            _current_scopes.reset(token)

        for name in _EVOLUTION_TOOLS:
            assert name not in names_without, f"'{name}' should be hidden without evolution scope"
            assert name in names_with, f"'{name}' should be visible with evolution scope"

    @pytest.mark.asyncio
    async def test_core_memory_tools_always_visible(self):
        """Core memory tools are visible regardless of evolution scope."""
        from open_brain.server import mcp

        core_tools = {"search", "save_memory", "timeline", "get_observations"}

        token = _current_scopes.set(["memory"])
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            for core in core_tools:
                assert core in tool_names, (
                    f"Core tool '{core}' should always be visible, "
                    f"but was absent with scopes=['memory']"
                )
        finally:
            _current_scopes.reset(token)


# ─── AK 4: Empty scopes (effectively unauthenticated via ContextVar) ──────────

class TestUnauthenticatedToolList:
    """AK 4: empty scopes = no evolution tools.

    Note: In production, unauthenticated requests never reach MCP — the
    BearerAuthMiddleware returns HTTP 401 before MCP is reached. This class
    tests the ContextVar-level behavior (scopes=[]) as an additional layer.
    """

    @pytest.mark.asyncio
    async def test_empty_scopes_hides_evolution_tools(self):
        """Evolution tools are absent when _current_scopes is empty list."""
        from open_brain.server import mcp

        token = _current_scopes.set([])
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            for evolution_tool in _EVOLUTION_TOOLS:
                assert evolution_tool not in tool_names, (
                    f"Expected evolution tool '{evolution_tool}' to be hidden "
                    f"with empty scopes, but it appeared in {tool_names}"
                )
        finally:
            _current_scopes.reset(token)

    @pytest.mark.asyncio
    async def test_empty_scopes_preserves_core_tools(self):
        """Core tools remain accessible with empty scopes (auth is handled by middleware)."""
        from open_brain.server import mcp

        core_tools = {"search", "save_memory", "timeline", "get_observations"}

        token = _current_scopes.set([])
        try:
            tools = await mcp.list_tools()
            tool_names = {t.name for t in tools}
            for core in core_tools:
                assert core in tool_names, (
                    f"Core tool '{core}' should remain visible with empty scopes"
                )
        finally:
            _current_scopes.reset(token)


# ─── Defense-in-depth: scope check at tool call time ─────────────────────────

@pytest.mark.integration
class TestScopeRuntimeEnforcement:
    """Defense-in-depth: scope check happens at tool call time, not just tool listing."""

    @pytest.mark.asyncio
    async def test_evolution_tool_raises_without_scope(self, mock_dl):
        """generate_evolution_suggestion raises PermissionError without evolution scope."""
        from open_brain.server import generate_evolution_suggestion

        with patch("open_brain.server.get_dl", return_value=mock_dl):
            token = _current_scopes.set(["memory"])
            try:
                with pytest.raises((PermissionError, RuntimeError)) as exc_info:
                    await generate_evolution_suggestion()
                # Verify the error message references the 'evolution' scope
                assert "evolution" in str(exc_info.value).lower(), (
                    f"Expected error message to reference 'evolution' scope, got: {exc_info.value}"
                )
            finally:
                _current_scopes.reset(token)

    @pytest.mark.asyncio
    async def test_evolution_tool_succeeds_with_scope(self, mock_dl):
        """generate_evolution_suggestion does not raise when evolution scope is present."""
        from open_brain.server import generate_evolution_suggestion
        from open_brain.evolution import EngagementReport, EvolutionSuggestion

        # Mock the analyze_engagement and generate_suggestion calls
        engagement_report = EngagementReport(
            period_days=7,
            total_briefings=10,
            has_sufficient_data=True,
            by_type=[],
        )
        with (
            patch("open_brain.server.get_dl", return_value=mock_dl),
            patch("open_brain.server.analyze_engagement", return_value=engagement_report),
            patch("open_brain.server.generate_suggestion", return_value=None),
        ):
            token = _current_scopes.set(["memory", "evolution"])
            try:
                result = await generate_evolution_suggestion()
                import json
                data = json.loads(result)
                assert "suggestion" in data
            finally:
                _current_scopes.reset(token)
