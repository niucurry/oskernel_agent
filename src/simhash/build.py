"""建库：从 functions.db 的 feature_tokens 计算 IDF，逐函数生成 SimHash 并建分段索引。"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path

from loguru import logger

from src.normalize.store import DEFAULT_DB
from src.buildlib.coverage import db_mapping_signature

from .index import SegmentedIndex
from .simhash import SimHasher

DEFAULT_IDF = "data/db/idf.json"
DEFAULT_INDEX = "data/db/simhash_index.pkl"


def compute_idf(token_sets: list[set[str]]) -> tuple[dict[str, float], float]:
    """weight(t) = log(N / df(t))；返回 (idf, default_weight)。default 用于未见 token（df=1）。"""
    n = len(token_sets)
    df: Counter[str] = Counter()
    for ts in token_sets:
        df.update(ts)
    idf = {t: math.log(n / c) for t, c in df.items()} if n else {}
    default_weight = math.log(n) if n > 1 else 1.0
    return idf, default_weight


def _load_tokens(db_path: str | Path) -> list[tuple[int, set[str]]]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, feature_tokens FROM functions").fetchall()
    conn.close()
    return [(fid, set(json.loads(ft or "[]"))) for fid, ft in rows]


def build_index(
    db_path: str | Path = DEFAULT_DB,
    *,
    idf_path: str | Path = DEFAULT_IDF,
    index_path: str | Path = DEFAULT_INDEX,
) -> dict:
    """计算 IDF + 建立 SimHash 分段索引并落盘。"""
    items = _load_tokens(db_path)
    logger.info("functions.db 共 {} 个函数", len(items))

    idf, default_weight = compute_idf([ts for _, ts in items])
    Path(idf_path).parent.mkdir(parents=True, exist_ok=True)
    Path(idf_path).write_text(
        json.dumps({"n": len(items), "default_weight": default_weight, "idf": idf}, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("IDF 权重表写入 {}（{} 个 token）", idf_path, len(idf))

    hasher = SimHasher(idf, default_weight)
    index = SegmentedIndex()
    for fid, tokens in items:
        fp, _ = hasher.compute(sorted(tokens))
        index.add(fid, fp)
    index.metadata = {
        "kind": "feature_simhash",
        "version": 1,
        "db_signature": db_mapping_signature(db_path),
    }
    index.save(index_path)
    logger.info("SimHash 索引写入 {}（{} 个指纹）", index_path, len(index))

    return {"functions": len(items), "tokens": len(idf), "index_path": str(index_path), "idf_path": str(idf_path)}


def load_hasher(idf_path: str | Path = DEFAULT_IDF) -> SimHasher:
    data = json.loads(Path(idf_path).read_text(encoding="utf-8"))
    return SimHasher(data["idf"], data.get("default_weight", 1.0))


class SimHashQuery:
    """加载 idf + 索引，提供 query(tokens) -> set[func_id]。"""

    def __init__(self, idf_path: str | Path = DEFAULT_IDF, index_path: str | Path = DEFAULT_INDEX, *,
                 relax: bool = True, db_path: str | Path | None = None,
                 db_signature: dict | None = None):
        self.hasher = load_hasher(idf_path)
        self.index = SegmentedIndex.load(index_path)
        if db_path is not None:
            expected = self.index.metadata.get("db_signature")
            current = db_signature if db_signature is not None else db_mapping_signature(db_path)
            if not expected or expected != current:
                raise ValueError("特征 SimHash 索引与 functions.db 不同代，请重建历史库")
        self.relax = relax

    def query(self, tokens: list[str]) -> set[int]:
        fp, bit_abs = self.hasher.compute(tokens)
        return self.index.query(fp, bit_abs, relax=self.relax)
