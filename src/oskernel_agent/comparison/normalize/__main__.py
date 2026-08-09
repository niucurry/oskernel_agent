"""normalize 模块 CLI：

  python -m oskernel_agent.comparison.normalize --repo data/repos/2024/team-x
  python -m oskernel_agent.comparison.normalize --all
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from .extract import DEFAULT_MIN_LINES
from .runner import (
    DEFAULT_MAX_LINES,
    DEFAULT_REPOS_ROOT,
    normalize_all,
    normalize_repo,
)
from .store import DEFAULT_DB, FunctionStore


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m oskernel_agent.comparison.normalize",
        description="基于 tree-sitter 的代码切分与归一化，结果写入 SQLite。",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--repo", help="单个仓库目录路径")
    g.add_argument("--all", action="store_true", help="遍历 --repos-root 下全部仓库")
    p.add_argument("--repos-root", default=DEFAULT_REPOS_ROOT, help=f"仓库根目录（默认 {DEFAULT_REPOS_ROOT}）")
    p.add_argument("--repo-id", default=None, help="覆盖自动推断的 repo_id（仅 --repo 时有效）")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 输出路径（默认 {DEFAULT_DB}）")
    p.add_argument("--min-lines", type=int, default=DEFAULT_MIN_LINES, help=f"小于该行数的函数跳过（默认 {DEFAULT_MIN_LINES}）")
    p.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES, help=f"超过该行数的文件跳过（默认 {DEFAULT_MAX_LINES}）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.all:
        results = normalize_all(
            args.repos_root, args.db, min_lines=args.min_lines, max_lines=args.max_lines
        )
        total = sum(r["functions"] for r in results)
        logger.info("全部完成：{} 个仓库，共 {} 个函数", len(results), total)
        return 0

    repo = Path(args.repo)
    if not repo.is_dir():
        logger.error("仓库目录不存在：{}", repo)
        return 1
    with FunctionStore(args.db) as store:
        res = normalize_repo(
            repo,
            store,
            repo_id=args.repo_id,
            repos_root=args.repos_root,
            min_lines=args.min_lines,
            max_lines=args.max_lines,
        )
    logger.info("完成：{} 个函数，模块分布 {}", res["functions"], res["distribution"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
