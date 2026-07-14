---
name: ob-triage
description: >-
  use when: manually reviewing open-brain memories with human-in-the-loop
  approval — classify keep/merge/archive/delete/promote, route promotes to
  marketplace primitives via the matching *-forge skill, scaffold beads from
  observations.
  NOT for: scheduled or silent lifecycle maintenance (use memory-heartbeat);
  writing new memories (use ob-migrate or ingest-content).
  boundary: ob-triage is interactive (AskUserQuestion per action); memory-
  heartbeat is autonomous (cron-triggered, silent when QUIET).
version: 0.3.0
requires_standards:
  - open-brain/cli-routing
  - open-brain/memory-status-conventions
requires:
  - standard:english-only
  - mcp:open-brain
  - skill:library
  - skill:skill-forge
  - skill:standard-forge
  - skill:agent-forge
  - skill:hook-forge
  - skill:script-forge
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

First call `mcp__open-brain__list_lifecycle_actions(state="staged")`. These are
proposals prepared by the heartbeat and must be reviewed before generating new
classifications. If staged actions exist, continue with those records at Step 2.

If no staged actions exist, call `mcp__open-brain__triage_memories` with the
user's scope and `dry_run=true`.

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

#### Promote — multi-stage via AskUserQuestion

Promote routes a memory into a **marketplace primitive** owned by the library platform.
CLAUDE.md and AGENTS.md are entrypoints/imports, **not** promotion destinations
(narrow escape hatch in Stage B fallback only).

Each promote runs three stages individually:

**Stage A — Classify the primitive type**

```
AskUserQuestion:
  question: "#10564 'UV muss mit --python 3.14 gesynct werden' (learning, open-brain)
             Text: 'Bei uv sync auf dem Server muss --python 3.14 angegeben werden,
             sonst wird die System-Python verwendet...'
             Welches Primitive passt?"
  header: "Primitive type"
  options:
    - label: "Standard"   description: "Faktische Regel/Referenz, über Trigger geladen"
    - label: "Skill"      description: "Wiederverwendbarer Workflow"
    - label: "Guardrail"  description: "Lifecycle-Hook / Enforcement"
    - label: "Discard"    description: "Kein Primitive — Memory behalten oder verwerfen"
```

Library primitive types (per `library/meta/library.yaml`): `skill`, `agent`, `standard`,
`guardrail`, `prompt`, `script`, `model-standard`, `golden-prompt`. Surface the 3 most
likely; the user can pick "Other" for the rest.

**Stage B — Marketplace lookup (NOT classified by ob-triage)**

Call the library to get ranked writable marketplaces for this primitive type + topic:

```bash
lib catalog match \
  --primitive-type=<chosen> \
  --topics=<topics extracted from memory> \
  --writable-only --json
```

Branch on the result:

- **1 confident candidate** → proceed silently to Stage C
- **Multiple tied candidates** → AskUserQuestion with the candidates returned by `lib`
  as options (e.g. `cognovis-core`, `sussdorff-core`, `open-brain`)
- **0 candidates** → fallback AskUserQuestion:
  - "Pick a writable marketplace anyway" → options from `lib catalog list --writable --json`
  - "CLAUDE.md one-liner (escape hatch)" → only for project-hard-facts that cannot
    become a primitive — should be rare
  - "Discard" / "Keep as memory"

ob-triage must **never** hard-code marketplace names or topic-to-marketplace mappings.
That logic lives in `library/meta` (see bead CL-744).

**Stage B.1 — Third-party (read-only) source handling**

If `lib <prim> list --name=<x>` shows the existing primitive came from a `writable: false`
catalog (e.g. `cognovis-samurai`, `anthropic-official`), ask the user:

