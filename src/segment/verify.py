"""分段向量验证：对 review/weak 嫌疑对做段级匹配，重打分并升降级。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from loguru import logger
from scipy.optimize import linear_sum_assignment

from src.normalize.segmenter import Segment, segment_function

DEFAULT_OUTPUT_DIR = "data/output"
SIM_THRESHOLD = 0.85
TARGET_TIERS = ("review", "weak")


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


def verify_segments(data: dict, embedder, *, sim_threshold: float = SIM_THRESHOLD) -> dict:
    """对 data["suspects"] 中 review/weak 档原地做分段验证、重打分、升降级。"""
    suspects = data.get("suspects", [])
    targets = [s for s in suspects if s.get("tier") in TARGET_TIERS]
    logger.info("待分段验证（review/weak）{} 个", len(targets))
    if not targets:
        return data

    # 分段 + 收集所有段文本一次性 batch 嵌入
    seg_pairs: list[tuple[list[Segment], list[Segment]]] = []
    all_texts: list[str] = []
    for s in targets:
        q, c = s["query_func"], s["candidate_func"]
        qs = segment_function(q.get("raw_code", ""), q.get("lang", "rust"), q["start_line"])
        cs = segment_function(c.get("raw_code", ""), c.get("lang", "rust"), c["start_line"])
        seg_pairs.append((qs, cs))
        all_texts.extend(seg.normalized_text for seg in qs)
        all_texts.extend(seg.normalized_text for seg in cs)

    vecs = embedder.encode_batch(all_texts) if all_texts else np.zeros((0, getattr(embedder, "dim", 256)))

    idx = 0
    tier_changes = {"upgraded": 0, "downgraded": 0}
    for s, (qs, cs) in zip(targets, seg_pairs):
        qv = vecs[idx : idx + len(qs)]; idx += len(qs)
        cv = vecs[idx : idx + len(cs)]; idx += len(cs)

        sh = _match_segments(qv, cv, qs, cs, sim_threshold)
        q_cov = sh["hits"] / sh["q_total"] if sh["q_total"] else 0.0
        c_cov = sh["hits"] / sh["c_total"] if sh["c_total"] else 0.0

        ev = s.setdefault("evidence", {})
        exact_ratio = float(s.get("final_score") or 0.0)  # Layer4 的 similar_line_ratio
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
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("写入 {}（原 {} 保留）", out_path, suspects_path.name)
    data["_output_path"] = str(out_path)
    return data
