"""嫌疑对生成流水线：召回结果 → 精确比对 → SuspectPair（分流）→ suspects.json。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from oskernel_agent.comparison.models import Evidence, FunctionRecord, ModuleTag, SuspectPair
from oskernel_agent.comparison.normalize.store import DEFAULT_DB
from oskernel_agent.comparison.retrieval_contract import require_complete_contract

from .identity import (function_identity, identity_can_rescue, identity_relation,
                       is_trivial_constant_stub)
from .matcher import ExactMatcher, clear_prepared_line_cache, remap_spans

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
    require_complete_recall: bool = False,
) -> dict:
    """对召回结果逐对精确比对，生成分流后的 SuspectPair 列表并落盘。"""
    recall_path = Path(recall_path)
    recall = json.loads(recall_path.read_text(encoding="utf-8"))
    if require_complete_recall:
        require_complete_contract(recall.get("retrieval_contract"), artifact="召回产物")
    matcher = matcher or ExactMatcher()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    suspects: list[SuspectPair] = []
    n_pairs = 0
    for item in recall["results"]:
        q = item["query"]
        q_rec = _record_from_query(q)
        for cand in item["candidates"]:
            fingerprint_match = bool(cand.get("fingerprint_match"))
            name_match = bool(cand.get("name_match"))
            structural_match = bool(cand.get("structural_hash_match"))
            identity_recall = bool(cand.get("identity_expansion"))
            if cand["score"] <= VECTOR_SIM_GATE and not (
                fingerprint_match or name_match or structural_match or identity_recall
            ):
                # 指纹通道是确定性结构命中，不受向量门槛影响。
                continue
            row = conn.execute(_CANDIDATE_SQL, (cand["id"],)).fetchone()
            if row is None:
                logger.warning("候选 func_id={} 不在 {}，跳过", cand["id"], db_path)
                continue
            # 对比报告只允许同一编程语言的代码进入精确核验。该防线同时兼容旧 recall
            # 产物，避免跨语言候选在 resume 场景重新流入报告。
            if (row["lang"] or "").lower() != (q_rec.lang or "").lower():
                continue
            n_pairs += 1
            res = matcher.match(q_rec.raw_code, row["raw_code"], lang=q_rec.lang)
            identity = function_identity(
                q_rec.func_name, q_rec.raw_code, row["func_name"], row["raw_code"] or "",
            )
            identity_score = max(
                float(cand.get("identity_score") or 0.0), float(identity["score"]),
            )
            relation = identity_relation(
                bool(identity["exact_name"]), identity_score, res.similar_line_ratio,
            )
            if (not identity["exact_name"]
                    and is_trivial_constant_stub(q_rec.raw_code)
                    and is_trivial_constant_stub(row["raw_code"] or "")):
                # 不同名字的常量占位函数没有行为身份可供配对。多行签名造成的高覆盖
                # 不能把两个完全不同的接口包装成“高行相似改名候选”。
                relation = "nonsemantic_stub"
            tier = tier_of(res.similar_line_ratio)
            if tier is None and fingerprint_match:
                # 归一化器还会泛化数字/字符串，结构指纹相同但逐行覆盖不足时至少进入人工复核，
                # 不能直接宣称“confirmed”，也绝不能掉回“原创”。
                tier = "review"
            if tier is None and structural_match:
                # 行顺序调整会让 SequenceMatcher 覆盖率显著下降；结构哈希命中先保留为 weak，
                # 交给下一层分段语义验证，不能在 exact 层提前丢弃。
                tier = "weak"
            if tier is None and identity_can_rescue(
                res.similar_line_ratio,
                res.exact_match_lines + res.renamed_match_lines,
                identity_score,
            ):
                # 同一候选文件内的具体函数身份能够修复“邻近模板函数配错”，但身份相同
                # 本身不是借鉴证据，因此只保留为 weak，继续交给分段/语义复核。
                tier = "weak"
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
                        line_similarity=res.similar_line_ratio,
                        code_simhash_distance=cand.get("code_simhash_distance"),
                        normalized_fingerprint_match=fingerprint_match,
                        function_name_recall=name_match,
                        structural_hash_recall=structural_match,
                        function_identity_score=identity_score,
                        function_identity_recall=identity_recall,
                        function_name_exact=bool(identity["exact_name"]),
                        function_identity_relation=relation,
                        exact_match_lines=res.exact_match_lines,
                        renamed_match_lines=res.renamed_match_lines,
                    ),
                    # final_score 在分段验证前就是原始逐行相似度。指纹/结构命中只改变
                    # 召回档位，不伪造 0.7/0.5 的相似度下限；否则下游无法区分
                    # “强召回信号”与“实际代码覆盖率”。
                    final_score=res.similar_line_ratio,
                    tier=tier,
                    matched_spans=abs_spans,
                    match_type_per_span=res.match_type_per_span,
                )
            )
    conn.close()
    # 后续分段/元数据仍在同一长运行进程中；候选侧预处理缓存已无用，
    # 立即释放以降低峰值工作集和换页风险。
    clear_prepared_line_cache()

    tier_counts = Counter(s.tier for s in suspects)
    suspects.sort(key=lambda s: s.final_score, reverse=True)
    out = {
        "query_repo_id": recall.get("query_repo_id"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compared_pairs": n_pairs,
        "tier_counts": dict(tier_counts),
        "retrieval_contract": recall.get("retrieval_contract", {}),
        "suspects": [s.model_dump(mode="json") for s in suspects],
    }

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = recall_path.name.replace("_recall.json", "") + "_suspects.json"
    out_path = out_dir / out_name
    out_path.write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    logger.info(
        "精确比对完成：比对 {} 对，输出 {} 个嫌疑对（{}）→ {}",
        n_pairs, len(suspects), dict(tier_counts), out_path,
    )
    out["_output_path"] = str(out_path)
    return out
