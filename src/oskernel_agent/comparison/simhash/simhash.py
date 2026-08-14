"""64 位加权 SimHash 生成与汉明距离。"""

from __future__ import annotations

import xxhash

BITS = 64


def token_hash(token: str) -> int:
    """token 的 64 位哈希。"""
    # xxhash 4.x no longer implicitly encodes str inputs. Keep the hash stable
    # across supported xxhash versions by always passing UTF-8 bytes.
    return xxhash.xxh64_intdigest(token.encode("utf-8"))


def hamming(a: int, b: int) -> int:
    """两个 64 位指纹的汉明距离。"""
    return ((a ^ b) & ((1 << BITS) - 1)).bit_count()


class SimHasher:
    """按 IDF 权重生成 64 位 SimHash 指纹。

    weights: token -> 权重；default_weight 用于库中未见过的 token。
    """

    def __init__(self, weights: dict[str, float], default_weight: float = 1.0):
        self.weights = weights
        self.default_weight = default_weight

    def compute(self, tokens: list[str]) -> tuple[int, list[int]]:
        """返回 (fingerprint, bit_abs)。

        bit_abs[i] 是第 i 位累加浮点值的绝对值，量化为 uint8（0-255），
        供分段查询时做比特松弛（翻转累加绝对值最小、即最“不确定”的位）。
        """
        acc = [0.0] * BITS
        for t in set(tokens):  # 特征按集合计
            w = self.weights.get(t, self.default_weight)
            if w <= 0:
                continue
            h = token_hash(t)
            for i in range(BITS):
                if (h >> i) & 1:
                    acc[i] += w
                else:
                    acc[i] -= w

        fingerprint = 0
        bit_abs = [0] * BITS
        for i in range(BITS):
            if acc[i] > 0:
                fingerprint |= 1 << i
            a = abs(acc[i])
            bit_abs[i] = 255 if a >= 255 else int(round(a))
        return fingerprint, bit_abs
