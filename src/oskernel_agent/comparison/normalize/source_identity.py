"""Exact source-token identity, retaining names, literals and syntax.

This is a conservative attribution check, not semantic equivalence. Parse errors,
missing source and unsupported languages produce unknown (None).
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import re


@lru_cache(maxsize=2)
def _language(lang: str):
    from tree_sitter import Language
    if lang == 'rust':
        import tree_sitter_rust
        return Language(tree_sitter_rust.language())
    if lang == 'c':
        import tree_sitter_c
        return Language(tree_sitter_c.language())
    return None


@lru_cache(maxsize=4096)
def source_token_fingerprint(code: str, lang: str) -> str | None:
    if not code or lang not in {'c', 'rust'}:
        return None
    try:
        from tree_sitter import Parser
        language = _language(lang)
        tree = Parser(language).parse(code.encode('utf-8'))
    except (ImportError, ValueError, TypeError):
        return None
    if tree.root_node.has_error:
        return None
    raw = code.encode('utf-8')
    stack = [tree.root_node]
    digest = hashlib.sha256()
    count = 0
    while stack:
        node = stack.pop()
        if node.type in {'comment', 'line_comment', 'block_comment'}:
            continue
        if node.children:
            stack.extend(reversed(node.children))
            continue
        value = raw[node.start_byte:node.end_byte]
        if not value:
            continue
        digest.update(node.type.encode('utf-8') + b'\0')
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
        count += 1
    return digest.hexdigest() if count else None


def complete_source_reference(pair: dict) -> dict | None:
    """A complete-body reference to an explicitly indexed public baseline."""
    from oskernel_agent.comparison.models import is_baseline_repo
    q = pair.get('query_func') or {}
    c = pair.get('candidate_func') or {}
    if not is_baseline_repo(str(c.get('repo_id') or '')):
        return None
    lang = str(q.get('lang') or '').lower()
    if lang != str(c.get('lang') or '').lower():
        return None
    query = source_record_fingerprint(q)
    base = source_record_fingerprint(c)
    if query is None or query != base:
        return None
    return dict(repo_id=c.get('repo_id'), file_path=c.get('file_path'),
        start_line=c.get('start_line'), end_line=c.get('end_line'),
        func_name=c.get('func_name'), lang=lang, token_fingerprint=base,
        raw_source_sha256=hashlib.sha256((c.get('raw_code') or '').encode()).hexdigest(),
        coverage_complete=True)


@lru_cache(maxsize=4096)
def _named_record_fingerprint(code: str, lang: str, name: str) -> str | None:
    if lang == 'asm':
        # The production extractor splits assembly at every named label. Exact
        # bytes give a stricter identity check here; no cross-ISA normalization.
        labels = [match.group(1) for line in code.splitlines()
                  if (match := re.match(r'^\s*([A-Za-z_.$][\w.$]*)\s*:(?!:)', line))]
        if labels != [name]:
            return None
        return hashlib.sha256(b'asm-source\0'+code.encode('utf-8')).hexdigest()
    fingerprint = source_token_fingerprint(code, lang)
    if fingerprint is None or not name:
        return None
    from tree_sitter import Parser
    from .extract import _c_func_name, _iter_top_functions
    tree = Parser(_language(lang)).parse(code.encode('utf-8'))
    entities = list(_iter_top_functions(tree.root_node, lang))
    if len(entities) != 1:
        return None
    entity = entities[0][0]
    if lang == 'rust':
        node = entity.child_by_field_name('name')
        parsed_name = node.text.decode('utf-8') if node else None
    else:
        parsed_name = _c_func_name(entity)
    return fingerprint if parsed_name == name else None


def source_record_fingerprint(record: dict) -> str | None:
    return _named_record_fingerprint(record.get('raw_code') or '',
        str(record.get('lang') or '').lower(), record.get('func_name') or '')


def reference_covers_query(query: dict, reference: dict | None) -> bool:
    from oskernel_agent.comparison.models import is_baseline_repo
    ref = reference or {}
    lang = str(query.get('lang') or '').lower()
    if (not is_baseline_repo(str(ref.get('repo_id') or ''))
            or lang != ref.get('lang') or not ref.get('raw_source_sha256')):
        return False
    fingerprint = source_record_fingerprint(query)
    return bool(fingerprint and fingerprint == ref.get('token_fingerprint'))
