"""Tests for structured logging and MCP tool call instrumentation.

AK1: LOG_LEVEL env var works — config reads it, __main__ applies it
AK2: Every tool call emits start/end log lines with tool_name and duration_ms
AK3: Failed tool calls emit ERROR with traceback
AK4: request_id appears in ContextVar during tool execution
AK5: LOG_FORMAT=json produces parseable JSON per line
"""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import AsyncMock, patch

import pytest


# ─── AK1: Config reads LOG_LEVEL and LOG_FORMAT ───────────────────────────────

class TestConfigLoggingFields:
    """AK1: Config class has LOG_LEVEL and LOG_FORMAT fields."""

    def test_log_level_default_is_info(self, reset_config_singleton):
        with patch.dict(os.environ, {}, clear=False):
            # Remove LOG_LEVEL from env if present so we see default
            env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}
            with patch.dict(os.environ, env, clear=True):
                from open_brain.config import Config
                # Need required fields
                cfg = Config(
                    DATABASE_URL="postgresql://test:test@localhost/test",
                    MCP_SERVER_URL="http://localhost:8091",
                    AUTH_USER="user",
                    AUTH_PASSWORD="password123",
                    JWT_SECRET="a" * 32,
                    VOYAGE_API_KEY="key",
                )
                assert cfg.LOG_LEVEL == "INFO"

    def test_log_format_default_is_human(self, reset_config_singleton):
        env = {k: v for k, v in os.environ.items() if k not in ("LOG_LEVEL", "LOG_FORMAT")}
        with patch.dict(os.environ, env, clear=True):
            from open_brain.config import Config
            cfg = Config(
                DATABASE_URL="postgresql://test:test@localhost/test",
                MCP_SERVER_URL="http://localhost:8091",
                AUTH_USER="user",
                AUTH_PASSWORD="password123",
                JWT_SECRET="a" * 32,
                VOYAGE_API_KEY="key",
            )
            assert cfg.LOG_FORMAT == "human"

    def test_log_level_reads_from_env(self, reset_config_singleton):
        env = {k: v for k, v in os.environ.items() if k != "LOG_LEVEL"}
        env["LOG_LEVEL"] = "DEBUG"
        with patch.dict(os.environ, env, clear=True):
            from open_brain.config import Config
            cfg = Config(
                DATABASE_URL="postgresql://test:test@localhost/test",
                MCP_SERVER_URL="http://localhost:8091",
                AUTH_USER="user",
                AUTH_PASSWORD="password123",
                JWT_SECRET="a" * 32,
                VOYAGE_API_KEY="key",
            )
            assert cfg.LOG_LEVEL == "DEBUG"

    def test_log_format_json_accepted(self, reset_config_singleton):
        env = {k: v for k, v in os.environ.items() if k not in ("LOG_LEVEL", "LOG_FORMAT")}
        env["LOG_FORMAT"] = "json"
        with patch.dict(os.environ, env, clear=True):
            from open_brain.config import Config
            cfg = Config(
                DATABASE_URL="postgresql://test:test@localhost/test",
                MCP_SERVER_URL="http://localhost:8091",
                AUTH_USER="user",
                AUTH_PASSWORD="password123",
                JWT_SECRET="a" * 32,
                VOYAGE_API_KEY="key",
            )
            assert cfg.LOG_FORMAT == "json"


# ─── AK2 + AK3 + AK4: logged_tool decorator ──────────────────────────────────

class TestLoggedToolDecorator:
    """AK2: start/end lines emitted; AK3: error logged with traceback; AK4: request_id in ContextVar."""

    @pytest.mark.asyncio
    async def test_tool_start_and_end_logged(self, caplog):
        """AK2: logged_tool emits tool_start and tool_end lines."""
        from open_brain.server import logged_tool, _request_id

        @logged_tool
        async def dummy_tool(x: int = 1) -> str:
            return "ok"

        with caplog.at_level(logging.INFO, logger="open_brain.server"):
            result = await dummy_tool(x=5)

        assert result == "ok"
        messages = [r.message for r in caplog.records]
        start_msgs = [m for m in messages if "tool_start" in m and "dummy_tool" in m]
        end_msgs = [m for m in messages if "tool_end" in m and "dummy_tool" in m]
        assert start_msgs, f"Expected tool_start log, got: {messages}"
        assert end_msgs, f"Expected tool_end log, got: {messages}"

    @pytest.mark.asyncio
    async def test_tool_end_includes_duration_ms(self, caplog):
        """AK2: tool_end line includes duration_ms."""
        from open_brain.server import logged_tool

        @logged_tool
        async def timed_tool() -> str:
            return "done"

        with caplog.at_level(logging.INFO, logger="open_brain.server"):
            await timed_tool()

        end_msgs = [r.message for r in caplog.records if "tool_end" in r.message]
        assert end_msgs, "Expected tool_end log line"
        assert "duration_ms" in end_msgs[0], f"Expected duration_ms in: {end_msgs[0]}"

    @pytest.mark.asyncio
    async def test_failed_tool_logs_error_with_traceback(self, caplog):
        """AK3: failed tool emits ERROR log with traceback."""
        from open_brain.server import logged_tool

        @logged_tool
        async def failing_tool() -> str:
            raise RuntimeError("deliberate failure")

        with caplog.at_level(logging.ERROR, logger="open_brain.server"):
            with pytest.raises(RuntimeError, match="deliberate failure"):
                await failing_tool()

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected ERROR log record"
        err = error_records[0]
        assert "tool_error" in err.message or "tool_error" in err.getMessage()
        # exc_info should be set (traceback available)
        assert err.exc_info is not None and err.exc_info[0] is not None

    @pytest.mark.asyncio
    async def test_request_id_set_in_contextvar(self):
        """AK4: request_id ContextVar is set during tool execution."""
        from open_brain.server import logged_tool, _request_id

        captured_rid: list[str | None] = []

        @logged_tool
        async def capturing_tool() -> str:
            captured_rid.append(_request_id.get())
            return "captured"

        await capturing_tool()

        assert len(captured_rid) == 1
        rid = captured_rid[0]
        assert rid is not None
        assert len(rid) > 0

    @pytest.mark.asyncio
    async def test_request_id_in_log_messages(self, caplog):
        """AK4: request_id appears in all log messages for a tool call."""
        from open_brain.server import logged_tool, _request_id

        @logged_tool
        async def id_tool() -> str:
            return "done"

        with caplog.at_level(logging.INFO, logger="open_brain.server"):
            await id_tool()

        # Both start and end should mention request_id
        for r in caplog.records:
            if "id_tool" in r.getMessage():
                assert "request_id" in r.getMessage(), (
                    f"Expected request_id in: {r.getMessage()}"
                )

    @pytest.mark.asyncio
    async def test_tool_error_includes_status_error(self, caplog):
        """AK3: tool_error log line includes status=error."""
        from open_brain.server import logged_tool

        @logged_tool
        async def boom() -> str:
            raise ValueError("boom")

        with caplog.at_level(logging.ERROR, logger="open_brain.server"):
            with pytest.raises(ValueError):
                await boom()

        error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("status=error" in m for m in error_msgs), (
            f"Expected status=error in error log, got: {error_msgs}"
        )


