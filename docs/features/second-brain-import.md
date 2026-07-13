# Second Brain Vault Importer

Deterministic bulk importer for historical Obsidian/Markdown vault notes. Designed for one-shot
or repeated migration of an existing Second Brain vault into open-brain memory, with idempotent
apply semantics and a machine-readable reconciliation report.

---

## When to Use This

Use the vault importer when you want to migrate a collection of Markdown notes (Obsidian vault
or compatible format) into open-brain in bulk. For single-note or interactive capture, use the
`ob-migrate` skill instead.

---

## CLI Reference

```bash
python -m open_brain.second_brain_import <vault_path> \
    [--apply] \
    [--paperless-map <mapping.json>] \
    [--output <report.json>]
```

| Argument | Description |
|---|---|
| `vault_path` | Path to the vault root directory (required) |
| `--apply` | Write importable notes to the database. Omit for a dry run (default). |
| `--paperless-map` | Path to a JSON file mapping attachment filenames to Paperless document IDs. |
| `--output` | Write the reconciliation report to this file in addition to stdout. |

### Examples

Dry run — inspect what would be imported without writing anything:

```bash
python -m open_brain.second_brain_import ~/Documents/MyVault
```

Apply — write importable notes, resolve wikilinks and attachments:

```bash
python -m open_brain.second_brain_import ~/Documents/MyVault \
    --apply \
    --paperless-map paperless-mapping.json \
    --output import-report.json
```

---

## How It Works

### 1. Vault parsing

The importer recursively scans the vault directory for `.md` files. Each file is parsed:

- YAML frontmatter (between `---` fences) is extracted and stored as memory metadata.
  YAML date/datetime values are normalized to ISO strings before storage.
- The `type` frontmatter key, if present, is used as the memory type. Daily notes
  (filename matching `YYYY-MM-DD`) default to type `journal`.
- Filesystem timestamps are recorded as `created_at` / `updated_at` provenance.
- Obsidian wikilinks (`[[Target]]`) are collected from the note body.
- Attachment references (embedded images, PDFs, office files via `![[...]]` or
  standard Markdown links) are collected separately.

### 2. Duplicate detection

