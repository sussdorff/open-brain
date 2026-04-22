"""tree_sitter_utils.py — Shared tree-sitter language registry and AST walker.

Provides a declarative LanguageSpec registry for TypeScript, JavaScript,
TSX, JSX, Go, and Rust, plus a generic parse_file_with_treesitter() function.

Gracefully degrades if tree-sitter-language-pack is not installed (ImportError).
"""

# /// script
# requires-python = ">=3.11"
# dependencies = ["tree-sitter-language-pack>=1.6.2"]
# ///

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


# ─── Node Protocol ────────────────────────────────────────────────────────────


class _Node(Protocol):
    """Protocol matching the tree-sitter Node interface."""

    type: str
    text: bytes | None
    start_point: tuple  # (row, col)
    end_point: tuple    # (row, col)
    start_byte: int
    end_byte: int
    children: list  # list of _Node


# ─── LanguageSpec Registry ────────────────────────────────────────────────────


@dataclass
class LanguageSpec:
    """Declarative per-language configuration for tree-sitter AST traversal."""

    name: str
    """Tree-sitter language name (e.g. 'typescript')."""

    extensions: tuple[str, ...]
    """File extensions handled by this spec (e.g. ('.ts', '.tsx'))."""

    function_node_types: tuple[str, ...]
    """Node types that represent callable functions/methods."""

    class_node_types: tuple[str, ...]
    """Node types that represent classes, structs, enums, interfaces."""

    function_name_child_type: str
    """Child node type that carries the symbol name for functions."""

    class_name_child_type: str
    """Child node type that carries the symbol name for classes."""

    arrow_var_node_type: str = ""
    """Node type for variable declarations containing arrow functions."""

    interface_node_type: str = ""
    """Node type for interface declarations (empty = not supported)."""

    interface_name_child_type: str = ""
    """Child node type that carries the name for interfaces."""

    method_name_child_type: str = ""
    """Child node type for method names when different from function_name_child_type."""


# Registry: extension -> LanguageSpec
_LANGUAGE_REGISTRY: dict[str, LanguageSpec] = {}


def _register(spec: LanguageSpec) -> None:
    for ext in spec.extensions:
        _LANGUAGE_REGISTRY[ext] = spec


_register(LanguageSpec(
    name="typescript",
    extensions=(".ts",),
    function_node_types=("function_declaration",),
    class_node_types=("class_declaration",),
    function_name_child_type="identifier",
    class_name_child_type="type_identifier",
    arrow_var_node_type="lexical_declaration",
    interface_node_type="interface_declaration",
    interface_name_child_type="type_identifier",
))

_register(LanguageSpec(
    name="tsx",
    extensions=(".tsx",),
    function_node_types=("function_declaration",),
    class_node_types=("class_declaration",),
    function_name_child_type="identifier",
    class_name_child_type="type_identifier",
    arrow_var_node_type="lexical_declaration",
    interface_node_type="interface_declaration",
    interface_name_child_type="type_identifier",
))

_register(LanguageSpec(
    name="javascript",
    extensions=(".js",),
    function_node_types=("function_declaration",),
    class_node_types=("class_declaration",),
    function_name_child_type="identifier",
    class_name_child_type="identifier",
    arrow_var_node_type="lexical_declaration",
))

_register(LanguageSpec(
    name="javascript",
    extensions=(".jsx",),
    function_node_types=("function_declaration",),
    class_node_types=("class_declaration",),
    function_name_child_type="identifier",
    class_name_child_type="identifier",
    arrow_var_node_type="lexical_declaration",
))

# Go: method_declaration uses "field_identifier" for name; type_declaration
# wraps its name inside a type_spec child (handled by explicit walker logic).
_register(LanguageSpec(
    name="go",
    extensions=(".go",),
    function_node_types=("function_declaration",),
    class_node_types=("type_declaration",),
    function_name_child_type="identifier",
    class_name_child_type="type_identifier",
    method_name_child_type="field_identifier",
))

# Rust: impl_item is NOT a class — it's an implementation block.
# Methods inside impl blocks are picked up as top-level function_item symbols.
_register(LanguageSpec(
    name="rust",
    extensions=(".rs",),
    function_node_types=("function_item",),
    class_node_types=("struct_item", "enum_item"),
    function_name_child_type="identifier",
    class_name_child_type="type_identifier",
))


def get_language_spec(file_path: Path) -> LanguageSpec | None:
    """Return the LanguageSpec for a given file extension, or None."""
    return _LANGUAGE_REGISTRY.get(file_path.suffix)


# ─── AST Symbol dataclass ────────────────────────────────────────────────────


@dataclass
class ASTSymbol:
    """A symbol extracted from a tree-sitter AST."""

    name: str
    type: str
    line_start: int
    line_end: int
    source_code: str


# ─── Generic Tree Walker ──────────────────────────────────────────────────────


def _get_child_name(node: _Node, name_type: str) -> str | None:
    """Extract name from a direct child node of the given type."""
    for child in node.children:
        if child.type == name_type:
            return child.text.decode("utf-8") if child.text else None
    return None


