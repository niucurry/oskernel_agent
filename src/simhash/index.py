"""分段哈希表：64 位切 4 段 × 16 位，每段一个 dict[seg_value, [func_id]]。

查询时 4 段分别查表取并集；比特松弛开启时，每段额外翻转「累加绝对值最低的 1 位」
再查一次（每段查 2 次，共 8 次查表），以召回有少量比特差异的近似指纹。
"""

from __future__ import annotations

import pickle
from collections import defaultdict
from itertools import combinations
from pathlib import Path

N_SEGMENTS = 4
SEG_BITS = 16
SEG_MASK = (1 << SEG_BITS) - 1


def seg_value(fingerprint: int, seg: int) -> int:
    return (fingerprint >> (seg * SEG_BITS)) & SEG_MASK


def _relaxed_seg_value(fingerprint: int, bit_abs: list[int], seg: int) -> int:
    """翻转该段内累加绝对值最低的一位后的段值。"""
    base = seg * SEG_BITS
    weakest = min(range(base, base + SEG_BITS), key=lambda i: bit_abs[i])
    flipped = fingerprint ^ (1 << weakest)
    return seg_value(flipped, seg)


class SegmentedIndex:
    def __init__(self):
        self.segments: list[dict[int, list[int]]] = [defaultdict(list) for _ in range(N_SEGMENTS)]
        self.fingerprints: dict[int, int] = {}
        self.metadata: dict = {}

    def add(self, func_id: int, fingerprint: int) -> None:
        self.fingerprints[func_id] = fingerprint
        for s in range(N_SEGMENTS):
            self.segments[s][seg_value(fingerprint, s)].append(func_id)

    def __len__(self) -> int:
        return len(self.fingerprints)

    def query(self, fingerprint: int, bit_abs: list[int] | None = None, *, relax: bool = True) -> set[int]:
        """4 段查表取并集；relax 时每段额外查一次松弛段值。"""
        out: set[int] = set()
        for s in range(N_SEGMENTS):
            out.update(self.segments[s].get(seg_value(fingerprint, s), ()))
            if relax and bit_abs is not None:
                out.update(self.segments[s].get(_relaxed_seg_value(fingerprint, bit_abs, s), ()))
        return out

    def query_multiprobe(self, fingerprint: int, *, bits_per_segment: int = 3) -> set[int]:
        """枚举每个 16-bit 分段内至多 N 位翻转后取并集。

        ``bits_per_segment=3`` 时，对全局汉明距离 <=15 的历史指纹有确定性召回保证：
        若四段每段都相差 >=4 位，总距离至少为 16，反之必有一段能被本查询命中。
        """
        if not 0 <= bits_per_segment <= 3:
            raise ValueError("bits_per_segment 仅支持 0..3，避免组合爆炸")
        masks = [0]
        for n in range(1, bits_per_segment + 1):
            masks.extend(sum(1 << bit for bit in combo)
                         for combo in combinations(range(SEG_BITS), n))
        out: set[int] = set()
        for seg in range(N_SEGMENTS):
            base = seg_value(fingerprint, seg)
            table = self.segments[seg]
            for mask in masks:
                out.update(table.get(base ^ mask, ()))
        return out

    # ---- 持久化 ----
    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "segments": [dict(seg) for seg in self.segments],
            "fingerprints": self.fingerprints,
            "n_segments": N_SEGMENTS,
            "seg_bits": SEG_BITS,
            "metadata": self.metadata,
        }
        with open(path, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def load(cls, path: str | Path) -> "SegmentedIndex":
        with open(path, "rb") as f:
            payload = pickle.load(f)
        idx = cls()
        idx.segments = [defaultdict(list, seg) for seg in payload["segments"]]
        idx.fingerprints = payload["fingerprints"]
        idx.metadata = payload.get("metadata", {})
        return idx
