"""嫌疑卡片构造器：把一个 SuspectPair 渲染成给 LLM 的文本卡片。

- 双方代码用 raw_code，按**绝对行号**标注；
- 代码超长时只保留 matched_spans ± context_lines 行，中间用「... [省略 n 行未匹配代码] ...」；
- 卡片总长控制在 max_tokens 内（超了逐步收缩上下文窗口）。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]")


def approx_tokens(text: str) -> int:
    """粗略 token 估计：CJK 字符按 1 token，其余按 word/符号切分。无需 tiktoken。"""
    return len(_CJK.findall(text)) + len(_WORD.findall(text))


def _merge_ranges(ranges: list[tuple[int, int]], context: int) -> list[tuple[int, int]]:
    """把若干 [start,end] 各扩 ±context 后合并重叠区间。"""
    if not ranges:
        return []
    expanded = sorted((s - context, e + context) for s, e in ranges)
    merged = [list(expanded[0])]
    for s, e in expanded[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def render_code(raw_code: str, start_line: int, matched_ranges: list[tuple[int, int]], context: int) -> str:
    """渲染带绝对行号的代码；matched_ranges 为空则全量。"""
    lines = raw_code.splitlines()
    end_line = start_line + len(lines) - 1
    keep = _merge_ranges(matched_ranges, context) if matched_ranges else [(start_line, end_line)]

    out: list[str] = []
    abs_no = start_line
    idx = 0
    n = len(lines)
    # 用区间过滤
    kept_flags = [False] * n
    for s, e in keep:
        for ln in range(max(start_line, s), min(end_line, e) + 1):
            kept_flags[ln - start_line] = True

    i = 0
    while i < n:
        if kept_flags[i]:
            out.append(f"{start_line + i:>5} | {lines[i]}")
            i += 1
        else:
            j = i
            while j < n and not kept_flags[j]:
                j += 1
            out.append(f"      ... [省略 {j - i} 行未匹配代码] ...")
            i = j
    return "\n".join(out)


def _func_header(side: str, f: dict) -> str:
    return (
        f"【{side}】repo={f.get('repo_id')} | file={f.get('file_path')} "
        f"| module={f.get('module_tag')} | func={f.get('func_name')} "
        f"| lang={f.get('lang')} | 行 {f.get('start_line')}-{f.get('end_line')}"
    )


def _segment_block(ev: dict) -> str:
    """段级匹配信息（来自 Layer 3 分段验证）。"""
    sh = ev.get("segment_hits")
    if not isinstance(sh, dict) or not sh:
        return ""
    pairs = sh.get("matched_segment_pairs", []) or []
    lines = [
        f"  - 新 {p['q_lines'][0]}-{p['q_lines'][1]} ↔ 旧 {p['c_lines'][0]}-{p['c_lines'][1]}  (cos {p['sim']})"
        for p in pairs
    ]
    body = "\n".join(lines) if lines else "  （无命中段对）"
    return (
        f"\n【段级匹配】命中 {sh.get('hits')} 段（新 {sh.get('q_total')} 段 / 旧 {sh.get('c_total')} 段）:\n{body}"
    )


def _evidence_block(suspect: dict) -> str:
    ev = suspect.get("evidence", {}) or {}
    spans = suspect.get("matched_spans", []) or []
    types = suspect.get("match_type_per_span", []) or []
    span_lines = []
    for k, sp in enumerate(spans):
        t = types[k] if k < len(types) else "?"
        span_lines.append(f"  - 新 {sp[0]}-{sp[1]} ↔ 旧 {sp[2]}-{sp[3]}  ({t})")
    spans_text = "\n".join(span_lines) if span_lines else "  （无）"
    return (
        "【下层证据】\n"
        f"  向量相似度(vector_similarity): {ev.get('vector_similarity')}\n"
        f"  精确匹配行数(exact_match_lines): {ev.get('exact_match_lines')}\n"
        f"  tier: {suspect.get('tier')} | final_score: {suspect.get('final_score')}\n"
        f"  匹配行区间(绝对行号):\n{spans_text}"
        f"{_segment_block(ev)}"
    )


def build_card(suspect: dict, *, max_tokens: int = 6000, context_lines: int = 10) -> str:
    """构造嫌疑卡片文本，自动收缩上下文以满足 token 预算。"""
    q = suspect["query_func"]
    c = suspect["candidate_func"]
    spans = suspect.get("matched_spans", []) or []
    q_ranges = [(sp[0], sp[1]) for sp in spans]
    c_ranges = [(sp[2], sp[3]) for sp in spans]

    header = (
        "# 嫌疑对复核卡片\n"
        f"{_func_header('新作品', q)}\n"
        f"{_func_header('历史作品', c)}\n\n"
        f"{_evidence_block(suspect)}\n"
    )

    for ctx in (context_lines, 6, 3, 1, 0):
        q_code = render_code(q.get("raw_code", ""), q["start_line"], q_ranges, ctx)
        c_code = render_code(c.get("raw_code", ""), c["start_line"], c_ranges, ctx)
        card = (
            f"{header}\n"
            "【新作品代码】\n```\n" + q_code + "\n```\n\n"
            "【历史作品代码】\n```\n" + c_code + "\n```\n"
        )
        if approx_tokens(card) <= max_tokens:
            return card

    # 上下文已收到 0 仍超：硬截断两侧代码行
    return _hard_truncate(header, q, c, q_ranges, c_ranges, max_tokens)


def _hard_truncate(header, q, c, q_ranges, c_ranges, max_tokens) -> str:
    def head_tail(raw, start, ranges, limit):
        code = render_code(raw, start, ranges, 0).splitlines()
        if len(code) <= limit:
            return "\n".join(code)
        keep = limit // 2
        return "\n".join(code[:keep] + ["      ... [卡片超长，已截断] ..."] + code[-keep:])

    limit = 80
    while limit >= 10:
        q_code = head_tail(q.get("raw_code", ""), q["start_line"], q_ranges, limit)
        c_code = head_tail(c.get("raw_code", ""), c["start_line"], c_ranges, limit)
        card = (
            f"{header}\n【新作品代码】\n```\n{q_code}\n```\n\n【历史作品代码】\n```\n{c_code}\n```\n"
        )
        if approx_tokens(card) <= max_tokens:
            return card
        limit -= 20
    return card
