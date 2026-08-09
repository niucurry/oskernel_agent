"""tests.evaluation 测试：四类变换合法性、合成集构造、指标渲染/回归阈值。"""

from __future__ import annotations

from pathlib import Path

from oskernel_agent.comparison.models import FunctionRecord
from oskernel_agent.comparison.normalize.store import FunctionStore
from tests.evaluation.run import check_thresholds, render_report
from tests.evaluation.synthesize import synthesize
from tests.evaluation.transforms import is_valid, transform

IF_ELSE_FN = """pub fn classify(x: i32, y: i32) -> i32 {
    let sum = x + y;
    let scaled = sum * 2;
    if scaled > 10 {
        return scaled;
    } else {
        return sum;
    }
}"""

FOR_FN = """pub fn total(values: &Vec<usize>) -> usize {
    let mut acc = 0;
    let bias = 3;
    for v in values.iter() {
        acc += v;
        acc += bias;
    }
    acc
}"""


# ---------- 变换合法性 ----------

def test_t1_t2_valid_and_renamed():
    assert is_valid(transform(IF_ELSE_FN, "T1"), "rust")
    t2 = transform(IF_ELSE_FN, "T2", "rust", seed=1)
    assert t2 and is_valid(t2, "rust")
    assert "r0_" in t2 and t2 != IF_ELSE_FN          # 系统性改名
    # 关键词/类型未被破坏
    assert "if " in t2 and "else" in t2 and "i32" in t2


def test_t3_edit_valid():
    t3 = transform(FOR_FN, "T3", "rust", seed=2)
    assert t3 and is_valid(t3, "rust")               # 增删后仍合法


def test_t4_restructure_valid():
    t4_if = transform(IF_ELSE_FN, "T4", "rust", seed=3)
    assert t4_if and is_valid(t4_if, "rust")
    assert "match" in t4_if                            # if-else → match
    t4_for = transform(FOR_FN, "T4", "rust", seed=3)
    assert t4_for and is_valid(t4_for, "rust")
    assert "while let" in t4_for                       # for → while-let


# ---------- 合成集 ----------

def _tiny_db(tmp_path) -> Path:
    db = tmp_path / "functions.db"
    funcs = [IF_ELSE_FN, FOR_FN] * 3  # 6 个，含 if-else 与 for，语句充足
    with FunctionStore(db) as s:
        for i, code in enumerate(funcs):
            rec = FunctionRecord(
                repo_id="hist/x", file_path=f"f{i}.rs", start_line=1,
                end_line=1 + code.count("\n"), func_name=f"fn{i}", module_tag="other",
                lang="rust", raw_code=code, normalized_code="",
            )
            s.add_function(rec, [], [])
        s.conn.commit()
    return db


def test_synthesize_builds_labeled_set(tmp_path):
    db = _tiny_db(tmp_path)
    data = synthesize(db, per_class=2, seed=0, min_pool=4)
    counts = data["counts"]
    assert counts.get("T1") == 2 and counts.get("T2") == 2
    n_pos = sum(v for k, v in counts.items() if k != "NEG")
    assert counts.get("NEG") == n_pos                 # 等量负样本

    for s in data["samples"]:
        assert s["label"] in (0, 1)
        assert s["variant"]["normalized_code"] is not None
        assert "feature_tokens" in s["variant"]
    # T2 变体确实改了名
    t2s = [s for s in data["samples"] if s["cls"] == "T2"]
    assert any("r0_" in s["variant"]["raw_code"] for s in t2s)


# ---------- 指标渲染 / 回归阈值 ----------

def test_check_thresholds():
    good = {"by_class": {"T1": {"recall_final": 0.98}, "T2": {"recall_final": 0.96},
                         "T3": {"recall_final": 0.85}}}
    assert check_thresholds(good) == []
    bad = {"by_class": {"T1": {"recall_final": 0.90}, "T2": {"recall_final": 0.96},
                        "T3": {"recall_final": 0.70}}}
    fails = check_thresholds(bad)
    assert len(fails) == 2 and any("T1" in f for f in fails) and any("T3" in f for f in fails)


def test_render_report_contains_sections():
    result = {
        "date": "2026-06-13", "counts": {"T1": 2},
        "by_class": {"T1": {"n": 2, "recall_layer1": 1.0, "recall_layer2": 1.0,
                            "recall_cascade": 1.0, "recall_final": 1.0}},
        "overall": {"precision": 1.0, "recall": 1.0, "tp": 2, "fp": 0, "neg_total": 2, "pos_total": 2},
        "timings_sec": {"embed_all": 1.2},
    }
    md = render_report(result)
    assert "评测报告" in md and "各类召回率" in md and "precision" in md
