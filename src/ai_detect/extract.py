"""桥接层：用本仓库 `src.normalize` 的 tree-sitter 解析器抽取函数，转成上游 FunctionBlock。

这样就**不需要安装** `tree-sitter-languages`（上游 extractor.py 的依赖，与本仓库锁定的
tree-sitter==0.25.2 冲突）。函数切分口径与查重主流程完全一致（同一套解析/排除规则）。

仅桥接 rust / c（DetectCodeGPT 论文覆盖、且本赛道主力语言）；asm 不在上游支持范围内，跳过。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from src.normalize.discovery import discover_files
from src.normalize.extract import extract_functions

from .vendor.ai_code_detector.models import FunctionBlock, Language

# 本仓库 normalize 的 lang 串 → 上游 Language 枚举
_LANG_MAP: dict[str, Language] = {
    "rust": Language.RUST,
    "c": Language.C,
}

# 行级注释前缀（与上游 extractor._count_loc 口径一致：统计非空、非注释行）
_COMMENT_PREFIXES = ("//", "*", "/*", "#")


def _effective_loc(source: str) -> int:
    """有效行数：非空、非纯注释行（rust/c 通用前缀）。"""
    count = 0
    for line in source.splitlines():
        s = line.strip()
        if not s or s.startswith(_COMMENT_PREFIXES):
            continue
        count += 1
    return count


def extract_blocks(
    repo: str | Path,
    *,
    min_lines: int = 5,
    max_functions: int = 0,
) -> list[FunctionBlock]:
    """遍历仓库，返回 rust/c 函数的 FunctionBlock 列表（file_path 为绝对路径）。

    Args:
        min_lines: 传给 normalize 的切分下限（上游 LOC 闸门另会把 <20 行判 Uncertain）。
        max_functions: >0 时只取前 N 个（限额/冒烟）。
    """
    repo = Path(repo).resolve()
    blocks: list[FunctionBlock] = []
    skipped_lang: set[str] = set()

    for df in discover_files(repo):
        lang = _LANG_MAP.get(df.lang)
        if lang is None:
            skipped_lang.add(df.lang)
            continue
        try:
            text = df.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("[ai_detect] 读取失败 {}: {}", df.rel_path, exc)
            continue

        for fn in extract_functions(text, df.lang, min_lines=min_lines):
            blocks.append(
                FunctionBlock(
                    name=fn.func_name,
                    qualified_name=fn.func_name,
                    source=fn.raw_code,
                    start_line=fn.start_line,
                    end_line=fn.end_line,
                    loc=_effective_loc(fn.raw_code),
                    language=lang,
                    file_path=df.path,  # 绝对路径；聚合时按 repo 根相对化
                )
            )
            if max_functions and len(blocks) >= max_functions:
                logger.info("[ai_detect] 达到 max_functions={} 上限，提前停止抽取", max_functions)
                return blocks

    if skipped_lang:
        logger.info("[ai_detect] 跳过上游不支持的语言: {}", sorted(skipped_lang))
    logger.info("[ai_detect] 抽取函数 {} 个（rust/c）", len(blocks))
    return blocks
