---
name: ob-smart-outline
description: >-
  Show the structural skeleton of a file: classes, methods, functions with line numbers.
  Use when: "outline file", "show structure", "smart_outline", "what's in this file",
  "file structure", "list all functions", "Dateistruktur", "zeige Klassen".
---

# ob-smart-outline

Generate a compact structural skeleton of a source file — 60-80% shorter than reading the full file.
WHY: Avoids loading implementation details when you only need to know what symbols exist and where.

## When to Use

- "Show me the structure of `server.py`"
- "What classes and functions are in `data_layer/postgres.py`?"
- "outline this file before I edit it"
- "smart_outline for large files (>200 lines)"

Do NOT use for files <100 lines where full Read is cheaper.

## Main Procedure

<outline>
Run the smart_outline script:

```bash
cd <project_root>
uv run python plugin/scripts/smart_outline.py <file_path>
```

Output format:
```json
{
  "file": "/path/to/module.py",
  "symbols": [
    {"name": "MyClass", "type": "class", "line": 10, "end_line": 85, "signature": "MyClass"},
    {"name": "process", "type": "method", "line": 15, "end_line": 30, "signature": "process(self, data: list) -> dict"},
    {"name": "standalone_func", "type": "function", "line": 90, "end_line": 95, "signature": "standalone_func(x: int) -> str"}
  ]
}
```
</outline>

## Symbol Types

| Type | Description |
|------|-------------|
| `class` | Class definition |
| `method` | Method inside a class |
| `function` | Top-level function |
| `import` | Module import |
| `interface` | TypeScript interface |
| `arrow_function` | `const fn = () => {}` pattern |

## Workflow: Outline then Unfold

1. Run `smart_outline` to see structure
2. Identify the symbol you need
3. Use `smart_unfold` to extract just that symbol

This pattern saves 60-80% tokens vs reading the full file.

## Graceful Fallback

- Python: AST-based (accurate line numbers, end_line, full signatures)
- Other languages: tries tree-sitter CLI, falls back to regex pattern matching
- `end_line` may equal `line` in fallback mode (line number only)

## Limitations

- For non-Python files without tree-sitter: `end_line` is approximate
- Does not show variable assignments or decorator details
- Nested functions (functions inside functions) are not included
