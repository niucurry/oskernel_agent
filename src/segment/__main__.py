"""segment 模块 CLI：

  python -m src.segment --suspects data/output/xxx_suspects.json
  （输出 xxx_suspects_v2.json，原文件保留）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from src.embed.embedder import get_embedder

from .verify import DEFAULT_OUTPUT_DIR, run_segment


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.segment", description="函数内分段向量验证。")
    p.add_argument("--suspects", required=True, help="阶段 3/4 的 *_suspects.json 路径")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"输出目录（默认 {DEFAULT_OUTPUT_DIR}）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suspects = Path(args.suspects)
    if not suspects.is_file():
        logger.error("suspects 文件不存在：{}", suspects)
        return 1
    embedder = get_embedder(show_progress=True)
    run_segment(suspects, embedder, output_dir=args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
