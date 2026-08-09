"""函数内 AST 分段（供 Layer 3 分段向量验证）。

切分规则：
- 函数 < min_func_lines(15) 行：整函数作为单段；
- 否则按顶层控制流切：每个 if/else 分支体、loop/while/for 体、match 臂各一段；
  其余连续语句每 chunk_lines(10) 行聚成一段；
- 段 < min_seg_lines(4) 行的并入相邻段。

每段带绝对行号 [start_line, end_line] 与 normalized_text（逐段归一化）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .keep_symbols import load_keep_symbols
from .normalizer import normalize_asm, normalize_snippet
from .ts import parse

DEFAULT_MIN_FUNC_LINES = 15
DEFAULT_CHUNK_LINES = 10
DEFAULT_MIN_SEG_LINES = 4

_RUST_LOOPS = {"while_expression", "for_expression", "loop_expression"}
_C_OTHER_CF = {"while_statement", "for_statement", "do_statement", "switch_statement"}


@dataclass
class Segment:
    start_line: int
    end_line: int
    normalized_text: str

    @property
    def n_lines(self) -> int:
        return self.end_line - self.start_line + 1


def _unwrap(node):
    """剥掉 expression_statement 外壳，拿到内部表达式。"""
    if node.type == "expression_statement" and node.named_child_count >= 1:
        return node.named_child(0)
    return node


def _line(node, which: str) -> int:
    """节点的 1-based 行号（相对函数源码）。"""
    pt = node.start_point if which == "start" else node.end_point
    return pt[0] + 1


def _cf_branches(child, lang: str) -> list[tuple[int, int]] | None:
    """若 child 是控制流结构，返回其分支体的相对行号区间列表；否则 None。"""
    node = _unwrap(child)
    t = node.type
    ranges: list[tuple[int, int]] = []

    def rng(n) -> tuple[int, int]:
        return (_line(n, "start"), _line(n, "end"))

    if lang == "rust":
        if t == "if_expression":
            cons = node.child_by_field_name("consequence")
            alt = node.child_by_field_name("alternative")
            end_cons = _line(cons, "end") if cons else _line(node, "end")
            ranges.append((_line(node, "start"), end_cons))
            if alt is not None:
                ranges.append(rng(alt))
        elif t == "match_expression":
            body = node.child_by_field_name("body")
            arms = [a for a in (body.named_children if body else []) if a.type == "match_arm"]
            ranges.extend(rng(a) for a in arms) if arms else ranges.append(rng(node))
        elif t in _RUST_LOOPS:
            ranges.append(rng(node))
        else:
            return None
    elif lang == "c":
        if t == "if_statement":
            cons = node.child_by_field_name("consequence")
            alt = node.child_by_field_name("alternative")
            end_cons = _line(cons, "end") if cons else _line(node, "end")
            ranges.append((_line(node, "start"), end_cons))
            if alt is not None:
                ranges.append(rng(alt))
        elif t in _C_OTHER_CF:
            ranges.append(rng(node))
        else:
            return None
    else:
        return None
    return ranges or None


def _chunk(start: int, end: int, chunk_lines: int) -> list[tuple[int, int]]:
    out = []
    cur = start
    while cur <= end:
        out.append((cur, min(end, cur + chunk_lines - 1)))
        cur += chunk_lines
    return out


def _merge_small(segs: list[tuple[int, int]], min_seg_lines: int) -> list[tuple[int, int]]:
    segs = sorted(set(segs))
    merged: list[tuple[int, int]] = []
    for s, e in segs:
        small = (e - s + 1) < min_seg_lines
        prev_small = merged and (merged[-1][1] - merged[-1][0] + 1) < min_seg_lines
        if merged and (small or prev_small):
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _body_node(root, lang: str):
    """找到函数体 block / compound_statement。"""
    fn_types = ("function_item",) if lang == "rust" else ("function_definition",)
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in fn_types:
            return n.child_by_field_name("body")
        stack.extend(n.children)
    return None


def _relative_segments(raw_code: str, lang: str, chunk_lines: int) -> list[tuple[int, int]]:
    """计算相对函数的分段行号区间。"""
    tree = parse(raw_code.encode("utf-8"), lang)
    body = _body_node(tree.root_node, lang)
    n = len(raw_code.splitlines())
    if body is None:
        return _chunk(1, n, chunk_lines)

    segs: list[tuple[int, int]] = []
    run: list[int] | None = None

    def flush():
        nonlocal run
        if run is not None:
            segs.extend(_chunk(run[0], run[1], chunk_lines))
            run = None

    for ch in body.named_children:
        branches = _cf_branches(ch, lang)
        if branches:
            flush()
            segs.extend(branches)
        else:
            rs, re = _line(ch, "start"), _line(ch, "end")
            if run is None:
                run = [rs, re]
            else:
                run[1] = re
    flush()
    return segs or _chunk(1, n, chunk_lines)


def segment_function(
    raw_code: str,
    lang: str,
    start_line: int = 1,
    *,
    keep=None,
    min_func_lines: int = DEFAULT_MIN_FUNC_LINES,
    chunk_lines: int = DEFAULT_CHUNK_LINES,
    min_seg_lines: int = DEFAULT_MIN_SEG_LINES,
) -> list[Segment]:
    """把一个函数切成若干 Segment（带绝对行号与归一化文本）。"""
    keep = keep if keep is not None else load_keep_symbols()
    lines = raw_code.splitlines()
    n = len(lines)
    if n == 0:
        return []

    def _norm(text: str) -> str:
        if lang == "asm":
            return normalize_asm(text).code
        return normalize_snippet(text, lang, keep).code

    def _make(rel_start: int, rel_end: int) -> Segment:
        rel_start = max(1, rel_start)
        rel_end = min(n, rel_end)
        text = "\n".join(lines[rel_start - 1 : rel_end])
        return Segment(start_line + rel_start - 1, start_line + rel_end - 1, _norm(text))

    # 短函数 / 汇编：不按 AST 分段
    if n < min_func_lines:
        return [_make(1, n)]
    if lang == "asm":
        rel = _merge_small(_chunk(1, n, chunk_lines), min_seg_lines)
        return [_make(s, e) for s, e in rel]

    rel = _merge_small(_relative_segments(raw_code, lang, chunk_lines), min_seg_lines)
    return [_make(s, e) for s, e in rel]
