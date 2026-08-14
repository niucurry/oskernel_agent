"""全流水线总入口：

  python -m oskernel_agent.comparison.pipeline --repo <新作品路径或 git url> [--top-k 20]
                         [--resume-from <step>] [--baselines]

按序执行 ingest → fastpath → recall(含 normalize) → exact → segment → metadata
→ ai_detect → report，每步落盘中间结果，打印每步耗时与漏斗数字。`ai_detect` 默认运行；
只有显式传入 `--skip-ai-detect` 才跳过，此时不会生成缺少该模块的交付报告。
（LLM 复核在报告阶段只处理规则难例。）
"""

from __future__ import annotations

import argparse
import gc
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

from oskernel_agent.comparison.normalize.store import DEFAULT_DB

from .steps import STEPS, local_ingest, tier_counts

DEFAULT_OUTPUT = "data/output"
DEFAULT_QDRANT = "data/db/qdrant_local"
DEFAULT_IDF = "data/db/idf.json"
DEFAULT_INDEX = "data/db/simhash_index.pkl"
DEFAULT_CODE_SIMHASH = "data/db/code_simhash_index.pkl"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m oskernel_agent.comparison.pipeline", description="作品查重全流水线。")
    p.add_argument("--repo", required=True, help="新作品本地路径或 git url")
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--resume-from", choices=STEPS, default=None, help="从指定步骤续跑（需前序产物存在）")
    p.add_argument("--no-simhash", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--baselines", action="store_true", help="启用基线扣除（需 Qdrant 已有基线数据）")
    ai_group = p.add_mutually_exclusive_group()
    ai_group.add_argument(
        "--ai-detect", dest="ai_detect", action="store_true", default=True,
        help="运行 AI 生成代码模型检测（默认；保留该参数以兼容已有命令）",
    )
    ai_group.add_argument(
        "--skip-ai-detect", dest="ai_detect", action="store_false",
        help="仅诊断前序阶段；跳过后报告完整性门禁会拒绝生成交付报告",
    )
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--history-config", default="config/repos.yaml",
                   help="历史作品清单；运行前逐仓核验 functions.db 覆盖率")
    p.add_argument("--qdrant-path", default=DEFAULT_QDRANT)
    p.add_argument("--idf", default=DEFAULT_IDF)
    p.add_argument("--simhash-index", default=DEFAULT_INDEX)
    p.add_argument("--code-simhash-index", default=DEFAULT_CODE_SIMHASH)
    p.add_argument("--repos-root", default="data/repos")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT)
    return p


def _should_run(step: str, resume_from: str | None) -> bool:
    if resume_from is None:
        return True
    return STEPS.index(step) >= STEPS.index(resume_from)


_AI_DETECT_TIMEOUT = 5400


def _ai_detect_usable(artifact: dict | None) -> bool:
    """AI 检测产物能否进入交付报告：仅 status=ok，或经校验的空适用范围。"""
    if not isinstance(artifact, dict):
        return False
    status = str(artifact.get("status") or "")
    if status == "ok":
        return True
    if status == "skipped":
        # 与报告层 _ai_detect_section 的「不适用」口径一致：scope 必须非空且
        # 可归属/已分析函数均为 0（有可归属函数却未检测是模型失败，不得放行）。
        scope = artifact.get("scope")
        if not isinstance(scope, dict) or not scope:
            return False
        try:
            if (int(scope.get("eligible_functions") or 0) == 0
                    and int(scope.get("analyzed_functions") or 0) == 0):
                return True
        except (TypeError, ValueError):
            return False
    return False


