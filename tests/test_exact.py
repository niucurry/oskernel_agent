"""src.exact 测试：行级比对、行号换算、分流与 verify 流水线。"""

from __future__ import annotations

import json
from pathlib import Path

from src.exact.matcher import ExactMatcher, ExactMatchResult, remap_spans
from src.exact.verify import tier_of, verify_recall
from src.models import FunctionRecord
from src.normalize.store import FunctionStore

CAND = (
    "fn schedule(ready: &Vec<usize>) -> usize {\n"
    "    let chosen = ready[0];\n"
    "    let limit = 8;\n"
    "    do_switch(chosen, limit);\n"
    "    chosen\n"
    "}"
)
# 改名版：仅变量名不同
QUERY_RENAMED = (
    "fn schedule(rq: &Vec<usize>) -> usize {\n"
    "    let picked = rq[0];\n"
    "    let lim = 8;\n"
    "    do_switch(picked, lim);\n"
    "    picked\n"
    "}"
)
UNRELATED = (
    "fn read_block(dev: u32, idx: usize) -> Buffer {\n"
    "    let cache = lookup(dev);\n"
    "    cache.fetch(idx)\n"
    "}"
)


# ---------- 行号换算（最易出 bug，覆盖函数不在文件开头） ----------

def test_remap_spans_function_not_at_file_start():
    # 函数内 1-based span，函数 A 从文件第 50 行开始，B 从第 120 行开始
    spans = [(1, 1, 1, 1), (3, 5, 2, 4)]
    out = remap_spans(spans, a_start_line=50, b_start_line=120)
    assert out == [(50, 50, 120, 120), (52, 54, 121, 123)]


def test_remap_identity_when_start_is_one():
    spans = [(2, 4, 6, 8)]
    assert remap_spans(spans, 1, 1) == [(2, 4, 6, 8)]


def test_matcher_spans_are_function_local_and_remap_to_absolute():
    m = ExactMatcher()
    res = m.match(CAND, QUERY_RENAMED, "rust")
    # 函数内行号：最大不超过函数行数（6 行）
    assert all(1 <= a_s <= 6 and 1 <= b_s <= 6 for (a_s, _, b_s, _) in res.matched_spans)
    remapped = remap_spans(res.matched_spans, 100, 200)
    # A 侧落在 100-105，B 侧落在 200-205
    assert all(100 <= a_s <= 105 and 200 <= b_s <= 205 for (a_s, _, b_s, _) in remapped)


# ---------- exact / renamed 分流 ----------

def test_identical_code_all_exact():
    res = ExactMatcher().match(CAND, CAND, "rust")
    assert res.similar_line_ratio == 1.0
    assert set(res.match_type_per_span) == {"exact"}
    assert res.renamed_match_lines == 0


def test_renamed_copy_tagged_renamed_high_ratio():
    res = ExactMatcher().match(CAND, QUERY_RENAMED, "rust")
    assert res.similar_line_ratio > 0.95          # 改名后整体仍高度相似
    assert "renamed" in res.match_type_per_span    # 至少有 renamed 段
    assert res.renamed_match_lines > 0


def test_unrelated_low_ratio():
    res = ExactMatcher().match(CAND, UNRELATED, "rust")
    assert res.similar_line_ratio < 0.5


# D2：仅寄存器名不同的汇编必须落 renamed，绝不误判 exact
ASM_REG_A = (
    "    mv t0, a0\n"
    "    ld t1, 0(t0)\n"
    "    add t2, t1, a1\n"
    "    sd t2, 0(t0)\n"
)
ASM_REG_B = (  # 仅寄存器改名 t0/t1/t2 → s0/s1/s2
    "    mv s0, a0\n"
    "    ld s1, 0(s0)\n"
    "    add s2, s1, a1\n"
    "    sd s2, 0(s0)\n"
)


def test_asm_register_rename_is_renamed_not_exact():
    res = ExactMatcher().match(ASM_REG_A, ASM_REG_B, "asm")
    assert res.similar_line_ratio > 0.95         # 改名后整体高度相似
    assert res.exact_match_lines == 0            # 没有逐字节相同的行
    assert res.renamed_match_lines > 0           # 全部经掩码后相同 → renamed
    assert set(res.match_type_per_span) == {"renamed"}


def test_tier_thresholds():
    assert tier_of(0.99) == "confirmed"
    assert tier_of(0.95) == "review"   # 0.95 不算 >0.95
    assert tier_of(0.8) == "review"
    assert tier_of(0.6) == "weak"
    assert tier_of(0.4) is None


# ---------- verify 流水线 ----------

def _make_db_with_candidate(tmp_path: Path) -> tuple[Path, int]:
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        rec = FunctionRecord(
            repo_id="2023/team_hist",
            file_path="os/src/sched/task.rs",
            start_line=100, end_line=105,
            func_name="schedule", module_tag="sched", lang="rust",
            raw_code=CAND, normalized_code="",
        )
        fid = store.add_function(rec, [])
        store.conn.commit()
    return db, fid


