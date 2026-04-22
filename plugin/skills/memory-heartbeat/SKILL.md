---
name: memory-heartbeat
description: >-
  Periodic memory maintenance for open-brain. Runs lifecycle pipeline during work hours,
  generates daily digest at end-of-day, and produces weekly summary on Fridays.
  Triggers on /memory-heartbeat, memory heartbeat, heartbeat maintenance, memory lifecycle.
---

# Memory Heartbeat

Run the appropriate open-brain maintenance action for the current time window.
Determine time context, select the correct mode, execute MCP tools, and report results.

## Self-Test Scenarios

These scenarios document the expected behavior for each time window.
Use these as a verification reference when testing or reviewing the skill.

| Scenario | Input (DOW, HOUR) | Expected Mode | Expected Action |
|----------|-------------------|---------------|-----------------|
| Monday morning | Mon, 09:00 | WORK-HOURS | run_lifecycle_pipeline(scope="recent") |
| Wednesday afternoon | Wed, 14:30 | WORK-HOURS | run_lifecycle_pipeline(scope="recent") |
| WORK-HOURS + extraction due | Mon, 09:00, no last_learnings_run | WORK-HOURS | lifecycle pipeline + spawn learning-extractor(scope=all-projects) |
| WORK-HOURS + extraction recent | Mon, 10:00, last_learnings_run=1h ago | WORK-HOURS | lifecycle pipeline only, skip learnings extraction |
| Thursday end-of-day | Thu, 17:30 | END-OF-DAY | search today + stats → daily digest |
| Friday end-of-day | Fri, 17:30 | WEEKLY | run_lifecycle_pipeline(scope=None) + weekly summary |
| Friday morning | Fri, 09:00 | WORK-HOURS | run_lifecycle_pipeline(scope="recent") |
| Saturday midday | Sat, 12:00 | QUIET | silent exit, no MCP calls |
| Sunday evening | Sun, 20:00 | QUIET | silent exit, no MCP calls |
| Monday evening | Mon, 20:00 | QUIET | silent exit, no MCP calls (after 7PM, no mode matches → quiet) |
| Monday late night | Mon, 23:00 | QUIET | silent exit, no MCP calls (10PM-6AM) |
| Tuesday early morning | Tue, 05:00 | QUIET | silent exit, no MCP calls (10PM-6AM) |
| Tuesday 5PM-6PM | Tue, 17:30 | END-OF-DAY | daily digest (5PM-7PM overlap: EOD beats work-hours) |
| Friday 5PM-6PM | Fri, 17:30 | WEEKLY | weekly supersedes EOD in overlap |
| Second run same window | any active window | IDEMPOTENT | "No pending work in this window." if 0 actions |
| MCP unreachable | any active window | ERROR | warning message + reconnect hint |

---

## Step 1: Detect Time Context

Run: `uv run python plugin/scripts/check_time_window.py`
Parse the JSON output: `window`, `hour`, `dow`.

Classify the window using this priority order (highest first):

1. **QUIET** — if `HOUR < 6` OR `HOUR >= 22` → applies any day
2. **QUIET** — if `DOW >= 6` (Saturday=6, Sunday=7) → weekend, always quiet
3. **WEEKLY** — if `DOW == 5` AND `HOUR >= 17` AND `HOUR < 19` (Friday 5PM-7PM)
4. **END-OF-DAY** — if `DOW <= 4` AND `HOUR >= 17` AND `HOUR < 19` (Mon-Thu 5PM-7PM; Friday is handled by WEEKLY above)
5. **WORK-HOURS** — if `DOW <= 5` AND `HOUR >= 6` AND `HOUR < 17` (Mon-Fri 6AM-5PM)

Note: Step 3 checks Friday explicitly before step 4 so Friday 5PM-7PM always routes to WEEKLY.
Any hour not matched by rules 1-5 (e.g. weekday 7PM-10PM) falls through to QUIET (no active mode).

---

## Step 2: Execute Mode

### QUIET mode

Output nothing. Do not call any MCP tools. Exit silently.

---

### WORK-HOURS mode

Call `mcp__open-brain__run_lifecycle_pipeline` with `scope="recent"` and `dry_run=false`.

If the tool call fails (MCP unreachable):
```
⚠️ open-brain MCP unreachable — heartbeat skipped. Reconnect with /mcp reconnect open-brain
```

If the result shows 0 actions processed:
```
No pending work in this window.
```

Otherwise summarize:
```
Memory Heartbeat — Work Hours

Lifecycle pipeline (scope=recent):
- Processed: <N> memories
- <summary of actions taken from result>
```

Then proceed to **Step 3: Provenance Check** (see below).

### Learnings Extraction (every 4h)

After running the lifecycle pipeline, check if periodic learnings extraction is due:

1. Check the rate-limit in processing-state.json:
   <!-- NOTE: Rate-limit logic (4h interval, last_learnings_run key) is duplicated in
        open_brain/learnings_state.py — keep both in sync when changing interval or key name. -->

   Run: `uv run python plugin/scripts/check_learnings_due.py`
   Parse `data.result` from the execution-result envelope: `due` or `skip`.