```
AskUserQuestion:
  question: "Skill 'aidbox' kommt aus cognovis-samurai (read-only). Forken nach cognovis-core
             und dort die Learning anwenden?"
  header: "Fork upstream"
  options:
    - label: "Forken"        description: "Kopie in writable marketplace, swap install pointer"
    - label: "Überspringen"  description: "Upstream unverändert, Memory bleibt offen"
```

If user accepts: dispatch `/library <prim> fork <name> --to=<writable-marketplace>` and
then proceed to Stage C against the forked copy. The fork mechanics are owned by the
`library` skill, not ob-triage.

**Stage C — Show diff and get final approval**

Show the proposed file content (new primitive or patch to existing) and ask for final
approval before writing. Existing primitive → edit in place; new primitive → forge
scaffold. No "merge editor" — Edit applies the change directly.

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

After the user has reviewed and approved actions, execute them. Every status
write follows the `update_memory` patterns in the
`open-brain/memory-status-conventions` standard.

For actions loaded from `list_lifecycle_actions`, call
`set_lifecycle_action_state` after the decision: `resolved` after a successful
approved action or keep decision, `needs_review` when deferred, and `failed`
when execution fails. Include a concise resolution note naming any external
effect. `resolved` records the review outcome; it is not proof that the ledger
tool executed an effect. This transition does not replace the memory
`metadata.status` write described below.

1. **Merge**: Call `mcp__open-brain__refine_memories(scope="duplicates")` or
   `mcp__open-brain__update_memory` to update the surviving memory with merged
   text. Then mark the deleted memory as discarded with
   `discard_reason='merged into #<surviving-id>'`.

2. **Archive**: Call `mcp__open-brain__materialize_memories` with the approved
   archive actions, then mark the memory discarded with
   `discard_reason='archived — <reason>'`.