def _write_recall(tmp_path: Path, cand_id: int) -> Path:
    recall = {
        "query_repo_id": "2024/team_new",
        "top_k": 20,
        "results": [
            {
                "query": {
                    "repo_id": "2024/team_new",
                    "file_path": "kernel/sched.rs",
                    "start_line": 200, "end_line": 205,
                    "func_name": "schedule", "module_tag": "sched",
                    "lang": "rust", "raw_code": QUERY_RENAMED, "normalized_code": "",
                },
                "candidates": [
                    {"id": cand_id, "score": 0.97,
                     "payload": {"repo_id": "2023/team_hist", "func_name": "schedule"}},
                    # 低于向量门限，应被跳过
                    {"id": cand_id, "score": 0.5,
                     "payload": {"repo_id": "2023/team_hist", "func_name": "schedule"}},
                ],
            }
        ],
    }
    p = tmp_path / "team_new_recall.json"
    p.write_text(json.dumps(recall, ensure_ascii=False), encoding="utf-8")
    return p


def test_verify_pipeline_confirmed_with_absolute_lines(tmp_path):
    db, fid = _make_db_with_candidate(tmp_path)
    recall = _write_recall(tmp_path, fid)
    out = verify_recall(recall, db_path=db, output_dir=tmp_path / "out")

    # 仅 score>0.7 的候选进入比对（两个候选里跳过 0.5 的那个）
    assert out["compared_pairs"] == 1
    assert Path(out["_output_path"]).exists()
    assert out["tier_counts"].get("confirmed", 0) == 1

    sp = out["suspects"][0]
    assert sp["tier"] == "confirmed"
    assert sp["query_func"]["func_name"] == "schedule"
    assert sp["candidate_func"]["repo_id"] == "2023/team_hist"
    assert sp["evidence"]["vector_similarity"] == 0.97
    # 行号已换算回绝对：候选侧落在 100-105，query 侧落在 200-205
    for (a_s, a_e, b_s, b_e) in sp["matched_spans"]:
        assert 200 <= a_s <= 205 and 100 <= b_s <= 105
    assert "renamed" in sp["match_type_per_span"]


def test_verify_discards_below_half(tmp_path):
    db, fid = _make_db_with_candidate(tmp_path)
    # query 与候选无关 → ratio < 0.5 → 丢弃
    recall = {
        "query_repo_id": "2024/team_new",
        "results": [{
            "query": {
                "repo_id": "2024/team_new", "file_path": "k.rs",
                "start_line": 1, "end_line": 4, "func_name": "read_block",
                "module_tag": "fs", "lang": "rust", "raw_code": UNRELATED, "normalized_code": "",
            },
            "candidates": [{"id": fid, "score": 0.95, "payload": {}}],
        }],
    }
    p = tmp_path / "x_recall.json"
    p.write_text(json.dumps(recall), encoding="utf-8")
    out = verify_recall(p, db_path=db, output_dir=tmp_path / "out")
    assert out["compared_pairs"] == 1
    assert out["suspects"] == []


