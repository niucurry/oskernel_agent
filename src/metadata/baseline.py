"""通道 2：基线白底库扣除。

对每个嫌疑对，分别查双方函数与基线库（Qdrant payload is_baseline=true）的最高相似度；
若双方都与**同一个**基线函数相似度 > 阈值，则判定为 baseline_derived（公共/模板代码）。
"""

from __future__ import annotations

from typing import Protocol


# A historical candidate is allowed to survive query-level baseline propagation
# only when it contains a meaningful amount of code not explained by the best
# explicit baseline candidate.  Requiring both dimensions prevents a long
# function with a tiny ratio gain, or a short function with only one extra line,
# from being treated as independent history evidence.
MIN_INCREMENTAL_LINE_SIM_DELTA = 0.10
MIN_INCREMENTAL_MATCH_LINES = 3


def pair_line_evidence(suspect: dict) -> tuple[float, int]:
    """Return comparable (line similarity, matched lines) for a suspect pair.

    New artifacts provide ``line_similarity`` directly.  The fallback exists so
    report-boundary checks remain safe for older/external artifacts; it uses only
    line evidence and deliberately does not substitute vector similarity.
    """
    evidence = suspect.get("evidence") or {}
    matched = int(evidence.get("exact_match_lines") or 0) + int(
        evidence.get("renamed_match_lines") or 0
    )
    value = evidence.get("line_similarity")
    if value is not None:
        try:
            return max(0.0, min(1.0, float(value))), matched
        except (TypeError, ValueError):
            pass

    if matched:
        query_code = ((suspect.get("query_func") or {}).get("raw_code") or "")
        candidate_code = ((suspect.get("candidate_func") or {}).get("raw_code") or "")
        query_lines = sum(1 for line in query_code.splitlines() if line.strip())
        candidate_lines = sum(1 for line in candidate_code.splitlines() if line.strip())
        denominator = max(query_lines, candidate_lines, 1)
        return min(1.0, matched / denominator), matched
    return 0.0, 0


def has_incremental_history_evidence(
    candidate: dict,
    explicit_baselines: list[dict],
    *,
    min_similarity_delta: float = MIN_INCREMENTAL_LINE_SIM_DELTA,
    min_extra_lines: int = MIN_INCREMENTAL_MATCH_LINES,
) -> bool:
    """Whether history evidence materially exceeds every explicit baseline hit.

    This is intentionally repository-agnostic: the decision is based on actual
    matched coverage, not repository, path, language, or function-name rules.
    """
    if not explicit_baselines:
        return False
    candidate_similarity, candidate_lines = pair_line_evidence(candidate)
    baseline_evidence = [pair_line_evidence(item) for item in explicit_baselines]
    strongest_similarity = max(item[0] for item in baseline_evidence)
    most_lines = max(item[1] for item in baseline_evidence)
    return (
        candidate_similarity >= strongest_similarity + min_similarity_delta
        and candidate_lines >= most_lines + min_extra_lines
    )


class BaselineMatcher(Protocol):
    def match(self, normalized_code: str) -> tuple[int | None, float]:
        """返回 (最相似基线函数 id, 相似度)；无基线命中返回 (None, 0.0)。"""
        ...


def is_baseline_derived(
    q_match: tuple[int | None, float],
    c_match: tuple[int | None, float],
    *,
    threshold: float = 0.85,
    bilateral_threshold: float = 0.70,
) -> tuple[bool, str]:
    """判定是否上游基线衍生（公共/模板代码，不计借鉴）。返回 (是否, 判据)。

    两条命中路径（任一即可，系统性覆盖 vendored 上游，不靠目录名枚举）：
      - 双侧同基线（强信号）：query 与 candidate 都与**同一**基线函数相似 > bilateral_threshold → 两队都源自同一上游。
        信号远强于单侧（两侧独立收敛到同一基线 id），故门槛低于单侧。覆盖「4 队共同改造 rcore-v3
        原始函数、互相 1.0 但对原始版 sim<0.85」的因果倒置场景——只要双方仍与同一基线函数
        相似 > bilateral_threshold 即判共同衍生。
      - 单侧 query 命中基线（vendored 上游）：query 与某基线函数相似 > threshold → 新作品该函数本就是
        上游代码（vendored 或紧随上游），无论 candidate 是另一队的副本还是别的。这条覆盖「队伍把
        ArceOS vendored 到任意目录名」的情形——只要函数代码与上游 ArceOS 近似即判基线，不靠路径名。
    """
    qid, qsim = q_match
    cid, csim = c_match
    if qid is not None and cid is not None and qid == cid and qsim > bilateral_threshold and csim > bilateral_threshold:
        return True, "双侧均与同一基线函数相似"
    if qid is not None and qsim > threshold:
        return True, "新作品函数与上游基线函数相似（vendored/紧随上游）"
    return False, ""


class VectorBaselineMatcher:
    """用 Embedder + 内存矩阵乘（仅 is_baseline=true 子集）查最相似基线函数。

    基线子集小（数千），构造时一次性把全部基线向量拉到内存并 L2 归一化；之后 query 批量
    编码 + 一次矩阵乘即得对全部基线的余弦相似度——取每行 argmax 即最相似基线。彻底甩掉
    Qdrant local 模式对 20w+ 点逐次带过滤搜索的串行瓶颈（原 9600 次搜索 → 1 次矩阵乘）。
    """

    def __init__(self, embedder, store):
        self.embedder = embedder
        self.store = store
        import numpy as np
        vecs, ids = store.fetch_baseline_vectors()
        self._ids = ids
        if vecs.size:
            norm = np.linalg.norm(vecs, axis=1, keepdims=True)
            norm[norm == 0] = 1.0
            self._base = (vecs / norm).astype(np.float32)   # [N, dim] 已归一化
        else:
            self._base = vecs

    def match(self, normalized_code: str) -> tuple[int | None, float]:
        return self.match_batch([normalized_code])[0]

    def match_batch(self, codes: list[str]) -> list[tuple[int | None, float]]:
        """批量编码 query + 一次矩阵乘算对全部基线的余弦相似度，取每行最相似基线。
        返回与 codes 等长的 (baseline_id|None, sim) 列表。"""
        import numpy as np
        if not codes or self._base.size == 0:
            return [(None, 0.0)] * len(codes)
        q = np.asarray(self.embedder.encode_batch([c or " " for c in codes]), dtype=np.float32)
        qn = np.linalg.norm(q, axis=1, keepdims=True)
        qn[qn == 0] = 1.0
        q = q / qn
        sims = q @ self._base.T                       # [Q, N] 余弦相似度
        best = sims.argmax(axis=1)
        return [(self._ids[j], float(sims[i, j])) for i, j in enumerate(best)]
