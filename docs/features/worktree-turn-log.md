# Worktree Turn-Log Hook

Automatic session turn logging via Stop/SubagentStop hooks in Claude Code worktrees, appending structured turn metadata (user input, assistant summary, tool calls) to a local `.worktree-turns.jsonl` file for session analytics and audit trails.

## Was

The Worktree Turn-Log Hook is a plugin feature that automatically captures and logs session turn metadata whenever a Claude Code session ends (Stop event) or a subagent session ends (SubagentStop event). It writes tool-agnostic JSONL records to a `.worktree-turns.jsonl` file in the worktree root, creating an audit trail of all interactions within a worktree.

**Key capabilities:**
- Runs only when inside a Claude Code worktree (`.claude/worktrees/` path detection)
- Parses session transcript (JSONL format) to extract last user input, assistant response, and tool calls
- Writes one JSON line per turn to `.worktree-turns.jsonl` in the worktree root
- Self-healing: adds `/.worktree-turns.jsonl` to git exclude file to prevent accidental commits
- Fault-tolerant: continues gracefully if transcript is missing, corrupted, or parsing fails
- Tool-agnostic schema (ready for Codex/Pi compatibility)

## Für wen

**Primary users:**
- **Session analysts** — Inspect turn logs post-session to understand what was done and why
- **Automation frameworks** — Use turn logs for session replay, debugging, or validation
- **Future AI agents** — Codex, Pi, or other Claude Code variants can read the same format
- **Worktree auditing** — Track which beads were worked on and what changes were attempted

**Use cases:**
- **Session replay** — Reconstruct the conversation from turn logs without relying on transcript storage
- **Debugging failed sessions** — See which tool calls failed and why (from tool_calls summary)
- **Bead completion verification** — Check if a worktree touched the intended files via tool logs
- **Analytics** — Aggregate turn logs across multiple worktrees to understand usage patterns
- **Future AI compatibility** — The schema is intentionally tool-agnostic for Codex/Pi adoption

## Wie es funktioniert

### Hook Integration

The hook is registered in `plugin/hooks/hooks.json` under two events:
- **Stop** — Fires when a main Claude Code session ends
- **SubagentStop** — Fires when a subagent session ends

On each event, the hook runs `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/worktree_turn_log.py`.

### Core Logic

**1. Worktree Detection**

```python
def _is_worktree(cwd: str) -> bool:
    """Return True if cwd is inside a Claude Code worktree."""
    return ".claude/worktrees/" in cwd
```

- Checks if current working directory contains `.claude/worktrees/` path segment
- If not in a worktree, the hook is a no-op (prints `{"continue": true}` and exits)

**2. Bead ID Extraction**

```python
def _extract_bead_id(path: str) -> str | None:
    """Extract bead ID from 'bead-open-brain-krx' → 'open-brain-krx'."""
    match = re.search(r"(?:^|/)\.claude/worktrees/bead-([a-z0-9][a-z0-9-]+?)(?:/|$)", path)
    return match.group(1) if match else None
```

- Parses the worktree directory name to extract the bead ID
- Example: `/home/.claude/worktrees/bead-open-brain-krx/src` → bead_id = `"open-brain-krx"`

**3. Transcript Parsing**

Reads the session transcript (JSONL format) and extracts:
- **Last real user message** — Skips tool_result-only entries, takes text from the most recent user message with actual text content
- **Assistant responses** — Concatenates all assistant messages after the last user message
- **Tool calls** — Extracts tool names and targets from tool_use blocks

```python
def _parse_transcript(transcript_path: Path) -> dict:
    """Parse JSONL transcript and extract the last turn's user + assistant data."""
    # Returns:
    {
        "user_input_excerpt": "Please edit the file...",  # First 500 chars
        "assistant_summary_excerpt": "I will...",          # First 500 chars  
        "tool_calls": [
            {"name": "Edit", "target": "server.py"},
            {"name": "Bash", "target": "git status"},
        ]
    }
```

#### Tool Call Target Extraction

Tool calls use a priority list of known parameter keys to extract the "target" (what the tool was called on):

**Priority keys:** `file_path`, `path`, `command`, `pattern`, `query`, `url`, `prompt`, `cmd`

