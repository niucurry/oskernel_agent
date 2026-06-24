"""
Function extractor using tree-sitter.

Parses source files across 7 languages and yields FunctionBlock objects
(= FunctionChunk) suitable for downstream perplexity scoring.

Public API:
    extractor = FunctionExtractor()
    blocks: list[FunctionBlock]    = extractor.extract_from_file(path)
    stream: Iterator[FunctionBlock] = extractor.extract_from_repo(repo_path)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from tree_sitter import Node
from tree_sitter_languages import get_parser as _ts_get_parser

from .models import FunctionBlock, FunctionChunk, Language

# ---------------------------------------------------------------------------
# Language mappings
# ---------------------------------------------------------------------------

LANG_EXTENSIONS: dict[Language, list[str]] = {
    Language.PYTHON: [".py"],
    Language.JAVA: [".java"],
    Language.GO: [".go"],
    Language.C: [".c", ".h"],
    Language.CPP: [".cpp", ".cc", ".cxx", ".hpp", ".hxx"],
    Language.JAVASCRIPT: [".js", ".mjs", ".cjs"],
    Language.TYPESCRIPT: [".ts", ".tsx"],
    Language.RUST: [".rs"],
}

EXT_TO_LANG: dict[str, Language] = {
    ext: lang for lang, exts in LANG_EXTENSIONS.items() for ext in exts
}

# tree-sitter grammar name per Language
_TS_GRAMMAR: dict[Language, str] = {
    Language.PYTHON: "python",
    Language.JAVA: "java",
    Language.GO: "go",
    Language.C: "c",
    Language.CPP: "cpp",
    Language.JAVASCRIPT: "javascript",
    Language.TYPESCRIPT: "typescript",
    Language.RUST: "rust",
}

# ---------------------------------------------------------------------------
# Function (and constructor) node types per language
# ---------------------------------------------------------------------------

FUNC_TYPES: dict[Language, frozenset[str]] = {
    Language.PYTHON: frozenset({
        "function_definition",
        "async_function_definition",
    }),
    Language.JAVA: frozenset({
        "method_declaration",
        "constructor_declaration",
    }),
    Language.GO: frozenset({
        "function_declaration",
        "method_declaration",
    }),
    Language.C: frozenset({"function_definition"}),
    Language.CPP: frozenset({"function_definition"}),
    Language.JAVASCRIPT: frozenset({
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function",  # anonymous function expression
    }),
    Language.TYPESCRIPT: frozenset({
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function",
    }),
    Language.RUST: frozenset({"function_item"}),
}

# Class-like node types whose name becomes the qualifier
_CLASS_TYPES: dict[Language, frozenset[str]] = {
    Language.PYTHON: frozenset({"class_definition"}),
    Language.JAVA: frozenset({
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "annotation_type_declaration",
    }),
    Language.GO: frozenset(),  # qualified via receiver, handled separately
    Language.C: frozenset(),
    Language.CPP: frozenset({"class_specifier", "struct_specifier"}),
    Language.JAVASCRIPT: frozenset({"class_declaration", "class"}),
    Language.TYPESCRIPT: frozenset({"class_declaration", "class"}),
}

# ---------------------------------------------------------------------------
# Filtering constants
# ---------------------------------------------------------------------------

SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "vendor", "third_party", "third-party",
    "venv", ".venv", "env", ".env",
    "__pycache__", ".mypy_cache", ".ruff_cache",
    "dist", "build", "target", "out", ".idea", ".vscode",
})

_GENERATED_PATTERNS: list[re.Pattern] = [
    re.compile(r'\.pb\.go$'),
    re.compile(r'_pb2\.py$'),
    re.compile(r'\.min\.(js|ts)$'),
    re.compile(r'\.generated\.'),
    re.compile(r'\.gen\.go$'),
    re.compile(r'_generated\.go$'),
]

MAX_FILE_BYTES: int = 1024 * 1024  # 1 MB
MIN_LOC: int = 5


# ---------------------------------------------------------------------------
# LOC helper
# ---------------------------------------------------------------------------

# Single-line comment prefixes per language (used for LOC counting)
_COMMENT_PREFIXES: dict[Language, tuple[str, ...]] = {
    Language.PYTHON: ("#",),
    Language.JAVA: ("//", "*", "/*"),
    Language.GO: ("//", "*", "/*"),
    Language.C: ("//", "*", "/*"),
    Language.CPP: ("//", "*", "/*"),
    Language.JAVASCRIPT: ("//", "*", "/*"),
    Language.TYPESCRIPT: ("//", "*", "/*"),
    Language.RUST: ("//", "*", "/*"),  # covers //, ///, //!, block comments
}


def _count_loc(source: str, language: Language) -> int:
    """Count non-blank, non-comment lines in *source*."""
    prefixes = _COMMENT_PREFIXES.get(language, ("//",))
    count = 0
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in prefixes):
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# Name extraction helpers
# ---------------------------------------------------------------------------

def _node_text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace") if node.text else ""


def _extract_c_name(declarator: Node) -> str:
    """Recurse through C/C++ declarator chain to find the function identifier.

    Handles: identifier, field_identifier, function_declarator,
    pointer_declarator, reference_declarator, scoped_identifier,
    operator_name, destructor_name.
    """
    t = declarator.type
    # Terminal name nodes
    if t in ("identifier", "field_identifier"):
        return _node_text(declarator)
    if t in ("destructor_name", "operator_name"):
        return _node_text(declarator)
    # Scoped identifier: ClassName::method
    if t == "scoped_identifier":
        name_node = declarator.child_by_field_name("name")
        if name_node:
            return _node_text(name_node)
    # Nodes that carry a named 'declarator' field
    if t in ("function_declarator", "pointer_declarator", "parenthesized_declarator"):
        inner = declarator.child_by_field_name("declarator")
        if inner:
            return _extract_c_name(inner)
    # reference_declarator has no 'declarator' field; its non-& child IS the declarator
    if t == "reference_declarator":
        for child in declarator.children:
            if child.type not in ("&", "&&"):
                return _extract_c_name(child)
    # Fallback: first terminal name child
    for child in declarator.children:
        if child.type in ("identifier", "field_identifier"):
            return _node_text(child)
    return "<unknown>"


def _get_go_receiver_type(receiver_node: Node) -> str | None:
    """Extract the base type name from a Go method receiver node.

    Handles plain types (Client), pointer types (*Client), and
    generic types (Stack[T] / *Stack[T]).
    """
    def _type_name(type_node: Node) -> str | None:
        t = type_node.type
        if t == "type_identifier":
            return _node_text(type_node)
        if t == "pointer_type":
            for child in type_node.children:
                result = _type_name(child)
                if result:
                    return result
        if t == "generic_type":
            # Stack[T] — the base name is the first type_identifier child
            for child in type_node.children:
                if child.type == "type_identifier":
                    return _node_text(child)
        return None

    for child in receiver_node.children:
        if child.type == "parameter_declaration":
            type_node = child.child_by_field_name("type")
            if type_node is not None:
                result = _type_name(type_node)
                if result:
                    return result
    return None


def _get_arrow_function_name(node: Node) -> str:
    """Derive a name for an arrow_function from its assignment context."""
    parent = node.parent
    if parent is None:
        return "<anonymous>"
    # const/let/var foo = () => …
    if parent.type == "variable_declarator":
        name_node = parent.child_by_field_name("name")
        if name_node:
            return _node_text(name_node)
    # foo = () => …
    if parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        if left and left.type == "identifier":
            return _node_text(left)
    # { key: () => … }  object pair
    if parent.type == "pair":
        key = parent.child_by_field_name("key")
        if key:
            return _node_text(key)
    # export default () => …   or   return () => …
    return "<anonymous>"


def _get_rust_impl_type(node: Node) -> str | None:
    """Walk a Rust function's parent chain to the nearest enclosing impl/trait type.

    Rust `impl_item` exposes the implementing type via the `type` field
    (e.g. `Sha256` in `impl Sha256 { ... }`), and `trait_item` via `name`.
    Generic impls like `impl<T> Stack<T>` resolve to the base type identifier.
    """
    parent = node.parent
    while parent:
        if parent.type == "impl_item":
            type_node = parent.child_by_field_name("type")
            if type_node is not None:
                t = type_node.type
                if t == "type_identifier":
                    return _node_text(type_node)
                if t == "generic_type":
                    base = type_node.child_by_field_name("type")
                    if base is not None:
                        return _node_text(base)
                    for child in type_node.children:
                        if child.type == "type_identifier":
                            return _node_text(child)
                # scoped or other forms: fall back to raw text
                return _node_text(type_node)
        elif parent.type == "trait_item":
            name_node = parent.child_by_field_name("name")
            if name_node is not None:
                return _node_text(name_node)
        parent = parent.parent
    return None


def _get_enclosing_class_name(node: Node, language: Language) -> str | None:
    """Walk *node*'s parent chain and return the nearest enclosing class name."""
    class_types = _CLASS_TYPES.get(language, frozenset())
    if not class_types:
        return None
    parent = node.parent
    while parent:
        if parent.type in class_types:
            name_node = parent.child_by_field_name("name")
            if name_node:
                return _node_text(name_node)
        parent = parent.parent
    return None


