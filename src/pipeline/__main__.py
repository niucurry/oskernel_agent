"""全流水线总入口：

  python -m src.pipeline --repo <新作品路径或 git url> [--top-k 20]
                         [--resume-from <step>] [--no-simhash] [--baselines]

按序执行 ingest → fastpath → recall(含 normalize) → exact → segment → metadata
→ ai_detect → report，每步落盘中间结果，打印每步耗时与漏斗数字。
（LLM 复核步已下线：tier 由 exact/segment/metadata 确定性级联判定。）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from loguru import logger

from src.normalize.store import DEFAULT_DB

from .steps import STEPS, build_local_meta, local_ingest, tier_counts

DEFAULT_OUTPUT = "data/output"
DEFAULT_QDRANT = "data/db/qdrant_local"
DEFAULT_IDF = "data/db/idf.json"
DEFAULT_INDEX = "data/db/simhash_index.pkl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.pipeline", description="作品查重全流水线。")
    p.add_argument("--repo", required=True, help="新作品本地路径或 git url")
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--resume-from", choices=STEPS, default=None, help="从指定步骤续跑（需前序产物存在）")
    p.add_argument("--no-simhash", action="store_true", help="召回不启用 SimHash 粗筛")
    p.add_argument("--baselines", action="store_true", help="启用基线扣除（需 Qdrant 已有基线数据）")
    p.add_argument("--skip-ai-detect", action="store_true",
                   help="跳过 AI 生成代码检测（无参考模型/GPU 时；report 章六给出未运行说明）")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--qdrant-path", default=DEFAULT_QDRANT)
    p.add_argument("--idf", default=DEFAULT_IDF)
    p.add_argument("--simhash-index", default=DEFAULT_INDEX)
    p.add_argument("--repos-root", default="data/repos")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    return p


def _should_run(step: str, resume_from: str | None) -> bool:
    if resume_from is None:
        return True
    return STEPS.index(step) >= STEPS.index(resume_from)


def main(argv: list[str] | None = None) -> int:  # noqa: C901 — 顺序编排
    from dotenv import load_dotenv
    load_dotenv()
    args = build_parser().parse_args(argv)

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    funnel: dict[str, object] = {}
    timings: dict[str, float] = {}

    def timed(step, fn):
        t0 = time.perf_counter()
        r = fn()
        timings[step] = round(time.perf_counter() - t0, 2)
        logger.info("[{}] 用时 {:.2f}s", step, timings[step])
        return r

    # ---- ingest ----
    repo_path = local_ingest(args.repo, Path(args.output_dir) / "_repos")
    repo_name = repo_path.name
    filematch_path = out / f"{repo_name}_filematch.json"
    recall_path = out / f"{repo_name}_recall.json"
    suspects_path = out / f"{repo_name}_suspects.json"
    v2_path = out / f"{repo_name}_suspects_v2.json"
    final_path = out / f"{repo_name}_suspects_final.json"
    ai_detect_path = out / f"{repo_name}_ai_detect.json"

    meta_commits = []
    if _should_run("ingest", args.resume_from):
        meta_commits = timed("ingest", lambda: build_local_meta(repo_path))
        logger.info("[ingest] {} | commits={}", repo_path, len(meta_commits))
    else:
        meta_commits = build_local_meta(repo_path)

    # 共享后端（按需懒加载）
    embedder = None
    vector_store = None          # 本地 Qdrant local 模式独占文件锁，全流程共用一个实例，
                                 # 避免 recall 与 metadata(--baselines) 各建实例触发 AlreadyLocked

    def get_emb():
        nonlocal embedder
        if embedder is None:
            from src.embed.embedder import get_embedder
            embedder = get_embedder(show_progress=False)
        return embedder

    def get_store():
        nonlocal vector_store
        if vector_store is None:
            from src.embed.settings import load_settings
            from src.embed.vector_store import VectorStore
            st = load_settings()
            vector_store = VectorStore(st.qdrant.collection, path=args.qdrant_path)
        return vector_store

    # ---- fastpath (L0 文件指纹层) ----
    skip_files: set[str] = set()
    if _should_run("fastpath", args.resume_from):
        from src.fastpath.scan import scan_repo
        fm = timed("fastpath", lambda: scan_repo(
            repo_path, db_path=args.db, repos_root=args.repos_root, output_dir=out))
        skip_files = set(fm["skip_files"])
        funnel["fastpath_filematch"] = len(fm["matched_files"])
    elif filematch_path.exists():
        skip_files = set(json.loads(filematch_path.read_text(encoding="utf-8")).get("skip_files", []))

    # ---- recall (含 normalize) ----
    if _should_run("recall", args.resume_from):
        from src.embed.query import query_repo

        store = get_store()
        simhash_query = None
        if not args.no_simhash and Path(args.idf).exists() and Path(args.simhash_index).exists():
            from src.simhash.build import SimHashQuery
            simhash_query = SimHashQuery(args.idf, args.simhash_index)

        def _recall():
            return query_repo(repo_path, store, get_emb(), top_k=args.top_k,
                              repos_root=args.repos_root, output_dir=out, simhash_query=simhash_query,
                              skip_files=skip_files)
        recall = timed("recall", _recall)
        funnel["recall_query_funcs"] = len(recall["results"])
        funnel["recall_candidates"] = recall["simhash"]["total_recalled"]

    # ---- exact ----
    if _should_run("exact", args.resume_from):
        from src.exact.verify import verify_recall
        res = timed("exact", lambda: verify_recall(recall_path, db_path=args.db, output_dir=out))
        funnel["exact_compared"] = res["compared_pairs"]
        funnel["after_exact"] = res["tier_counts"]

    # ---- segment ----
    if _should_run("segment", args.resume_from):
        from src.segment.verify import run_segment
        res = timed("segment", lambda: run_segment(suspects_path, get_emb(), output_dir=out))
        funnel["after_segment"] = tier_counts(res["suspects"])

    # ---- metadata ----
    if _should_run("metadata", args.resume_from):
        from src.metadata.runner import run_metadata
        baseline_matcher = None
        if args.baselines:
            from src.metadata.baseline import VectorBaselineMatcher
            # 复用 recall 步已建的 store（共用 Qdrant local 文件锁），避免 AlreadyLocked
            baseline_matcher = VectorBaselineMatcher(get_emb(), get_store())
        # commit 信号通道已停用（git blame 逐函数分析过慢、对查重结论非必需）
        res = timed("metadata", lambda: run_metadata(
            v2_path, db_path=args.db, output_dir=out, baseline_matcher=baseline_matcher))
        funnel["metadata"] = res["metadata_summary"]
        funnel["after_metadata"] = tier_counts(res["suspects"])

    # ---- ai_detect（AI 生成代码检测，独立于查重漏斗；缺模型则优雅跳过）----
    if not args.skip_ai_detect and _should_run("ai_detect", args.resume_from):
        from src.ai_detect.runner import run_ai_detect
        # 排除借鉴代码：文件级（fastpath 整文件命中）+ 函数级（查重命中的可疑函数），
        # 只对未匹配上的原创代码做 AI 生成检测（借鉴自参考 OS 的代码不计入）
        exclude_files = {p.replace("\\", "/") for p in skip_files}
        exclude_funcs: set[tuple[str, str]] = set()
        if final_path.exists():
            try:
                _sd = json.loads(final_path.read_text(encoding="utf-8"))
                exclude_funcs = {
                    (s.get("query_func", {}).get("file_path", "").replace("\\", "/"),
                     s.get("query_func", {}).get("func_name", ""))
                    for s in _sd.get("suspects", [])
                    if s.get("tier") in ("confirmed", "review", "weak")
                }
            except (OSError, json.JSONDecodeError):
                exclude_funcs = set()
        res = timed("ai_detect", lambda: run_ai_detect(
            repo_path, output_dir=out, repo_name=repo_name, show_progress=False,
            exclude_files=exclude_files, exclude_funcs=exclude_funcs))
        funnel["ai_detect_status"] = res.get("status")
        if res.get("status") == "ok":
            funnel["ai_detect"] = res["aggregated"]["overall"]
        else:
            funnel["ai_detect_reason"] = res.get("reason")

    # ---- report（语义级对比报告，直接产出 HTML + GitLab 在线链接）----
    if _should_run("report", args.resume_from):
        from src.report.semantic_compare import run_semantic_compare
        res = timed("report", lambda: run_semantic_compare(
            suspects_path   = final_path,
            query_repo_path = str(repo_path),
            recall_path     = recall_path,
            output_dir      = out,
            filematch_path  = filematch_path,
            ai_detect_path  = ai_detect_path,
        ))
        funnel["report"] = res["html_path"]
        funnel["report_html"] = res["html_path"]

    logger.info("===== 漏斗 =====")
    for k, v in funnel.items():
        logger.info("  {}: {}", k, v)
    logger.info("===== 耗时 (s) =====  {}", timings)
    print(json.dumps({"funnel": funnel, "timings": timings,
                      "report": funnel.get("report")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