2. If output is `due`:
   - Spawn `Agent(subagent_type="learning-extractor")` with prompt:
     ```
     Extract learnings from all projects (scope=all-projects).
     After completion, update ~/.claude/learnings/processing-state.json by adding
     a top-level key "last_learnings_run" set to the current UTC ISO timestamp.
     ```
   - Report how many new learnings were extracted (from agent's summary output)

3. If output is `skip`:
   - Include in summary: "Learnings extraction: skipped (last run < 4h ago)"

---

### END-OF-DAY mode

Goal: Produce a daily digest of what was stored in open-brain today.

1. Get today's date: `TODAY=$(date +%Y-%m-%d)`
2. Call `mcp__open-brain__search` with:
   - `query` = "session summary observations decisions"
   - `date_start` = TODAY's date
   - `order_by` = "created_at"
   - `limit` = 50
3. Call `mcp__open-brain__stats` (no arguments)

If either tool call fails (MCP unreachable):
```
⚠️ open-brain MCP unreachable — heartbeat skipped. Reconnect with /mcp reconnect open-brain
```

If no memories found today and nothing notable in stats:
```
No pending work in this window.
```

Otherwise produce a digest:
```
Memory Heartbeat — End of Day (<date>)

Today's memories: <count from search>
Total memories: <from stats>

Key themes today:
- <summarize top 3-5 themes from search results>

Sessions today: <count if available>
```

---

### WEEKLY mode

Goal: Full triage + weekly summary.

1. Call `mcp__open-brain__run_lifecycle_pipeline` without a `scope` argument (omit the parameter entirely) and with `dry_run=false`.
2. Call `mcp__open-brain__stats` (no arguments).
3. Call `mcp__open-brain__search` with:
   - `query` = "session summary week"
   - `date_start` = date 7 days ago (`date -v-7d +%Y-%m-%d` on macOS; Linux: `date -d '7 days ago' +%Y-%m-%d`)
   - `order_by` = "created_at"
   - `limit` = 100

If any tool call fails (MCP unreachable):
```
⚠️ open-brain MCP unreachable — heartbeat skipped. Reconnect with /mcp reconnect open-brain
```

If lifecycle returned 0 actions and no memories in the past week:
```
No pending work in this window.
```

Otherwise produce a weekly summary:
```
Memory Heartbeat — Weekly Summary (<date>)

Full lifecycle triage:
- Processed: <N> memories
- <key actions from pipeline result>

This week's memory stats:
- Total memories: <from stats>
- Memories this week: <count from search>
- DB size: <from stats if available>

Weekly themes:
- <summarize top 5-7 themes from search results>

Notable patterns:
- <any recurring topics, decisions, or projects from the week>
```

---

## Step 3: Provenance Check (WORK-HOURS only)

After the lifecycle pipeline, run a provenance check on code-referencing memories.

Goal: Find top-10 memories most likely to reference code artifacts, verify they are still
      valid, and update their `confidence_score` + `last_verified` metadata.

The implementation module lives at:
`malte/skills/memory_heartbeat/provenance.py` (pure functions, no MCP calls).

### 3.1 Search for code-referencing memories

Call `mcp__open-brain__search` with:
- `query` = "file path function code artifact"
- `limit` = 10

### 3.2 For each memory, run the staleness check

Use `plugin/scripts/provenance_check.py` to compute the metadata patch.

Assign the JSON-serialised dict for each memory to a shell variable, then pipe it:

```bash
MEMORY_JSON=$(...)   # JSON-serialised dict for the memory returned by the search
printf '%s' "$MEMORY_JSON" | uv run python plugin/scripts/provenance_check.py
```

Parse stdout as JSON; `null` means no code refs found.

**Important**: always assign the JSON to a variable first and pipe via `printf '%s' "$MEMORY_JSON"`.
Do NOT use `echo '<literal_json>'` — single quotes cannot be nested and newlines break the shell
syntax if the JSON contains either.
Set the `PROVENANCE_REPO_ROOT` env var to the absolute path of the claude-config repo root
(e.g. `/Users/malte/code/claude`) so relative code refs are resolved correctly.

### 3.3 Apply updates — stale memories (AK4)

For each memory where `update["metadata_patch"]["confidence_score"] == "low"`:

1. Call `mcp__open-brain__update_memory(id=X, metadata=update["metadata_patch"])`
   — stores `confidence_score="low"`, `last_verified=<now>`, `stale_refs=[...]`
2. Call `mcp__open-brain__update_memory(id=X, type="archived")`
   — auto-archives the stale memory (AK4)

### 3.4 Apply updates — valid memories

For each memory where `update` is not None and `confidence_score` is "high" or "medium":

Call `mcp__open-brain__update_memory(id=X, metadata=update["metadata_patch"])`

Skip memories where `build_provenance_update` returned `null` (no code refs found).

### 3.5 Report summary

```
Provenance check: N memories scanned, M stale → archived, K verified
```

---

## Step 4: Idempotency

The skill is idempotent within a time window by design:

- **WORK-HOURS**: `run_lifecycle_pipeline(scope="recent")` is naturally idempotent — if there's nothing new to process it returns 0 actions → output "No pending work in this window."
- **END-OF-DAY**: Search returns the same memories on repeated calls → re-running produces the same read-only digest without side effects, which is acceptable idempotency for a stateless skill (no actions are duplicated).
- **WEEKLY**: `run_lifecycle_pipeline` with no scope is idempotent — already-triaged memories won't be re-processed.

---

## Error Handling Reference

| Situation | Response |
|-----------|----------|
| MCP tool call throws / unreachable | `⚠️ open-brain MCP unreachable — heartbeat skipped. Reconnect with /mcp reconnect open-brain` |
| Lifecycle returns 0 actions | `No pending work in this window.` |
| Search returns 0 results | Include "No memories found today/this week" in digest |
| date command fails | Fall back to QUIET mode (safe default) |
