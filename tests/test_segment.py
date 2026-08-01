"""src.segment 测试：分段器、重打分/升降级逻辑、覆盖率（真克隆 vs 主题相似）。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import numpy as np
import pytest

import src.segment.verify as segment_verify
from src.normalize.segmenter import segment_function
from src.segment.verify import (_raw_line_similarity, _rescore, _retier,
                                _select_segment_targets, run_segment,
                                verify_segments)

# 优先级扫描调度器（~18 行）
PRIO = """pub fn schedule(&mut self, queue: &mut Vec<Task>) -> Option<usize> {
    let mut best = None;
    let mut best_prio = 0;
    for (i, t) in queue.iter().enumerate() {
        if t.ready {
            if t.priority > best_prio {
                best_prio = t.priority;
                best = Some(i);
            }
        }
    }
    match best {
        Some(idx) => {
            queue[idx].state = State::Running;
            Some(idx)
        }
        None => None,
    }
}"""
# 同一算法，仅重命名变量（真克隆）
PRIO_RENAMED = """pub fn schedule(&mut self, q: &mut Vec<Task>) -> Option<usize> {
    let mut chosen = None;
    let mut top = 0;
    for (k, task) in q.iter().enumerate() {
        if task.ready {
            if task.priority > top {
                top = task.priority;
                chosen = Some(k);
            }
        }
    }
    match chosen {
        Some(id) => {
            q[id].state = State::Running;
            Some(id)
        }
        None => None,
    }
}"""
# 同主题（调度器）但完全不同算法：轮转 + 时间片
RR = """pub fn schedule(&mut self, queue: &mut VecDeque<Task>) -> Option<usize> {
    let front = queue.pop_front()?;
    self.current_slice = self.current_slice.wrapping_sub(1);
    if self.current_slice == 0 {
        self.current_slice = DEFAULT_QUANTUM;
        queue.push_back(front.clone());
        self.ticks += front.cost;
        return Some(front.id);
    }
    queue.push_front(front);
    self.idle_ticks += 1;
    None
}"""


def _suspect(qcode, ccode, tier="review", final=0.5, vec=0.8):
    return {
        "tier": tier,
        "final_score": final,
        "query_func": {"repo_id": "2024/n", "file_path": "a.rs", "start_line": 100,
                       "end_line": 100 + qcode.count("\n"), "func_name": "schedule",
                       "module_tag": "sched", "lang": "rust", "raw_code": qcode, "normalized_code": ""},
        "candidate_func": {"repo_id": "2021/h", "file_path": "b.rs", "start_line": 200,
                           "end_line": 200 + ccode.count("\n"), "func_name": "schedule",
                           "module_tag": "sched", "lang": "rust", "raw_code": ccode, "normalized_code": ""},
        "evidence": {"vector_similarity": vec, "line_similarity": final,
                     "exact_match_lines": 3},
        "matched_spans": [], "match_type_per_span": [],
    }


@pytest.fixture(scope="module")
def embedder():
    from src.embed.embedder import get_embedder
    try:
        return get_embedder(show_progress=False)
    except Exception as exc:
        pytest.skip(f"codet5p 模型不可用：{exc}")


# ---------- 分段器 ----------

def test_short_function_single_segment():
    segs = segment_function("fn f(a:usize)->usize{ let b=a+1; b }", "rust", start_line=10)
    assert len(segs) == 1
    assert segs[0].start_line == 10


def test_long_function_multiple_segments_absolute_lines():
    segs = segment_function(PRIO, "rust", start_line=100)
    assert len(segs) >= 2
    assert segs[0].start_line >= 100                  # 绝对行号
    assert all(s.end_line >= s.start_line for s in segs)


# ---------- 重打分 / 升降级（纯函数） ----------

def test_rescore_formula():
    assert _rescore(0.5, 0.5, 0.2, 0.8) == pytest.approx(0.5)
    assert _rescore(1, 1, 1, 1) == 1.0
    assert _rescore(0, 0, 0, 0) == 0.0


def test_raw_line_similarity_is_not_replaced_by_composite_score():
    suspect = _suspect(PRIO, RR, final=0.86)
    suspect["evidence"]["line_similarity"] = 0.14
    assert _raw_line_similarity(suspect) == pytest.approx(0.14)


def test_retier_weak_upgrade():
    assert _retier("weak", 0.8, 0.9, 0.9, 0.9)[0] == "review"
    assert _retier("weak", 0.5, 0.9, 0.9, 0.9)[0] == "weak"  # 不到 0.75 不升


def test_retier_review_downgrade():
    tier, reason = _retier("review", 0.3, 0.1, 0.1, 0.1)
    assert tier == "dismissed" and reason
    assert _retier("review", 0.3, 0.5, 0.5, 0.5)[0] == "review"  # coverage 够高不降


# ---------- 覆盖率：真克隆 vs 主题相似实现不同 ----------

def test_clone_coverage_higher_than_theme_similar(embedder):
    data = {"suspects": [
        _suspect(PRIO, PRIO_RENAMED),   # 真克隆
        _suspect(PRIO, RR),             # 同主题不同算法
    ]}
    verify_segments(data, embedder)
    clone, theme = data["suspects"][0], data["suspects"][1]

    def min_cov(s):
        return min(s["segment"]["q_coverage"], s["segment"]["c_coverage"])

    assert min_cov(clone) > min_cov(theme)        # 真克隆覆盖更高
    assert min_cov(clone) >= 0.5                  # 克隆段大量命中
    assert min_cov(theme) < min_cov(clone) - 0.2  # 主题相似明显更低


def test_segment_verification_deduplicates_source_and_embedding_work(monkeypatch):
    first = _suspect(PRIO, RR)
    second = deepcopy(first)
    second["query_func"].update({"repo_id": "2025/other", "start_line": 300})
    second["candidate_func"].update({"repo_id": "2020/mirror", "start_line": 400})
    calls = []
    real_segment = segment_verify.segment_function

    def counted_segment(*args, **kwargs):
        calls.append((args, kwargs))
        return real_segment(*args, **kwargs)

    class RecordingEmbedder:
        dim = 2

        def __init__(self):
            self.texts = []

        def encode_batch(self, texts):
            self.texts = list(texts)
            return np.asarray([[1.0, 0.0] for _ in texts], dtype=np.float32)

    monkeypatch.setattr(segment_verify, "segment_function", counted_segment)
    recorder = RecordingEmbedder()
    data = {"suspects": [first, second]}

    verify_segments(data, recorder)

    assert len(calls) == 2  # 两个唯一源码，而不是 4 个函数实例
    assert len(recorder.texts) == len(set(recorder.texts))
    raw_segment_instances = 2 * (
        len(real_segment(PRIO, "rust", 1)) + len(real_segment(RR, "rust", 1))
    )
    assert len(recorder.texts) < raw_segment_instances
    second_hits = second["evidence"]["segment_hits"]["matched_segment_pairs"]
    assert second_hits and min(pair["q_lines"][0] for pair in second_hits) >= 300
    assert min(pair["c_lines"][0] for pair in second_hits) >= 400


def test_segment_selection_keeps_best_distinct_contents_and_defers_the_rest():
    candidates = []
    for index, ratio in enumerate((0.52, 0.68, 0.59), start=1):
        suspect = _suspect(PRIO, RR + f"\n// variant {index}", final=ratio)
        suspect["candidate_func"].update({
            "repo_id": f"202{index}/team", "start_line": 200 + index * 10,
        })
        suspect["evidence"].update({
            "line_similarity": ratio,
            "exact_match_lines": round(ratio * 20),
            "function_identity_score": 0.8,
        })
        candidates.append(suspect)

    selected, eligible, selected_contents = _select_segment_targets(candidates)

    assert eligible == 3
    assert selected_contents == 2
    assert {item["evidence"]["line_similarity"] for item in selected} == {0.68, 0.59}
    assert candidates[0]["evidence"]["segment_selection"] == "deferred_secondary"
    assert candidates[0]["tier"] == "review"  # 只延后昂贵分段，不删除召回证据


def test_run_segment_writes_v2_and_keeps_original(embedder, tmp_path):
    src = tmp_path / "x_suspects.json"
    src.write_text(json.dumps({"query_repo_id": "r", "suspects": [_suspect(PRIO, RR)]}), encoding="utf-8")
    out = run_segment(src, embedder, output_dir=tmp_path)
    assert src.exists()                                  # 原版保留
    assert Path(out["_output_path"]).name == "x_suspects_v2.json"
    assert "segment_summary" in out
    sp = out["suspects"][0]
    assert "segment_hits" in sp["evidence"] and "segment" in sp
