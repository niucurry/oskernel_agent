"""单个嫌疑对的多次投票与聚合。"""

from __future__ import annotations

from collections import Counter
from statistics import mean

from .card import build_card
from .config import LLMSettings
from .llm import LLMClient
from .postcheck import postcheck
from .prompts import JSON_CORRECTION, SYSTEM_PROMPT, build_user_prompt
from .schema import AGG_DISPUTED, AGG_PARSE_ERROR, Verdict, parse_verdict


async def _single_vote(client: LLMClient, base_messages: list[dict], temperature: float) -> dict:
    """一次投票：解析失败则追加纠正指令重试一次；仍失败标 parse_error。"""
    text = await client.complete(base_messages, temperature)
    try:
        return {"verdict": parse_verdict(text), "raw": text}
    except ValueError:
        retry_messages = base_messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": JSON_CORRECTION},
        ]
        text2 = await client.complete(retry_messages, temperature)
        try:
            return {"verdict": parse_verdict(text2), "raw": text2}
        except ValueError as exc:
            return {"verdict": None, "raw": text2, "error": f"{exc}"}


def aggregate(votes: list[dict]) -> dict:
    """把多次投票聚合成一个 review dict。"""
    parsed: list[Verdict] = [v["verdict"] for v in votes if v["verdict"] is not None]
    raws = [v["raw"] for v in votes]

    if not parsed:
        return {
            "verdict": AGG_PARSE_ERROR,
            "clone_type": "none",
            "confidence": 0.0,
            "reasoning": "全部投票输出均无法解析为合法 JSON。",
            "evidence": [],
            "could_be_coincidence": "",
            "recommendation_for_reviewer": "需人工查看原始输出。",
            "raw_outputs": raws,
            "n_votes": len(votes),
            "n_parsed": 0,
        }

    counts = Counter(p.verdict for p in parsed)
    ranked = counts.most_common()
    avg_conf = round(mean(p.confidence for p in parsed), 4)
    tie = len(ranked) >= 2 and ranked[0][1] == ranked[1][1]

    if tie:  # 含「三次全不同」与「最高票平票」→ disputed，保留所有原始判定
        return {
            "verdict": AGG_DISPUTED,
            "clone_type": "none",
            "confidence": avg_conf,
            "reasoning": "多次复核结论分歧，无明确多数。",
            "evidence": [],
            "could_be_coincidence": "",
            "recommendation_for_reviewer": "结论分歧，建议人工裁定。",
            "votes": [p.model_dump() for p in parsed],
            "raw_outputs": raws,
            "vote_distribution": dict(counts),
            "n_votes": len(votes),
            "n_parsed": len(parsed),
        }

    top_label = ranked[0][0]
    rep = max((p for p in parsed if p.verdict == top_label), key=lambda p: p.confidence)
    review = rep.model_dump()
    review["verdict"] = top_label
    review["confidence"] = avg_conf  # 取多次均值
    review["vote_distribution"] = dict(counts)
    review["n_votes"] = len(votes)
    review["n_parsed"] = len(parsed)
    return review


async def review_one(suspect: dict, client: LLMClient, settings: LLMSettings) -> dict:
    """复核单个嫌疑对：构卡 → 投票 → 聚合 → 后置校验。返回增广后的 suspect。"""
    card = build_card(
        suspect, max_tokens=settings.max_card_tokens, context_lines=settings.context_lines
    )
    base_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(card)},
    ]
    votes = [await _single_vote(client, base_messages, settings.temperature) for _ in range(settings.votes)]
    review = aggregate(votes)

    q = suspect["query_func"]
    c = suspect["candidate_func"]
    warnings = postcheck(review, (q["start_line"], q["end_line"]), (c["start_line"], c["end_line"]))

    out = dict(suspect)
    out["review"] = review
    out["review_warnings"] = warnings
    return out
