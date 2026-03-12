---
name: ob-smart-unfold
description: >-
  Extract a complete function or class from a file, AST-bounded (no mid-function cuts).
  Use when: "unfold symbol", "show function X", "smart_unfold", "extract class X",
  "show me the implementation of X", "zeige Funktion X", "entfalte Symbol".
---

# ob-smart-unfold

Extract exactly one symbol (function or class) from a file — complete and AST-bounded.
WHY: Avoids reading entire large files when you only need one specific symbol.

## When to Use

- "Show me the implementation of `process_data`"
- "Unfold `UserRepository` class"
- "smart_unfold `handle_request` in server.py"
- After `smart_outline` identified a symbol you want to inspect

Do NOT use when you need to see multiple symbols — use `smart_outline` + Read for that.

## Main Procedure

<unfold>
Run the smart_unfold script:

```bash
cd <project_root>
uv run python plugin/scripts/smart_unfold.py <file_path> <symbol_name>
```

Output format (success):
```json
{
  "file": "/path/to/module.py",
  "symbol": "process_data",
  "line_start": 42,
  "line_end": 67,
  "source_code": "def process_data(items: list) -> dict:\n    ..."
}
```

Output format (not found):
```json
{"error": "Symbol 'xyz' not found in path/to/file.py"}
```
</unfold>

## Guarantees

- **AST-bounded** for Python: `line_end` comes from the AST, never cuts mid-function
- **Complete class extraction**: includes all methods when extracting a class
- **Graceful fallback** for non-Python: uses brace-counting + indentation heuristics

## Common Workflow

```
# 1. Find which file contains the symbol
smart_search "process_data" --path=src

# 2. Optionally: see all symbols in that file
smart_outline src/service.py

# 3. Extract the exact symbol
smart_unfold src/service.py process_data
```

## Graceful Fallback

- Python: AST (`ast.parse` → `end_lineno`) — exact boundaries guaranteed
- Other languages: tries tree-sitter CLI, falls back to brace-counting `{}` or indentation tracking
- Returns `null` (exit code 1) if symbol is not found — not an error, just absent

## Limitations

- Symbol names must be exact (case-sensitive for Python, regex-matched for fallback)
- For non-Python without tree-sitter: end boundary is heuristic-based (brace counting)
- Does not handle overloaded methods — returns the first match
- Nested symbols (function inside function): finds the first match by name