**Fallback:** If none of the priority keys exist, uses the first string value in the input dict.

**Truncation:** Targets longer than 200 characters (e.g., multi-line Bash scripts) are truncated.

**Example:**
```json
{
  "type": "tool_use",
  "name": "Bash",
  "input": {"command": "git log --oneline -n 10"}
}
```
→ `{"name": "Bash", "target": "git log --oneline -n 10"}`

#### User Input Identification

The parser carefully distinguishes between:
- **Real user messages** — Have `content` field with type="text" blocks
- **Tool result callbacks** — Have `content` field with only tool_result blocks (returned from assistant tool_use)

Example transcript structure:
```
user (text) "Please read the file"
  ↓
assistant (text + tool_use) "I will read it" + Read("config.yml")
  ↓
user (tool_result) "file contents..." ← NOT treated as a new user message
  ↓
assistant (text) "The config says..." ← This is the summary extracted
```

**4. JSONL Line Formation**

```json
{
  "ts": "2026-04-17T14:23:45.123456+00:00",
  "agent": "claude-code",
  "session_id": "sess-123",
  "parent_session_id": null,
  "hook_type": "Stop",
  "worktree": ".claude/worktrees/bead-open-brain-abc",
  "bead_id": "open-brain-abc",
  "user_input_excerpt": "Please add a new endpoint",
  "assistant_summary_excerpt": "I will add the endpoint...",
  "tool_calls": [
    {"name": "Edit", "target": "server.py"},
    {"name": "Bash", "target": "git status"}
  ]
}
```

| Field | Type | Meaning |
|-------|------|---------|
| **ts** | ISO8601 | Timestamp when the hook ran (UTC) |
| **agent** | string | Always `"claude-code"` (extensible for Codex, Pi) |
| **session_id** | string | Session ID from hook_data |
| **parent_session_id** | string or null | Parent session if this is a SubagentStop |
| **hook_type** | string | `"Stop"` or `"SubagentStop"` |
| **worktree** | string | Relative path from git toplevel (e.g., `.claude/worktrees/bead-open-brain-abc`) |
| **bead_id** | string or null | Extracted from worktree directory name (or null if not parseable) |
| **user_input_excerpt** | string | First 500 chars of last user message (empty if missing) |
| **assistant_summary_excerpt** | string | First 500 chars of assistant responses (empty if missing) |
| **tool_calls** | array | List of `{name, target}` objects; empty if no tool calls made |

**5. File Append**

Appends the JSON line to `.worktree-turns.jsonl` in the worktree root:
```python
output_file = Path(worktree_root) / ".worktree-turns.jsonl"
with open(output_file, "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")
```

Multiple turns are appended as separate lines; each line is independently valid JSON.

**6. Self-Healing Git Exclude**

After writing the line, the hook updates the git exclude file:

```python
def _ensure_exclude(git_common_dir: Path) -> None:
    """Idempotently add /.worktree-turns.jsonl to git exclude file."""
    exclude_file = git_common_dir / "info" / "exclude"
    # Create if missing, append if not present
    if "/.worktree-turns.jsonl" not in existing_lines:
        content += "/.worktree-turns.jsonl\n"
        exclude_file.write_text(content, encoding="utf-8")
```

