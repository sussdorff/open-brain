# Python Test Suite

## Real Postgres Integration Tests

Integration tests that need the repository-owned Postgres schema require a
fresh Postgres database with pgvector available and a caller-provided
`DATABASE_URL`. The shared `integration_database_url` fixture automatically
applies the schema in `scripts/bootstrap_test_schema.sql` before tests that use
the fixture run.

Example local database:

```bash
docker run --rm -d --name open-brain-test-pg \
  -e POSTGRES_USER=open_brain \
  -e POSTGRES_PASSWORD=test \
  -e POSTGRES_DB=open_brain_test \
  -p 55432:5432 \
  pgvector/pgvector:pg18-bookworm

export DATABASE_URL="postgresql://open_brain:test@localhost:55432/open_brain_test"
```

One-shot manual bootstrap from the repository root:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/bootstrap_test_schema.sql
```

Run the currently available real-DB schema-backed integration test:

```bash
cd python
uv run pytest -m integration tests/test_typed_relationships.py::TestBackfillScriptIntegration -v
```

When `tests/test_postgres.py::TestPostgresPoolMigrations` is present, include it
in the same command:

```bash
cd python
uv run pytest -m integration \
  tests/test_typed_relationships.py::TestBackfillScriptIntegration \
  tests/test_postgres.py::TestPostgresPoolMigrations \
  -v
```
