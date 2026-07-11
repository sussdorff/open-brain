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

## Notes and Limitations

- The importer is a standalone migration module. It does not implement the ingest adapter
  registry protocol; for interactive single-note capture, use `ob-migrate`.
- Wikilink resolution is vault-local. Cross-vault links are reported as unresolved.
- The `--apply` flag is required for any database writes. Without it, the command is always
  a safe read-only dry run.
- Re-running `--apply` on a partially-imported vault is safe: already-imported notes are
  detected as duplicates and skipped.
