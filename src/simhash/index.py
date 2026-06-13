"""分段哈希表：64 位切 4 段 × 16 位，每段一个 dict[seg_value, [func_id]]。

查询时 4 段分别查表取并集；比特松弛开启时，每段额外翻转「累加绝对值最低的 1 位」
再查一次（每段查 2 次，共 8 次查表），以召回有少量比特差异的近似指纹。
"""

from __future__ import annotations

import pickle
from collections import defaultdict
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

    # ---- 持久化 ----
    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "segments": [dict(seg) for seg in self.segments],
            "fingerprints": self.fingerprints,
            "n_segments": N_SEGMENTS,
            "seg_bits": SEG_BITS,
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
        return idx