def _run_ai_detect_subprocess(
    repo_path: Path,
    out: Path,
    repo_name: str,
    exclude_files: set[str],
    exclude_funcs: set[tuple[str, str]],
) -> dict:
    """子进程运行 AI 检测（重量级 3B 检测模型）。

    检测模型与主进程的嵌入模型同驻时，本机内存紧张会触发原生分配失败——
    进程无 traceback 直接退出，任何 Python 异常都捕不住。子进程隔离后：
    OS 在子进程退出时彻底回收嵌入模型内存，检测模型获得干净进程；即便仍原生
    崩溃，也只会丢掉这一个子进程，主流水线落盘 status="crashed" 诊断产物并
    继续前进（报告章由 _ai_detect_usable 门控，缺失即省略，不再整条流水线
    陪葬、逼驱动整轮 --skip-ai-detect 重跑）。
    """
    exclude_path = out / f"{repo_name}_ai_exclude.json"
    exclude_path.write_text(
        json.dumps(
            {"files": sorted(exclude_files), "funcs": sorted(exclude_funcs)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log_path = out / f"{repo_name}_ai_detect.log"
    cmd = [
        sys.executable, "-m", "oskernel_agent.comparison.ai_detect",
        "--repo", str(repo_path),
        "-o", str(out),
        "--name", repo_name,
        "--no-progress",
        "--exclude-file", str(exclude_path),
    ]
    try:
        with log_path.open("w", encoding="utf-8") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT,
                                  timeout=_AI_DETECT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {
            "status": "crashed",
            "reason": f"检测子进程超时（>{_AI_DETECT_TIMEOUT}s），详见 {log_path.name}",
        }
    if proc.returncode != 0:
        reason = f"检测子进程退出码 {proc.returncode}（原生崩溃无 traceback），详见 {log_path.name}"
        logger.warning("[ai_detect] {}", reason)
        return {"status": "crashed", "reason": reason}
    artifact = out / f"{repo_name}_ai_detect.json"
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "crashed",
            "reason": f"检测子进程退出码 0 但未产出合法产物 {artifact.name}",
        }
    return payload


def _resolve_git_revision(repo_path: Path) -> str:
    """不依赖仓库所有权配置，直接解析 HEAD，供报告记录实际被分析版本。"""
    git_path = repo_path / ".git"
    if git_path.is_file():
        try:
            pointer = git_path.read_text(encoding="utf-8").strip()
            if pointer.startswith("gitdir:"):
                raw = pointer.split(":", 1)[1].strip()
                git_path = (repo_path / raw).resolve()
        except OSError:
            return ""
    try:
        head = (git_path / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not head.startswith("ref:"):
        return head if re.fullmatch(r"[0-9a-fA-F]{7,64}", head) else ""
    ref = head.split(":", 1)[1].strip()
    try:
        return (git_path / ref).read_text(encoding="utf-8").strip()
    except OSError:
        try:
            for line in (git_path / "packed-refs").read_text(encoding="utf-8").splitlines():
                if line and not line.startswith(("#", "^")):
                    sha, name = line.split(" ", 1)
                    if name == ref:
                        return sha
        except (OSError, ValueError):
            pass
    return ""


def _restore_semantic_cache(out: Path, repo_name: str, query_repo_id: str) -> int:
    """把上次成功报告的内容寻址模型缓存恢复到本次工作目录。

    缓存键包含 prompt 版本、模型、函数名和双方完整源码；源码或规则改变时会自然 miss，
    因而可以跨重复测试复用而不会把旧结论套到新代码。当前失败续跑缓存优先于归档缓存。
    """
    import shutil

    archive_dir = out / repo_name / ".semantic_cache"
    if not archive_dir.is_dir():
        return 0
    work_dir = out / f"{query_repo_id}_semantic_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    restored = 0
    for source in archive_dir.rglob("*"):
        if not source.is_file():
            continue
        destination = work_dir / source.relative_to(archive_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.name.startswith("review_judgment_") and source.suffix == ".json":
                archived = json.loads(source.read_text(encoding="utf-8"))
                current = (json.loads(destination.read_text(encoding="utf-8"))
                           if destination.exists() else {})
                if not isinstance(archived, dict) or not isinstance(current, dict):
                    continue
                merged = {**archived, **current}
                destination.write_text(
                    json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
                restored += len(archived)
            elif not destination.exists():
                shutil.copy2(source, destination)
                restored += 1
        except (OSError, json.JSONDecodeError):
            logger.warning("[report] 忽略损坏的历史模型缓存 {}", source)
    if restored:
        logger.info("[report] 恢复 {} 条历史模型缓存；代码或 prompt 变化的条目会自动重算", restored)
    return restored


def _finalize_comparison_output(out: Path, repo_name: str, html_path: Path,
                                query_repo_id: str, *intermediate_paths: Path,
                                preserve_paths: tuple[Path, ...] = ()) -> Path:
    """删除大体积中间产物，保留最终 HTML、模型产物与内容寻址复核缓存。

    缓存不包含被比较源码，只保存哈希键和模型结构化结论，供相同代码重复测试复用。
    """
    import shutil

    final_dir = out / repo_name
    final_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out / f"{query_repo_id}_semantic_work"
    cache_dir = final_dir / ".semantic_cache"
    review_caches = list(work_dir.glob("review_judgment_*.json")) if work_dir.is_dir() else []
    if review_caches:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for cache_file in review_caches:
            shutil.copy2(cache_file, cache_dir / cache_file.name)
    content_cache = work_dir / "cache"
    if content_cache.is_dir():
        shutil.copytree(content_cache, cache_dir / "cache", dirs_exist_ok=True)

    preserved_destinations: set[Path] = set()
    for source in preserve_paths:
        source = Path(source)
        if not source.is_file():
            continue
        destination = final_dir / source.name
        preserved_destinations.add(destination.resolve())
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)

    for p in intermediate_paths:
        path = Path(p)
        if path.resolve() not in preserved_destinations:
            path.unlink(missing_ok=True)

    shutil.rmtree(work_dir, ignore_errors=True)

    final_html = final_dir / html_path.name
    if html_path.resolve() != final_html.resolve():
        html_path.replace(final_html)
    return final_html


def main(argv: list[str] | None = None) -> int:  # noqa: C901 — 顺序编排
    from dotenv import load_dotenv
    load_dotenv()
    pipeline_perf_started = time.perf_counter()
    args = build_parser().parse_args(argv)

    # 查重必须 fail closed：历史库缺仓时继续出报告会把“没查到”误写成原创。
    from oskernel_agent.comparison.buildlib.coverage import audit_config
    try:
        coverage = audit_config(args.db, args.history_config)
    except (OSError, ValueError) as exc:
        logger.error("历史库覆盖率核验失败：{}", exc)
        return 2
    if not coverage.complete:
        logger.error(
            "历史库不完整：仅覆盖 {}/{} 个配置作品，缺失 {}。"
            "请先运行 `python -m oskernel_agent.comparison.buildlib` 修复建库；本次拒绝生成可能误导的报告。",
            coverage.covered, coverage.configured, list(coverage.missing_repo_ids),
        )
        return 2

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
    archived_ai_detect_path = out / repo_name / ai_detect_path.name
    if (not ai_detect_path.is_file() and archived_ai_detect_path.is_file()
            and not _should_run("ai_detect", args.resume_from)):
        try:
            from oskernel_agent.comparison.ai_detect.runner import compute_source_fingerprint
            archived_ai = json.loads(archived_ai_detect_path.read_text(encoding="utf-8"))
            current_fingerprint = compute_source_fingerprint(repo_path)
            if archived_ai.get("source_fingerprint") == current_fingerprint:
                ai_detect_path = archived_ai_detect_path
                logger.info("[ai_detect] 源码指纹一致，复用上次成功交付时保存的模型产物 {}",
                            ai_detect_path)
            else:
                logger.warning("[ai_detect] 已保存模型产物与当前源码指纹不一致，不予复用")
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[ai_detect] 已保存模型产物校验失败，不予复用：{}", exc)

    # 共享后端（按需懒加载）
    embedder = None
    vector_store = None          # 本地 Qdrant local 模式独占文件锁，全流程共用一个实例，
                                 # 避免 recall 与 metadata(--baselines) 各建实例触发 AlreadyLocked

    def get_emb():
        nonlocal embedder
        if embedder is None:
            from oskernel_agent.comparison.embed.embedder import CachingEmbedder, get_embedder
            embedder = CachingEmbedder(get_embedder(show_progress=False))
        return embedder

    def get_store():
        nonlocal vector_store
        if vector_store is None:
            from oskernel_agent.comparison.embed.settings import load_settings
            from oskernel_agent.comparison.embed.vector_store import VectorStore
            st = load_settings()
            vector_store = VectorStore(st.qdrant.collection, path=args.qdrant_path)
        return vector_store

    def release_store() -> None:
        """阶段边界释放本地 Qdrant/向量索引内存，需要时再按需重开。"""
        nonlocal vector_store
        if vector_store is None:
            return
        try:
            vector_store.client.close()
        except Exception as exc:  # noqa: BLE001 — 释放失败不覆盖已生成阶段产物
            logger.warning("[内存] 关闭向量库客户端失败：{}", exc)
        vector_store = None
        gc.collect()

    def release_embedder() -> None:
        """释放召回/分段阶段加载的嵌入模型（torch），给后续重量级模型腾出内存。

        ai_detect 会再加载约 3B 参数的检测模型；本机内存紧张时，两套模型同驻进程
        会触发原生分配失败——进程无 traceback 直接退出，任何 Python 异常都捕不住。
        报告阶段不再使用嵌入向量，可在 ai_detect 前安全释放。
        """
        nonlocal embedder
        if embedder is None:
            return
        embedder = None
        gc.collect()

    # ---- fastpath (L0 文件指纹层) ----
    skip_files: set[str] = set()
    if _should_run("fastpath", args.resume_from):
        from oskernel_agent.comparison.fastpath.scan import scan_repo
        fm = timed("fastpath", lambda: scan_repo(
            repo_path, db_path=args.db, repos_root=args.repos_root, output_dir=out))
        skip_files = set(fm["skip_files"])
        funnel["fastpath_filematch"] = len(fm["matched_files"])
    elif filematch_path.exists():
        skip_files = set(json.loads(filematch_path.read_text(encoding="utf-8")).get("skip_files", []))

    # ---- recall (含 normalize) ----
    if _should_run("recall", args.resume_from):
        from oskernel_agent.comparison.embed.query import query_repo

        simhash_query = None
        if args.no_simhash:
            logger.error("完整查全模式不允许关闭 SimHash；如需调试请单独调用底层模块")
            return 2
        if not Path(args.idf).exists() or not Path(args.simhash_index).exists():
            logger.error("缺少特征 SimHash 索引/IDF，拒绝退化运行；请重建历史库")
            return 2
        from oskernel_agent.comparison.buildlib.coverage import db_mapping_signature
        current_db_signature = db_mapping_signature(args.db)
        if Path(args.idf).exists() and Path(args.simhash_index).exists():
            from oskernel_agent.comparison.simhash.build import SimHashQuery
            try:
                simhash_query = SimHashQuery(
                    args.idf, args.simhash_index, db_path=args.db,
                    db_signature=current_db_signature)
            except (OSError, ValueError) as exc:
                logger.error("特征 SimHash 索引不可用：{}", exc)
                return 2
        if not Path(args.code_simhash_index).exists():
            logger.error("缺少结构 SimHash 索引 {}，拒绝退化运行；请重建历史库",
                         args.code_simhash_index)
            return 2
        from oskernel_agent.comparison.simhash.code_index import CodeSimHashQuery
        try:
            code_simhash_query = CodeSimHashQuery(
                args.code_simhash_index, db_path=args.db,
                db_signature=current_db_signature,
            )
        except (OSError, ValueError) as exc:
            logger.error("结构 SimHash 索引不可用：{}", exc)
            return 2

        def _recall():
            # 完整模式强制使用已签名且与 functions.db 同代的 FAISS 索引；召回阶段
            # 无需提前打开 24 万点的 Qdrant local。基线阶段需要时再按需打开。
            return query_repo(repo_path, None, get_emb(), top_k=args.top_k,
                              repos_root=args.repos_root, output_dir=out, simhash_query=simhash_query,
                              skip_files=skip_files, db_path=args.db,
                              code_simhash_query=code_simhash_query,
                              history_coverage=coverage.as_dict(),
                              require_signed_faiss=True,
                              db_signature=current_db_signature)
        recall = timed("recall", _recall)
        funnel["recall_query_funcs"] = len(recall["results"])
        funnel["recall_candidates"] = recall["simhash"]["total_recalled"]
        # exact 从已写盘产物读取；不让召回对象和本地向量库与下一份
        # 大 JSON 在内存中重叠。metadata 需要基线向量时会按需重开 store。
        del recall
        release_store()
        gc.collect()

    # ---- exact ----
    if _should_run("exact", args.resume_from):
        from oskernel_agent.comparison.exact.verify import verify_recall
        res = timed("exact", lambda: verify_recall(
            recall_path, db_path=args.db, output_dir=out, require_complete_recall=True))
        funnel["exact_compared"] = res["compared_pairs"]
        funnel["after_exact"] = res["tier_counts"]
        del res
        gc.collect()

    # ---- segment ----
    if _should_run("segment", args.resume_from):
        from oskernel_agent.comparison.segment.verify import run_segment
        res = timed("segment", lambda: run_segment(suspects_path, get_emb(), output_dir=out))
        funnel["after_segment"] = tier_counts(res["suspects"])
        del res
        gc.collect()

    # ---- metadata ----
    if _should_run("metadata", args.resume_from):
        from oskernel_agent.comparison.metadata.runner import run_metadata
        baseline_matcher = None
        if args.baselines:
            from oskernel_agent.comparison.metadata.baseline import VectorBaselineMatcher
            # 复用 recall 步已建的 store（共用 Qdrant local 文件锁），避免 AlreadyLocked
            baseline_matcher = VectorBaselineMatcher(get_emb(), get_store())
        res = timed("metadata", lambda: run_metadata(
            v2_path, db_path=args.db, output_dir=out, baseline_matcher=baseline_matcher,
            query_repo=repo_path))
        funnel["metadata"] = res["metadata_summary"]
        funnel["after_metadata"] = tier_counts(res["suspects"])
        del res
        # matcher 持有同一个本地向量库客户端；先解除引用，再关闭 store，
        # 避免后续报告阶段继续占用大块索引内存。
        baseline_matcher = None
        release_store()
        gc.collect()

    # ---- ai_detect（AI 生成代码检测，独立于查重漏斗；失败状态不得进入交付报告）----
    if args.ai_detect and _should_run("ai_detect", args.resume_from):
        release_embedder()   # 报告阶段不再需要嵌入模型，先腾出内存给检测模型
        from oskernel_agent.comparison.report.libraries import discover_library_context, match_library
        # 排除借鉴代码：文件级（fastpath 整文件命中）+ 函数级（查重命中的可疑函数），
        # 只对未匹配上的原创代码做 AI 生成检测（借鉴自参考 OS 的代码不计入）
        library_context = discover_library_context(repo_path)
        exclude_files = {
            p.replace("\\", "/") for p in skip_files
            if not match_library(
                p.replace("\\", "/"), context=library_context)
        }
        exclude_funcs: set[tuple[str, str]] = set()
        if final_path.exists():
            try:
                _sd = json.loads(final_path.read_text(encoding="utf-8"))
                exclude_funcs = {
                    (s.get("query_func", {}).get("file_path", "").replace("\\", "/"),
                     s.get("query_func", {}).get("func_name", ""))
                    for s in _sd.get("suspects", [])
                    if s.get("tier") in ("confirmed", "review", "weak")
                    and not s.get("reuse_library")
                }
            except (OSError, json.JSONDecodeError):
                exclude_funcs = set()
        res = timed("ai_detect", lambda: _run_ai_detect_subprocess(
            repo_path, out, repo_name, exclude_files, exclude_funcs))
        if res.get("status") == "crashed":
            # 原生崩溃无法在进程内捕获：落盘诊断产物供报告层判定，
            # 主流水线继续前进，不再整条陪葬。
            crashed_path = out / f"{repo_name}_ai_detect.json"
            try:
                crashed_path.write_text(
                    json.dumps({
                        "status": "crashed",
                        "reason": res.get("reason"),
                        "repo_id": repo_name,
                        "scope": {},
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
        funnel["ai_detect_status"] = res.get("status")
        if res.get("status") == "ok":
            funnel["ai_detect"] = res["aggregated"]["overall"]
        else:
            funnel["ai_detect_reason"] = res.get("reason")

    # ---- report（语义级对比报告，直接产出 HTML + GitLab 在线链接）----
    if _should_run("report", args.resume_from):
        from oskernel_agent.comparison.report.semantic_compare import run_semantic_compare
        try:
            report_input = json.loads(final_path.read_text(encoding="utf-8"))
            report_query_repo_id = str(report_input.get("query_repo_id") or repo_name)
            del report_input
            gc.collect()
        except (OSError, json.JSONDecodeError):
            report_query_repo_id = repo_name
        _restore_semantic_cache(out, repo_name, report_query_repo_id)
        report_ai_detect_path = None
        if args.ai_detect:
            ai_artifact = None
            if ai_detect_path.is_file():
                try:
                    ai_artifact = json.loads(ai_detect_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    ai_artifact = None
            if _ai_detect_usable(ai_artifact):
                report_ai_detect_path = ai_detect_path
            else:
                # 检测模型在本机不可用/崩溃：报告省略该章（与 --skip-ai-detect 同构），
                # 但不再需要整轮重跑；诊断产物（status/reason）保留在 ai_detect.json。
                logger.warning(
                    "[pipeline] AI 检测产物不可用于交付（status={}），报告将省略该章",
                    (ai_artifact or {}).get("status") or "缺失",
                )
        res = timed("report", lambda: run_semantic_compare(
            suspects_path   = final_path,
            query_repo_path = str(repo_path),
            recall_path     = recall_path,
            output_dir      = out,
            filematch_path  = filematch_path,
            ai_detect_path  = report_ai_detect_path,
            functions_db_path = args.db,
        ))
        comparison_digest_path = Path(res["digest_path"])

        # 清理大体积流水线中间产物；保留最终报告与内容寻址模型缓存，使相同代码的
        # 重复测试无需再次请求模型。源码或 prompt 改变时缓存键会自动失效。
        final_html = _finalize_comparison_output(
            out, repo_name, Path(res["html_path"]), res["query_repo_id"],
            filematch_path, recall_path, suspects_path, v2_path, final_path,
            comparison_digest_path,
            *([ai_detect_path] if args.ai_detect else []),
            preserve_paths=(
                *((ai_detect_path,) if args.ai_detect else ()),
                comparison_digest_path,
            ),
        )
        pipeline_finished_at = datetime.now().astimezone()
        total_elapsed = time.perf_counter() - pipeline_perf_started
        timings["total"] = round(total_elapsed, 2)
        logger.info(
            "[report] 完成时间 {}，完整流水线总耗时 {:.2f}s，目标版本 {}",
            pipeline_finished_at.isoformat(timespec="seconds"), total_elapsed,
            _resolve_git_revision(repo_path) or "未知",
        )
        funnel["report"] = str(final_html)
        funnel["report_html"] = str(final_html)

    logger.info("===== 漏斗 =====")
    for k, v in funnel.items():
        logger.info("  {}: {}", k, v)
    logger.info("===== 耗时 (s) =====  {}", timings)
    print(json.dumps({"funnel": funnel, "timings": timings,
                      "report": funnel.get("report")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