- Ensures `/.worktree-turns.jsonl` is in `info/exclude` (prevents `git status` noise)
- Creates the exclude file if it doesn't exist
- Idempotent — running multiple times doesn't duplicate the entry
- Exceptions caught and logged to stderr (doesn't fail the hook)

## Zusammenspiel

- **Session transcripts** — Hook reads from `transcript_path` provided by Claude Code runtime
- **Git worktrees** — Determines worktree root via `git rev-parse --show-toplevel`; adds exclude pattern via `git rev-parse --git-common-dir`
- **Bead orchestration** — Bead ID extracted from directory name and stored for audit trails
- **Subagent spawning** — parent_session_id field populated for nested agent sessions
- **Future agents** — Codex, Pi, or other Claude variants can read the same `.worktree-turns.jsonl` format

## Besonderheiten

### Edge Cases & Fault Tolerance

1. **Not in a worktree** — Hook is a no-op; returns `{"continue": true}` without writing anything
2. **Missing transcript** — Parses an empty dict; user_input_excerpt and tool_calls are empty strings/arrays
3. **Corrupted transcript** — JSON parse errors are caught; affected lines skipped gracefully
4. **Tool result-only messages** — Parser filters out tool_result-only entries (not real user input)
5. **Missing git metadata** — `git rev-parse` fails; hooks logs warning but still writes the line
6. **Long targets** — Bash commands, file paths, or other targets > 200 chars are truncated
7. **Multiple tool calls** — All extracted; complex tool inputs reduced to the primary parameter

### Timeouts & Performance

- **Transcript parsing** — Full read into memory; typically <100ms even for large transcripts (Claude sessions don't exceed ~50MB)
- **Git operations** — `git rev-parse --git-common-dir` and `git rev-parse --show-toplevel` run in parallel; timeout 5 seconds
- **File append** — Synchronous write, typically <1ms on modern filesystems
- **Hook timeout** — Set to 10 seconds in `hooks.json` (generous headroom)

### Storage & Lifecycle

- **File location** — `.worktree-turns.jsonl` in worktree root (sibling to `.git`)
- **Append-only** — No truncation or rotation; files grow indefinitely
- **Cleanup** — User responsibility to archive or delete logs (not handled by the hook)
- **Size estimate** — Each turn ~500–2000 bytes; 100 turns = 50–200KB; 1000 turns = 0.5–2MB

### Git Ignore Behavior

- **Auto-added pattern** — `/.worktree-turns.jsonl` added to shared git exclude (`.git/info/exclude`)
- **Scope** — Pattern only affects the worktree root; subdir `logs/.worktree-turns.jsonl` would not be ignored
- **Idempotent** — Safe to run on shared repos; pattern is not duplicated across multiple calls

## Technische Details

### Script Location

- **Plugin script** — `plugin/scripts/worktree_turn_log.py` (292 lines)
- **Hook registration** — `plugin/hooks/hooks.json` (Stop and SubagentStop entries)

### Integration Points

**Hook Events:**
- `Stop` — Main session end
- `SubagentStop` — Subagent session end

**Input (hook_data):**
- `cwd` — Current working directory (string)
- `session_id` — Session ID (string)
- `parent_session_id` — Parent session ID for subagents (string or null)
- `hook_event_name` — `"Stop"` or `"SubagentStop"` (string)
- `transcript_path` — Full path to session transcript JSONL file (string)

**Output:**
- JSONL line appended to `.worktree-turns.jsonl` in worktree root
- Exit status: Always 0 (`{"continue": true}` printed)
- Stderr: Warnings logged if git operations fail; no exception propagation

### Git Commands Used

```bash
git rev-parse --git-common-dir   # Get shared .git directory (for exclude)
git rev-parse --show-toplevel    # Get repo root (for relative path)
```

Both run with 5-second timeout; failures are logged but don't block the hook.

### Environment

- **Language** — Python 3.8+
- **Dependencies** — Standard library only (json, subprocess, pathlib, datetime, re, sys)
- **Performance** — Runs in <100ms on typical sessions

### Testing

- **Unit tests** — `python/tests/test_worktree_turn_log.py` (23 tests, 595 lines)
- **Coverage** — Worktree detection, bead ID extraction, transcript parsing, JSONL output, git exclude self-healing, error tolerance
- **Test patterns** — pytest fixtures for fake worktree, fake transcript, fake git dirs

**Test command:**
```bash
cd python && uv run pytest tests/test_worktree_turn_log.py
```

### OpenAPI / API Documentation

No API endpoints exposed. This is a plugin hook (internal infrastructure), not a user-facing API. No OpenAPI schema needed.

### Future Extensions

- **Encryption** — JSONL could be encrypted before append (per-line)
- **Compression** — .worktree-turns.jsonl could be a .jsonl.gz file
- **Cloud sync** — Upload logs to cloud storage periodically
- **Structured querying** — Index the JSONL for efficient search (Duckdb, SQLite)
- **Multi-agent support** — Codex, Pi, other Claude variants using the same schema
