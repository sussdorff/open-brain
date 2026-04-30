# People Enrichment

Opt-in CLI workflow that enriches person memories (extracted from transcripts) with additional
metadata — org, role, and profile URL — sourced from SearXNG web search.

---

## How It Works

During transcript ingest, any person memory that is newly created (name only, no org/role)
gets an `enrich_pending = "true"` metadata flag. Enrichment never runs automatically;
it is triggered explicitly by the user via:

```bash
ob people enrichment
```

The command:
1. Lists all person memories with `enrich_pending = "true"` **plus** any pre-existing person
   memories that have a name but no `org` or `role` set.
2. For each candidate, runs a SearXNG web search:
   - Query: `"<name>" <context-keywords> site:linkedin.com OR site:xing.com OR company bio`
   - Context keywords are extracted from the linked meeting memory (non-name words from the
     transcript passage where the person was mentioned).
3. Presents up to 3 search results per candidate with:
   - Proposed org, role, profile URL
   - Confidence score (0.0–1.0)
   - Provenance: source URL + snippet from the search result
4. Prompts `Apply enrichment for <name>? [y/N]` (interactive) — or applies automatically
   in non-interactive mode (see below).
5. On approval: updates the existing person memory with `org`, `role`, `profile_url`,
   `confidence`, `provenance_url`, `provenance_snippet`, and `provenance`. Clears
   `enrich_pending`.

---

## Setup

### 1. SearXNG instance

You need a running [SearXNG](https://docs.searxng.org/) instance with the JSON API enabled.
The simplest setup is a local Docker container:

```bash
docker run -d --name searxng -p 8080:8080 \
  -e SEARXNG_SECRET=change-me \
  searxng/searxng
```

Verify: `curl -s "http://localhost:8080/search?q=test&format=json" | jq .`

If the response is `{"error": "..."}` or a 403, make sure `format: json` is enabled in
SearXNG's `settings.yml`:
```yaml
search:
  formats:
    - html
    - json
```

### 2. Configure the URL

**Option A — XDG client config (recommended for local use):**

```bash
# ~/.config/open-brain/config.json
{
  "server_url": "https://open-brain.sussdorff.org",
  "searxng_url": "http://localhost:8080"
}
```

**Option B — environment variable:**

```bash
export OB_SEARXNG_URL=http://localhost:8080
```

**Option C — server-side `.env` (for server deployments):**

```
SEARXNG_URL=http://searxng:8080
```

**Option D — per-invocation CLI flag:**

```bash
ob people enrichment --searxng-url http://localhost:8080
```

Resolution order: `--searxng-url` → `OB_SEARXNG_URL` → `~/.config/open-brain/config.json`
→ server `SEARXNG_URL`.

### 3. DATABASE_URL

The enrichment command reads and writes person memories **directly** (bypasses MCP), so it
needs a local `DATABASE_URL`:

```bash
export DATABASE_URL=postgresql://user:pass@localhost:5432/open_brain
```

Or add it to a `.env` file in your working directory.

---

## Usage

```bash
# Interactive review (default): y/N prompt for each candidate
ob people enrichment

# Non-interactive: auto-apply when confidence >= 0.8 AND org or role is found
ob people enrichment --auto-apply

# Lower the threshold to 0.7
ob people enrichment --auto-apply --min-confidence 0.7

# Override the SearXNG URL for this invocation
ob people enrichment --searxng-url http://localhost:8080

# Alias (shorter)
ob people enrich
```

### Example output

```
Found 3 enrichment candidate(s).

Candidate: Alice Smith (memory 1042)
  No transcript context found.
  Best match:
    Org:         Acme Corp
    Role:        CTO
    Profile URL: https://www.linkedin.com/in/alice-smith-acme
    Confidence:  0.85
    Source:      https://www.linkedin.com/in/alice-smith-acme
    Snippet:     Alice Smith · CTO at Acme Corp · Previously VP Engineering at ...
Apply enrichment for Alice Smith? [y/N] y
  Applied enrichment for Alice Smith.

Candidate: Bob Miller (memory 1055)
  Best match:
    Org:         —
    Role:        —
    Profile URL: https://www.xing.com/profile/Bob_Miller
    Confidence:  0.52
    Source:      https://www.xing.com/profile/Bob_Miller
    Snippet:     Bob Miller — XING Profil
  Skipped.

Enrichment complete: 1 applied, 1 skipped, 1 no results.
```

---

## Confidence Scoring

| Signal | Score |
|--------|-------|
| Profile URL is a LinkedIn or Xing profile | base +0.70 |
| Snippet contains exact name match | +0.15 |
| Snippet contains transcript context keyword | +0.15 |
| Maximum | 1.00 |

**Hard floors:**
- `confidence < 0.6` → **never** auto-applied, even with `--auto-apply`
- `org` and `role` both absent → never auto-applied (only org or role present qualifies)

Interactive mode (`y/N`) has no floor — the user can approve any result including
low-confidence or URL-only matches.

---

## Ingest Integration

The enrichment flag is set at ingest time for every **new** or **ambiguous** person memory
(one where the dedup system couldn't confidently match the name to an existing record).
Existing person memories that pre-date the feature (no `enrich_pending` flag) are also
surfaced if they have a `name` but no `org` or `role`.

Enrichment is always **opt-in** and **non-blocking** — ingest never waits for SearXNG and
never fails if SearXNG is unavailable.

---

## Related

- [People-Aware Memory](people-aware-memory.md) — architecture overview, ingest pipeline
- [Domain Metadata Schemas](domain-metadata-schemas.md) — `PersonMetadata` TypedDict fields
  (`name`, `org`, `role`, `profile_url`, `confidence`, `provenance`, `enrich_pending`)
