"""metadata 模块 CLI：

  python -m src.metadata --suspects data/output/xxx_suspects_v2.json
  （通道1 总是运行；--query-repo 启用 commit 信号；--baselines 启用基线扣除）
  输出 xxx_suspects_final.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loguru import logger

from src.normalize.store import DEFAULT_DB

from .config import load_metadata_settings
from .runner import DEFAULT_OUTPUT_DIR, run_metadata


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.metadata", description="三个辅助信号通道。")
    p.add_argument("--suspects", required=True, help="阶段 3/4 的 *_suspects(_v2).json")
    p.add_argument("--db", default=DEFAULT_DB, help=f"历史函数库（默认 {DEFAULT_DB}）")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--query-repo", default=None, help="新作品仓库路径（启用 commit 信号通道，需含 _meta.json）")
    p.add_argument("--baselines", action="store_true", help="启用基线扣除（需 Qdrant 中已有 is_baseline 数据）")
    p.add_argument("--qdrant-path", default=None, help="本地 Qdrant 路径")
    p.add_argument("--qdrant-url", default=None, help="Qdrant 服务地址")
    return p


def _load_meta_commits(query_repo: str) -> list[dict]:
    meta = Path(query_repo) / "_meta.json"
    if not meta.is_file():
        logger.warning("未找到 {}，跳过 commit 信号通道", meta)
        return []
    return json.loads(meta.read_text(encoding="utf-8")).get("commits", [])


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suspects = Path(args.suspects)
    if not suspects.is_file():
        logger.error("suspects 文件不存在：{}", suspects)
        return 1

    settings = load_metadata_settings()

    baseline_matcher = None
    if args.baselines:
        from src.embed.embedder import get_embedder
        from src.embed.settings import load_settings
        from src.embed.vector_store import VectorStore

        from .baseline import VectorBaselineMatcher

        st = load_settings()
        store = VectorStore(
            st.qdrant.collection,
            url=None if args.qdrant_path else (args.qdrant_url or st.qdrant.url),
            path=args.qdrant_path,
        )
        baseline_matcher = VectorBaselineMatcher(get_embedder(show_progress=False), store)

    meta_commits = None
    if args.query_repo:
        meta_commits = _load_meta_commits(args.query_repo)

    run_metadata(
        suspects, db_path=args.db, output_dir=args.output_dir, settings=settings,
        baseline_matcher=baseline_matcher,
        query_repo=args.query_repo if meta_commits else None,
        meta_commits=meta_commits or None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
