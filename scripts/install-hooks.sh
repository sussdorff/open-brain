#!/bin/bash
# Install git hooks for open-brain development
# Run this once after cloning the repository.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Support both regular repos (.git dir) and git worktrees (.git file pointing elsewhere)
GIT_COMMON_DIR="$(git -C "$REPO_ROOT" rev-parse --git-common-dir)"
HOOKS_DIR="$GIT_COMMON_DIR/hooks"

if [ ! -d "$HOOKS_DIR" ]; then
    echo "ERROR: .git/hooks directory not found at $HOOKS_DIR. Are you in a git repository?"
    exit 1
fi

echo "Installing pre-commit hook..."

cat > "$HOOKS_DIR/pre-commit" << 'EOF'
#!/bin/bash
# pre-commit hook: run fast test suite before every commit
# Skips integration tests that require external services (Postgres, Voyage API)

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"

echo "Running test suite (not integration)..."
cd "$REPO_ROOT/python" && uv run pytest -m "not integration" -q --tb=short

echo "Tests passed."
EOF

chmod +x "$HOOKS_DIR/pre-commit"

echo "Done. Pre-commit hook installed at $HOOKS_DIR/pre-commit"
echo "On every commit, 'uv run pytest -m \"not integration\"' will run automatically."