3. **Delete**: No MCP delete tool exists (by design — see decision #10597).
   Delete via the server REST API:

   ```
   ssh services 'curl -s -X DELETE "http://localhost:8091/api/memories" \
     -H "Content-Type: application/json" -H "X-API-Key: <key>" \
     -d "{\"ids\": [...]}"'
   ```

   Read the API key from `/opt/open-brain/.env.tpl` on the server (`API_KEYS=`
   line). Mark the memory discarded with
   `discard_reason='deleted — <reason>'` before the REST call.

4. **Promote**: Delegate to the matching forge skill — **never write primitive files
   directly**. Forges already own create *and* update conventions, frontmatter shape,
   and validation. ob-triage stays thin: classify → route → delegate → commit.

   | Primitive type   | Forge skill     | Mode                |
   |------------------|-----------------|---------------------|
   | skill            | `skill-forge`   | create or update    |
   | standard         | `standard-forge`| create or update    |
   | agent            | `agent-forge`   | create or update    |
   | guardrail / hook | `hook-forge`    | create or update    |
   | script           | `script-forge`  | create or update    |
   | prompt           | (no forge yet — use Edit/Write directly on the marketplace clone) |
   | model-standard, golden-prompt | (no forge yet — Edit/Write directly) |

   Pass to the forge: the marketplace's `local_path` (from `lib catalog match`), the
   memory content, and mode (`create` if new, `update` if `lib <prim> list --name=<x>`
   returns a hit in the chosen marketplace).

   After the forge has written the file, commit in the marketplace repo (no auto-push):

   ```bash
   git -C <marketplace.local_path> add <relative-path-inside-marketplace>
   git -C <marketplace.local_path> commit -m "<primitive-type>(<name>): from memory #<id>"
   ```

   Then update the memory with `status: materialized` and
   `materialized_to: "<marketplace>/<primitive-type>/<name>"` per the
   status-conventions standard. Examples:

   - `"cognovis-core/standards/python-uv-sync"`
   - `"sussdorff-core/skills/terminal-cleanup"`
   - `"open-brain/hooks/memory-heartbeat"`

   **Never edit `library/meta/library.yaml`** from this skill — catalog
   inventory refresh is `lib catalog sync`'s job (see bead CL-744).

   Fallback (rare): if Stage B fell through to the CLAUDE.md one-liner escape
   hatch, record as `materialized_to: "CLAUDE.md (<file>): <one-liner summary>"`
   and skip the forge + commit steps.

5. **Scaffold**: Run `bd create` with the approved parameters.

After execution, call `mcp__open-brain__materialize_memories` with `dry_run=false`
for any remaining server-side state updates (priority changes, etc.).

### Step 5: Report

Include a **Pending commits** section listing per-marketplace commits that are staged
but not pushed — the user (or session-close) decides when to push.

```
Triage complete:
- 3 kept
- 2 merged
- 2 archived
- 1 deleted
- 1 promoted → cognovis-core/standards/python-uv-sync
- 1 promoted → open-brain/skills/ob-search (update)
- 1 scaffolded → bead open-brain-xyz

Pending commits (no auto-push):
- ~/code/library/cognovis-core: 1 commit
- ~/code/open-brain:            1 commit
```

## Rules

- **NEVER execute actions without user approval** — the whole point is HITL
- **NEVER skip Step 2** (fetching full details) — the user cannot decide on IDs alone
- **Promote routes to marketplaces, never to CLAUDE.md / AGENTS.md** — CLAUDE.md one-liners
  are a narrow Stage-B escape hatch for project-hard-facts only
- **Marketplace selection is a lookup, not a decision** — always go through
  `lib catalog match`; never hard-code marketplace names or topic mappings here
- **Always delegate file create/update to the matching `*-forge` skill** — ob-triage
  does not write SKILL.md / standard / agent / hook frontmatter or validation logic
- **One commit per promote in the marketplace repo, no auto-push** — surface pending
  commits per marketplace in Step 5
- **Never edit `library/meta/library.yaml`** — catalog inventory is `lib catalog sync`'s job
- **Promote is ALWAYS individual** — never en-bloc, marketplace + content must be approved
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

## Learning Lifecycle

open-brain is the Single Source of Truth for all learnings. The full lifecycle of a learning:

```
save_memory(type="learning")          →  status: open (default)
      │
      ▼
/ob-triage type:learning              →  review each learning interactively
      │
      ├─ Promote → materialized      →  Stage A: classify primitive type
      │                                 Stage B: lib catalog match → writable marketplace
      │                                 Stage C: <type>-forge writes/updates in marketplace clone
      │                                 git commit in marketplace repo (no auto-push)
      │                                 update_memory(status='materialized',
      │                                               materialized_to='<marketplace>/<type>/<name>')
      │
      └─ Discard → discarded         →  update_memory(status='discarded',
                                                       discard_reason='<reason>')
                   Archive or delete the memory
```

### Workflow for `/ob-triage type:learning`

When the user runs `/ob-triage type:learning`, the triage presents all learnings with
`status: open` for review. Each learning gets its own `AskUserQuestion` call (Promote flow,
see above) because learnings always need an explicit materialization target or discard reason.

**Never batch learnings** — each one must be individually reviewed and routed to one of:
- A marketplace primitive (skill / standard / agent / guardrail / prompt / script /
  model-standard / golden-prompt) via the matching forge, written into a writable
  marketplace chosen by `lib catalog match`
- A bead (for actionable follow-up work, via `bd create`)
- Discarded (if superseded, duplicate, or no longer relevant)
- (Narrow exception) A CLAUDE.md / AGENTS.md one-liner — only for project-hard-facts
  that cannot reasonably become a primitive (deployment URL, language hard-boundary, etc.)

After every promote or discard, update the memory's `status` metadata so it no longer
appears in future `/ob-triage type:learning` runs.

## Integration with learning extraction

open-brain is the Single Source of Truth for all learnings. The `learning-extractor`
agent writes new learnings directly to open-brain via `save_memory(type="learning")`.
The review phase is handled entirely by this skill (`/ob-triage type:learning`).
