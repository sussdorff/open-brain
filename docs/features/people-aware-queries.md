# People-Aware Queries

Three MCP tools for querying person-centric data: meetings attended, stale contacts, and mention frequency.

## Overview

These tools provide structured, cross-client query surfaces for people-related data stored in open-brain. They return structured dicts — formatting is left to the caller (see the `people-query` skill for Claude Code).

## Tool Reference

### `people_discussed_with`

Return meetings and mentions linked to a given person memory, sorted by date descending.

**Parameters:**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `person_id` | `int` | required | The memory ID of the person |
| `since` | `str \| None` | `None` | ISO date lower bound (e.g. `"2026-01-01"`) |
| `limit` | `int` | `20` | Maximum results |

**Returns:** List of `{memory_id, title, date, link_type}`

**Example input:**
```json
{"person_id": 42, "since": "2026-01-01", "limit": 10}
```

**Example output:**
```json
[
  {"memory_id": 101, "title": "Q1 Planning Meeting", "date": "2026-03-15T10:00:00+00:00", "link_type": "attended_by"},
  {"memory_id": 87,  "title": "Product sync notes",  "date": "2026-02-20T14:00:00+00:00", "link_type": "mentioned_in"}
]
```

**Implementation:** Uses `traverse(anchor_id=person_id, link_types=["attended_by", "mentioned_in"], direction="inbound")` to find source memories, then fetches and filters them.

---

### `people_stale_contacts`

Return person memories whose `last_contact` metadata is older than `min_days` or absent.

**Parameters:**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `min_days` | `int` | `90` | Minimum age in days to be considered stale |
| `limit` | `int` | `50` | Maximum results |

**Returns:** List of `{memory_id, title, last_contact, days_stale}`

- `last_contact`: ISO datetime string, or `null` if never set
- `days_stale`: integer days since last contact, or `null` if no date

**Example input:**
```json
{"min_days": 90, "limit": 20}
```

**Example output:**
```json
[
  {"memory_id": 200, "title": "Bob Smith",   "last_contact": null,                    "days_stale": null},
  {"memory_id": 201, "title": "Carol Jones", "last_contact": "2024-06-15T00:00:00+00:00", "days_stale": 679}
]
```

**Implementation:** Queries `memories WHERE type='person' AND (last_contact IS NULL OR last_contact < NOW() - interval)`. Sorted oldest first.

---

### `people_mentions_window`

Aggregate how many times each person was mentioned (via `mentioned_in` or `attended_by` edges) in the last N days.

**Parameters:**

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `days` | `int` | `30` | Look-back window in days |
| `min_count` | `int` | `1` | Minimum mention count to include |

**Returns:** List of `{person_id, mention_count, last_mentioned_at}`

**Example input:**
```json
{"days": 30, "min_count": 2}
```

**Example output:**
```json
[
  {"person_id": 300, "mention_count": 5, "last_mentioned_at": "2026-04-20T10:00:00+00:00"},
  {"person_id": 301, "mention_count": 3, "last_mentioned_at": "2026-04-18T09:30:00+00:00"}
]
```

**Implementation:** Aggregates `memory_relationships JOIN memories` where `link_type IN ('mentioned_in', 'attended_by')` and `created_at >= NOW() - interval`. Groups by `target_id` (the person).

---

## Usage from Claude Code

Use the `people-query` skill (at `~/.claude/skills/open-brain/people-query/SKILL.md`). Trigger phrases:

- "what did I discuss with …" / "worüber habe ich mit … gesprochen"
- "stale contacts" / "wen habe ich lange nicht gesehen"
- "mentions of …" / "who mentioned …"

The skill resolves person names to IDs via `search(type='person')` and formats the response as a markdown table.

## Schema Notes

- All tools return `str` (JSON-serialized) from the MCP perspective
- Dates are ISO 8601 strings (`created_at` with timezone)
- `person_id` in `people_mentions_window` is the `target_id` of the edge (the person memory ID)
- Tools never return prose — formatting belongs to the caller
