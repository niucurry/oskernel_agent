"""分段向量验证：对 review/weak 嫌疑对做段级匹配，重打分并升降级。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from loguru import logger
from scipy.optimize import linear_sum_assignment

from oskernel_agent.comparison.normalize.segmenter import Segment, segment_function

DEFAULT_OUTPUT_DIR = "data/output"
SIM_THRESHOLD = 0.85
TARGET_TIERS = ("review", "weak")
# 分段向量只用于精确逐行证据之后的次级验证。同一目标函数可能因同名
# 多实现、镜像仓库和身份邻域产生大量候选；仅对便宜证据排名最高的若干个
# 不同候选内容执行昂贵的分段嵌入。其余 pair 保留原始逐行档位，不会被写成原创。
MAX_SEGMENT_CONTENTS_PER_QUERY = 2


def _normalize_rows(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _match_segments(qv: np.ndarray, cv: np.ndarray, q_segs, c_segs, threshold: float) -> dict:
    """匈牙利最优一对一匹配，统计命中段对。"""
    if len(q_segs) == 0 or len(c_segs) == 0:
        return {"hits": 0, "q_total": len(q_segs), "c_total": len(c_segs), "matched_segment_pairs": []}

    sim = _normalize_rows(qv) @ _normalize_rows(cv).T  # (nq, nc)
    rows, cols = linear_sum_assignment(-sim)  # 最大化相似度
    pairs = []
    for r, c in zip(rows, cols):
        s = float(sim[r, c])
        if s > threshold:
            pairs.append(
                {
                    "q_lines": [q_segs[r].start_line, q_segs[r].end_line],
                    "c_lines": [c_segs[c].start_line, c_segs[c].end_line],
                    "sim": round(s, 4),
                }
            )
    return {
        "hits": len(pairs),
        "q_total": len(q_segs),
        "c_total": len(c_segs),
        "matched_segment_pairs": pairs,
    }


def _rescore(q_cov: float, c_cov: float, exact_ratio: float, vec_sim: float) -> float:
    score = 0.4 * min(q_cov, c_cov) + 0.3 * exact_ratio + 0.3 * vec_sim
    return round(min(1.0, max(0.0, score)), 4)


def _raw_line_similarity(suspect: dict) -> float:
    """读取原始逐行相似度，并兼容未包含该字段的旧产物。"""
    evidence = suspect.get("evidence") or {}
    value = evidence.get("line_similarity")
    if value is not None:
        return min(1.0, max(0.0, float(value)))

    matched = int(evidence.get("exact_match_lines") or 0) + int(
        evidence.get("renamed_match_lines") or 0
    )
    if matched:
        query_code = ((suspect.get("query_func") or {}).get("raw_code") or "")
        candidate_code = ((suspect.get("candidate_func") or {}).get("raw_code") or "")
        query_lines = sum(1 for line in query_code.splitlines() if line.strip())
        candidate_lines = sum(1 for line in candidate_code.splitlines() if line.strip())
        denominator = max(query_lines, candidate_lines, 1)
        return min(1.0, matched / denominator)

    # 旧产物没有原始字段和匹配行数时只能使用旧 final_score；新产物不会走到这里。
    return min(1.0, max(0.0, float(suspect.get("final_score") or 0.0)))


def _retier(tier: str, final_score: float, q_cov: float, c_cov: float, exact_ratio: float) -> tuple[str, str]:
    """返回 (新 tier, 变更原因)。"""
    if tier == "weak" and final_score > 0.75:
        return "review", f"weak→review：分段重打分 {final_score} > 0.75"
    if tier == "review" and min(q_cov, c_cov) < 0.3 and exact_ratio < 0.2:
        return (
            "dismissed",
            f"review→dismissed：双向覆盖 min({q_cov:.2f},{c_cov:.2f})<0.3 且 exact_ratio {exact_ratio:.2f}<0.2",
        )
    return tier, ""


def _segment_cache_key(func: dict) -> tuple[str, str]:
    """函数分段只由语言和源码决定；仓库、路径及绝对行号不影响切分结果。"""
    return str(func.get("lang") or "rust").lower(), str(func.get("raw_code") or "")


def _segments_at_start(segments: list[Segment], start_line: int) -> list[Segment]:
    """把从第 1 行切出的缓存段平移到函数的真实绝对行号。"""
    offset = int(start_line) - 1
    if offset == 0:
        return segments
    return [
        Segment(seg.start_line + offset, seg.end_line + offset, seg.normalized_text)
        for seg in segments
    ]


def _segment_query_key(suspect: dict) -> tuple:
    query = suspect.get("query_func") or {}
    return (
        str(query.get("repo_id") or ""), str(query.get("file_path") or ""),
        int(query.get("start_line") or 0), str(query.get("func_name") or ""),
    )


def _segment_candidate_content_key(suspect: dict) -> tuple[str, str]:
    return _segment_cache_key(suspect.get("candidate_func") or {})


def _segment_selection_rank(suspect: dict) -> tuple:
    """便宜、确定性的候选排名；仅用于决定先算哪些分段，不改变证据档位。"""
    evidence = suspect.get("evidence") or {}
    matched = int(evidence.get("exact_match_lines") or 0) + int(
        evidence.get("renamed_match_lines") or 0
    )
    return (
        bool(evidence.get("normalized_fingerprint_match")),
        _raw_line_similarity(suspect),
        matched,
        float(evidence.get("function_identity_score") or 0.0),
        float(evidence.get("vector_similarity") or 0.0),
    )


def _select_segment_targets(
    suspects: list[dict], *, max_contents_per_query: int = MAX_SEGMENT_CONTENTS_PER_QUERY,
) -> tuple[list[dict], int, int]:
    """每个目标函数只选最有证据的 N 个不同候选内容；镜像来源共享选择。"""
    eligible = [s for s in suspects if s.get("tier") in TARGET_TIERS]
    by_query: dict[tuple, dict[tuple[str, str], list[dict]]] = {}
    for suspect in eligible:
        content_groups = by_query.setdefault(_segment_query_key(suspect), {})
        content_groups.setdefault(
            _segment_candidate_content_key(suspect), [],
        ).append(suspect)

    selected: list[dict] = []
    selected_contents = 0
    for content_groups in by_query.values():
        ranked = sorted(
            content_groups.values(),
            key=lambda group: max(_segment_selection_rank(item) for item in group),
            reverse=True,
        )
        chosen = ranked[:max_contents_per_query]
        selected_contents += len(chosen)
        for group in chosen:
            selected.extend(group)
            for suspect in group:
                suspect.setdefault("evidence", {})["segment_selection"] = "selected"
        for group in ranked[max_contents_per_query:]:
            for suspect in group:
                suspect.setdefault("evidence", {})["segment_selection"] = "deferred_secondary"
    return selected, len(eligible), selected_contents


def verify_segments(data: dict, embedder, *, sim_threshold: float = SIM_THRESHOLD) -> dict:
    """对 data["suspects"] 中 review/weak 档原地做分段验证、重打分、升降级。"""
    suspects = data.get("suspects", [])
    targets, eligible_count, selected_content_count = _select_segment_targets(suspects)
    logger.info(
        "待分段验证（review/weak）{} 个 pair / {} 个不同候选内容；"
        "原始候选 {} 个，每目标最多 {} 个内容",
        len(targets), selected_content_count, eligible_count,
        MAX_SEGMENT_CONTENTS_PER_QUERY,
    )
    if not targets:
        return data

    # 同一份代码经常被多个历史仓库重复收录。先按“语言+源码”缓存相对分段，再按段文本
    # 去重嵌入；这只消除重复计算，不改变候选、阈值、向量或匹配规则。
    segment_cache: dict[tuple[str, str], list[Segment]] = {}
    seg_pairs: list[tuple[list[Segment], list[Segment]]] = []
    unique_texts: dict[str, None] = {}
    segment_instances = 0
    for s in targets:
        q, c = s["query_func"], s["candidate_func"]
        q_key = _segment_cache_key(q)
        c_key = _segment_cache_key(c)
        if q_key not in segment_cache:
            segment_cache[q_key] = segment_function(q_key[1], q_key[0], 1)
        if c_key not in segment_cache:
            segment_cache[c_key] = segment_function(c_key[1], c_key[0], 1)
        qs = _segments_at_start(segment_cache[q_key], int(q.get("start_line") or 1))
        cs = _segments_at_start(segment_cache[c_key], int(c.get("start_line") or 1))
        seg_pairs.append((qs, cs))
        for seg in (*qs, *cs):
            unique_texts.setdefault(seg.normalized_text, None)
            segment_instances += 1

    texts = list(unique_texts)
    vecs = (embedder.encode_batch(texts) if texts
            else np.zeros((0, getattr(embedder, "dim", 256))))
    vectors_by_text = {text: vecs[i] for i, text in enumerate(texts)}
    logger.info(
        "分段去重：{} 个函数实例 → {} 份唯一源码；{} 个段实例 → {} 个唯一段文本",
        len(targets) * 2, len(segment_cache), segment_instances, len(texts),
    )

    tier_changes = {"upgraded": 0, "downgraded": 0}
    for s, (qs, cs) in zip(targets, seg_pairs):
        qv = (np.stack([vectors_by_text[seg.normalized_text] for seg in qs])
              if qs else np.zeros((0, getattr(embedder, "dim", 256))))
        cv = (np.stack([vectors_by_text[seg.normalized_text] for seg in cs])
              if cs else np.zeros((0, getattr(embedder, "dim", 256))))

        sh = _match_segments(qv, cv, qs, cs, sim_threshold)
        q_cov = sh["hits"] / sh["q_total"] if sh["q_total"] else 0.0
        c_cov = sh["hits"] / sh["c_total"] if sh["c_total"] else 0.0

        ev = s.setdefault("evidence", {})
        exact_ratio = _raw_line_similarity(s)
        ev["line_similarity"] = exact_ratio
        vec_sim = float(ev.get("vector_similarity") or 0.0)
        ev["segment_hits"] = sh

        final = _rescore(q_cov, c_cov, exact_ratio, vec_sim)
        tier_before = s["tier"]
        new_tier, reason = _retier(tier_before, final, q_cov, c_cov, exact_ratio)

        s["final_score"] = final
        s["tier"] = new_tier
        s["segment"] = {
            "q_coverage": round(q_cov, 4),
            "c_coverage": round(c_cov, 4),
            "hits": sh["hits"],
            "exact_match_ratio": round(exact_ratio, 4),
            "vector_similarity": round(vec_sim, 4),
            "final_score_before": exact_ratio,
            "tier_before": tier_before,
            "tier_after": new_tier,
            "reason": reason,
        }
        if new_tier != tier_before:
            if new_tier == "review":
                tier_changes["upgraded"] += 1
            elif new_tier == "dismissed":
                tier_changes["downgraded"] += 1

    data["segment_summary"] = {
        "verified": len(targets),
        "eligible": eligible_count,
        "selected_content_pairs": selected_content_count,
        "deferred": eligible_count - len(targets),
        "upgraded_weak_to_review": tier_changes["upgraded"],
        "downgraded_to_dismissed": tier_changes["downgraded"],
    }
    logger.info("分段验证完成：{}", data["segment_summary"])
    return data


def run_segment(suspects_path: str | Path, embedder, *, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict:
    """加载 suspects.json → 分段验证 → 写 {stem}_v2.json（保留原版）。"""
    suspects_path = Path(suspects_path)
    data = json.loads(suspects_path.read_text(encoding="utf-8"))
    data = verify_segments(data, embedder)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{suspects_path.stem}_v2.json"
    out_path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    logger.info("写入 {}（原 {} 保留）", out_path, suspects_path.name)
    data["_output_path"] = str(out_path)
    return data
