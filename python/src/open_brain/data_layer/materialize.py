"""Materialization of triage actions: promote to files, scaffold beads, archive."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from open_brain.data_layer.interface import (
    MaterializeActionResult,
    Memory,
    TriageAction,
)

logger = logging.getLogger(__name__)


def _resolve_promote_path(memory: Memory) -> Path:
    """Resolve the file path to write a promoted memory.

    Priority:
    1. memory.metadata["materialize_path"] if set
    2. ~/.claude/projects/<project>/MEMORY.md based on memory.project (index_id → project)
    3. Fallback: ~/.claude/projects/general/MEMORY.md
    """
    if memory.metadata.get("materialize_path"):
        return Path(memory.metadata["materialize_path"]).expanduser()

    # Derive project name from index — not directly available on Memory, so we use a
    # generic path. Callers that know the project name can pass it via materialize_path.
    projects_dir = Path("~/.claude/projects").expanduser()
    return projects_dir / "general" / "MEMORY.md"


def _resolve_promote_path_for_project(memory: Memory, project: str | None) -> Path:
    """Resolve promote path with optional project name override."""
    if memory.metadata.get("materialize_path"):
        return Path(memory.metadata["materialize_path"]).expanduser()

    if project:
        safe = project.replace("/", "-").replace(" ", "-")
        return Path(f"~/.claude/projects/{safe}/MEMORY.md").expanduser()

    return Path("~/.claude/projects/general/MEMORY.md").expanduser()


def materialize_promote(memory: Memory, project: str | None = None) -> MaterializeActionResult:
    """Write memory content to the appropriate standards/MEMORY.md file."""
    path = _resolve_promote_path_for_project(memory, project)
    title = memory.title or f"Memory {memory.id}"
    content = memory.content or memory.narrative or ""
    section = f"\n## Memory: {title}\n{content}\n"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        # Idempotency check: skip if section already present
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if f"## Memory: {title}" in existing:
                return MaterializeActionResult(
                    memory_id=memory.id,
                    action="promote",
                    success=True,
                    detail=f"Already present in {path} (idempotent)",
                )

        with path.open("a", encoding="utf-8") as fh:
            fh.write(section)

        logger.info("Promoted memory %d to %s", memory.id, path)
        return MaterializeActionResult(
            memory_id=memory.id,
            action="promote",
            success=True,
            detail=f"Written to {path}",
        )
    except OSError as err:
        logger.error("Failed to promote memory %d: %s", memory.id, err)
        return MaterializeActionResult(
            memory_id=memory.id,
            action="promote",
            success=False,
            detail=f"Write failed: {err}",
        )


def materialize_scaffold(memory: Memory) -> MaterializeActionResult:
    """Create a bead/task via `bd create` for a scaffold-action memory."""
    title = memory.title or f"Memory {memory.id}"
    description = memory.content or memory.narrative or ""

    cmd = [
        "bd",
        "create",
        f"--title={title}",
        "--type=task",
        f"--description={description}",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or "non-zero exit code"
            logger.warning("bd create failed for memory %d: %s", memory.id, error_msg)
            return MaterializeActionResult(
                memory_id=memory.id,
                action="scaffold",
                success=False,
                detail=f"bd create failed: {error_msg}",
            )

        # Parse bead ID from output (bd typically prints something like "Created issue XXX-NNN")
        output = result.stdout.strip()
        bead_id: str | None = None
        for line in output.splitlines():
            parts = line.split()
            for part in parts:
                # Match patterns like "open-brain-42" or "pvv-1" or "XXX-NNN"
                if "-" in part and any(c.isdigit() for c in part):
                    bead_id = part
                    break
            if bead_id:
                break

        detail = f"Created bead {bead_id}" if bead_id else f"Scaffolded (output: {output})"
        logger.info("Scaffolded memory %d → bead %s", memory.id, bead_id)
        return MaterializeActionResult(
            memory_id=memory.id,
            action="scaffold",
            success=True,
            detail=detail,
        )
    except subprocess.TimeoutExpired:
        return MaterializeActionResult(
            memory_id=memory.id,
            action="scaffold",
            success=False,
            detail="bd create timed out",
        )
    except FileNotFoundError:
        return MaterializeActionResult(
            memory_id=memory.id,
            action="scaffold",
            success=False,
            detail="bd command not found",
        )
    except Exception as err:
        logger.error("Scaffold error for memory %d: %s", memory.id, err)
        return MaterializeActionResult(
            memory_id=memory.id,
            action="scaffold",
            success=False,
            detail=f"Unexpected error: {err}",
        )


async def materialize_archive(
    memory: Memory,
    update_fn,  # async callable: (memory_id, priority) -> None
) -> MaterializeActionResult:
    """Deprioritize a memory (archive = set priority to 0.1)."""
    try:
        await update_fn(memory.id, 0.1)
        logger.info("Archived memory %d (priority → 0.1)", memory.id)
        return MaterializeActionResult(
            memory_id=memory.id,
            action="archive",
            success=True,
            detail="Priority set to 0.1",
        )
    except Exception as err:
        logger.error("Archive failed for memory %d: %s", memory.id, err)
        return MaterializeActionResult(
            memory_id=memory.id,
            action="archive",
            success=False,
            detail=f"Archive failed: {err}",
        )


async def execute_triage_actions(
    actions: list[TriageAction],
    memories_by_id: dict[int, Memory],
    archive_fn,  # async (memory_id: int, priority: float) -> None
    project_by_index_id: dict[int, str] | None = None,
) -> list[MaterializeActionResult]:
    """Execute a list of triage actions and return results.

    Args:
        actions: Triage actions to execute.
        memories_by_id: Mapping from memory_id to Memory object.
        archive_fn: Async function to update memory priority.
        project_by_index_id: Optional mapping from index_id to project name.
    """
    results: list[MaterializeActionResult] = []

    for action in actions:
        memory = memories_by_id.get(action.memory_id)
        if memory is None:
            results.append(
                MaterializeActionResult(
                    memory_id=action.memory_id,
                    action=action.action,
                    success=False,
                    detail=f"Memory {action.memory_id} not found",
                )
            )
            continue

        project: str | None = None
        if project_by_index_id and memory.index_id in project_by_index_id:
            project = project_by_index_id[memory.index_id]

        match action.action:
            case "keep":
                results.append(
                    MaterializeActionResult(
                        memory_id=action.memory_id,
                        action="keep",
                        success=True,
                        detail="No-op: memory retained",
                    )
                )
            case "promote":
                results.append(materialize_promote(memory, project))
            case "scaffold":
                results.append(materialize_scaffold(memory))
            case "archive":
                results.append(await materialize_archive(memory, archive_fn))
            case "merge":
                # Merge is handled by the pipeline via refine_memories
                results.append(
                    MaterializeActionResult(
                        memory_id=action.memory_id,
                        action="merge",
                        success=True,
                        detail="Delegated to refine_memories",
                    )
                )
            case _:
                results.append(
                    MaterializeActionResult(
                        memory_id=action.memory_id,
                        action=action.action,
                        success=False,
                        detail=f"Unknown action: {action.action}",
                    )
                )

    return results
