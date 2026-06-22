"""评测集构造：从历史库随机抽函数，生成 T1-T4 已知克隆 + 等量随机负样本。

  python -m tests.evaluation.synthesize [--per-class 50] [--db ...] [--out ...] [--seed 0]
输出 tests/fixtures/eval_set.json（含 ground truth 标签）。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from src.normalize.extract import extract_functions
from src.normalize.store import DEFAULT_DB

from .transforms import CLASSES, is_valid, transform

DEFAULT_OUT = "tests/fixtures/eval_set.json"


def _load_pool(db_path: str | Path) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, repo_id, file_path, start_line, end_line, func_name, lang, raw_code "
        "FROM functions WHERE lang='rust' AND (end_line - start_line) BETWEEN 6 AND 120"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _variant_payload(raw_code: str, lang: str, func_name: str) -> dict | None:
    funcs = extract_functions(raw_code, lang, min_lines=1)
    if not funcs:
        return None
    f = funcs[0]
    return {
        "func_name": func_name, "lang": lang, "file_path": f"synthetic/{func_name}.rs",
        "start_line": 1, "end_line": raw_code.count("\n") + 1,
        "raw_code": raw_code, "normalized_code": f.normalized_code, "feature_tokens": f.feature_tokens,
    }


def _target(src: dict) -> dict:
    return {k: src[k] for k in ("id", "repo_id", "file_path", "start_line", "end_line", "func_name")}


def synthesize(db_path: str | Path = DEFAULT_DB, *, per_class: int = 50, seed: int = 0, min_pool: int = 20) -> dict:
    rng = random.Random(seed)
    pool = _load_pool(db_path)
    if len(pool) < min_pool:
        raise RuntimeError(f"历史库函数太少（{len(pool)}），无法构造评测集，请先建库。")
    logger.info("源池 rust 函数 {} 个", len(pool))

    samples: list[dict] = []
    sid = 0
    for cls in CLASSES:
        candidates = pool[:]
        rng.shuffle(candidates)
        made = 0
        for src in candidates:
            if made >= per_class:
                break
            out = transform(src["raw_code"], cls, "rust", seed=rng.randint(0, 10**6))
            if not out or not is_valid(out, "rust") or out == src["raw_code"] and cls != "T1":
                continue
            payload = _variant_payload(out, "rust", src["func_name"])
            if payload is None:
                continue
            samples.append({"sample_id": sid, "cls": cls, "label": 1, "target": _target(src), "variant": payload})
            sid += 1
            made += 1
        logger.info("类 {}：生成正样本 {}/{}", cls, made, per_class)

    # 等量随机负样本：variant=随机函数 B，target=不相关函数 A
    n_pos = len(samples)
    neg = 0
    while neg < n_pos:
        a, b = rng.sample(pool, 2)
        if a["id"] == b["id"] or a["func_name"] == b["func_name"]:
            continue
        payload = _variant_payload(b["raw_code"], "rust", b["func_name"])
        if payload is None:
            continue
        samples.append({"sample_id": sid, "cls": "NEG", "label": 0, "target": _target(a), "variant": payload})
        sid += 1
        neg += 1

    counts = {}
    for s in samples:
        counts[s["cls"]] = counts.get(s["cls"], 0) + 1
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_db": str(db_path), "seed": seed, "counts": counts, "samples": samples,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tests.evaluation.synthesize")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--per-class", type=int, default=50)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)

    data = synthesize(args.db, per_class=args.per_class, seed=args.seed)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("评测集写入 {}：{}", args.out, data["counts"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
