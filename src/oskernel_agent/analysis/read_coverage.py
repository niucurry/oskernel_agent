"""Function boundaries and bounded read requirements; no semantic recall claims."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def syntax_tree(suffix: str, data: bytes):
    """Parse C/C++/Rust source; None for other languages, raises if no parser."""
    if suffix not in {".c", ".h", ".cc", ".cpp", ".hpp", ".rs"}:
        return None
    from tree_sitter import Language, Parser
    if suffix in {".c", ".h"}:
        import tree_sitter_c as grammar
    elif suffix == ".rs":
        import tree_sitter_rust as grammar
    else:
        import tree_sitter_cpp as grammar
    return Parser(Language(grammar.language())).parse(data)


def source_functions(repo: Path, raw_path: str) -> dict:
    root = repo.resolve()
    try:
        path = (root / raw_path).resolve()
        relative = path.relative_to(root).as_posix()
        if path.stat().st_size > 10_000_000:
            return {"status": "too_large", "functions": []}
        data = path.read_bytes()
    except (OSError, ValueError):
        return {"status": "unavailable", "functions": []}
    result = {"path": relative, "sha256": hashlib.sha256(data).hexdigest(),
              "total_lines": len(data.splitlines()), "functions": []}
    try:
        tree = syntax_tree(path.suffix.lower(), data)
    except (ImportError, RuntimeError, TypeError, ValueError):
        return dict(result, status="parser_unavailable")
    if tree is None:
        return dict(result, status="unsupported")
    visit = walk

    for node in visit(tree.root_node):
        if node.type not in {"function_definition", "function_item"} or node.has_error:
            continue
        body = node.child_by_field_name("body")
        name = node.child_by_field_name("name")
        if name is None:
            decl = node.child_by_field_name("declarator")
            fn = next((n for n in visit(decl) if n.type == "function_declarator"), None) if decl else None
            name = fn.child_by_field_name("declarator") if fn else None
            while name and name.type in {"pointer_declarator", "parenthesized_declarator"}:
                name = name.child_by_field_name("declarator") or next(iter(name.named_children), None)
        if not name or not body or data[body.start_byte:body.start_byte + 1] != b"{":
            continue
        symbol = name.text.decode("utf-8", errors="replace")
        if symbol in {"if", "else", "for", "while", "do", "switch"}:
            continue
        calls = sorted({n.child_by_field_name("function").text.decode("utf-8", errors="replace")
                        for n in visit(body) if n.type == "call_expression"
                        and n.child_by_field_name("function") is not None})
        result["functions"].append({"symbol": symbol, "start_line": node.start_point[0] + 1,
                                    "end_line": node.end_point[0] + 1,
                                    "body_byte": body.start_byte, "calls": calls,
                                    "branch_points": sum(n.type in {
                                        "if_statement", "if_expression", "for_statement", "for_expression",
                                        "while_statement", "while_expression", "do_statement",
                                        "case_statement", "match_arm", "conditional_expression",
                                    } for n in visit(body))})
    return dict(result, status="partial" if tree.root_node.has_error else "parsed")


_REFERENCE = re.compile(
    r"([\w./\\-]+\.(?:c|h|cc|cpp|hpp|rs|S|s|asm|toml|md|py|sh)):(\d+)(?:[-–—](\d+))?"
)


def source_references(value: object) -> list[tuple[str, int, int]]:
    """Inspect user-visible values, excluding model-provided audit metadata."""
    refs = set()

    def visit(item):
        if isinstance(item, str):
            for path, low, high in _REFERENCE.findall(item):
                refs.add((path.replace("\\", "/"), int(low), int(high or low)))
        elif isinstance(item, dict):
            for key, child in item.items():
                if key not in {"read_audit", "read_evidence", "canary_observations", "evidence_plan", "evaluation_assertions", "recall_check"}:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)
    return sorted(refs)


def read_requirements(repo: Path, result: dict) -> list[dict]:
    inventories = {}
    requirements = {}
    for raw, low, high in source_references(result):
        if raw not in inventories:
            inventories[raw] = source_functions(repo, raw)
        inventory = inventories[raw]
        path = inventory.get("path", raw)
        valid = bool(inventory.get("sha256")) and 1 <= low <= high <= inventory.get("total_lines", 0)
        # Expand to every intersecting function, while retaining any cited
        # declarations outside functions as a separate line-range requirement.
        functions = [f for f in inventory["functions"]
                     if f["start_line"] <= high and low <= f["end_line"]] if valid else []
        spans = [dict(f, scope="function") for f in functions]
        if not any(f["start_line"] <= low and high <= f["end_line"] for f in functions):
            spans.append({"start_line": low, "end_line": high, "symbol": "",
                          "scope": "cited_lines" if valid else "unresolved_reference"})
        for span in spans:
            item = {"path": path, "sha256": inventory.get("sha256"),
                    "start_line": span["start_line"], "end_line": span["end_line"],
                    "symbol": span["symbol"], "scope": span["scope"],
                    "parser_status": inventory["status"]}
            requirements[(path, item["start_line"], item["end_line"], item["scope"])] = item
    return list(requirements.values())


def check_requirements(repo: Path, audit: dict | None, requirements: list[dict]) -> list[dict]:
    files = {f["path"]: f for f in (audit or {}).get("files", [])}
    hashes = {}
    checked = []
    for req in requirements:
        item = dict(req, status="unknown", missing_ranges=[])
        if req["scope"] == "unresolved_reference" or not 1 <= req["start_line"] <= req["end_line"]:
            item["status"] = "unresolved_reference"
            checked.append(item)
            continue
        path = req["path"]
        if path not in hashes:
            try:
                candidate = (repo / path).resolve()
                candidate.relative_to(repo.resolve())
                hashes[path] = hashlib.sha256(candidate.read_bytes()).hexdigest()
            except (OSError, ValueError):
                hashes[path] = None
        file = files.get(path)
        if not req.get("sha256") or hashes[path] != req["sha256"]:
            item["status"] = "source_changed_or_unavailable"
        elif file and file["sha256"] != req["sha256"]:
            item["status"] = "source_changed_or_unavailable"
        else:
            start, end = req["start_line"], req["end_line"]
            for lo, hi in sorted((file or {}).get("returned_ranges", [])):
                if hi < start or lo > end:
                    continue
                if start < lo:
                    item["missing_ranges"].append([start, min(lo - 1, end)])
                start = max(start, hi + 1)
            if start <= end:
                item["missing_ranges"].append([start, end])
            if not item["missing_ranges"]:
                item["status"] = "returned"
            elif audit and audit.get("stream_status") == "complete" and not audit.get("unresolved_tool_calls"):
                item["status"] = "not_observed"
        checked.append(item)
    return checked
