"""函数级切分：把一个源文件切成若干「函数」并归一化。

- Rust：function_item（含 impl 块内方法），macro_definition 单独成类；闭包不单独切。
- C：function_definition。
- 汇编：按标号（label）切分为段。
- 小于 min_lines 行的函数跳过（宏定义除外）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .normalizer import (
    extract_asm_feature_tokens,
    extract_feature_tokens,
    normalize_asm,
    normalize_node,
)
from .ts import parse

# 小于此行数的函数视为样板/噪音跳过（宏定义除外）。整文件复制由 L0 文件指纹层兜底，
# 故提高下限不会加重漏报（见反馈 D4：5–9 行小函数泛滥）。
DEFAULT_MIN_LINES = 10

# 汇编标号：行首（可缩进）的 name: ，允许 . _ $ 开头（本地标号 .L1 等）
_ASM_LABEL = re.compile(r"^\s*([A-Za-z_.$][\w.$]*)\s*:(?!:)")


@dataclass
class ExtractedFunction:
    func_name: str
    start_line: int
    end_line: int
    lang: str
    raw_code: str
    normalized_code: str
    strings: list[str] = field(default_factory=list)
    feature_tokens: list[str] = field(default_factory=list)
    is_macro: bool = False

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1


def _c_func_name(fn_node) -> str:
    """从 C function_definition 中挖出函数名（可能被 pointer_declarator 包裹）。"""
    node = fn_node.child_by_field_name("declarator")
    while node is not None and node.type != "function_declarator":
        node = node.child_by_field_name("declarator")
    if node is None:
        return "<anon>"
    idn = node.child_by_field_name("declarator")
    while idn is not None and idn.type != "identifier":
        idn = idn.child_by_field_name("declarator")
    return idn.text.decode("utf-8", "replace") if idn is not None else "<anon>"


def _iter_top_functions(root, lang: str):
    """前序遍历，找到函数/宏节点后不再下钻（避免嵌套函数重复、闭包不单独切）。"""
    stack = [root]
    while stack:
        n = stack.pop()
        if lang == "rust":
            if n.type == "function_item":
                yield n, False
                continue
            if n.type == "macro_definition":
                yield n, True
                continue
        elif lang == "c" and n.type == "function_definition":
            yield n, False
            continue
        # 逆序压栈以保持源码顺序
        stack.extend(reversed(n.children))


def _extract_code(code: bytes, lang: str, keep, min_lines: int) -> list[ExtractedFunction]:
    tree = parse(code, lang)
    out: list[ExtractedFunction] = []
    for node, is_macro in _iter_top_functions(tree.root_node, lang):
        start = node.start_point[0] + 1
        end = node.end_point[0] + 1
        if not is_macro and (end - start + 1) < min_lines:
            continue
        if lang == "rust":
            nm = node.child_by_field_name("name")
            name = nm.text.decode("utf-8", "replace") if nm is not None else "<anon>"
        else:
            name = _c_func_name(node)
        norm = normalize_node(node, lang, keep)
        out.append(
            ExtractedFunction(
                func_name=name,
                start_line=start,
                end_line=end,
                lang=lang,
                raw_code=code[node.start_byte : node.end_byte].decode("utf-8", "replace"),
                normalized_code=norm.code,
                strings=norm.strings,
                feature_tokens=extract_feature_tokens(node, lang, keep),
                is_macro=is_macro,
            )
        )
    # 按起始行排序，保证稳定顺序
    out.sort(key=lambda f: (f.start_line, f.end_line))
    return out


def _extract_asm(text: str, min_lines: int) -> list[ExtractedFunction]:
    lines = text.splitlines()
    # 找出每个标号所在行号（1-based）
    segments: list[tuple[str, int]] = []  # (name, start_line)
    for i, line in enumerate(lines, 1):
        m = _ASM_LABEL.match(line)
        if m:
            segments.append((m.group(1), i))

    if not segments:
        return []

    # 首个标号前的内容是版权头/指示符样板（_preamble），不是函数，跳过不查重。
    spans: list[tuple[str, int, int]] = []  # (name, start, end)
    for idx, (name, start) in enumerate(segments):
        end = segments[idx + 1][1] - 1 if idx + 1 < len(segments) else len(lines)
        spans.append((name, start, end))

    out: list[ExtractedFunction] = []
    for name, start, end in spans:
        raw = "\n".join(lines[start - 1 : end])
        norm = normalize_asm(raw)
        # 阈值按归一化后的有效行数算：去掉注释/空行后仍 < min_lines 的段是样板噪音，跳过。
        # （版权注释撑大的“虚胖”段在此被过滤，避免纯指示符骨架误判相似。）
        if norm.code.count("\n") + 1 < min_lines:
            continue
        out.append(
            ExtractedFunction(
                func_name=name,
                start_line=start,
                end_line=end,
                lang="asm",
                raw_code=raw,
                normalized_code=norm.code,
                strings=norm.strings,
                feature_tokens=extract_asm_feature_tokens(raw),
            )
        )
    return out


def extract_functions(
    code: str,
    lang: str,
    *,
    keep=None,
    min_lines: int = DEFAULT_MIN_LINES,
) -> list[ExtractedFunction]:
    """从单文件源码切分并归一化出函数列表。"""
    if lang == "asm":
        return _extract_asm(code, min_lines)
    return _extract_code(code.encode("utf-8"), lang, keep, min_lines)