def test_verify_never_compares_cross_language_candidate(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        rec = FunctionRecord(
            repo_id="2023/c_hist", file_path="kernel/sched.c",
            start_line=1, end_line=6, func_name="schedule",
            module_tag="sched", lang="c", raw_code=QUERY_RENAMED, normalized_code="",
        )
        fid = store.add_function(rec, [])
        store.conn.commit()
    recall = {
        "query_repo_id": "2024/new",
        "results": [{
            "query": {"repo_id": "2024/new", "file_path": "kernel/sched.rs",
                      "start_line": 1, "end_line": 6, "func_name": "schedule",
                      "module_tag": "sched", "lang": "rust",
                      "raw_code": QUERY_RENAMED, "normalized_code": ""},
            "candidates": [{"id": fid, "score": 1.0, "payload": {}}],
        }],
    }
    path = tmp_path / "cross_lang_recall.json"
    path.write_text(json.dumps(recall), encoding="utf-8")

    out = verify_recall(path, db_path=db, output_dir=tmp_path / "out")

    assert out["compared_pairs"] == 0
    assert out["suspects"] == []


def test_fingerprint_candidate_bypasses_vector_gate_and_never_becomes_original(tmp_path):
    db, fid = _make_db_with_candidate(tmp_path)
    recall = {
        "query_repo_id": "2024/team_new",
        "results": [{
            "query": {
                "repo_id": "2024/team_new", "file_path": "k.rs",
                "start_line": 1, "end_line": 4, "func_name": "run_tasks",
                "module_tag": "sched", "lang": "rust", "raw_code": UNRELATED,
                "normalized_code": "same-structural-fingerprint",
            },
            "candidates": [{"id": fid, "score": 0.1, "fingerprint_match": True, "payload": {}}],
        }],
    }
    p = tmp_path / "fingerprint_recall.json"
    p.write_text(json.dumps(recall), encoding="utf-8")
    out = verify_recall(p, db_path=db, output_dir=tmp_path / "out")
    assert out["compared_pairs"] == 1
    assert out["suspects"][0]["tier"] == "review"
    assert out["suspects"][0]["evidence"]["normalized_fingerprint_match"] is True
    assert out["suspects"][0]["final_score"] < 0.5
    assert (out["suspects"][0]["evidence"]["line_similarity"]
            == out["suspects"][0]["final_score"])


def test_strict_exact_rejects_recall_without_completeness_contract(tmp_path):
    db, fid = _make_db_with_candidate(tmp_path)
    recall = _write_recall(tmp_path, fid)
    import pytest
    with pytest.raises(RuntimeError, match="完整性契约"):
        verify_recall(recall, db_path=db, output_dir=tmp_path / "out",
                      require_complete_recall=True)


def test_structural_candidate_survives_low_line_ratio_for_segment_review(tmp_path):
    db, fid = _make_db_with_candidate(tmp_path)
    recall = {
        "query_repo_id": "2024/team_new",
        "results": [{
            "query": {
                "repo_id": "2024/team_new", "file_path": "k.rs",
                "start_line": 1, "end_line": 4, "func_name": "renamed_and_reordered",
                "module_tag": "sched", "lang": "rust", "raw_code": UNRELATED,
                "normalized_code": "",
            },
            "candidates": [{
                "id": fid, "score": 0.05, "structural_hash_match": True,
                "code_simhash_distance": 13, "payload": {},
            }],
        }],
    }
    p = tmp_path / "structural_recall.json"
    p.write_text(json.dumps(recall), encoding="utf-8")
    out = verify_recall(p, db_path=db, output_dir=tmp_path / "out")
    assert out["compared_pairs"] == 1
    assert out["suspects"][0]["tier"] == "weak"
    assert out["suspects"][0]["evidence"]["structural_hash_recall"] is True
    assert out["suspects"][0]["evidence"]["code_simhash_distance"] == 13
    assert out["suspects"][0]["final_score"] < 0.5


def test_identity_neighbor_with_shared_code_survives_for_specific_pair_review(tmp_path):
    db, fid = _make_db_with_candidate(tmp_path)
    recall = {
        "query_repo_id": "2024/team_new",
        "results": [{
            "query": {
                "repo_id": "2024/team_new", "file_path": "k.rs",
                "start_line": 1, "end_line": 6, "func_name": "schedule",
                "module_tag": "sched", "lang": "rust", "raw_code": QUERY_RENAMED,
                "normalized_code": "",
            },
            "candidates": [{
                "id": fid, "score": 0.0, "identity_expansion": True,
                "identity_score": 0.9, "payload": {},
            }],
        }],
    }
    path = tmp_path / "identity_recall.json"
    path.write_text(json.dumps(recall), encoding="utf-8")

    class LowPartialMatcher:
        def match(self, *_args, **_kwargs):
            return ExactMatchResult(
                similar_line_ratio=0.3,
                matched_spans=[(1, 3, 1, 3)],
                match_type_per_span=["exact"],
                exact_match_lines=3,
                renamed_match_lines=0,
            )

    out = verify_recall(
        path, db_path=db, output_dir=tmp_path / "out", matcher=LowPartialMatcher())

    suspect = out["suspects"][0]
    assert suspect["tier"] == "weak"
    assert suspect["evidence"]["function_identity_recall"] is True
    assert suspect["evidence"]["function_identity_score"] >= 0.9
    assert suspect["evidence"]["function_name_exact"] is True
    assert suspect["evidence"]["function_identity_relation"] == "exact_counterpart"


def test_same_name_recall_with_substantial_partial_match_cannot_fall_back_to_original(tmp_path):
    """同名函数被大幅扩写后即使覆盖率不足 0.5，也应进入弱相似复核而非消失。"""
    db, fid = _make_db_with_candidate(tmp_path)
    recall = {
        "query_repo_id": "2026/team_new",
        "results": [{
            "query": {
                "repo_id": "2026/team_new", "file_path": "kernel/sched.rs",
                "start_line": 200, "end_line": 205, "func_name": "schedule",
                "module_tag": "sched", "lang": "rust", "raw_code": QUERY_RENAMED,
                "normalized_code": "",
            },
            "candidates": [{
                "id": fid, "score": 0.0, "name_match": True, "payload": {},
            }],
        }],
    }
    path = tmp_path / "same_name_recall.json"
    path.write_text(json.dumps(recall), encoding="utf-8")

    class ExpandedImplementationMatcher:
        def match(self, *_args, **_kwargs):
            return ExactMatchResult(
                similar_line_ratio=0.4,
                matched_spans=[(1, 3, 1, 3)],
                match_type_per_span=["renamed"],
                exact_match_lines=0,
                renamed_match_lines=3,
            )

    out = verify_recall(
        path, db_path=db, output_dir=tmp_path / "out",
        matcher=ExpandedImplementationMatcher(),
    )

    assert out["compared_pairs"] == 1
    assert len(out["suspects"]) == 1
    assert out["suspects"][0]["tier"] == "weak"
    assert out["suspects"][0]["evidence"]["function_name_recall"] is True
    assert out["suspects"][0]["evidence"]["function_identity_relation"] == "exact_counterpart"
