"""review 模块 CLI：

  python -m src.review --suspects data/output/xxx_suspects.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from .config import load_llm_settings
from .llm import OpenAICompatClient
from .reviewer import DEFAULT_OUTPUT_DIR, run_review


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.review", description="基于 LLM 的嫌疑对复核。")
    p.add_argument("--suspects", required=True, help="阶段 3 的 *_suspects.json 路径")
    p.add_argument("--settings", default=None, help="settings.yaml 路径")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"输出目录（默认 {DEFAULT_OUTPUT_DIR}）")
    p.add_argument("--concurrency", type=int, default=None, help="并发度（覆盖 settings）")
    p.add_argument("--limit", type=int, default=None, help="只复核前 N 个 review 档（成本/调试控制）")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    suspects = Path(args.suspects)
    if not suspects.is_file():
        logger.error("suspects 文件不存在：{}", suspects)
        return 1

    settings = load_llm_settings(args.settings)
    if args.concurrency:
        settings.concurrency = args.concurrency
    if not settings.api_key:
        logger.error("未设置 LLM_API_KEY 环境变量（可写入 .env）。复核需要可用的 LLM API。")
        return 2

    client = OpenAICompatClient(settings)
    run_review(suspects, client, settings, output_dir=args.output_dir, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
