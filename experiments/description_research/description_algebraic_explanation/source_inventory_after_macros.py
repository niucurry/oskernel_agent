"""Small source symbol inventories; no call graph or compilation assumptions."""

from __future__ import annotations

import re

_RUST_SYMBOL_RE = re.compile(
    r"\b(?:pub\s+)?(?:unsafe\s+)?(?:extern\s+\"C\"\s+)?fn\s+([A-Za-z_][\w]*)"
    r"|^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_][\w]*)",
    re.MULTILINE,
)

# Unexpanded statement macros can make a partial C tree parse a control-flow
# statement as a function definition. These reserved words cannot name functions.
_CONTROL_KEYWORDS = {"if", "else", "for", "while", "do", "switch", "case", "return"}


def quick_symbols(text: str, suffix: str) -> dict:
    if suffix.lower() == ".rs":
        return {"symbols": [(m.group(1) or m.group(2), text.count("\n", 0, m.start()) + 1)
                            for m in _RUST_SYMBOL_RE.finditer(text)], "status": "heuristic"}
    if suffix.lower() not in {".c", ".h", ".cc", ".cpp", ".hpp"}:
        return {"symbols": [], "status": "unsupported"}
    try:
        from tree_sitter import Language, Parser
        if suffix.lower() in {".cc", ".cpp", ".hpp"}:
            import tree_sitter_cpp as grammar
        else:
            import tree_sitter_c as grammar
        root = Parser(Language(grammar.language())).parse(text.encode("utf-8")).root_node
    except (ImportError, RuntimeError, TypeError, ValueError):
        return {"symbols": [], "status": "unavailable"}

    def visit(node):
        yield node
        for child in node.children:
            yield from visit(child)

    symbols = []
    for node in visit(root):
        if node.type == "function_definition":
            declarator = node.child_by_field_name("declarator")
            function = next((n for n in visit(declarator) if n.type == "function_declarator"), None) if declarator else None
            name = function.child_by_field_name("declarator") if function else None
            while name and name.type in {"pointer_declarator", "parenthesized_declarator"}:
                name = name.child_by_field_name("declarator") or next(iter(name.named_children), None)
            if name and name.type in {"identifier", "field_identifier", "qualified_identifier"}:
                symbol = name.text.decode("utf-8")
                if symbol not in _CONTROL_KEYWORDS:
                    symbols.append((symbol, name.start_point[0] + 1))
        elif node.type == "preproc_function_def":
            # Header-only services may be function-like macros. These are
            # source identities, not compiled functions or enabled features.
            name = node.child_by_field_name("name")
            if name:
                symbols.append((name.text.decode("utf-8"), name.start_point[0] + 1))
        elif node.type in {"struct_specifier", "enum_specifier", "class_specifier"}:
            name = node.child_by_field_name("name")
            if name and node.child_by_field_name("body"):
                symbols.append((name.text.decode("utf-8"), name.start_point[0] + 1))
    return {"symbols": sorted(set(symbols), key=lambda item: (item[1], item[0])),
            "status": "partial" if root.has_error else "parsed"}
