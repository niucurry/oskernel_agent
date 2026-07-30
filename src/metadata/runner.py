"""metadata 编排：在 suspects 上叠加三个辅助信号通道，输出 *_final.json。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from loguru import logger

from src.exact.matcher import ExactMatcher, remap_spans
from src.exact.verify import tier_of
from src.models import is_baseline_repo
from src.normalize.normalizer import normalize_snippet
from src.normalize.store import DEFAULT_DB

from .baseline import (BaselineMatcher, has_incremental_history_evidence,
                       is_baseline_derived, pair_line_evidence)
from .commits import analyze_function
from .config import MetadataSettings, load_metadata_settings
from .strings import build_reverse_index, fetch_function, string_hits_for_func

DEFAULT_OUTPUT_DIR = "data/output"


def _suspect_key(s: dict) -> tuple:
    q, c = s["query_func"], s["candidate_func"]
    return (q["file_path"], q["start_line"], c["repo_id"], c["file_path"], c["start_line"])


_EXACT_MATCHER = ExactMatcher()


def drop_cross_language_pairs(data: dict) -> int:
    """移除旧产物或旁路通道遗留的跨语言 pair，返回移除数。"""
    suspects = data.get("suspects") or []
    kept = [
        s for s in suspects
        if ((s.get("query_func") or {}).get("lang") or "").lower()
        == ((s.get("candidate_func") or {}).get("lang") or "").lower()
    ]
    removed = len(suspects) - len(kept)
    data["suspects"] = kept
    return removed


def _new_suspect(qf: dict, hf: dict, count: int) -> dict:
    """字符串通道新建的嫌疑对（独立召回路径）。

    旁路通道不再硬编码 ``final_score=0``：对新对实跑行级精确比对，用 similar_line_ratio
    填 final_score、tier_of 定档（行级相同的对从此显示真实相似度）。独特字符串命中可以
    绕过普通相似度召回门槛，但不应伪装成中等逐行相似；ratio<0.5 时仅保留为 weak，交由
    后续“职责 + 共同代码锚点”逐对复核。
    """
    res = _EXACT_MATCHER.match(qf.get("raw_code", ""), hf.get("raw_code", ""), qf.get("lang", "rust"))
    tier = tier_of(res.similar_line_ratio) or "weak"
    abs_spans = remap_spans(res.matched_spans, qf.get("start_line", 1), hf.get("start_line", 1))
    return {
        "tier": tier,
        "source": "string_channel",
        "final_score": res.similar_line_ratio,
        "query_func": qf,
        "candidate_func": {
            "repo_id": hf["repo_id"], "file_path": hf["file_path"],
            "start_line": hf["start_line"], "end_line": hf["end_line"],
            "func_name": hf["func_name"], "module_tag": hf["module_tag"],
            "lang": hf["lang"], "raw_code": hf["raw_code"], "normalized_code": hf["normalized_code"],
        },
        "evidence": {
            "vector_similarity": None,
            "line_similarity": res.similar_line_ratio,
            "exact_match_lines": res.exact_match_lines,
            "renamed_match_lines": res.renamed_match_lines,
            "unique_string_matches": count,
        },
        "matched_spans": abs_spans,
        "match_type_per_span": res.match_type_per_span,
    }


def channel_unique_strings(data: dict, db_path: str | Path, settings: MetadataSettings) -> int:
    """通道 1：独特字符串召回，bump 已有对或新建对。返回新建数。"""
    suspects: list[dict] = data["suspects"]
    index = build_reverse_index(db_path, generic_threshold=settings.string_generic_repo_threshold)

    existing = {_suspect_key(s): s for s in suspects}
    query_funcs: dict[tuple, dict] = {}
    for s in suspects:
        q = s["query_func"]
        query_funcs.setdefault((q["file_path"], q["start_line"]), q)

    new_count = 0
    for qf in query_funcs.values():
        strings = normalize_snippet(qf.get("raw_code", ""), qf.get("lang", "rust")).strings
        for hist_fid, (hist_repo, count) in string_hits_for_func(
            strings, index, exclude_repo_id=qf.get("repo_id")
        ).items():
            hf = fetch_function(db_path, hist_fid)
            if not hf:
                continue
            if (hf.get("lang") or "").lower() != (qf.get("lang") or "").lower():
                continue
            key = (qf["file_path"], qf["start_line"], hf["repo_id"], hf["file_path"], hf["start_line"])
            if key in existing:
                ev = existing[key].setdefault("evidence", {})
                ev["unique_string_matches"] = max(ev.get("unique_string_matches") or 0, count)
            else:
                sp = _new_suspect(qf, hf, count)
                existing[key] = sp
                suspects.append(sp)
                new_count += 1
    logger.info("通道1 独特字符串：新建嫌疑对 {} 个", new_count)
    return new_count


def channel_baseline(data: dict, matcher: BaselineMatcher, settings: MetadataSettings) -> int:
    """通道 2：基线扣除。query/candidate 命中上游基线（vendored/紧随上游/共同衍生）则降级 baseline_derived。返回扣除数。

    性能：去重 query 与 candidate 的 normalized_code，各一次矩阵乘批量算对基线集的相似度
    （~2546 基线向量；双侧各 ~1000 唯一代码 → 2 次 GPU 矩阵乘，秒级），再回填。

    两条路径（见 is_baseline_derived）：
      - 双侧同基线（强信号）：query 与 candidate 都命中同一基线函数 > bilateral_threshold → 两队共同衍生自上游。
        覆盖「4 队共同改造 rcore-v3 原始函数、互相 1.0 但对原始版 sim<0.85」的因果倒置场景。
      - 单侧 query 命中基线：query 与某基线函数相似 > baseline_sim_threshold → vendored/紧随上游。
    """
    suspects = data["suspects"]

    def _batch(codes):
        if not codes:
            return {}
        if hasattr(matcher, "match_batch"):
            results = matcher.match_batch(codes)
        else:  # 兜底：非 VectorBaselineMatcher 时逐条
            results = [matcher.match(c) for c in codes]
        return dict(zip(codes, results))

    q_uniq: dict[str, None] = {}
    c_uniq: dict[str, None] = {}
    for s in suspects:
        q_nc = (s.get("query_func") or {}).get("normalized_code", "") or " "
        q_uniq.setdefault(q_nc, None)
        c_nc = (s.get("candidate_func") or {}).get("normalized_code", "") or " "
        c_uniq.setdefault(c_nc, None)
    q_cache = _batch(list(q_uniq))
    c_cache = _batch(list(c_uniq))

    n = 0
    for s in suspects:
        q_nc = (s.get("query_func") or {}).get("normalized_code", "") or " "
        c_nc = (s.get("candidate_func") or {}).get("normalized_code", "") or " "
        q_match = q_cache.get(q_nc, (None, 0.0))
        c_match = c_cache.get(c_nc, (None, 0.0))
        candidate_repo = str((s.get("candidate_func") or {}).get("repo_id") or "")
        if is_baseline_repo(candidate_repo):
            # 候选记录本身来自显式基线库，这是比“再次向量命中基线”更直接的来源证据。
            # 不能因为向量分低于阈值就把基线仓库当成普通历史团队。
            derived, basis = True, "候选函数直接来自显式公共基线仓库"
            query_scope = False
        else:
            derived, basis = is_baseline_derived(
                q_match, c_match,
                threshold=settings.baseline_sim_threshold,
                bilateral_threshold=settings.baseline_bilateral_threshold)
            query_scope = derived
        if derived:
            ev = s.setdefault("evidence", {})
            ev["baseline_flag"] = True
            ev["baseline_query_scope"] = query_scope
            s["tier"] = "baseline_derived"
            s["baseline_note"] = (
                f"{basis} (qid={q_match[0]}, q={q_match[1]:.3f}; cid={c_match[0]}, c={c_match[1]:.3f})"
            )
            n += 1

    # 基线来源是目标函数级属性，而不是某一条候选 pair 的属性。若同一目标函数已有一条
    # pair 建立了公共基线来源，其余历史候选在没有“扣除基线后残差相似”证据的情况下也
    # 不能继续被归因为某个团队；否则同一函数会同时出现在借鉴/存疑与基线排除两节。
    baseline_by_query: dict[tuple, list[dict]] = defaultdict(list)
    for s in suspects:
        if s.get("tier") == "baseline_derived":
            baseline_by_query[_query_key(s)].append(s)
    for s in suspects:
        baselines = baseline_by_query.get(_query_key(s), [])
        if not baselines or s.get("tier") == "baseline_derived":
            continue
        query_scope = any(
            bool((item.get("evidence") or {}).get("baseline_query_scope"))
            for item in baselines
        )
        explicit_baselines = [
            item for item in baselines
            if is_baseline_repo(str((item.get("candidate_func") or {}).get("repo_id") or ""))
        ]
        if (not query_scope
                and has_incremental_history_evidence(s, explicit_baselines)):
            ev = s.setdefault("evidence", {})
            ev["baseline_overlap"] = True
            ev["baseline_incremental_evidence"] = True
            candidate_sim, candidate_lines = pair_line_evidence(s)
            strongest_sim = max(pair_line_evidence(item)[0] for item in explicit_baselines)
            most_lines = max(pair_line_evidence(item)[1] for item in explicit_baselines)
            s["baseline_note"] = (
                "目标函数也命中公共基线，但当前历史候选提供了基线之外的增量同源证据"
                f"（逐行 {candidate_sim:.3f}/{candidate_lines} 行；"
                f"最强基线 {strongest_sim:.3f}/{most_lines} 行）"
            )
            continue
        ev = s.setdefault("evidence", {})
        ev["baseline_flag"] = True
        s["tier"] = "baseline_derived"
        s["baseline_note"] = (
            "目标函数已有候选证明来自公共基线；当前候选未提供扣除基线后的增量同源证据"
        )
        n += 1
    logger.info("通道2 基线扣除：{} 个降为 baseline_derived（批量编码 query {} / candidate {} 个唯一函数）",
                n, len(q_uniq), len(c_uniq))
    return n


def _query_key(s: dict) -> tuple:
    """稳定标识目标函数；起始行用于区分同文件内同名方法/实现。"""
    q = s.get("query_func") or {}
    return (q.get("file_path", ""), int(q.get("start_line") or 0), q.get("func_name", ""))


def channel_common_code(data: dict, settings: MetadataSettings) -> int:
    """通道 4：广泛共享提示（不单独判定公共代码）。

    一个 query 函数若以高相似度（vector >= common_code_sim_threshold，或已判 confirmed）命中
    >= common_code_repo_threshold 个**不同历史仓库**，只记录“广泛共享”背景。仓库数无法区分
    公共上游、fork 链和多次传播，因此不能单独把 confirmed/review 降为 common_code；真正排除仍
    需要 baseline、vendored 路径或其他公共来源证明。返回被标注的嫌疑对数。
    """
    suspects = data["suspects"]
    by_query: dict[tuple, list[dict]] = defaultdict(list)
    for s in suspects:
        q = s["query_func"]
        by_query[(q["file_path"], q["start_line"])].append(s)

    n = 0
    for group in by_query.values():
        strong_repos = set()
        for s in group:
            vec = (s.get("evidence") or {}).get("vector_similarity")
            if s.get("tier") == "confirmed" or (vec is not None and vec >= settings.common_code_sim_threshold):
                strong_repos.add(s["candidate_func"]["repo_id"])
        if len(strong_repos) >= settings.common_code_repo_threshold:
            for s in group:
                ev = s.setdefault("evidence", {})
                ev["widespread_match_repos"] = len(strong_repos)
                s["widespread_match_note"] = (
                    f"该函数高相似命中 {len(strong_repos)} 个不同历史仓库；"
                    "这只能证明广泛传播，不能单独证明属于公共/框架代码"
                )
                n += 1
    logger.info("通道4 广泛共享提示：{} 个嫌疑对已标注（不改变 tier）", n)
    return n


def channel_commits(data: dict, repo_path: str | Path, meta_commits: list[dict], settings: MetadataSettings) -> int:
    """通道 3：commit 异常信号（附注，不改 tier）。返回有信号的嫌疑数。"""
    n = 0
    for s in data["suspects"]:
        signals = analyze_function(repo_path, meta_commits, s["query_func"], settings)
        if signals:
            s.setdefault("evidence", {})["commit_signals"] = signals
            n += 1
    logger.info("通道3 commit 信号：{} 个嫌疑函数命中信号", n)
    return n


def process_metadata(
    data: dict,
    db_path: str | Path = DEFAULT_DB,
    *,
    settings: MetadataSettings | None = None,
    baseline_matcher: BaselineMatcher | None = None,
    query_repo: str | Path | None = None,
    meta_commits: list[dict] | None = None,
) -> dict:
    """按需运行三通道（通道1 总是运行；2/3 视后端可用性）。"""
    settings = settings or load_metadata_settings()
    summary = {"cross_language_filtered": drop_cross_language_pairs(data)}
    summary["string_new_pairs"] = channel_unique_strings(data, db_path, settings)
    summary["widespread_match_pairs"] = channel_common_code(data, settings)
    if baseline_matcher is not None:
        summary["baseline_derived"] = channel_baseline(data, baseline_matcher, settings)
    if query_repo is not None and meta_commits is not None:
        summary["commit_signal_hits"] = channel_commits(data, query_repo, meta_commits, settings)
    data["metadata_summary"] = summary
    return data


def run_metadata(suspects_path: str | Path, *, db_path=DEFAULT_DB, output_dir=DEFAULT_OUTPUT_DIR,
                 baseline_matcher=None, query_repo=None, meta_commits=None, settings=None) -> dict:
    suspects_path = Path(suspects_path)
    data = json.loads(suspects_path.read_text(encoding="utf-8"))
    data = process_metadata(
        data, db_path, settings=settings, baseline_matcher=baseline_matcher,
        query_repo=query_repo, meta_commits=meta_commits,
    )
    stem = suspects_path.stem
    base = stem[:-3] if stem.endswith("_v2") else stem
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{base}_final.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("metadata 完成：{} → {}", data.get("metadata_summary"), out_path)
    data["_output_path"] = str(out_path)
    return data
