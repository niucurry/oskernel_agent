"""四类"已知克隆"代码变换（基于 tree-sitter，保证语法合法）。

T1 原样复制 / T2 系统性改名 / T3 增删语句 / T4 结构重写（if-else↔match, for↔while-let）。
每个变换产出后用 tree-sitter 重新解析，含 ERROR 则视为失败返回 None。
"""

from __future__ import annotations

import random

from oskernel_agent.comparison.normalize.keep_symbols import load_keep_symbols
from oskernel_agent.comparison.normalize.ts import parse

CLASSES = ("T1", "T2", "T3", "T4")


def is_valid(code: str, lang: str = "rust") -> bool:
    if not code.strip():
        return False
    return not parse(code.encode("utf-8"), lang).root_node.has_error


def _apply_edits(code: str, edits: list[tuple[int, int, str]]) -> str:
    """edits: (start_byte, end_byte, new_text)；按起点逆序应用。"""
    b = bytearray(code.encode("utf-8"))
    for sb, eb, new in sorted(edits, key=lambda e: e[0], reverse=True):
        b[sb:eb] = new.encode("utf-8")
    return b.decode("utf-8", "replace")


def _function_node(root, lang: str):
    fn_types = ("function_item",) if lang == "rust" else ("function_definition",)
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in fn_types:
            return n
        stack.extend(n.children)
    return None


# ---------- T1 ----------

def t1_copy(code: str, lang: str = "rust") -> str | None:
    return code if is_valid(code, lang) else None


# ---------- T2 系统性改名 ----------

def t2_rename(code: str, lang: str = "rust", seed: int = 0) -> str | None:
    keep = load_keep_symbols()
    tree = parse(code.encode("utf-8"), lang)
    mapping: dict[str, str] = {}
    edits: list[tuple[int, int, str]] = []

    def visit(n):
        if n.type == "identifier" and n.child_count == 0:
            name = n.text.decode("utf-8", "replace")
            if name not in keep:
                new = mapping.setdefault(name, f"r{len(mapping)}_{name}")
                edits.append((n.start_byte, n.end_byte, new))
        for c in n.children:
            visit(c)

    visit(tree.root_node)
    if not edits:
        return None
    out = _apply_edits(code, edits)
    return out if is_valid(out, lang) else None


# ---------- T3 增删语句 ----------

_LOG_STMTS = [
    'log::info!("checkpoint reached");',
    'let _trace_guard = 0u32;',
    'log::debug!("state updated");',
]


def t3_edit(code: str, lang: str = "rust", seed: int = 0) -> str | None:
    rng = random.Random(seed)
    tree = parse(code.encode("utf-8"), lang)
    fn = _function_node(tree.root_node, lang)
    body = fn.child_by_field_name("body") if fn else None
    if body is None:
        return None
    stmts = list(body.named_children)
    if len(stmts) < 4:
        return None

    edits: list[tuple[int, int, str]] = []
    # 随机删 ~20% 语句（最多保留结构）
    k = max(1, int(len(stmts) * 0.2))
    for st in rng.sample(stmts, k):
        edits.append((st.start_byte, st.end_byte, ""))
    # 在若干语句前插入无关日志语句
    for st in rng.sample(stmts, max(1, k)):
        log = rng.choice(_LOG_STMTS)
        edits.append((st.start_byte, st.start_byte, log + "\n    "))

    out = _apply_edits(code, edits)
    return out if is_valid(out, lang) else None


# ---------- T4 结构重写 ----------

def _rewrite_if_else(code: str, node) -> str | None:
    cond = node.child_by_field_name("condition")
    cons = node.child_by_field_name("consequence")
    alt = node.child_by_field_name("alternative")
    if not (cond and cons and alt):
        return None
    alt_block = alt.named_child(0) if alt.type == "else_clause" and alt.named_child_count else alt
    if alt_block.type != "block":
        return None  # else-if 不处理
    c = code.encode("utf-8")
    cond_t = c[cond.start_byte:cond.end_byte].decode()
    cons_t = c[cons.start_byte:cons.end_byte].decode()
    alt_t = c[alt_block.start_byte:alt_block.end_byte].decode()
    repl = f"match ({cond_t}) {{ true => {cons_t} false => {alt_t} }}"
    return _apply_edits(code, [(node.start_byte, node.end_byte, repl)])


def _rewrite_for(code: str, node) -> str | None:
    pat = node.child_by_field_name("pattern")
    it = node.child_by_field_name("value")
    body = node.child_by_field_name("body")
    if not (pat and it and body):
        return None
    c = code.encode("utf-8")
    pat_t = c[pat.start_byte:pat.end_byte].decode()
    it_t = c[it.start_byte:it.end_byte].decode()
    body_t = c[body.start_byte:body.end_byte].decode()
    repl = f"{{ let mut __it = ({it_t}).into_iter(); while let Some({pat_t}) = __it.next() {body_t} }}"
    return _apply_edits(code, [(node.start_byte, node.end_byte, repl)])


def t4_restructure(code: str, lang: str = "rust", seed: int = 0) -> str | None:
    if lang != "rust":
        return None
    tree = parse(code.encode("utf-8"), lang)

    # 优先 if-else → match
    found = {"if": None, "for": None}

    def visit(n):
        if n.type == "if_expression" and found["if"] is None:
            alt = n.child_by_field_name("alternative")
            if alt is not None:
                found["if"] = n
        elif n.type == "for_expression" and found["for"] is None:
            found["for"] = n
        for c in n.children:
            visit(c)

    visit(tree.root_node)
    out = None
    if found["if"] is not None:
        out = _rewrite_if_else(code, found["if"])
    if (out is None or not is_valid(out, lang)) and found["for"] is not None:
        out = _rewrite_for(code, found["for"])
    if out is None:
        return None
    return out if is_valid(out, lang) else None


TRANSFORMS = {"T1": t1_copy, "T2": t2_rename, "T3": t3_edit, "T4": t4_restructure}


def transform(code: str, cls: str, lang: str = "rust", seed: int = 0) -> str | None:
    fn = TRANSFORMS[cls]
    return fn(code, lang) if cls == "T1" else fn(code, lang, seed)
