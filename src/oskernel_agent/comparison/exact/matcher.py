"""精确比对适配层（difflib 自建行级比对器）。

接口：ExactMatcher.match(code_a, code_b, lang) -> ExactMatchResult。

实现：
- exact 通道：直接对两段 raw 代码逐行 difflib 比对，原文相同的行 → exact；
- renamed 通道：对 raw 代码做「逐行保序的轻量掩码」（标识符→ID、数字→NUM、
  字符串→STR、去注释，但不重排行）后再 difflib 比对，仅掩码后才相同的行 → renamed。

之所以 renamed 通道用「逐行掩码」而非 oskernel_agent.comparison.normalize 的 normalized_code：后者按 AST
语句重排了行，行号无法映射回原文件；逐行掩码保持行数与行号不变，使 matched_spans
能精确换算回绝对文件行号（见 remap_spans）。

纯 Python 实现，无外部进程；若将来换成命令行比对器，可在此类内用 subprocess 封装并
设置 30s 超时。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

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


def normalized_file_lines(text: str, lang: str) -> list[str]:
    """逐行去注释 + 折叠连续空白 + 去空行，返回规范化后的非空行列表。"""
    out: list[str] = []
    for line in text.splitlines():
        stripped = re.sub(r"\s+", " ", _strip_comments(line, lang)).strip()
        if stripped:
            out.append(stripped)
    return out


def normalized_file_hash(text: str, lang: str) -> str:
    """整文件规范化哈希：逐行去注释 + 折叠连续空白 + 去空行后 sha1。

    用于 L0 文件指纹层：消化「仅空格/格式/注释差异」的整文件复制（回应 D3 与待确认 #4）。
    与 ExactMatcher 共用 _strip_comments，口径一致。
    """
    joined = "\n".join(normalized_file_lines(text, lang))
    return hashlib.sha1(joined.encode("utf-8", "replace")).hexdigest()


def raw_file_hash(text: str) -> str:
    """原文 sha1（逐字节相同判定）。"""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


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


@lru_cache(maxsize=32_768)
def _prepared_lines(code: str, lang: str) -> tuple[tuple[int, ...], tuple[str, ...], tuple[str, ...]]:
    """缓存函数的非空行与轻量掩码。

    同一目标函数会与多个历史候选比较，镜像仓库也会重复出现相同源码。
    掩码是纯函数，按（语言，源码）复用不改变任何匹配结果，只避免逐 pair
    重复执行正则分词。
    """
    numbers, texts = _numbered_nonblank(code)
    masked = tuple(_mask_line(text, lang) for text in texts)
    return tuple(numbers), tuple(texts), masked


def clear_prepared_line_cache() -> None:
    """释放精确匹配阶段的热点预处理缓存，避免长流水线后续阶段换页。"""
    _prepared_lines.cache_clear()


def _equal_blocks(seq_a: list[str], seq_b: list[str]):
    """difflib 的相等块 (i, j, n)，过滤哨兵零块。"""
    sm = SequenceMatcher(a=seq_a, b=seq_b, autojunk=False)
    return [(i, j, n) for i, j, n in sm.get_matching_blocks() if n > 0]


class ExactMatcher:
    def match(self, code_a: str, code_b: str, lang: str = "rust") -> ExactMatchResult:
        a_no, a_text, masked_a = _prepared_lines(code_a, lang)
        b_no, b_text, masked_b = _prepared_lines(code_b, lang)
        if not a_text or not b_text:
            return ExactMatchResult(similar_line_ratio=0.0)

        # 1) exact 通道：原文逐行比对
        exact_spans: list[tuple[int, int, int, int]] = []
        exact_a: set[int] = set()
        for i, j, n in _equal_blocks(a_text, b_text):
            exact_spans.append((a_no[i], a_no[i + n - 1], b_no[j], b_no[j + n - 1]))
            exact_a.update(a_no[i : i + n])

        # 2) renamed 通道：逐行掩码后比对
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
