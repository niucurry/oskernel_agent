"""exact 模块 CLI：

  python -m src.exact verify --recall data/output/xxx_recall.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from src.normalize.store import DEFAULT_DB

from .verify import DEFAULT_OUTPUT_DIR, verify_recall


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.exact", description="精确比对与嫌疑对生成。")
    sub = p.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("verify", help="对召回结果做精确比对，生成 suspects.json")
    pv.add_argument("--recall", required=True, help="阶段 2 的 *_recall.json 路径")
    pv.add_argument("--db", default=DEFAULT_DB, help=f"历史函数库（默认 {DEFAULT_DB}）")
    pv.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"输出目录（默认 {DEFAULT_OUTPUT_DIR}）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "verify":
        recall = Path(args.recall)
        if not recall.is_file():
            logger.error("召回文件不存在：{}", recall)
            return 1
        verify_recall(recall, db_path=args.db, output_dir=args.output_dir)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