def _extract_source(node: _Node, code_bytes: bytes) -> str:
    """Extract source text for a node using byte offsets (efficient, no line splitting)."""
    return code_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _walk_node(
    node: _Node,
    code_bytes: bytes,
    spec: LanguageSpec,
    symbols: list[ASTSymbol],
    in_class: bool = False,
) -> None:
    """Recursively walk AST node and collect symbols."""
    node_type: str = node.type

    # Go method_declaration: uses "field_identifier" for the method name
    if node_type == "method_declaration" and spec.name == "go":
        name_type = spec.method_name_child_type or spec.function_name_child_type
        name = _get_child_name(node, name_type)
        if name:
            symbols.append(ASTSymbol(
                name=name,
                type="function",
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                source_code=_extract_source(node, code_bytes),
            ))
        # Recurse into children
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=False)
        return

    # Function declarations
    if node_type in spec.function_node_types:
        name = _get_child_name(node, spec.function_name_child_type)
        if name:
            sym_type = "method" if in_class else "function"
            symbols.append(ASTSymbol(
                name=name,
                type=sym_type,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                source_code=_extract_source(node, code_bytes),
            ))
        # Walk into children to find nested functions
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=False)
        return

    # Go type_declaration: name is inside a type_spec child
    if node_type == "type_declaration" and spec.name == "go":
        # Walk into type_spec children to extract the type name
        for child in node.children:
            if child.type == "type_spec":
                name = _get_child_name(child, "type_identifier")
                if name:
                    symbols.append(ASTSymbol(
                        name=name,
                        type="class",
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        source_code=_extract_source(node, code_bytes),
                    ))
                break
        # Recurse into children
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=True)
        return

    # Class declarations (non-Go)
    if node_type in spec.class_node_types:
        name = _get_child_name(node, spec.class_name_child_type)
        if not name:
            name = _get_child_name(node, "identifier")
        if name:
            symbols.append(ASTSymbol(
                name=name,
                type="class",
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                source_code=_extract_source(node, code_bytes),
            ))
        # Walk class body for methods
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=True)
        return

    # Interface declarations
    if spec.interface_node_type and node_type == spec.interface_node_type:
        name = _get_child_name(node, spec.interface_name_child_type)
        if name:
            symbols.append(ASTSymbol(
                name=name,
                type="interface",
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                source_code=_extract_source(node, code_bytes),
            ))
        # Recurse into interface body
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=in_class)
        return

    # Arrow function variables: lexical_declaration > variable_declarator > arrow_function
    if spec.arrow_var_node_type and node_type == spec.arrow_var_node_type:
        for declarator in node.children:
            if declarator.type == "variable_declarator":
                # Check if it contains an arrow_function
                has_arrow = any(c.type == "arrow_function" for c in declarator.children)
                if has_arrow:
                    name = _get_child_name(declarator, "identifier")
                    if name:
                        symbols.append(ASTSymbol(
                            name=name,
                            type="arrow_function",
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            source_code=_extract_source(node, code_bytes),
                        ))
        return

    # Method definitions (inside class body)
    if node_type == "method_definition" and in_class:
        name = _get_child_name(node, "property_identifier")
        if name:
            symbols.append(ASTSymbol(
                name=name,
                type="method",
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                source_code=_extract_source(node, code_bytes),
            ))
        # Recurse into method body
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=False)
        return

    # Export statement: unwrap and walk the exported declaration
    if node_type == "export_statement":
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=in_class)
        return

    # Class body: walk children as class members
    if node_type == "class_body":
        for child in node.children:
            _walk_node(child, code_bytes, spec, symbols, in_class=True)
        return

    # Default: recurse into children
    for child in node.children:
        _walk_node(child, code_bytes, spec, symbols, in_class=in_class)


# ─── Public API ───────────────────────────────────────────────────────────────


def parse_file_with_treesitter(file_path: Path) -> list[ASTSymbol] | None:
    """Parse a file using the tree-sitter library and return extracted symbols.

    Returns None if:
    - tree-sitter-language-pack is not installed (ImportError)
    - The file extension is not in the registry
    - The file cannot be read

    The caller is responsible for falling back to grep or other methods.
    """
    spec = get_language_spec(file_path)
    if spec is None:
        return None

    try:
        from tree_sitter_language_pack import get_parser  # type: ignore[import]
    except ImportError:
        return None

    try:
        code_bytes = file_path.read_bytes()
    except OSError:
        return None

    parser = get_parser(spec.name)
    tree = parser.parse(code_bytes)

    symbols: list[ASTSymbol] = []
    _walk_node(tree.root_node, code_bytes, spec, symbols)
    return symbols


def find_symbol_in_file(file_path: Path, symbol_name: str) -> ASTSymbol | None:
    """Find a specific symbol by name using tree-sitter AST parsing.

    Returns None if not found or if tree-sitter is not available.
    """
    symbols = parse_file_with_treesitter(file_path)
    if symbols is None:
        return None
    for sym in symbols:
        if sym.name == symbol_name:
            return sym
    return None
