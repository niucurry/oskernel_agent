"""simhash 模块 CLI：

  python -m src.simhash build [--db data/db/functions.db]
  python -m src.simhash query --repo <仓库路径>      # 报告每函数 SimHash 召回候选数
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from statistics import mean

from loguru import logger

from src.normalize.runner import derive_repo_id, normalize_repo
from src.normalize.store import DEFAULT_DB, FunctionStore

from .build import DEFAULT_IDF, DEFAULT_INDEX, SimHashQuery, build_index


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.simhash", description="SimHash 函数粗筛层。")
    p.add_argument("--idf", default=DEFAULT_IDF, help=f"IDF 权重表（默认 {DEFAULT_IDF}）")
    p.add_argument("--index", default=DEFAULT_INDEX, help=f"SimHash 索引（默认 {DEFAULT_INDEX}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="从 functions.db 建立 IDF + SimHash 索引")
    pb.add_argument("--db", default=DEFAULT_DB, help=f"functions.db（默认 {DEFAULT_DB}）")

    pq = sub.add_parser("query", help="对仓库做 SimHash 粗筛，报告候选规模")
    pq.add_argument("--repo", required=True, help="仓库目录")
    pq.add_argument("--repos-root", default="data/repos")
    pq.add_argument("--no-relax", action="store_true", help="关闭比特松弛")
    return p


def _query_repo(args) -> int:
    repo = Path(args.repo)
    if not repo.is_dir():
        logger.error("仓库目录不存在：{}", repo)
        return 1
    repo_id = derive_repo_id(repo, Path(args.repos_root))
    with FunctionStore(":memory:") as ns:
        normalize_repo(repo, ns, repo_id=repo_id, repos_root=args.repos_root)
        ns.conn.row_factory = sqlite3.Row
        rows = ns.conn.execute("SELECT func_name, feature_tokens FROM functions").fetchall()

    sq = SimHashQuery(args.idf, args.index, relax=not args.no_relax)
    sizes = [len(sq.query(json.loads(r["feature_tokens"] or "[]"))) for r in rows]
    logger.info(
        "[{}] {} 个函数；SimHash 候选集：总 {}，均值 {:.1f}，最大 {}",
        repo_id, len(rows), sum(sizes), mean(sizes) if sizes else 0, max(sizes) if sizes else 0,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "build":
        res = build_index(args.db, idf_path=args.idf, index_path=args.index)
        logger.info("建库完成：{}", res)
        return 0
    if args.cmd == "query":
        return _query_repo(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
