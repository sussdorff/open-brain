# Contributing to open-brain

Thank you for your interest in contributing to open-brain! This document provides guidelines for contributing to the project.

## Getting Started

### Prerequisites

- Python 3.13+ (3.14 recommended)
- [uv](https://docs.astral.sh/uv/) for dependency management
- Postgres 17+ with pgvector extension (or use Docker)
- A [Voyage AI](https://www.voyageai.com/) API key for embeddings

### Development Setup

```bash
# Clone the repo
git clone https://github.com/sussdorff/open-brain.git
cd open-brain

# Install dependencies
cd python
uv sync --dev

# Run tests (no external services needed)
uv run pytest -m "not integration"

# Run all tests (requires VOYAGE_API_KEY)
VOYAGE_API_KEY=your-key uv run pytest
```

### Running Locally with Docker

```bash
cp .env.example .env
# Edit .env with your API keys and secrets
docker compose up -d
curl http://localhost:8091/health
```

## Making Changes

### Branch Naming

Use the pattern: `type/short-description`

Examples: `feat/timeline-pagination`, `fix/search-empty-query`, `docs/architecture`

### Code Style

- Follow existing patterns in the codebase
- Use type hints for function signatures
- Keep functions focused and testable
- No unnecessary abstractions — simple and direct

### Testing

All changes should include tests. The test suite uses pytest with mocked asyncpg for unit tests.

```bash
cd python

# Run all unit tests
uv run pytest -m "not integration"

# Run a specific test file
uv run pytest tests/test_search.py

# Run with verbose output
uv run pytest -v
```

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(search): add date range filter to hybrid search
fix(triage): exclude materialized memories from queries
docs(readme): update quick start instructions
```

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes with tests
3. Ensure all tests pass: `uv run pytest -m "not integration"`
4. Submit a PR with a clear description of what and why
5. Address any review feedback

### PR Description Template

```
## Summary
Brief description of the change.

## Motivation
Why is this change needed?

## Test Plan
How was this tested?
```

## Architecture Overview

See [docs/architecture.md](docs/architecture.md) for a detailed overview of the system design, including:
- Hybrid search algorithm (pgvector + tsvector + RRF)
- Memory lifecycle (save → embed → search → refine → triage)
- OAuth 2.1 authentication flow

## Reporting Issues

- Use GitHub Issues for bug reports and feature requests
- Include reproduction steps for bugs
- For security vulnerabilities, see [SECURITY.md](SECURITY.md)

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
