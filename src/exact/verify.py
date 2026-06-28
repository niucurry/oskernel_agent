"""嫌疑对生成流水线：召回结果 → 精确比对 → SuspectPair（分流）→ suspects.json。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.models import Evidence, FunctionRecord, ModuleTag, SuspectPair
from src.normalize.store import DEFAULT_DB

from .matcher import ExactMatcher, remap_spans

DEFAULT_OUTPUT_DIR = "data/output"

# 召回候选进入精确比对的最低向量相似度
VECTOR_SIM_GATE = 0.7

_CANDIDATE_SQL = (
    "SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag, lang, "
    "raw_code, normalized_code FROM functions WHERE id=?"
)


def tier_of(ratio: float) -> str | None:
    """按精确匹配比例分流；< 0.5 返回 None（丢弃）。"""
    if ratio > 0.95:
        return "confirmed"
    if ratio >= 0.7:
        return "review"
    if ratio >= 0.5:
        return "weak"
    return None


def _record_from_query(q: dict) -> FunctionRecord:
    return FunctionRecord(
        repo_id=q["repo_id"],
        file_path=q["file_path"],
        start_line=q["start_line"],
        end_line=q["end_line"],
        func_name=q["func_name"],
        module_tag=ModuleTag(q.get("module_tag", "other")),
        lang=q.get("lang", "rust"),
        raw_code=q.get("raw_code", ""),
        normalized_code=q.get("normalized_code", ""),
    )


def _record_from_row(row: sqlite3.Row) -> FunctionRecord:
    return FunctionRecord(
        repo_id=row["repo_id"],
        file_path=row["file_path"],
        start_line=row["start_line"],
        end_line=row["end_line"],
        func_name=row["func_name"],
        module_tag=ModuleTag(row["module_tag"]),
        lang=row["lang"],
        raw_code=row["raw_code"],
        normalized_code=row["normalized_code"],
    )


def verify_recall(
    recall_path: str | Path,
    *,
    db_path: str | Path = DEFAULT_DB,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    matcher: ExactMatcher | None = None,
) -> dict:
    """对召回结果逐对精确比对，生成分流后的 SuspectPair 列表并落盘。"""
    recall_path = Path(recall_path)
    recall = json.loads(recall_path.read_text(encoding="utf-8"))
    matcher = matcher or ExactMatcher()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    suspects: list[SuspectPair] = []
    n_pairs = 0
    for item in recall["results"]:
        q = item["query"]
        q_rec = _record_from_query(q)
        for cand in item["candidates"]:
            if cand["score"] <= VECTOR_SIM_GATE:  # 仅比对向量相似度 > 0.7 的候选
                continue
            row = conn.execute(_CANDIDATE_SQL, (cand["id"],)).fetchone()
            if row is None:
                logger.warning("候选 func_id={} 不在 {}，跳过", cand["id"], db_path)
                continue
            n_pairs += 1
            res = matcher.match(q_rec.raw_code, row["raw_code"], lang=q_rec.lang)
            tier = tier_of(res.similar_line_ratio)
            if tier is None:  # < 0.5 丢弃
                continue

            cand_rec = _record_from_row(row)
            abs_spans = remap_spans(res.matched_spans, q_rec.start_line, cand_rec.start_line)
            suspects.append(
                SuspectPair(
                    query_func=q_rec,
                    candidate_func=cand_rec,
                    evidence=Evidence(
                        vector_similarity=cand["score"],
                        exact_match_lines=res.exact_match_lines,
                        renamed_match_lines=res.renamed_match_lines,
                    ),
                    final_score=res.similar_line_ratio,
                    tier=tier,
                    matched_spans=abs_spans,
                    match_type_per_span=res.match_type_per_span,
                )
            )
    conn.close()

    tier_counts = Counter(s.tier for s in suspects)
    suspects.sort(key=lambda s: s.final_score, reverse=True)
    out = {
        "query_repo_id": recall.get("query_repo_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compared_pairs": n_pairs,
        "tier_counts": dict(tier_counts),
        "suspects": [s.model_dump(mode="json") for s in suspects],
    }

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = recall_path.name.replace("_recall.json", "") + "_suspects.json"
    out_path = out_dir / out_name
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "精确比对完成：比对 {} 对，输出 {} 个嫌疑对（{}）→ {}",
        n_pairs, len(suspects), dict(tier_counts), out_path,
    )
    out["_output_path"] = str(out_path)
    return out
