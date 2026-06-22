"""review 编排：并发复核 suspects.json 中 tier=review 的嫌疑对，写 reviewed.json。"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from .config import LLMSettings
from .llm import LLMClient
from .voting import review_one

DEFAULT_OUTPUT_DIR = "data/output"
REVIEW_TIER = "review"


async def review_all(data: dict, client: LLMClient, settings: LLMSettings, *, limit: int | None = None) -> dict:
    """核心：并发复核全部 tier=review 的嫌疑对，返回 reviewed 输出 dict（不落盘）。

    limit 给定时只复核前 limit 个 review 档（成本/调试控制），其余 review 档标 skipped。
    """
    suspects = data.get("suspects", [])
    all_review = [s for s in suspects if s.get("tier") == REVIEW_TIER]
    targets = all_review[:limit] if limit is not None else all_review
    target_ids = {id(s) for s in targets}
    logger.info("待复核（tier=review）嫌疑对 {}/{} 个，并发度 {}", len(targets), len(all_review), settings.concurrency)

    sem = asyncio.Semaphore(settings.concurrency)

    async def worker(idx: int, s: dict) -> dict:
        async with sem:
            logger.info("复核 ({}/{}) {} ↔ {}", idx, len(targets),
                        s["query_func"]["func_name"], s["candidate_func"]["func_name"])
            return await review_one(s, client, settings)

    reviewed = await asyncio.gather(*(worker(i, s) for i, s in enumerate(targets, 1)))

    # 把复核结果按原顺序合并回全部 suspects（confirmed/weak 原样带过，标 skipped）
    it = iter(reviewed)
    out_suspects = []
    for s in suspects:
        if id(s) in target_ids:
            out_suspects.append(next(it))
        elif s.get("tier") == REVIEW_TIER:
            s2 = dict(s)
            s2["review"] = {"verdict": "skipped", "reason": "超出 --limit，未复核"}
            out_suspects.append(s2)
        else:
            s2 = dict(s)
            s2["review"] = {"verdict": "skipped", "reason": f"tier={s.get('tier')} 不在复核范围"}
            out_suspects.append(s2)

    verdict_counts = Counter(r["review"]["verdict"] for r in reviewed)
    return {
        "query_repo_id": data.get("query_repo_id"),
        "model": settings.model,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_reviewed": len(targets),
        "verdict_counts": dict(verdict_counts),
        "suspects": out_suspects,
    }


def run_review(
    suspects_path: str | Path,
    client: LLMClient,
    settings: LLMSettings,
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    limit: int | None = None,
) -> dict:
    """加载 suspects.json → 复核 → 写 {repo}_reviewed.json。"""
    suspects_path = Path(suspects_path)
    data = json.loads(suspects_path.read_text(encoding="utf-8"))
    result = asyncio.run(review_all(data, client, settings, limit=limit))

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = suspects_path.stem.split("_suspects")[0] or suspects_path.stem
    out_path = out_dir / f"{base}_reviewed.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("复核完成：{} 个 → {}（verdict 分布 {}）", result["n_reviewed"], out_path, result["verdict_counts"])
    result["_output_path"] = str(out_path)
    return result
