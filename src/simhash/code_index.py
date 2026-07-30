"""归一化代码结构 SimHash：不依赖函数名、目录、ANN top-k 的高查全通道。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from loguru import logger

from src.buildlib.coverage import db_mapping_signature

from .index import SegmentedIndex
from .simhash import SimHasher, hamming

DEFAULT_CODE_INDEX = "data/db/code_simhash_index.pkl"
SHINGLE_SIZE = 4
MIN_TOKENS = 12
MAX_HAMMING = 15
PROBE_BITS = 3
INDEX_VERSION = 1


def code_shingles(normalized_code: str, size: int = SHINGLE_SIZE) -> list[str]:
    """把归一化 token 流切成相邻 shingle；表层改名已在 normalize 阶段消化。"""
    tokens = normalized_code.split()
    if len(tokens) < max(MIN_TOKENS, size):
        return []
    return ["\x1f".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)]


def build_code_index(db_path: str | Path, index_path: str | Path = DEFAULT_CODE_INDEX) -> dict:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("SELECT id, normalized_code FROM functions ORDER BY id")
    hasher = SimHasher({}, 1.0)
    index = SegmentedIndex()
    token_counts: dict[int, int] = {}
    indexed = 0
    while True:
        rows = cur.fetchmany(1000)
        if not rows:
            break
        for func_id, code in rows:
            tokens = code.split()
            shingles = code_shingles(code)
            if not shingles:
                continue
            fp, _ = hasher.compute(shingles)
            index.add(int(func_id), fp)
            token_counts[int(func_id)] = len(tokens)
            indexed += 1
    conn.close()
    index.metadata = {
        "kind": "normalized_code_simhash",
        "version": INDEX_VERSION,
        "shingle_size": SHINGLE_SIZE,
        "min_tokens": MIN_TOKENS,
        "max_hamming": MAX_HAMMING,
        "probe_bits": PROBE_BITS,
        "token_counts": token_counts,
        "db_signature": db_mapping_signature(db_path),
    }
    index.save(index_path)
    logger.info("结构 SimHash 索引写入 {}（{} 个函数）", index_path, indexed)
    return {"indexed": indexed, "index_path": str(index_path),
            "db_signature": index.metadata["db_signature"]}


class CodeSimHashQuery:
    def __init__(self, index_path: str | Path = DEFAULT_CODE_INDEX, *,
                 db_path: str | Path | None = None,
                 db_signature: dict | None = None,
                 max_hamming: int = MAX_HAMMING, probe_bits: int = PROBE_BITS):
        self.index = SegmentedIndex.load(index_path)
        meta = self.index.metadata
        if meta.get("kind") != "normalized_code_simhash" or meta.get("version") != INDEX_VERSION:
            raise ValueError(f"结构 SimHash 索引格式不兼容：{index_path}")
        if db_path is not None:
            current = db_signature if db_signature is not None else db_mapping_signature(db_path)
            if meta.get("db_signature") != current:
                raise ValueError("结构 SimHash 索引与 functions.db 不同代，请重建历史库")
        self.token_counts: dict[int, int] = meta.get("token_counts", {})
        self.max_hamming = max_hamming
        self.probe_bits = probe_bits
        self.hasher = SimHasher({}, 1.0)

    def query(self, normalized_code: str) -> dict[int, int]:
        tokens = normalized_code.split()
        shingles = code_shingles(normalized_code)
        if not shingles:
            return {}
        fp, _ = self.hasher.compute(shingles)
        pool = self.index.query_multiprobe(fp, bits_per_segment=self.probe_bits)
        qn = len(tokens)
        out: dict[int, int] = {}
        for func_id in pool:
            cn = self.token_counts.get(func_id, 0)
            # exact matcher 以较长函数为分母；长度相差超过约 2 倍不可能达到 0.5。
            if not cn or min(qn, cn) / max(qn, cn) < 0.45:
                continue
            dist = hamming(fp, self.index.fingerprints[func_id])
            if dist <= self.max_hamming:
                out[func_id] = dist
        return out
