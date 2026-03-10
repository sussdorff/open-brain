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

### Step 3: Present Results by Action Type

Group results and present in this order:

#### Keep (no approval needed)
Brief summary only:
```
**Keep** (3 memories): "Deploy setup notes", "Voyage API config", "Auth flow docs"
```

#### Merge — show details, ask en-bloc or individual

**En-bloc view:**
```
**Merge** (2 pairs):

1. "Deploy-Probleme open-brain" (#10612) + "uv PATH auf Server" (#10606)
   → Grund: Both describe server deployment quirks

2. "Voyage API retry" (#10590) + "Embedding-Fehler bei leeren Strings" (#10585)
   → Grund: Both cover Voyage-4 error handling

Alle mergen oder einzeln durchgehen?
```

**Individual view** (when user chooses "einzeln"):
```
── Merge 1 ──────────────────────────────
Source A (#10612): "Deploy-Probleme open-brain"
  Text: Nach deploy.sh dropped MCP connection. Muss /mcp reconnect...

Source B (#10606): "uv PATH auf Server"
  Text: uv ist unter ~/.local/bin/uv, deploy script setzt PATH...

Vorgeschlagener Merge-Text:
  "Deployment open-brain: Nach deploy.sh dropped MCP connection,
   muss /mcp reconnect. uv unter ~/.local/bin/uv — deploy script
   setzt PATH aber manuelle SSH-Commands brauchen full path..."

→ [Annehmen / Ablehnen / Ändern]
```

To generate the merge text: call `mcp__open-brain__refine_memories` with
`scope="duplicates"` or construct the merged text from both sources, keeping
all unique information and removing redundancy.

#### Archive — show with reason

```
**Archive** (2 memories — remain searchable at low priority):

1. (#10608) "Session Summary 2026-03-08" — session_summary, 2 days old
   → Grund: Session summary, context already captured in project memory

2. (#10561) "Initial server setup notes" — observation, 30 days old
   → Grund: Setup complete, info now in CLAUDE.md

Archive all or review individually?
```

#### Delete — show with reason (separate from archive!)

```
**Delete** (1 memory — permanently removed):

1. (#10555) "test observation" — observation
   → Grund: Test data, no meaningful content

Delete? [Ja / Nein / Einzeln]
```

#### Promote — ALWAYS individual HITL

For each promote action, show the memory AND discuss the destination:

```
**Promote** (#10564): "UV muss mit --python 3.14 gesynct werden"
  Type: learning | Project: open-brain
  Text: "Bei uv sync auf dem Server muss --python 3.14 angegeben werden,
         sonst wird die System-Python verwendet..."

  Empfohlenes Ziel: CLAUDE.md (Deployment-Sektion)

  Mögliche Ziele:
  - CLAUDE.md (projekt) → Deployment-Hinweis
  - CLAUDE.md (global) → Allgemeine UV-Konvention
  - standards/ → Standard für UV-basierte Projekte
  - Skill → z.B. project-setup Skill erweitern
  - Bead → Aufgabe erstellen (bd create)

  Wohin materialisieren?
```

Wait for the user to choose. Then:
- **CLAUDE.md** → Show the proposed text addition and target section. User approves before writing.
- **standards/** → Show proposed standard content. User approves file path + content.
- **Skill** → Show which skill and what to add. User approves.
- **Bead** → Show proposed `bd create` command. User approves before execution.

#### Scaffold — ALWAYS individual HITL

```
**Scaffold** (#10570): "API rate limiting fehlt noch"
  Type: observation | Project: open-brain

  Vorgeschlagener Bead:
    Title: "Implement API rate limiting for open-brain"
    Type: feature
    Priority: 3
    Description: "..."

  Erstellen? [Ja / Nein / Ändern]
```

### Step 4: Execute Approved Actions

After the user has reviewed and approved actions, execute them:

1. **Merge**: Call `mcp__open-brain__refine_memories(scope="duplicates")` or
   `mcp__open-brain__update_memory` to update the surviving memory with merged text,
   then archive/delete the duplicate.

2. **Archive**: Call `mcp__open-brain__materialize_memories` with the approved archive actions.

3. **Delete**: Call `mcp__open-brain__update_memory` to delete, or use materialize with
   a delete action if supported. If not supported via MCP, inform the user.

4. **Promote**: Execute the materialization to the chosen target:
   - CLAUDE.md → Use Edit tool on the target CLAUDE.md file
   - standards/ → Use Write/Edit tool on the standard file + update index.yml
   - Skill → Use Edit tool on the skill's SKILL.md
   - Bead → Run `bd create` via Bash

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