Before importing, the importer checks the database for existing memories with the same
`source_ref` (the note's relative POSIX path within the vault). Matched notes are reported
as `duplicate` and skipped regardless of `--apply`.

### 3. Wikilink resolution

Each collected wikilink target is matched against the parsed vault index by stem name
(case-insensitive, ignoring `.md` suffix, Obsidian aliases, and heading anchors). Resolved
links become typed open-brain relationships. Unresolvable targets are listed in the
`unresolved_wikilinks` section of the reconciliation report; they do not block the import
of the source note.

The relationship type used for wikilinks is `references` (added to the open-brain typed
relationship vocabulary in this release).

### 4. Attachment handling

Attachment references (from frontmatter `attachments:` lists, embedded wikilinks to binary
files, or standard Markdown links to files with attachment suffixes) are resolved via the
optional `--paperless-map` file. The mapping is a JSON object:

```json
{
  "filename.pdf": 42,
  "scan2024.jpg": 187
}
```

Where values are Paperless-ngx document IDs. Resolved attachments are stored as
`paperless_reference` domain-metadata entries on the note's memory. Unresolved attachments
are listed in the `unresolved_attachments` section of the report and skipped without blocking
the note itself.

---

## Reconciliation Report

The importer always emits a JSON reconciliation report to stdout. Structure:

```json
{
  "applied": true,
  "counts": {
    "importable": 12,
    "duplicate": 3,
    "skipped": 1,
    "unresolved_wikilinks": 2,
    "unresolved_attachments": 0
  },
  "items": [
    {
      "source_ref": "notes/project-alpha.md",
      "action": "import",
      "title": "Project Alpha",
      "memory_id": "abc123"
    },
    {
      "source_ref": "notes/old-note.md",
      "action": "duplicate",
      "existing_id": "xyz789"
    }
  ],
  "unresolved_wikilinks": ["NonExistentPage", "AnotherMissingNote"],
  "unresolved_attachments": []
}
```

`action` is one of:
- `import` — note was (or would be, in dry run) written to the database.
- `duplicate` — note already exists; skipped.
- `skip` — note could not be parsed safely (reason included in the report item).

---

## Supported Frontmatter Keys

| Key | Effect |
|---|---|
| `type` | Sets the memory type (e.g. `journal`, `meeting`, `concept`). |
| `title` | Overrides the note title (defaults to the filename stem). |
| `attachments` | List of attachment filenames to resolve via Paperless. |
| Any other key | Preserved verbatim in memory `metadata`. |

---

## Cutover Verifier

Before archiving the legacy Second Brain vault, run the cutover verifier to confirm that
all prerequisite Open Brain capabilities are live, all Paperless references resolve, the
vault importer reports nothing outstanding, and the portable backup round-trip passes.

```bash
python scripts/verify_second_brain_cutover.py \
    --vault-path ~/Documents/MyVault \
    --paperless-probe-id 42 \
    --paperless-probe-id 187 \
    --report-path artifacts/second-brain-cutover-report.json
```

Exit code `0` means all four gates are green and the vault is safe to archive. Exit code
`1` means at least one gate is red; inspect the report file for which gate failed and why.
Exit code `2` indicates a verifier-level error (misconfiguration, unreachable data layer).

**Current limitation:** the CLI entry point above does not yet wire a restore-safe
`backup_store_factory` into `run_cutover()`, so the `backup_round_trip` gate always reports
red when run via this exact command today — this is the intentional fail-closed default
(the live production Open Brain database must never be a restore target). A safe backup
store factory (e.g. an ephemeral pgvector container) needs to be wired before the CLI can
produce an all-green result; that wiring is planned as part of the Second Brain archival
follow-up work. Until then, use `run_cutover()` as a library call with an injected
`backup_store_factory` to exercise a real green run, or rely on the hermetic fixture test
in `python/tests/test_verify_second_brain_cutover.py` as the executed evidence for this gate.

### Gates

| Gate | What it checks |
|---|---|
| `open_brain_capabilities` | Seven prerequisite features are live: canonical entities, vocabulary, capture inbox, daily/weekly review, Paperless resolution, vault import, portable backup. |
| `paperless_references` | Every `--paperless-probe-id` resolves successfully against the live Paperless-ngx instance. |
| `migration_reconciliation` | Dry-run of the vault importer reports zero importable notes, zero unresolved wikilinks, zero unresolved attachments, and zero skipped notes. |
| `backup_round_trip` | Export → restore → verify round-trip over a temporary directory passes with a non-empty closure. |

Each gate fails independently; the overall status is green only when all four are green.

### Report schema

The verifier writes a `cutover-report.v1` JSON report after every run. The report contains
only redacted aggregate counts — no raw content, no PII, no credentials. The committed
evidence artifact is at `artifacts/second-brain-cutover-report.json`.

```json
{
  "schema_version": "cutover-report.v1",
  "overall_status": "green",
  "gates": [
    {
      "id": "open_brain_capabilities",
      "status": "green",
      "counts": { "required": 7, "satisfied": 7, "missing": 0, "stats_memories": 1234 },
      "detail": "required capabilities verified"
    }
  ],
  "meta": {
    "generated_at": "2026-07-12T10:00:00+00:00",
    "open_brain_git_sha": "abc1234",
    "verifier_version": "20260712.1"
  }
}
```

### CLI reference

| Argument | Description |
|---|---|
| `--vault-path` | Path to the Second Brain vault directory (required). |
| `--paperless-probe-id` | Paperless document ID to resolve; repeat for multiple probes (required). |
| `--paperless-mapping-path` | Optional JSON file mapping attachment filenames to Paperless document IDs, passed to the migration reconciliation gate. |
| `--required-capability` | Override the default set of seven required capability IDs; accepts any of the `REQUIRED_CAPABILITY_IDS` identifiers. |
| `--report-path` | Path for the output redacted report JSON (required). |

---

## Notes and Limitations

- The importer is a standalone migration module. It does not implement the ingest adapter
  registry protocol; for interactive single-note capture, use `ob-migrate`.
- Wikilink resolution is vault-local. Cross-vault links are reported as unresolved.
- The `--apply` flag is required for any database writes. Without it, the command is always
  a safe read-only dry run.
- Re-running `--apply` on a partially-imported vault is safe: already-imported notes are
  detected as duplicates and skipped.
