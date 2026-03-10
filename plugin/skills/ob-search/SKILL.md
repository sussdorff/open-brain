---
name: ob-search
description: >
  Search open-brain memory for past observations, session summaries, and learnings.
  Use when: "search memory", "what did I learn", "past observations", "past sessions",
  "what happened before", "previous context", "recall", "remember", "erinnere dich",
  "was habe ich gelernt", "letzte Session", "memory context".
version: 0.1.0
---

# open-brain Memory Search

Efficiently search and retrieve memories from open-brain using the 3-layer workflow.

## 3-Layer Workflow (ALWAYS FOLLOW)

### Layer 1: Search (discovery)
Use `mcp__open-brain__search` to find relevant memories. Returns compact index with IDs.

```
search(query="<your search term>", project="<project name>", limit=20)
```

**Filters available:** `type`, `date_start`, `date_end`, `file_path`, `order_by`

Common types: `observation`, `session_summary`, `discovery`, `decision`, `bugfix`, `feature`, `refactor`, `change`

### Layer 2: Timeline (context)
Use `mcp__open-brain__timeline` to get context around interesting results.

```
timeline(anchor=<memory_id>, depth_before=5, depth_after=5)
```

Or by date range:
```
timeline(date_start="2026-03-01", date_end="2026-03-09", project="my-project")
```

### Layer 3: Get Observations (full details)
Use `mcp__open-brain__get_observations` to fetch full details ONLY for filtered IDs.

```
get_observations(ids=[123, 456, 789])
```

## Rules

- **NEVER skip to Layer 3** — always start with search to find relevant IDs first
- **Filter before fetching** — use search results to pick only the IDs you need
- **Token budget**: ~50-100 tokens per search result, ~500-1000 tokens per full observation
- **10x savings**: The 3-layer approach uses 10x fewer tokens than fetching everything

## Example Workflows

### "What did I learn about X?"
1. `search(query="X", type="discovery")` → get IDs
2. `get_observations(ids=[...])` → read full details

### "What happened in my last session?"
1. `search(type="session_summary", limit=3, order_by="newest")` → recent summaries
2. `get_observations(ids=[...])` → full summary text

### "Show me all decisions in project Y"
1. `search(type="decision", project="Y")` → decision index
2. `timeline(anchor=<most_relevant_id>)` → context around it
3. `get_observations(ids=[...])` → full decision details

### "What was I working on last week?"
1. `timeline(date_start="2026-03-02", date_end="2026-03-09")` → browse by date

## Other Useful Tools

- `mcp__open-brain__search_by_concept(query="...")` — pure semantic/vector search
- `mcp__open-brain__get_context(project="...")` — recent session summaries
- `mcp__open-brain__stats()` — database overview (counts, types, DB size)