# ─── AK5: JsonFormatter ───────────────────────────────────────────────────────

class TestJsonFormatter:
    """AK5: LOG_FORMAT=json produces parseable JSON per line."""

    def test_json_formatter_produces_valid_json(self):
        """AK5: JsonFormatter.format() returns a valid JSON string."""
        from open_brain.logging_config import JsonFormatter

        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Hello %s",
            args=("world",),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)  # raises if not valid JSON
        assert isinstance(parsed, dict)

    def test_json_formatter_includes_required_fields(self):
        """AK5: JSON output has ts, level, logger, msg fields."""
        from open_brain.logging_config import JsonFormatter

        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="open_brain.server",
            level=logging.INFO,
            pathname="server.py",
            lineno=10,
            msg="tool_start tool=%s",
            args=("search",),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "ts" in parsed
        assert "level" in parsed
        assert "logger" in parsed
        assert "msg" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "open_brain.server"
        assert "tool_start tool=search" in parsed["msg"]

    def test_json_formatter_includes_exc_on_exception(self):
        """AK5: JSON output includes exc key when exception info is present."""
        import sys
        from open_brain.logging_config import JsonFormatter

        formatter = JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="error occurred",
            args=(),
            exc_info=exc_info,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "exc" in parsed
        assert "ValueError" in parsed["exc"]

    def test_json_formatter_one_object_per_line(self):
        """AK5: Each call to format() produces exactly one JSON object (no newlines in output)."""
        from open_brain.logging_config import JsonFormatter

        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="single line",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        # Should not have embedded newlines (newline-delimited JSON)
        assert "\n" not in output.rstrip("\n")
        # Should be valid JSON
        json.loads(output)


# ─── AK2: existing tools are wrapped ─────────────────────────────────────────

class TestExistingToolsWrapped:
    """AK2: All existing @mcp.tool definitions emit start/end lines."""

    @pytest.mark.asyncio
    async def test_search_tool_emits_start_end(self, caplog):
        """AK2: Calling search() emits tool_start and tool_end."""
        from open_brain.server import search

        mock_result = AsyncMock()
        mock_result.total = 0
        mock_result.results = []

        with (
            patch("open_brain.server.get_dl") as mock_get_dl,
            caplog.at_level(logging.INFO, logger="open_brain.server"),
        ):
            mock_dl = AsyncMock()
            mock_dl.search = AsyncMock(return_value=mock_result)
            mock_get_dl.return_value = mock_dl
            await search(query="test")

        messages = [r.message for r in caplog.records]
        assert any("tool_start" in m and "search" in m for m in messages), (
            f"Expected tool_start for search, got: {messages}"
        )
        assert any("tool_end" in m and "search" in m for m in messages), (
            f"Expected tool_end for search, got: {messages}"
        )


# ─── AK1 pipeline: env → config → dictConfig ────────────────────────────────

class TestConfigurePipeline:
    """AK1: env var → Config → _configure_logging → logger level applied."""

    def test_configure_logging_applies_debug_level(self, reset_config_singleton):
        """AK1: _configure_logging('DEBUG', 'human') sets open_brain logger to DEBUG."""
        from open_brain.__main__ import _configure_logging

        _configure_logging("DEBUG", "human")

        assert logging.getLogger("open_brain").isEnabledFor(logging.DEBUG), (
            "Expected open_brain logger to be enabled for DEBUG after _configure_logging('DEBUG', 'human')"
        )

    def test_configure_logging_json_format_uses_json_formatter(self, reset_config_singleton):
        """AK5: _configure_logging with log_format='json' installs JsonFormatter on console handler."""
        from open_brain.__main__ import _configure_logging
        from open_brain.logging_config import JsonFormatter

        _configure_logging("INFO", "json")

        root_logger = logging.getLogger()
        console_handlers = [
            h for h in root_logger.handlers
            if isinstance(h, logging.StreamHandler)
        ]
        assert console_handlers, "Expected at least one StreamHandler on root logger"
        json_handlers = [h for h in console_handlers if isinstance(h.formatter, JsonFormatter)]
        assert json_handlers, (
            f"Expected a handler with JsonFormatter, got: {[type(h.formatter).__name__ for h in console_handlers]}"
        )
