"""report 模块 CLI：

  python -m src.report report --reviewed X_reviewed.json --recall X_recall.json
  python -m src.report profile --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from src.normalize.store import DEFAULT_DB

from .generate import DEFAULT_OUTPUT_DIR, run_report
from .profile import DEFAULT_PROFILE_DIR, DEFAULT_REPOS_ROOT, run_profiles


def _maybe_client(no_llm: bool):
    if no_llm:
        return None
    from src.review.config import load_llm_settings
    from src.review.llm import OpenAICompatClient

    s = load_llm_settings()
    if not s.api_key:
        logger.warning("未设置 LLM_API_KEY，报告将用模板兜底（不调用 LLM）")
        return None
    return OpenAICompatClient(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.report", description="查重报告 / 历史作品档案生成。")
    p.add_argument("--no-llm", action="store_true", help="不调用 LLM，用模板兜底")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("report", help="从 reviewed/suspects + recall 生成报告")
    pr.add_argument("--reviewed", required=True, help="*_reviewed.json 或 *_suspects_final.json")
    pr.add_argument("--recall", required=True, help="*_recall.json（用于创新点章节）")
    pr.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)

    pp = sub.add_parser("profile", help="为历史仓库生成作品档案")
    pp.add_argument("--all", action="store_true", help="为 functions.db 中全部仓库生成")
    pp.add_argument("--db", default=DEFAULT_DB)
    pp.add_argument("--repos-root", default=DEFAULT_REPOS_ROOT)
    pp.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    client = _maybe_client(args.no_llm)

    if args.cmd == "report":
        for f in (args.reviewed, args.recall):
            if not Path(f).is_file():
                logger.error("文件不存在：{}", f)
                return 1
        run_report(args.reviewed, args.recall, client=client, output_dir=args.output_dir)
        return 0

    if args.cmd == "profile":
        if not args.all:
            logger.error("请使用 --all")
            return 1
        res = run_profiles(args.db, repos_root=args.repos_root, profile_dir=args.profile_dir, client=client)
        logger.info("档案生成完成：{}", res)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
