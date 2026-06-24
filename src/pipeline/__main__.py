"""全流水线总入口：

  python -m src.pipeline --repo <新作品路径或 git url> [--top-k 20] [--skip-llm]
                         [--resume-from <step>] [--no-simhash] [--baselines]

按序执行 ingest → recall(含 normalize) → exact → segment → metadata → review → report，
每步落盘中间结果，打印每步耗时与漏斗数字。
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
    p.add_argument("--skip-llm", action="store_true", help="跳过 LLM 复核（report 用模板兜底）")
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
    p.add_argument("--review-limit", type=int, default=None, help="只复核前 N 个 review 档")
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
    recall_path = out / f"{repo_name}_recall.json"
    suspects_path = out / f"{repo_name}_suspects.json"
    v2_path = out / f"{repo_name}_suspects_v2.json"
    final_path = out / f"{repo_name}_suspects_final.json"
    reviewed_path = out / f"{repo_name}_reviewed.json"
    ai_detect_path = out / f"{repo_name}_ai_detect.json"

    meta_commits = []
    if _should_run("ingest", args.resume_from):
        meta_commits = timed("ingest", lambda: build_local_meta(repo_path))
        logger.info("[ingest] {} | commits={}", repo_path, len(meta_commits))
    else:
        meta_commits = build_local_meta(repo_path)

    # 共享后端（按需懒加载）
    embedder = None

    def get_emb():
        nonlocal embedder
        if embedder is None:
            from src.embed.embedder import get_embedder
            embedder = get_embedder(show_progress=False)
        return embedder

    # ---- recall (含 normalize) ----
    if _should_run("recall", args.resume_from):
        from src.embed.query import query_repo
        from src.embed.settings import load_settings
        from src.embed.vector_store import VectorStore

        st = load_settings()
        store = VectorStore(st.qdrant.collection, path=args.qdrant_path)
        simhash_query = None
        if not args.no_simhash and Path(args.idf).exists() and Path(args.simhash_index).exists():
            from src.simhash.build import SimHashQuery
            simhash_query = SimHashQuery(args.idf, args.simhash_index)

        def _recall():
            return query_repo(repo_path, store, get_emb(), top_k=args.top_k,
                              repos_root=args.repos_root, output_dir=out, simhash_query=simhash_query)
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
            from src.embed.settings import load_settings
            from src.embed.vector_store import VectorStore
            from src.metadata.baseline import VectorBaselineMatcher
            st = load_settings()
            baseline_matcher = VectorBaselineMatcher(
                get_emb(), VectorStore(st.qdrant.collection, path=args.qdrant_path))
        res = timed("metadata", lambda: run_metadata(
            v2_path, db_path=args.db, output_dir=out, baseline_matcher=baseline_matcher,
            query_repo=str(repo_path) if meta_commits else None, meta_commits=meta_commits or None))
        funnel["metadata"] = res["metadata_summary"]
        funnel["after_metadata"] = tier_counts(res["suspects"])

    # ---- review ----
    report_input = final_path
    if not args.skip_llm and _should_run("review", args.resume_from):
        from src.review.config import load_llm_settings
        from src.review.llm import OpenAICompatClient
        from src.review.reviewer import run_review
        s = load_llm_settings()
        if not s.api_key:
            logger.warning("[review] 无 LLM_API_KEY，跳过复核")
        else:
            res = timed("review", lambda: run_review(
                final_path, OpenAICompatClient(s), s, output_dir=out, limit=args.review_limit))
            funnel["review_verdicts"] = res["verdict_counts"]
            report_input = reviewed_path
    elif reviewed_path.exists():
        report_input = reviewed_path

    # ---- ai_detect（AI 生成代码检测，独立于查重漏斗；缺模型则优雅跳过）----
    if not args.skip_ai_detect and _should_run("ai_detect", args.resume_from):
        from src.ai_detect.runner import run_ai_detect
        res = timed("ai_detect", lambda: run_ai_detect(
            repo_path, output_dir=out, repo_name=repo_name, show_progress=False))
        funnel["ai_detect_status"] = res.get("status")
        if res.get("status") == "ok":
            funnel["ai_detect"] = res["aggregated"]["overall"]
        else:
            funnel["ai_detect_reason"] = res.get("reason")

    # ---- report ----
    if _should_run("report", args.resume_from):
        from src.report.generate import run_report
        client = None
        if not args.skip_llm:
            from src.review.config import load_llm_settings
            from src.review.llm import OpenAICompatClient
            s = load_llm_settings()
            client = OpenAICompatClient(s) if s.api_key else None
        res = timed("report", lambda: run_report(
            report_input, recall_path, client=client, output_dir=out, repo_name=repo_name,
            ai_detect_path=ai_detect_path if ai_detect_path.exists() else None))
        funnel["report"] = res["output_path"]
        funnel["report_deleted_refs"] = res["deleted"]

    logger.info("===== 漏斗 =====")
    for k, v in funnel.items():
        logger.info("  {}: {}", k, v)
    logger.info("===== 耗时 (s) =====  {}", timings)
    print(json.dumps({"funnel": funnel, "timings": timings,
                      "report": funnel.get("report")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
