"""加载归一化保留符号白名单 config/keep_symbols.txt。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

DEFAULT_PATH = "config/keep_symbols.txt"

# 文件缺失时的最小兜底集合
_FALLBACK = {
    "Some", "None", "Ok", "Err", "Option", "Result", "Box", "Vec", "String",
    "self", "Self", "println", "print", "vec", "panic", "unwrap", "new",
    "printf", "malloc", "free", "memcpy", "memset", "NULL", "sizeof",
}


def _parse(text: str) -> set[str]:
    out: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(line)
    return out


@lru_cache(maxsize=8)
def load_keep_symbols(path: str | None = None) -> frozenset[str]:
    """读取保留符号集合；文件不存在时回退到内置最小集。"""
    p = Path(path or DEFAULT_PATH)
    if not p.exists():
        return frozenset(_FALLBACK)
    return frozenset(_parse(p.read_text(encoding="utf-8")))
