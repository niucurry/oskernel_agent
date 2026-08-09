"""tree-sitter 解析器与语言识别（rust / c 共享，asm 走文本处理）。"""

from __future__ import annotations

from functools import lru_cache

import tree_sitter_c as tsc
import tree_sitter_rust as tsr
from tree_sitter import Language, Parser

# 扩展名 → 语言
EXT_LANG: dict[str, str] = {
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".S": "asm",
    ".asm": "asm",
}

SUPPORTED_EXTS = tuple(EXT_LANG)


def lang_of(path) -> str | None:
    """根据扩展名返回语言（rust/c/asm），不支持的返回 None。注意 .S 大小写敏感。"""
    from pathlib import Path

    suffix = Path(path).suffix
    if suffix in EXT_LANG:
        return EXT_LANG[suffix]
    # 兼容小写 .s 汇编
    if suffix.lower() == ".s":
        return "asm"
    return None


@lru_cache(maxsize=None)
def get_parser(lang: str) -> Parser:
    """返回缓存的 tree-sitter Parser（仅 rust/c 有 grammar）。"""
    if lang == "rust":
        return Parser(Language(tsr.language()))
    if lang == "c":
        return Parser(Language(tsc.language()))
    raise ValueError(f"无 tree-sitter grammar 的语言：{lang}")


def parse(code: bytes, lang: str):
    """解析源码字节为语法树。"""
    return get_parser(lang).parse(code)
