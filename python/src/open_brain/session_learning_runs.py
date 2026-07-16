"""Durable execution ledger for bounded session-learning analyses."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Coroutine
from uuid import UUID, uuid4

from open_brain.data_layer.postgres import get_pool
from open_brain.session_learning_analysis import analyze_session_learnings


logger = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
_URL_CREDENTIAL_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@\s]+(@)", re.IGNORECASE)


def _safe_error(exc: BaseException) -> str:
    """Return bounded failure context without persisting URL credentials."""
    detail = _URL_CREDENTIAL_RE.sub(r"\1[redacted]\2", str(exc))
    return f"{type(exc).__name__}: {detail}"[:2000]


def normalize_run_id(value: str | None) -> str:
    """Return a canonical UUID string for a caller-supplied or new run ID."""
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("run_id must be a UUID") from exc


@dataclass(frozen=True)
class SessionLearningRun:
    """One persisted analysis execution and its terminal report."""

    run_id: str
    status: str
    parameters: dict[str, Any]
    source_memory_ids: list[int]
    next_cursor: str | None
    report: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "parameters": self.parameters,
            "source_memory_ids": self.source_memory_ids,
            "next_cursor": self.next_cursor,
            "report": self.report,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return dict(value or {})


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _record(row: Any) -> SessionLearningRun:
    return SessionLearningRun(
        run_id=str(row["run_id"]),
        status=str(row["status"]),
        parameters=_json_object(row["parameters"]),
        source_memory_ids=[int(value) for value in (row["source_memory_ids"] or [])],
        next_cursor=row["next_cursor"],
        report=_json_object(row["report"]) if row["report"] is not None else None,
        error=row["error"],
        created_at=_isoformat(row["created_at"]) or "",
        updated_at=_isoformat(row["updated_at"]) or "",
        completed_at=_isoformat(row["completed_at"]),
    )


_RUN_COLUMNS = """
    run_id, status, parameters, source_memory_ids, next_cursor,
    report, error, created_at, updated_at, completed_at
"""


async def create_session_learning_run(
    *,
    run_id: str | None,
    parameters: dict[str, Any],
) -> tuple[SessionLearningRun, bool]:
    """Create a running ledger row, returning an existing row on idempotent retry."""
    canonical_run_id = normalize_run_id(run_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO session_learning_analysis_runs (run_id, status, parameters)
            VALUES ($1::uuid, 'running', $2::jsonb)
            ON CONFLICT (run_id) DO NOTHING
            RETURNING {_RUN_COLUMNS}
            """,
            canonical_run_id,
            json.dumps(parameters),
        )
        if row is not None:
            return _record(row), True
        row = await conn.fetchrow(
            f"""
            SELECT {_RUN_COLUMNS}
              FROM session_learning_analysis_runs
             WHERE run_id = $1::uuid
            """,
            canonical_run_id,
        )
    if row is None:
        raise RuntimeError("analysis run disappeared after idempotent create")
    return _record(row), False


async def get_session_learning_run(run_id: str) -> SessionLearningRun | None:
    """Load one persisted run without changing its state."""
    canonical_run_id = normalize_run_id(run_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            SELECT {_RUN_COLUMNS}
              FROM session_learning_analysis_runs
             WHERE run_id = $1::uuid
            """,
            canonical_run_id,
        )
    return _record(row) if row is not None else None


async def _complete_session_learning_run(
    run_id: str,
    report: dict[str, Any],
) -> SessionLearningRun:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE session_learning_analysis_runs
               SET status = 'completed',
                   source_memory_ids = $2::bigint[],
                   next_cursor = $3,
                   report = $4::jsonb,
                   error = NULL,
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE run_id = $1::uuid
               AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            run_id,
            report.get("source_memory_ids") or [],
            report.get("next_cursor"),
            json.dumps(report),
        )
    if row is None:
        existing = await get_session_learning_run(run_id)
        if existing is None:
            raise RuntimeError("analysis run disappeared before completion")
        return existing
    return _record(row)


async def _fail_session_learning_run(
    run_id: str,
    error: str,
) -> SessionLearningRun:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE session_learning_analysis_runs
               SET status = 'failed',
                   error = $2,
                   completed_at = NOW(),
                   updated_at = NOW()
             WHERE run_id = $1::uuid
               AND status = 'running'
            RETURNING {_RUN_COLUMNS}
            """,
            run_id,
            error[:2000],
        )
    if row is None:
        existing = await get_session_learning_run(run_id)
        if existing is None:
            raise RuntimeError("analysis run disappeared before failure recording")
        return existing
    return _record(row)


@asynccontextmanager
async def _analysis_run_lock(run_id: str):
    """Hold a cross-worker PostgreSQL advisory lock for one LLM execution."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        acquired = bool(
            await conn.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
                run_id,
            )
        )
        try:
            yield acquired
        finally:
            if acquired:
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                    run_id,
                )


async def execute_session_learning_run(
    run_id: str,
    parameters: dict[str, Any],
) -> SessionLearningRun:
    """Execute one already-created run and persist its terminal state."""
    canonical_run_id = normalize_run_id(run_id)
    async with _analysis_run_lock(canonical_run_id) as acquired:
        if not acquired:
            existing = await get_session_learning_run(canonical_run_id)
            if existing is None:
                raise RuntimeError("analysis run disappeared while waiting for worker lock")
            return existing
        try:
            report = await analyze_session_learnings(**parameters)
        except asyncio.CancelledError:
            await asyncio.shield(
                _fail_session_learning_run(
                    canonical_run_id,
                    "analysis worker was cancelled before completion",
                )
            )
            raise
        except Exception as exc:
            logger.exception("Session-learning analysis run %s failed", canonical_run_id)
            return await _fail_session_learning_run(canonical_run_id, _safe_error(exc))
        try:
            return await _complete_session_learning_run(canonical_run_id, report)
        except Exception as exc:
            logger.exception(
                "Session-learning analysis run %s could not persist its report",
                canonical_run_id,
            )
            return await _fail_session_learning_run(canonical_run_id, _safe_error(exc))


def _retain_background_task(coroutine: Coroutine[Any, Any, None]) -> None:
    """Keep a strong reference to a fire-and-forget analysis task."""
    task = asyncio.create_task(coroutine)
    _BACKGROUND_TASKS.add(task)

    def finish(completed: asyncio.Task[None]) -> None:
        _BACKGROUND_TASKS.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error("Session-learning background task failed: %s", _safe_error(error))

    task.add_done_callback(finish)


async def _execute_background(run_id: str, parameters: dict[str, Any]) -> None:
    await execute_session_learning_run(run_id, parameters)


async def start_session_learning_run(
    *,
    run_id: str | None,
    parameters: dict[str, Any],
) -> SessionLearningRun:
    """Persist and start a run, returning immediately for short MCP transport."""
    run, created = await create_session_learning_run(
        run_id=run_id,
        parameters=parameters,
    )
    if created or run.status == "running":
        _retain_background_task(_execute_background(run.run_id, parameters))
    return run
