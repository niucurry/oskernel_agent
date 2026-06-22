"""src.review 测试：用 mock LLM 覆盖正常/重试/越界过滤/分歧等路径。"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.review.card import approx_tokens, build_card
from src.review.config import LLMSettings
from src.review.reviewer import review_all
from src.review.voting import review_one


# ---------- mock LLM ----------

class FakeLLM:
    """按顺序吐出预设回复；用尽后重复最后一条。记录调用次数。"""

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.calls = 0

    async def complete(self, messages, temperature):  # noqa: D401
        i = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[i]


def vjson(verdict="likely_clone", clone_type="renamed", conf=0.8, new="102-104", old="202-204", fence=False):
    obj = {
        "verdict": verdict,
        "clone_type": clone_type,
        "confidence": conf,
        "reasoning": "两处实现逐行对应。",
        "evidence": [{"new_lines": new, "old_lines": old, "observation": "结构一致"}],
        "could_be_coincidence": "可能是通用模式",
        "recommendation_for_reviewer": "人工确认",
    }
    s = json.dumps(obj, ensure_ascii=False)
    return f"```json\n{s}\n```" if fence else s


def make_suspect(qlen=11, clen=11, span=(102, 106, 202, 206), tier="review"):
    q_raw = "\n".join(f"    let v{i} = {i};" for i in range(qlen))
    c_raw = "\n".join(f"    let w{i} = {i};" for i in range(clen))
    return {
        "tier": tier,
        "final_score": 0.8,
        "query_func": {
            "repo_id": "2024/new", "file_path": "k.rs", "start_line": 100, "end_line": 100 + qlen - 1,
            "func_name": "schedule", "module_tag": "sched", "lang": "rust", "raw_code": q_raw,
            "normalized_code": "",
        },
        "candidate_func": {
            "repo_id": "2023/old", "file_path": "o.rs", "start_line": 200, "end_line": 200 + clen - 1,
            "func_name": "schedule", "module_tag": "sched", "lang": "rust", "raw_code": c_raw,
            "normalized_code": "",
        },
        "evidence": {"vector_similarity": 0.82, "exact_match_lines": 5},
        "matched_spans": [list(span)],
        "match_type_per_span": ["renamed"],
    }


def cfg(**kw):
    base = dict(votes=3, temperature=0.3, concurrency=2, max_card_tokens=6000, context_lines=10)
    base.update(kw)
    return LLMSettings(**base)


# ---------- 卡片构造 ----------

def test_card_has_absolute_line_numbers_and_evidence():
    card = build_card(make_suspect(), max_tokens=6000, context_lines=10)
    assert "100 |" in card                  # 新作品绝对行号从 100 起
    assert "200 |" in card                  # 历史作品从 200 起
    assert "vector_similarity" in card and "0.82" in card
    assert "新 102-106 ↔ 旧 202-206" in card


def test_card_trims_long_code_with_omission_marker():
    s = make_suspect(qlen=80, clen=80, span=(140, 142, 240, 242))
    card = build_card(s, max_tokens=6000, context_lines=10)
    assert "省略" in card                    # 远离匹配区的行被省略


def test_card_respects_token_budget():
    s = make_suspect(qlen=400, clen=400, span=(150, 152, 250, 252))
    card = build_card(s, max_tokens=1500, context_lines=10)
    assert approx_tokens(card) <= 1500


# ---------- 投票 / 解析 ----------

def test_normal_majority_verdict():
    s = make_suspect()
    client = FakeLLM([vjson(), vjson(fence=True), vjson()])
    out = asyncio.run(review_one(s, client, cfg()))
    r = out["review"]
    assert r["verdict"] == "likely_clone"
    assert r["n_parsed"] == 3
    assert r["evidence"]  # 行号在范围内，保留
    assert not out["review_warnings"]


def test_parse_error_then_retry_succeeds():
    s = make_suspect()
    # votes=1：第一次坏 JSON，纠正后第二次成功
    client = FakeLLM(["这不是 JSON", vjson()])
    out = asyncio.run(review_one(s, client, cfg(votes=1)))
    assert client.calls == 2          # 触发了一次重试
    assert out["review"]["verdict"] == "likely_clone"


def test_all_unparseable_marks_parse_error():
    s = make_suspect()
    client = FakeLLM(["nope"])        # 永远返回坏 JSON
    out = asyncio.run(review_one(s, client, cfg(votes=3)))
    r = out["review"]
    assert r["verdict"] == "parse_error"
    assert r["n_parsed"] == 0
    assert len(r["raw_outputs"]) == 3


def test_three_way_disputed():
    s = make_suspect()
    client = FakeLLM([
        vjson(verdict="likely_clone", conf=0.9),
        vjson(verdict="common_pattern", conf=0.5),
        vjson(verdict="false_positive", conf=0.3),
    ])
    out = asyncio.run(review_one(s, client, cfg()))
    r = out["review"]
    assert r["verdict"] == "disputed"
    assert len(r["votes"]) == 3            # 保留三次原始判定
    assert r["confidence"] == pytest.approx((0.9 + 0.5 + 0.3) / 3, abs=1e-3)


# ---------- 后置校验 ----------

def test_out_of_range_evidence_filtered():
    s = make_suspect()
    # 行号越界：new_lines 999 不在 100-110
    client = FakeLLM([vjson(verdict="high_similarity", new="999", old="202")] * 3)
    out = asyncio.run(review_one(s, client, cfg()))
    assert out["review"]["evidence"] == []
    assert out["review_warnings"]
    assert out["review"]["verdict"] == "high_similarity"  # 非 likely_clone 不降级


def test_likely_clone_empty_evidence_downgraded():
    s = make_suspect()
    client = FakeLLM([vjson(verdict="likely_clone", new="999", old="888")] * 3)
    out = asyncio.run(review_one(s, client, cfg()))
    assert out["review"]["verdict"] == "disputed"          # 证据清空 → 降级
    assert "downgraded_reason" in out["review"]


# ---------- 编排：只复核 review 档 ----------

def test_review_all_only_reviews_review_tier():
    data = {
        "query_repo_id": "2024/new",
        "suspects": [make_suspect(tier="review"), make_suspect(tier="confirmed")],
    }
    client = FakeLLM([vjson()])
    result = asyncio.run(review_all(data, client, cfg()))
    assert result["n_reviewed"] == 1
    assert len(result["suspects"]) == 2
    reviewed = [s for s in result["suspects"] if s["tier"] == "review"][0]
    skipped = [s for s in result["suspects"] if s["tier"] == "confirmed"][0]
    assert reviewed["review"]["verdict"] == "likely_clone"
    assert skipped["review"]["verdict"] == "skipped"
