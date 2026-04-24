# ADR-0002: Credentials and Privacy Policy

**Date:** 2026-04-24
**Status:** Accepted

---

## Context

The open-brain ingest pipeline will expand to access sensitive personal data sources, including:

- IMAP/SMTP email (app passwords, OAuth tokens)
- Matrix chat (access tokens, E2E encryption keys)
- WhatsApp (session credentials)
- Gmail OAuth tokens
- Any future adapter that requires long-lived credentials for unattended access

These adapters run in a Docker container on LXC116/Proxmox. Secrets must never appear in the repository, in `settings.json`, in container environment variables persisted to disk, or in any log output. At the same time, the server must be able to resolve credentials at runtime without human interaction.

Additionally, some ingested content (e.g. emails from unknown senders, Matrix DMs) may contain personally identifiable information (PII) that warrants a quarantine step before being merged into the general memory store.

---

## Decision

### 1. 1Password CLI as the Single Secret Store

All secrets are managed exclusively through the 1Password CLI (`op`). No plaintext credentials appear in:

- The git repository (any file)
- `settings.json` / `CLAUDE.md` / any configuration file checked into version control
- Docker environment variables passed via `docker-compose.yml` (values side)
- Application log files or structured log fields

Config references store only the **op:// URI** — a pointer, never the value:

```python
# python/src/open_brain/config.py  (reference pattern, NOT real credentials)
IMAP_PASSWORD_OP: str = "op://Private/email/app-password"
MATRIX_ACCESS_TOKEN_OP: str = "op://Personal/matrix/access-token"
GMAIL_OAUTH_REFRESH_TOKEN_OP: str = "op://Personal/gmail/refresh-token"
```

Resolution at runtime:

```python
import subprocess

def resolve_op(uri: str) -> str:
    """Resolve a 1Password op:// URI to its plaintext value at runtime."""
    return subprocess.check_output(["op", "read", uri], text=True).strip()
```

The resolved value is held only in memory for the duration of the request/task; it is never stored in a database column, log entry, or file.

### 2. PII-Flag Quarantine Routing

When an extractor signals that ingested content likely contains PII (e.g. full email threads, private DMs), it sets `contains_pii_hint=true` on the memory record. The routing rule is:

| `contains_pii_hint` | Target project |
|---------------------|----------------|
| `false` (default)   | `people`       |
| `true`              | `people-quarantine` |

Content in `people-quarantine` is excluded from standard retrieval until the user explicitly reviews and approves migration to `people`. This prevents PII from leaking into general memory context without user review.

### 3. E2E Encryption Keys (Matrix)

Matrix E2E encryption keys are exported once during initial setup and stored as a 1Password secure note referenced by an op:// URI (e.g. `op://Personal/matrix/e2e-export`). Key rotation is performed manually:

1. Export new key bundle from Matrix client
2. Update the 1Password item in-place
3. Restart the ingest adapter (no code change required)

Keys are never committed to the repository and never passed as plain environment variables.

### 4. Audit Logging

Every credential resolution is recorded in the `pipeline_runs` table:

```sql
-- Schema excerpt (audit columns)
adapter          TEXT NOT NULL,
credential_ref   TEXT NOT NULL,   -- op:// URI (the pointer, never the value)
resolved_at      TIMESTAMPTZ NOT NULL DEFAULT now()
```

The application logs a structured entry on each `op read` call:

```python
logger.info(
    "credential_resolved",
    adapter=adapter_name,
    credential_ref=op_uri,   # e.g. "op://Private/email/app-password"
    # NOTE: never log the resolved value
)
```

**It is a hard rule: the resolved secret value must never appear in any log line, structured field, or audit record.**

---

## Implementation Impact

Bead **cr3.4** (email ingest adapter) is the first concrete implementation of this policy. It introduces the `config.py` reference pattern described above: each secret is represented by a `*_OP` field containing its op:// URI, and the adapter resolves the value at startup via `subprocess.check_output(["op", "read", ...])`.

All subsequent ingest adapters (Matrix, WhatsApp, Gmail) must follow the same pattern established in cr3.4.

---

## Consequences

**Positive:**

- Secrets are never in the codebase or container environment in plaintext — a git leak or `docker inspect` reveals only op:// URIs, which are useless without 1Password access.
- PII quarantine gives users a review gate before sensitive content enters the general memory store.
- The audit log in `pipeline_runs` provides a full trail of which adapters accessed which credential references and when.
- Adding new credentials requires only updating 1Password and adding a new `*_OP` field to `config.py` — no infrastructure changes.

**Trade-offs / Constraints:**

- The `op` CLI must be installed and authenticated on the production host (LXC116). The deployment runbook must include a step to verify `op signin` is active before starting the ingest service.
- If the `op` session expires, ingest adapters will fail at credential resolution time. Monitoring should alert on `subprocess.CalledProcessError` from `op read` calls.
- PII quarantine adds manual review overhead. A future bead may automate approval workflows, but the default must remain "quarantine first."
- E2E key rotation for Matrix is a manual process; teams must document the rotation procedure and schedule periodic reviews.
