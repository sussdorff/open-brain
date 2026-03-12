---
name: ob-triage
description: >
  Triage open-brain memories with human-in-the-loop review. Classifies memories into
  keep/merge/archive/delete/promote and presents each action for user approval before executing.
  Use when: "triage memories", "memory review", "cleanup memories", "learnings review",
  "materialize learnings", "memory housekeeping", "ob triage", "memory triage",
  "Memories aufräumen", "Learnings reviewen".
version: 0.1.0
---

# open-brain Memory Triage

Interactive memory lifecycle management with human-in-the-loop review.

## Quick Start

```
/ob-triage                              # Triage recent memories (default)
/ob-triage project:open-brain           # Triage specific project
/ob-triage type:learning                # Triage only learnings
/ob-triage scope:low-priority           # Triage low-priority/stale memories
```

## Workflow

### Step 1: Run Triage (always dry-run first)

Call `mcp__open-brain__triage_memories` with the user's scope and `dry_run=true`.

```
triage_memories(scope="<from args>", dry_run=true)
```

Parse the scope from the user's arguments:
- No args → `scope="recent"` (default)
- `project:<name>` → `scope="project:<name>"`
- `type:<name>` → `scope="type:<name>"`
- `scope:<value>` → `scope="<value>"`
- `limit:<n>` → pass as `limit=<n>`

### Step 2: Fetch Full Details

The triage returns memory IDs and actions. Before presenting to the user, fetch the full
memory texts using `mcp__open-brain__get_observations(ids=[...])` for all actionable items
(everything except "keep").

### Step 3: Present Results via AskUserQuestion

Use the `AskUserQuestion` tool for all HITL decisions. This provides structured UI
with selectable options instead of free-text back-and-forth.

#### Keep (no approval needed)
Output as text only — no question needed:
```
**Keep** (3 memories): "Deploy setup notes", "Voyage API config", "Auth flow docs"
```

#### Merge, Archive, Delete — batch via AskUserQuestion

First output a text summary explaining what the triage found. Then present one
`AskUserQuestion` call with up to 4 questions (one per actionable memory).

For each memory, include the title, ID, triage reason, and a brief text excerpt
in the question text. Options depend on the recommended action:

**Merge** question (show both sources + proposed merge text in question):
```
AskUserQuestion:
  question: "#10612 'Deploy-Probleme' + #10606 'uv PATH auf Server' — Grund: Both describe
             server deployment quirks. Merge-Text: 'Deployment open-brain: Nach deploy.sh
             dropped MCP connection, muss /mcp reconnect. uv unter ~/.local/bin/uv...'
             Was tun?"
  header: "Merge #10612"
  options:
    - label: "Mergen"          description: "Zu einem Memory zusammenführen mit dem vorgeschlagenen Text"
    - label: "Behalten"        description: "Beide unverändert lassen"
    - label: "Löschen"         description: "Beide entfernen — Info existiert anderswo"
```

**Archive/Delete** question:
```
AskUserQuestion:
  question: "#10608 'Session Summary v2026.03.14' — Release notes, Docker-publish bead close.
             Grund: Transient session record, info in git tags. Was tun?"
  header: "#10608"
  options:
    - label: "Archivieren"     description: "Prio auf 0.1, bleibt durchsuchbar"
    - label: "Löschen"         description: "Komplett entfernen"
    - label: "Behalten"        description: "Unverändert lassen"
```

**Batching**: Group up to 4 questions per AskUserQuestion call. If there are more
than 4 actionable items, use multiple calls. This allows the user to decide on all
items at once in a single UI interaction.

#### Promote — ALWAYS individual via AskUserQuestion

Each promote gets its own question with destination options. Show the memory text
and narrative in the question text so the user has full context:

```
AskUserQuestion:
  question: "#10564 'UV muss mit --python 3.14 gesynct werden' (learning, open-brain)
             Text: 'Bei uv sync auf dem Server muss --python 3.14 angegeben werden,
             sonst wird die System-Python verwendet...'
             Wohin materialisieren?"
  header: "Promote"
  options:
    - label: "CLAUDE.md (projekt)"   description: "Deployment-Sektion im Projekt-CLAUDE.md"
    - label: "CLAUDE.md (global)"    description: "Allgemeine UV-Konvention in ~/.claude/CLAUDE.md"
    - label: "standards/"            description: "Standard-Datei für UV-basierte Projekte"
    - label: "Bead"                  description: "Aufgabe erstellen via bd create"
```

After the user selects a destination, show the proposed text/content and get final
approval before writing.

