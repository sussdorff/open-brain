---
name: ob-smart-search
description: >-
  Find symbols (functions, classes, methods) across a codebase using AST analysis.
  Use when: "where is X defined", "find function X", "smart_search", "find symbol",
  "where is class X", "suche Funktion", "wo ist X definiert".
---

# ob-smart-search

Find Python symbols via AST, with grep-based fallback for TypeScript/JavaScript/Go/Rust.

## When to Use

- "Where is `process_data` defined?"
- "Find all classes named `Repository`"
- "smart_search for `get_user`"
- "Which file defines `MyComponent`?"

Do NOT use for full-text search (use Grep instead). Use for symbol lookup by name.

## Main Procedure

<search>
Run the smart_search script:

```bash
cd <project_root>
uv run python plugin/scripts/smart_search.py "<query>" --path=<search_dir> [--pattern=**/*.py] [--max-results=20]
```

Output is a JSON array:
```json
[{"file": "src/service.py", "line": 42, "type": "function", "name": "process_data", "signature": "process_data(items: list) -> dict", "context": ""}]
```
</search>

## Output Interpretation

| Field | Meaning |
|-------|---------|
| `file` | Absolute path to file |
| `line` | 1-based line number |
| `type` | `function`, `class`, `method`, `arrow_function`, `interface`, `struct` |
| `name` | Symbol name |
| `signature` | Full signature with type annotations |
| `context` | Parent class name (for methods) |

Ranking: exact match > prefix match > substring match (case-insensitive).

## Patterns

```bash
# Find a Python function
uv run python plugin/scripts/smart_search.py "process_data" --path=.

# Find TypeScript symbols (grep fallback when tree-sitter unavailable)
uv run python plugin/scripts/smart_search.py "fetchUser" --path=src --pattern="**/*.ts"

# Limit results
uv run python plugin/scripts/smart_search.py "handle" --max-results=5
```

## Graceful Fallback

- Python files: always uses AST (no external dependencies)
- TS/JS/Go/Rust files: tries `ts parse` (tree-sitter CLI), falls back to regex patterns
- No crash when tree-sitter is absent — Python-only mode works everywhere

## Limitations

- Does not search inside function bodies (only symbol definitions)
- tree-sitter integration is basic — regex fallback may miss arrow functions in complex expressions
- Not for searching variable values or string literals (use Grep)
