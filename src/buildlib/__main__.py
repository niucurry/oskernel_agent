"""一键建库总入口：把 repos.yaml 里的历史作品建成完整可对比的历史库。

  python -m src.buildlib                      # 读 config/repos.yaml，全量重建
  python -m src.buildlib --skip-ingest        # 仓库已在 data/repos/，只重建索引
  python -m src.buildlib --incremental        # 新增仓库后增量补建（不清空已有向量）

按序执行 ingest（GitLab 克隆+元数据）→ normalize（切分归一化→functions.db）
→ embed（向量化→qdrant_local）→ simhash（IDF+指纹索引）。

产物全部落在 data/db/：functions.db / qdrant_local / idf.json / simhash_index.pkl，
即「阶段 A 历史库」，供 `python -m src.pipeline --repo <新作品>` 直接对比。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

from src.normalize.store import DEFAULT_DB

DEFAULT_CONFIG = "config/repos.yaml"
DEFAULT_REPOS_ROOT = "data/repos"
DEFAULT_QDRANT = "data/db/qdrant_local"
DEFAULT_IDF = "data/db/idf.json"
DEFAULT_INDEX = "data/db/simhash_index.pkl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.buildlib",
        description="一键把 repos.yaml 的历史作品建成完整历史库（拉取+归一化+向量化+SimHash）。",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"历史作品清单（默认 {DEFAULT_CONFIG}）")
    p.add_argument("--repos-root", default=DEFAULT_REPOS_ROOT, help=f"仓库克隆根目录（默认 {DEFAULT_REPOS_ROOT}）")
    p.add_argument("--db", default=DEFAULT_DB, help=f"functions.db 路径（默认 {DEFAULT_DB}）")
    p.add_argument("--qdrant-path", default=DEFAULT_QDRANT, help=f"本地向量库目录（默认 {DEFAULT_QDRANT}）")
    p.add_argument("--idf", default=DEFAULT_IDF, help=f"SimHash IDF 表（默认 {DEFAULT_IDF}）")
    p.add_argument("--simhash-index", default=DEFAULT_INDEX, help=f"SimHash 索引（默认 {DEFAULT_INDEX}）")
    p.add_argument("--skip-ingest", action="store_true", help="跳过 GitLab 克隆（仓库已在 repos-root）")
    p.add_argument("--force", action="store_true", help="ingest 时强制重新克隆已存在的仓库")
    p.add_argument("--no-commit-stats", action="store_true", help="ingest 跳过逐 commit 增删行抓取（更快）")
    p.add_argument(
        "--incremental",
        action="store_true",
        help="增量补建：向量库不清空，仅补新函数（注意：normalize 仍会整库重建 func id，"
        "增量模式仅适合“向 repos.yaml 追加仓库且未重跑 normalize”的场景，常规请用默认全量重建）",
    )
    return p


def main(argv: list[str] | None = None) -> int:  # noqa: C901 — 顺序编排
    load_dotenv()
    args = build_parser().parse_args(argv)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    timings: dict[str, float] = {}
    summary: dict[str, object] = {}

    def timed(step, fn):
        t0 = time.perf_counter()
        r = fn()
        timings[step] = round(time.perf_counter() - t0, 2)
        logger.info("[{}] 用时 {:.2f}s", step, timings[step])
        return r

    # ---- 1. ingest：GitLab 克隆 + 元数据 ----
    if not args.skip_ingest:
        from src.ingest.config import write_template
        from src.ingest.runner import ingest

        config_path = Path(args.config)
        if not config_path.exists():
            write_template(config_path)
            logger.error("未找到 {}，已生成示例模板。请填入真实仓库地址后重跑。", config_path)
            return 1

        token = os.getenv("GITLAB_TOKEN")
        if not token:
            logger.warning("未设置 GITLAB_TOKEN，将匿名访问（仅公开仓库，受速率限制）")

        res = timed("ingest", lambda: ingest(
            config_path, args.repos_root, token=token,
            force=args.force, fetch_commit_stats=not args.no_commit_stats,
        ))
        ok = sum(1 for r in res if "error" not in r)
        summary["ingest"] = {"total": len(res), "ok": ok}
        if ok == 0:
            logger.error("没有任何仓库克隆成功，终止建库。")
            return 1
    else:
        logger.info("[ingest] 已跳过（使用 {} 下既有仓库）", args.repos_root)
        if not any(Path(args.repos_root).glob("*")):
            logger.error("{} 下没有仓库，无法建库。", args.repos_root)
            return 1

    # ---- 2. normalize：切分归一化 → functions.db ----
    from src.normalize.runner import normalize_all

    norm = timed("normalize", lambda: normalize_all(args.repos_root, args.db))
    total_funcs = sum(r["functions"] for r in norm)
    summary["normalize"] = {"repos": len(norm), "functions": total_funcs}
    if total_funcs == 0:
        logger.error("归一化未产出任何函数，终止建库。")
        return 1

    # ---- 3. embed：向量化 → qdrant_local ----
    from src.embed.build import build_index as build_vectors
    from src.embed.embedder import get_embedder
    from src.embed.settings import load_settings
    from src.embed.vector_store import VectorStore

    settings = load_settings()
    store = VectorStore(settings.qdrant.collection, path=args.qdrant_path)
    embedder = get_embedder(settings.embedding)
    # 默认全量重建（recreate=True）：normalize 整库重建会改变 func id，
    # 不 recreate 会让旧向量变成对不上 id 的孤儿。增量模式才保留旧向量。
    recreate = not args.incremental
    vec = timed("embed", lambda: build_vectors(args.db, store, embedder, recreate=recreate))
    summary["embed"] = vec

    # ---- 4. simhash：IDF + 指纹分段索引 ----
    from src.simhash.build import build_index as build_simhash

    sim = timed("simhash", lambda: build_simhash(
        args.db, idf_path=args.idf, index_path=args.simhash_index))
    summary["simhash"] = sim

    logger.info("===== 历史库建成 =====")
    logger.info("  functions.db : {}", args.db)
    logger.info("  qdrant_local : {}", args.qdrant_path)
    logger.info("  simhash idf  : {}", args.idf)
    logger.info("  simhash index: {}", args.simhash_index)
    logger.info("===== 耗时 (s) =====  {}", timings)
    print(json.dumps({"summary": summary, "timings": timings}, ensure_ascii=False, indent=2))
    logger.info("现在可直接对比：python -m src.pipeline --repo <新作品路径或URL>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
