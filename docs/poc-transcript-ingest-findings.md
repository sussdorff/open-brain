# PoC: Transcript Ingest — Findings

**Bead:** open-brain-cr3.8  
**Date:** 2026-04-25  
**Status:** PoC complete — ready for review before broader rollout

---

## What was built

- `TranscriptIngestor` in `python/src/open_brain/ingest/adapters/transcript.py`  
  Full pipeline: LLM extraction → person dedup → memory persistence → relationship creation.
- `ingest_transcript` MCP tool in `python/src/open_brain/server.py`  
  Exposes the ingestor as an MCP-callable tool with idempotency guarantees.
- Integration test `python/tests/test_transcript_ingest_e2e.py`  
  Runs the 3-person meeting fixture against a real PostgreSQL DB, verifies counts, rolls back.

---

## What worked well

- **Idempotency** via `sha256(source_ref + sha256(text))` key stored in meeting memory metadata.  
  Second call with identical `(source_ref, text)` returns the same IDs without new saves.
- **Person dedup** via `match_person` correctly collapses misspellings (e.g. "Allice Brown" → "Alice Brown").  
  Conservative fallback: ambiguous or new names always create new person memories; no silent merges.
- **Rollback** via `delete_by_run_id` tags every memory and relationship with `run_id` in metadata,  
  allowing clean teardown in tests and after bad ingests.
- **Separation of concerns**: extraction, dedup, and persistence are separate layers — easy to test individually.

---

## Architecture observations

### LLM extraction (`ingest/extract.py`)
- Uses Claude Haiku 4.5 with a structured JSON prompt.
- Returns `{attendees, mentioned_people, topics, follow_up_tasks}`.
- No schema validation on the LLM response; relies on `.get()` with `or []` defaults.
  - **Recommendation**: Add Pydantic validation or at minimum a JSON schema check to catch hallucinated keys.

### Person dedup (`people/dedup.py`)
- `match_person` uses fuzzy name matching (Levenshtein-based).
- Works well for single-name-field records; degrades for multi-member/hub records.
- The dedup snapshot is loaded once per ingest call (200 records max).  
  - **Known limit**: ingests against large person databases (>200) may miss existing records.
  - **Recommendation**: Increase limit or use a two-phase approach (vector search + fuzzy).

### Memory schema
- Meeting memories store the first 4000 chars of the transcript (`text[:4000]`).  
  Long meetings lose context after the cut-off.
  - **Recommendation**: Store a LLM-generated summary instead of raw excerpt for long transcripts.
- `ingest_result` is embedded in `meeting.metadata` as a nested dict.  
  This is pragmatic for PoC but may complicate future migrations.

---

## Known failure modes (from bead notes)

| Failure mode | Description | Mitigation |
|---|---|---|
| Overlapping speakers | Two people speaking at once; transcription assigns utterance to wrong speaker | Accept as noise; person memories remain valid |
| Pronoun-only references | "He said..." — no extractable name | LLM correctly omits; no phantom person created |
| Tech jargon as names | "REST" or "API" extracted as person name | LLM prompt includes disambiguation instruction; still occasionally fires |
| Long meetings (>4000 chars) | Transcript truncated before `extract_from_transcript` | Extraction uses `text` directly, not `text[:4000]` — only the stored memory is truncated |
| German/English mixing | Code-switching mid-meeting; LLM may miss German name variants | Generally handled; edge cases possible with compound names |
| Indirect-quote sentiment | "Alice said Bob was unhappy" — sentiment attributed to Bob | Not extracted as sentiment; captured only as a mention |
| MacWhisper hallucinations | Filler words / phantom sentences at start/end of file | No filtering applied; recommend strip of common hallucination patterns before ingest |

---

## Recommendations before broader rollout

1. **Add LLM response schema validation** in `extract.py` — use Pydantic or `jsonschema` to catch malformed extraction responses early.
2. **Increase person dedup limit** or switch to vector-search-assisted dedup for large person databases.
3. **Store meeting summary instead of raw excerpt** for long transcripts — prompt Haiku to summarize before saving.
4. **Pre-process MacWhisper output** — strip known hallucination patterns (e.g. leading "Thank you." lines, trailing "Transcribed by Whisper").
5. **Add structured logging** of extraction results per run — useful for debugging LLM misclassifications.
6. **Consider a `dry_run` parameter** for the MCP tool — returns what would be created without committing, useful for spot-checking extraction quality.
