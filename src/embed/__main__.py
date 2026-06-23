"""embed 模块 CLI：

  python -m src.embed build --all
  python -m src.embed query --repo <新作品路径>

无 docker 时可用本地向量库：--qdrant-path data/db/qdrant_local 或 --in-memory。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from loguru import logger

from src.normalize.store import DEFAULT_DB as FUNCTIONS_DB

from .build import build_index
from .embedder import get_embedder
from .query import DEFAULT_OUTPUT_DIR, print_report, query_repo
from .settings import load_settings
from .vector_store import VectorStore


def _make_store(args, collection: str, url: str) -> VectorStore:
    return VectorStore(
        collection,
        url=None if (args.qdrant_path or args.in_memory) else url,
        path=args.qdrant_path,
        in_memory=args.in_memory,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.embed", description="函数级代码嵌入与向量检索。")
    p.add_argument("--settings", default=None, help="settings.yaml 路径")
    p.add_argument("--qdrant-path", default=None, help="本地 Qdrant 存储目录（无 docker 时用）")
    p.add_argument("--in-memory", action="store_true", help="使用内存 Qdrant（仅测试/演示）")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="遍历 functions.db 建立向量库")
    pb.add_argument("--all", action="store_true", help="遍历全部函数（占位开关，当前即全量）")
    pb.add_argument("--db", default=FUNCTIONS_DB, help=f"functions.db 路径（默认 {FUNCTIONS_DB}）")
    pb.add_argument("--recreate", action="store_true", help="重建 collection（清空已有向量）")
    pb.add_argument("--min-lines", type=int, default=0, help="只向量化行数 >= 此值的函数（functions.db 仍保留全部；0=全部）")

    pq = sub.add_parser("query", help="对新作品检索召回")
    pq.add_argument("--repo", required=True, help="新作品仓库目录")
    pq.add_argument("--repos-root", default="data/repos", help="仓库根目录（用于推断 repo_id）")
    pq.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=f"召回 JSON 输出目录（默认 {DEFAULT_OUTPUT_DIR}）")
    pq.add_argument("--top-k", type=int, default=None, help="每函数召回数（默认取 settings）")
    pq.add_argument("--with-simhash", action="store_true", help="先 SimHash 粗筛候选集再做向量检索")
    pq.add_argument("--idf", default=None, help="SimHash IDF 表路径（默认 data/db/idf.json）")
    pq.add_argument("--simhash-index", default=None, help="SimHash 索引路径（默认 data/db/simhash_index.pkl）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.settings)
    store = _make_store(args, settings.qdrant.collection, settings.qdrant.url)

    if args.cmd == "build":
        embedder = get_embedder(settings.embedding)
        res = build_index(args.db, store, embedder, recreate=args.recreate, min_lines=args.min_lines)
        logger.info("建库完成：{}", res)
        return 0

    if args.cmd == "query":
        repo = Path(args.repo)
        if not repo.is_dir():
            logger.error("仓库目录不存在：{}", repo)
            return 1
        embedder = get_embedder(settings.embedding)
        top_k = args.top_k or settings.retrieval.top_k
        simhash_query = None
        if args.with_simhash:
            from src.simhash.build import DEFAULT_IDF, DEFAULT_INDEX, SimHashQuery

            simhash_query = SimHashQuery(args.idf or DEFAULT_IDF, args.simhash_index or DEFAULT_INDEX)
        recall = query_repo(
            repo, store, embedder,
            top_k=top_k, repos_root=args.repos_root, output_dir=args.output_dir,
            simhash_query=simhash_query,
        )
        print_report(recall)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
