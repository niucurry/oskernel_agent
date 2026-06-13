"""报告后置校验：抽取 文件:行号 引用，回源验证，删除无法验证的句子。

验证依据：构造报告时喂给 LLM 的全部真实 文件:行号 区间（allowed）。LLM 若编造了
不在任何真实区间内的行号引用，整句删除，并在报告末尾统计删除条数。
"""

from __future__ import annotations

import re

# 匹配 path:line 或 path:line-line，path 必须以已知源码扩展名结尾（避免误匹配 word:num）
_REF = re.compile(r"([\w./\-]+\.(?:rs|c|h|S|asm)):(\d+)(?:-(\d+))?")
# 句子切分：按中文句号 / 换行 / 英文句号 切，保留分隔符
_SENT = re.compile(r"[^。\n]+[。\n]?")


def extract_refs(text: str) -> list[tuple[str, int, int]]:
    """抽取 (path, lo, hi) 列表（单行时 hi==lo）。"""
    out = []
    for m in _REF.finditer(text):
        lo = int(m.group(2))
        hi = int(m.group(3)) if m.group(3) else lo
        out.append((m.group(1), lo, hi))
    return out


def add_allowed(allowed: dict[str, list[tuple[int, int]]], ref_str: str) -> None:
    """把一个真实引用串（path:lo-hi）登记到 allowed。"""
    for path, lo, hi in extract_refs(ref_str):
        allowed.setdefault(path, []).append((lo, hi))


def _valid(ref: tuple[str, int, int], allowed: dict[str, list[tuple[int, int]]]) -> bool:
    path, lo, hi = ref
    ranges = allowed.get(path)
    if not ranges:
        return False
    return any(rlo <= lo and hi <= rhi for rlo, rhi in ranges)


def scrub(text: str, allowed: dict[str, list[tuple[int, int]]]) -> tuple[str, int]:
    """删除含无法回源引用的句子，返回 (清洗后文本, 删除句数)。"""
    kept: list[str] = []
    deleted = 0
    for sent in _SENT.findall(text):
        refs = extract_refs(sent)
        if refs and any(not _valid(r, allowed) for r in refs):
            deleted += 1
            continue
        kept.append(sent)
    return "".join(kept), deleted