def _extract_names(node: Node, language: Language) -> tuple[str, str]:
    """Return (name, qualified_name) for a function node."""
    t = node.type

    # ---- Python ----
    if language == Language.PYTHON:
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node) if name_node else "<unknown>"
        cls = _get_enclosing_class_name(node, language)
        qualified = f"{cls}.{name}" if cls else name
        return name, qualified

    # ---- Java ----
    if language == Language.JAVA:
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node) if name_node else "<unknown>"
        cls = _get_enclosing_class_name(node, language)
        qualified = f"{cls}.{name}" if cls else name
        return name, qualified

    # ---- Go ----
    if language == Language.GO:
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node) if name_node else "<unknown>"
        if t == "method_declaration":
            receiver = node.child_by_field_name("receiver")
            if receiver:
                recv_type = _get_go_receiver_type(receiver)
                qualified = f"{recv_type}.{name}" if recv_type else name
                return name, qualified
        return name, name

    # ---- C / C++ ----
    if language in (Language.C, Language.CPP):
        declarator = node.child_by_field_name("declarator")
        name = _extract_c_name(declarator) if declarator else "<unknown>"
        cls = _get_enclosing_class_name(node, language)
        qualified = f"{cls}.{name}" if cls else name
        return name, qualified

    # ---- JavaScript / TypeScript ----
    if language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
        if t == "arrow_function":
            name = _get_arrow_function_name(node)
            cls = _get_enclosing_class_name(node, language)
            qualified = f"{cls}.{name}" if cls else name
            return name, qualified
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node) if name_node else "<anonymous>"
        cls = _get_enclosing_class_name(node, language)
        qualified = f"{cls}.{name}" if cls else name
        return name, qualified

    # ---- Rust ----
    if language == Language.RUST:
        name_node = node.child_by_field_name("name")
        name = _node_text(name_node) if name_node else "<unknown>"
        impl_type = _get_rust_impl_type(node)
        qualified = f"{impl_type}.{name}" if impl_type else name
        return name, qualified

    return "<unknown>", "<unknown>"