#### Scaffold — individual via AskUserQuestion

```
AskUserQuestion:
  question: "#10570 'API rate limiting fehlt noch' (observation, open-brain)
             Vorgeschlagener Bead: 'Implement API rate limiting' (feature, P3)
             Erstellen?"
  header: "Scaffold"
  options:
    - label: "Erstellen"       description: "Bead mit vorgeschlagenem Titel/Prio anlegen"
    - label: "Ändern"          description: "Titel, Typ oder Prio anpassen vor Erstellung"
    - label: "Ablehnen"        description: "Kein Bead erstellen"
```

### Step 4: Execute Approved Actions

After the user has reviewed and approved actions, execute them:

1. **Merge**: Call `mcp__open-brain__refine_memories(scope="duplicates")` or
   `mcp__open-brain__update_memory` to update the surviving memory with merged text,
   then archive/delete the duplicate. After merging, mark the deleted memory as discarded:
   ```
   update_memory(id=<deleted-id>, metadata={'status': 'discarded', 'discard_reason': 'merged into #<surviving-id>'})
   ```

2. **Archive**: Call `mcp__open-brain__materialize_memories` with the approved archive actions.
   Then mark the memory as discarded:
   ```
   update_memory(id=<id>, metadata={'status': 'discarded', 'discard_reason': 'archived — <reason>'})
   ```

3. **Delete**: No MCP delete tool exists (by design — see decision #10597). Delete via
   REST API on the server: `ssh services 'curl -s -X DELETE "http://localhost:8091/api/memories"
   -H "Content-Type: application/json" -H "X-API-Key: <key>" -d "{\"ids\": [...]}"'`.
   Read the API key from `/opt/open-brain/.env.tpl` on the server (API_KEYS= line).
   Before deleting, mark the memory as discarded:
   ```
   update_memory(id=<id>, metadata={'status': 'discarded', 'discard_reason': 'deleted — <reason>'})
   ```

4. **Promote**: Execute the materialization to the chosen target:
   - CLAUDE.md → Use Edit tool on the target CLAUDE.md file
   - standards/ → Use Write/Edit tool on the standard file + update index.yml
   - Skill → Use Edit tool on the skill's SKILL.md
   - Bead → Run `bd create` via Bash

   After writing to the target, mark the memory as materialized:
   ```
   update_memory(id=<id>, metadata={'status': 'materialized', 'materialized_to': '<target>'})
   ```
   Where `<target>` is the human-readable destination, e.g.:
   - `"CLAUDE.md (open-brain, Deployment section)"`
   - `"~/.claude/CLAUDE.md (UV conventions)"`
   - `"standards/dev/uv.md"`
   - `"bead open-brain-xyz"`

5. **Scaffold**: Run `bd create` with the approved parameters.

After execution, call `mcp__open-brain__materialize_memories` with `dry_run=false`
for any remaining server-side state updates (priority changes, etc.).

### Step 5: Report

```
Triage complete:
- 3 kept
- 2 merged
- 2 archived
- 1 deleted
- 1 promoted → CLAUDE.md (open-brain, Deployment section)
- 1 scaffolded → bead open-brain-xyz
```

## Rules

- **NEVER execute actions without user approval** — the whole point is HITL
- **NEVER skip Step 2** (fetching full details) — the user cannot decide on IDs alone
- **Promote is ALWAYS individual** — never en-bloc, destination must be discussed
- **Scaffold is ALWAYS individual** — bead creation needs explicit approval
- **Merge must show the merge text** — user needs to see what the result will be
- **Archive vs Delete are separate categories** — don't conflate them
- **Keep needs no approval** — just list titles for transparency
- For en-bloc actions (merge/archive/delete), always offer "einzeln durchgehen" as option

## Arguments

First argument (optional) sets the scope:
- `project:<name>` — memories for a specific project
- `type:<name>` — memories of a specific type (learning, observation, session_summary, etc.)
- `scope:recent` — recent memories (default)
- `scope:low-priority` — low-priority / stale memories
- `limit:<n>` — max number of memories to triage

## Integration with learnings-pipeline

This skill replaces the `review` phase of the `learnings-pipeline` skill.
The extraction phase (`/learnings-pipeline extract`) remains unchanged but should
write to open-brain via `save_memory` instead of `learnings.jsonl`.

For migration of existing learnings.jsonl entries, use:
```
/ob-triage migrate-learnings
```
This imports all entries from `~/.claude/learnings/learnings.jsonl` into open-brain
as type "learning" and marks the JSONL entries as migrated.
