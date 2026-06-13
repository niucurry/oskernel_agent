"""通道 2：基线白底库扣除。

对每个嫌疑对，分别查双方函数与基线库（Qdrant payload is_baseline=true）的最高相似度；
若双方都与**同一个**基线函数相似度 > 阈值，则判定为 baseline_derived（公共/模板代码）。
"""

from __future__ import annotations

from typing import Protocol


class BaselineMatcher(Protocol):
    def match(self, normalized_code: str) -> tuple[int | None, float]:
        """返回 (最相似基线函数 id, 相似度)；无基线命中返回 (None, 0.0)。"""
        ...


def is_baseline_derived(
    q_match: tuple[int | None, float],
    c_match: tuple[int | None, float],
    *,
    threshold: float = 0.85,
) -> bool:
    """双侧都命中同一基线函数且相似度均 > 阈值 才判定 baseline_derived。"""
    qid, qsim = q_match
    cid, csim = c_match
    return qid is not None and qid == cid and qsim > threshold and csim > threshold


class VectorBaselineMatcher:
    """用 Embedder + Qdrant（仅 is_baseline=true 子集）查最相似基线函数。"""

    def __init__(self, embedder, store):
        self.embedder = embedder
        self.store = store

    def match(self, normalized_code: str) -> tuple[int | None, float]:
        vec = self.embedder.encode_batch([normalized_code or " "])[0]
        hits = self.store.search(vec, 1, baseline_only=True)
        if not hits:
            return None, 0.0
        return hits[0]["id"], hits[0]["score"]