# ---------------------------------------------------------------------------
# Tree traversal
# ---------------------------------------------------------------------------

def _iter_function_nodes(root: Node, func_types: frozenset[str]) -> Iterator[Node]:
    """DFS over the parse tree, yielding nodes whose type is in *func_types*."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in func_types:
            yield node
        # Always recurse — we want nested functions too
        stack.extend(reversed(node.children))


# ---------------------------------------------------------------------------
# Main extractor class
# ---------------------------------------------------------------------------

class ExtractionError(Exception):
    """Raised when tree-sitter fails to parse a file."""


class FunctionExtractor:
    """Extracts FunctionBlock objects from source files using tree-sitter.

    Args:
        languages: Subset of languages to scan. None = all 7 supported.
        max_file_bytes: Files larger than this are skipped.
        min_loc: Functions with fewer effective lines are skipped.
    """

    def __init__(
        self,
        languages: list[Language] | None = None,
        max_file_bytes: int = MAX_FILE_BYTES,
        min_loc: int = MIN_LOC,
    ) -> None:
        self.languages = frozenset(languages or list(Language))
        self.max_file_bytes = max_file_bytes
        self.min_loc = min_loc
        self._parsers: dict[Language, object] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_from_file(self, path: Path) -> list[FunctionBlock]:
        """Parse *path* and return all qualifying function blocks."""
        lang = self._detect_language(path)
        if lang is None or lang not in self.languages:
            return []
        if self._should_skip_file(path):
            return []
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return self._parse_source(source, lang, path)

    def extract_from_repo(self, repo_path: Path) -> Iterator[FunctionBlock]:
        """Walk *repo_path* and yield FunctionBlock objects for every function."""
        for file_path in self._walk_source_files(repo_path):
            yield from self.extract_from_file(file_path)

    # Legacy aliases (used by existing tests and pipeline)
    def extract_file(self, path: Path) -> list[FunctionChunk]:
        return self.extract_from_file(path)

    def extract_repo(self, repo_path: Path) -> list[FunctionChunk]:
        return list(self.extract_from_repo(repo_path))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_language(self, path: Path) -> Language | None:
        return EXT_TO_LANG.get(path.suffix.lower())

    def _get_parser(self, language: Language):
        if language not in self._parsers:
            self._parsers[language] = _ts_get_parser(_TS_GRAMMAR[language])
        return self._parsers[language]

    def _should_skip_file(self, path: Path) -> bool:
        # Size check
        try:
            if path.stat().st_size > self.max_file_bytes:
                return True
        except OSError:
            return True
        # Generated file name patterns
        name = path.name
        if any(pat.search(name) for pat in _GENERATED_PATTERNS):
            return True
        return False

    def _should_skip_path(self, path: Path, repo_root: Path) -> bool:
        """True if any component of the relative path is in SKIP_DIRS."""
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            return False
        return any(part in SKIP_DIRS for part in rel.parts)

    def _walk_source_files(self, repo_root: Path) -> Iterator[Path]:
        for path in repo_root.rglob("*"):
            if not path.is_file():
                continue
            if self._should_skip_path(path, repo_root):
                continue
            lang = self._detect_language(path)
            if lang is None or lang not in self.languages:
                continue
            yield path

    def _parse_source(
        self, source: str, language: Language, file_path: Path
    ) -> list[FunctionBlock]:
        parser = self._get_parser(language)
        source_bytes = source.encode("utf-8", errors="replace")
        tree = parser.parse(source_bytes)
        func_types = FUNC_TYPES[language]
        results: list[FunctionBlock] = []

        for node in _iter_function_nodes(tree.root_node, func_types):
            # Source text for this node
            node_source = source_bytes[node.start_byte:node.end_byte].decode(
                "utf-8", errors="replace"
            )
            loc = _count_loc(node_source, language)
            if loc < self.min_loc:
                continue

            # Lines are 0-indexed in tree-sitter → convert to 1-indexed
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1

            name, qualified_name = _extract_names(node, language)

            results.append(
                FunctionBlock(
                    name=name,
                    qualified_name=qualified_name,
                    source=node_source,
                    start_line=start_line,
                    end_line=end_line,
                    loc=loc,
                    language=language,
                    file_path=file_path,
                )
            )

        return results
