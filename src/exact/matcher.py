"""精确比对适配层（difflib 自建行级比对器）。

接口：ExactMatcher.match(code_a, code_b, lang) -> ExactMatchResult。

实现：
- exact 通道：直接对两段 raw 代码逐行 difflib 比对，原文相同的行 → exact；
- renamed 通道：对 raw 代码做「逐行保序的轻量掩码」（标识符→ID、数字→NUM、
  字符串→STR、去注释，但不重排行）后再 difflib 比对，仅掩码后才相同的行 → renamed。

之所以 renamed 通道用「逐行掩码」而非 src.normalize 的 normalized_code：后者按 AST
语句重排了行，行号无法映射回原文件；逐行掩码保持行数与行号不变，使 matched_spans
能精确换算回绝对文件行号（见 remap_spans）。

纯 Python 实现，无外部进程；若将来换成命令行比对器，可在此类内用 subprocess 封装并
设置 30s 超时。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# 语言关键字（掩码时保留，不替换为 ID）
_RUST_KW = {
    "fn", "let", "mut", "if", "else", "match", "for", "while", "loop", "return",
    "impl", "struct", "enum", "trait", "pub", "use", "mod", "const", "static",
    "unsafe", "self", "Self", "as", "in", "ref", "move", "where", "type", "dyn",
    "break", "continue", "true", "false", "crate", "super", "async", "await", "fn",
}
_C_KW = {
    "int", "char", "void", "short", "long", "unsigned", "signed", "float", "double",
    "return", "if", "else", "for", "while", "do", "switch", "case", "break",
    "continue", "struct", "union", "enum", "typedef", "static", "const", "extern",
    "sizeof", "goto", "default", "volatile", "register", "inline",
}
_KEYWORDS = {"rust": _RUST_KW, "c": _C_KW, "asm": set()}

_TOKEN = re.compile(
    r'"(?:[^"\\]|\\.)*"'      # 双引号字符串
    r"|'(?:[^'\\]|\\.)*'"     # 单引号字符/字符串
    r"|0[xX][0-9a-fA-F]+"     # 十六进制
    r"|\d+\.\d+|\d+"          # 数字
    r"|[A-Za-z_]\w*"          # 标识符
    r"|\S"                    # 其它单字符（运算符/标点）
)


@dataclass
class ExactMatchResult:
    similar_line_ratio: float
    matched_spans: list[tuple[int, int, int, int]] = field(default_factory=list)
    match_type_per_span: list[str] = field(default_factory=list)
    exact_match_lines: int = 0
    renamed_match_lines: int = 0


def _strip_comments(line: str, lang: str) -> str:
    line = re.sub(r"/\*.*?\*/", "", line)
    line = re.sub(r"//.*$", "", line)
    if lang == "asm":
        line = re.sub(r"[#;].*$", "", line)
    return line


def _mask_line(line: str, lang: str) -> str:
    """逐行掩码：去注释 + 标识符/数字/字符串占位，保留关键字与运算符。"""
    kw = _KEYWORDS.get(lang, set())
    out: list[str] = []
    for tok in _TOKEN.findall(_strip_comments(line, lang)):
        c = tok[0]
        if c == '"' or c == "'":
            out.append("STR")
        elif c.isdigit():
            out.append("NUM")
        elif c.isalpha() or c == "_":
            out.append(tok if tok in kw else "ID")
        else:
            out.append(tok)
    return " ".join(out)


def _numbered_nonblank(code: str) -> tuple[list[int], list[str]]:
    """返回 (行号列表(1-based, 函数内), 去空行后的文本列表)。"""
    nos: list[int] = []
    texts: list[str] = []
    for i, line in enumerate(code.splitlines(), 1):
        if line.strip():
            nos.append(i)
            texts.append(line)
    return nos, texts


def _equal_blocks(seq_a: list[str], seq_b: list[str]):
    """difflib 的相等块 (i, j, n)，过滤哨兵零块。"""
    sm = SequenceMatcher(a=seq_a, b=seq_b, autojunk=False)
    return [(i, j, n) for i, j, n in sm.get_matching_blocks() if n > 0]


class ExactMatcher:
    def match(self, code_a: str, code_b: str, lang: str = "rust") -> ExactMatchResult:
        a_no, a_text = _numbered_nonblank(code_a)
        b_no, b_text = _numbered_nonblank(code_b)
        if not a_text or not b_text:
            return ExactMatchResult(similar_line_ratio=0.0)

        # 1) exact 通道：原文逐行比对
        exact_spans: list[tuple[int, int, int, int]] = []
        exact_a: set[int] = set()
        for i, j, n in _equal_blocks(a_text, b_text):
            exact_spans.append((a_no[i], a_no[i + n - 1], b_no[j], b_no[j + n - 1]))
            exact_a.update(a_no[i : i + n])

        # 2) renamed 通道：逐行掩码后比对
        masked_a = [_mask_line(t, lang) for t in a_text]
        masked_b = [_mask_line(t, lang) for t in b_text]
        renamed_spans: list[tuple[int, int, int, int]] = []
        matched_a: set[int] = set(exact_a)
        for i, j, n in _equal_blocks(masked_a, masked_b):
            block_nos = a_no[i : i + n]
            matched_a.update(block_nos)
            if not all(x in exact_a for x in block_nos):  # 含非原文相同的行 → renamed
                renamed_spans.append((a_no[i], a_no[i + n - 1], b_no[j], b_no[j + n - 1]))

        denom = max(len(a_text), len(b_text))
        ratio = len(matched_a) / denom if denom else 0.0
        return ExactMatchResult(
            similar_line_ratio=ratio,
            matched_spans=exact_spans + renamed_spans,
            match_type_per_span=["exact"] * len(exact_spans) + ["renamed"] * len(renamed_spans),
            exact_match_lines=len(exact_a),
            renamed_match_lines=len(matched_a - exact_a),
        )


def remap_spans(
    spans: list[tuple[int, int, int, int]],
    a_start_line: int,
    b_start_line: int,
) -> list[tuple[int, int, int, int]]:
    """把函数内 1-based 行号换算回原始文件绝对行号。

    函数内第 1 行 == 文件第 start_line 行，故 abs = start_line - 1 + local。
    """
    da = a_start_line - 1
    db = b_start_line - 1
    return [(a_s + da, a_e + da, b_s + db, b_e + db) for (a_s, a_e, b_s, b_e) in spans]
