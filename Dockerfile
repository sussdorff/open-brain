# Build stage: install dependencies
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# Copy dependency files first for layer caching
COPY python/pyproject.toml python/uv.lock* ./

# Install dependencies into a virtual environment
RUN uv sync --frozen --no-dev --no-install-project

# Production stage
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application source
COPY python/src ./src

# Ensure the venv is on PATH
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"

# Default CLIENTS_FILE path for Docker (mount a volume or override via env)
ENV CLIENTS_FILE=/app/clients.json

EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8091/health')" || exit 1

CMD ["python", "-m", "open_brain"]
