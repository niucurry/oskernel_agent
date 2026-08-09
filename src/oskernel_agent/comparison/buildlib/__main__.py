"""一键建库总入口：把 repos.yaml 里的历史作品建成完整可对比的历史库。

  python -m oskernel_agent.comparison.buildlib                      # 读 config/repos.yaml，全量重建
  python -m oskernel_agent.comparison.buildlib --skip-ingest        # 仓库已在 data/repos/，只重建索引
  python -m oskernel_agent.comparison.buildlib --incremental        # 新增仓库后增量补建（不清空已有向量）

按序执行 ingest（GitLab 克隆）→ normalize（切分归一化→functions.db）
→ embed（向量化→qdrant_local）→ simhash（IDF+指纹索引）。

产物全部落在 data/db/：functions.db / qdrant_local / idf.json / simhash_index.pkl，
即「阶段 A 历史库」，供 `python -m oskernel_agent.comparison.pipeline --repo <新作品>` 直接对比。
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

from oskernel_agent.comparison.normalize.store import DEFAULT_DB

DEFAULT_CONFIG = "config/repos.yaml"
DEFAULT_REPOS_ROOT = "data/repos"
DEFAULT_QDRANT = "data/db/qdrant_local"
DEFAULT_IDF = "data/db/idf.json"
DEFAULT_INDEX = "data/db/simhash_index.pkl"
DEFAULT_FAISS_INDEX = "data/db/faiss_hnsw.index"
DEFAULT_FAISS_IDS = "data/db/faiss_ids.npy"
DEFAULT_CODE_SIMHASH = "data/db/code_simhash_index.pkl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m oskernel_agent.comparison.buildlib",
        description="一键把 repos.yaml 的历史作品建成完整历史库（拉取+归一化+向量化+SimHash）。",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"历史作品清单（默认 {DEFAULT_CONFIG}）")
    p.add_argument("--baselines-config", default="config/baselines.yaml",
                   help="基线清单（公共/模板/第三方库，入库标 is_baseline 供各层扣除；默认 config/baselines.yaml）")
    p.add_argument("--skip-baselines", action="store_true", help="不连带建基线库（仅建 --config 的历史作品）")
    p.add_argument("--repos-root", default=DEFAULT_REPOS_ROOT, help=f"仓库克隆根目录（默认 {DEFAULT_REPOS_ROOT}）")
    p.add_argument("--db", default=DEFAULT_DB, help=f"functions.db 路径（默认 {DEFAULT_DB}）")
    p.add_argument("--qdrant-path", default=DEFAULT_QDRANT, help=f"本地向量库目录（默认 {DEFAULT_QDRANT}）")
    p.add_argument("--idf", default=DEFAULT_IDF, help=f"SimHash IDF 表（默认 {DEFAULT_IDF}）")
    p.add_argument("--simhash-index", default=DEFAULT_INDEX, help=f"SimHash 索引（默认 {DEFAULT_INDEX}）")
    p.add_argument("--faiss-index", default=DEFAULT_FAISS_INDEX,
                   help=f"FAISS 索引（默认 {DEFAULT_FAISS_INDEX}）")
    p.add_argument("--faiss-ids", default=DEFAULT_FAISS_IDS,
                   help=f"FAISS 函数 ID 清单（默认 {DEFAULT_FAISS_IDS}）")
    p.add_argument("--code-simhash-index", default=DEFAULT_CODE_SIMHASH,
                   help=f"归一化代码结构 SimHash 索引（默认 {DEFAULT_CODE_SIMHASH}）")
    p.add_argument("--skip-ingest", action="store_true", help="跳过 GitLab 克隆（仓库已在 repos-root）")
    p.add_argument("--force", action="store_true", help="ingest 时强制重新克隆已存在的仓库")
    p.add_argument(
        "--reclaim-source",
        action="store_true",
        help="磁盘安全模式：逐仓「浅克隆→归一化入库→删源码」，适合大量/大体积历史仓库。"
        "克隆+归一化交替进行，全程只占用单仓空间。",
    )
    p.add_argument("--depth", type=int, default=None, help="git clone 深度（--reclaim-source 默认浅克隆 depth=1）")
    p.add_argument("--no-embed", action="store_true", help="只做 拉取+归一化（建 functions.db），跳过向量化与 SimHash")
    p.add_argument("--embed-min-lines", type=int, default=0, help="只向量化行数 >= 此值的函数（functions.db 仍保留全部；0=全部）")
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
    normalized_ids: set[str] = set()

    def timed(step, fn):
        t0 = time.perf_counter()
        r = fn()
        timings[step] = round(time.perf_counter() - t0, 2)
        logger.info("[{}] 用时 {:.2f}s", step, timings[step])
        return r

    # ---- 1+2. 磁盘安全模式：逐仓 浅克隆 → 归一化 → 删源码 ----
    if args.reclaim_source:
        import shutil

        from oskernel_agent.comparison.ingest.cloner import clone_repo
        from oskernel_agent.comparison.ingest.config import load_repos
        from oskernel_agent.comparison.normalize.classify import load_classifier
        from oskernel_agent.comparison.normalize.keep_symbols import load_keep_symbols
        from oskernel_agent.comparison.normalize.runner import normalize_repo
        from oskernel_agent.comparison.normalize.store import FunctionStore

        config_path = Path(args.config)
        if not config_path.exists():
            logger.error("未找到 {}，无法建库。", config_path)
            return 1
        token = os.getenv("GITLAB_TOKEN")
        if not token:
            logger.warning("未设置 GITLAB_TOKEN，私有仓库将克隆失败")
        depth = args.depth or 1
        entries = load_repos(config_path)
        if not args.skip_baselines and Path(args.baselines_config).exists():
            base_entries = load_repos(Path(args.baselines_config))
            logger.info("[reclaim] 连带基线库 {} 个（{}）", len(base_entries), args.baselines_config)
            entries = entries + base_entries
        repos_root = Path(args.repos_root)
        logger.info("[reclaim] 磁盘安全模式：{} 个仓库，浅克隆 depth={}", len(entries), depth)

        classifier = load_classifier()
        keep = load_keep_symbols()
        ok = 0
        total_funcs = 0

        def _reclaim_build():
            nonlocal ok, total_funcs
            with FunctionStore(args.db) as store:
                for i, entry in enumerate(entries, 1):
                    dest = repos_root / entry.rel_dir
                    logger.info("=== ({}/{}) {} ===", i, len(entries), entry.repo_id)
                    try:
                        clone_repo(entry.repo_url, dest, token=token, force=args.force, depth=depth)
                        res = normalize_repo(
                            dest, store, repo_id=entry.repo_id, repos_root=repos_root,
                            classifier=classifier, keep=keep)
                        total_funcs += res["functions"]
                        if res["functions"] > 0:
                            normalized_ids.add(entry.repo_id)
                        ok += 1
                    except Exception as exc:  # noqa: BLE001 — 记录并继续
                        logger.error("[{}] 失败：{}", entry.repo_id, exc)
                    finally:
                        if dest.exists():
                            shutil.rmtree(dest, ignore_errors=True)

        timed("ingest+normalize", _reclaim_build)
        summary["ingest"] = {"total": len(entries), "ok": ok}
        summary["normalize"] = {"repos": ok, "functions": total_funcs}
        logger.info("[reclaim] 完成：成功 {}/{}，共 {} 个函数", ok, len(entries), total_funcs)
        if total_funcs == 0:
            logger.error("没有任何函数入库，终止建库。")
            return 1
        if ok != len(entries):
            logger.error("历史库不完整：逐仓构建仅成功 {}/{}，拒绝生成可查询索引。", ok, len(entries))
            return 1

    # ---- 1. ingest：GitLab 克隆 + 元数据 ----
    elif not args.skip_ingest:
        from oskernel_agent.comparison.ingest.config import write_template
        from oskernel_agent.comparison.ingest.runner import ingest

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
            force=args.force,
        ))
        ok = sum(1 for r in res if "error" not in r)
        summary["ingest"] = {"total": len(res), "ok": ok}
        if ok == 0:
            logger.error("没有任何仓库克隆成功，终止建库。")
            return 1
        if ok != len(res):
            failed = [r.get("repo_id", "?") for r in res if "error" in r]
            logger.error("历史作品克隆不完整（失败 {}/{}）：{}。拒绝继续建库。",
                         len(failed), len(res), failed)
            return 1
        # 连带克隆基线库到同一 repos_root：后续 normalize_all 会一并归一化，
        # is_baseline 由 repo_id 的 baseline_ 约定自动判定（embed/L0 各层据此扣除）。
        if not args.skip_baselines and Path(args.baselines_config).exists():
            bres = timed("ingest-baselines", lambda: ingest(
                Path(args.baselines_config), args.repos_root, token=token,
                force=args.force,
            ))
            summary["ingest_baselines"] = {"total": len(bres),
                                           "ok": sum(1 for r in bres if "error" not in r)}
            if any("error" in r for r in bres):
                failed = [r.get("repo_id", "?") for r in bres if "error" in r]
                logger.error("基线仓库克隆不完整：{}。拒绝继续建库。", failed)
                return 1
    else:
        logger.info("[ingest] 已跳过（使用 {} 下既有仓库）", args.repos_root)
        if not any(Path(args.repos_root).glob("*")):
            logger.error("{} 下没有仓库，无法建库。", args.repos_root)
            return 1

    # ---- 2. normalize：切分归一化 → functions.db（reclaim 模式已在上面完成）----
    if not args.reclaim_source:
        from oskernel_agent.comparison.normalize.runner import normalize_all

        norm = timed("normalize", lambda: normalize_all(args.repos_root, args.db))
        total_funcs = sum(r["functions"] for r in norm)
        summary["normalize"] = {"repos": len(norm), "functions": total_funcs}
        if total_funcs == 0:
            logger.error("归一化未产出任何函数，终止建库。")
            return 1

        normalized_ids = {r["repo_id"] for r in norm if r.get("functions", 0) > 0}

    # 仅数据库里“总函数数>0”不代表查全：必须逐项对齐 repos.yaml，防止某个仓库
    # clone/checkout 失败却被整体成功数掩盖。reclaim 与普通模式执行同一硬门禁。
    from oskernel_agent.comparison.buildlib.coverage import audit_config, write_manifest
    audit = audit_config(args.db, args.config)
    manifest = Path(args.db).with_name("reference_coverage.json")
    write_manifest(audit, manifest)
    summary["coverage"] = audit.as_dict()
    current_missing = [repo_id for repo_id in audit.counts if repo_id not in normalized_ids]
    if not audit.complete or current_missing:
        missing = sorted(set(audit.missing_repo_ids) | set(current_missing))
        logger.error("历史库覆盖率不合格：{}/{} 个作品有函数，缺失 {}。"
                     "已写 {}，拒绝生成索引。",
                     audit.covered, audit.configured, missing, manifest)
        return 1

    if args.no_embed:
        total = summary.get("normalize", {}).get("functions", "?")
        logger.info("[--no-embed] 已完成 拉取+归一化（共 {} 个函数）；跳过向量化/SimHash。", total)
        logger.info("后续向量化：python -m oskernel_agent.comparison.buildlib --skip-ingest 复用 data/repos，"
                    "或直接 python -m oskernel_agent.comparison.embed build --recreate / python -m oskernel_agent.comparison.simhash build")
        print(json.dumps({"summary": summary, "timings": timings}, ensure_ascii=False, indent=2))
        return 0

    # ---- 3. embed：向量化 → qdrant_local ----
    from oskernel_agent.comparison.embed.build import build_index as build_vectors
    from oskernel_agent.comparison.embed.embedder import get_embedder
    from oskernel_agent.comparison.embed.settings import load_settings
    from oskernel_agent.comparison.embed.vector_store import VectorStore

    settings = load_settings()
    store = VectorStore(settings.qdrant.collection, path=args.qdrant_path)
    embedder = get_embedder(settings.embedding)
    # 默认全量重建（recreate=True）：normalize 整库重建会改变 func id，
    # 不 recreate 会让旧向量变成对不上 id 的孤儿。增量模式才保留旧向量。
    recreate = not args.incremental
    vec = timed("embed", lambda: build_vectors(
        args.db, store, embedder, recreate=recreate, min_lines=args.embed_min_lines))
    summary["embed"] = vec

    # functions.db 全量重建会重排 func_id，FAISS 必须同代重建；旧实现遗漏该步骤，
    # 查询可能拿旧向量映射到新函数，造成随机漏召回/错来源。
    from oskernel_agent.comparison.embed.faiss_store import build_faiss_index
    faiss_res = timed("faiss", lambda: build_faiss_index(
        store, index_path=args.faiss_index, ids_path=args.faiss_ids, db_path=args.db))
    summary["faiss"] = {"vectors": int(faiss_res[0].ntotal), "ids": len(faiss_res[1])}

    # ---- 4. simhash：IDF + 指纹分段索引 ----
    from oskernel_agent.comparison.simhash.build import build_index as build_simhash

    sim = timed("simhash", lambda: build_simhash(
        args.db, idf_path=args.idf, index_path=args.simhash_index))
    summary["simhash"] = sim

    from oskernel_agent.comparison.simhash.code_index import build_code_index
    code_sim = timed("code-simhash", lambda: build_code_index(
        args.db, index_path=args.code_simhash_index))
    summary["code_simhash"] = code_sim

    logger.info("===== 历史库建成 =====")
    logger.info("  functions.db : {}", args.db)
    logger.info("  qdrant_local : {}", args.qdrant_path)
    logger.info("  simhash idf  : {}", args.idf)
    logger.info("  simhash index: {}", args.simhash_index)
    logger.info("  code simhash : {}", args.code_simhash_index)
    logger.info("  faiss index  : {} / {}", args.faiss_index, args.faiss_ids)
    logger.info("===== 耗时 (s) =====  {}", timings)
    print(json.dumps({"summary": summary, "timings": timings}, ensure_ascii=False, indent=2))
    logger.info("现在可直接对比：python -m oskernel_agent.comparison.pipeline --repo <新作品路径或URL>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
