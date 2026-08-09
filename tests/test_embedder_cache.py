from __future__ import annotations

import numpy as np

from oskernel_agent.comparison.embed.embedder import BaseEmbedder, CachingEmbedder


class _CountingEmbedder(BaseEmbedder):
    dim = 2

    def __init__(self):
        self.calls: list[list[str]] = []

    def encode_batch(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        return np.asarray([[len(text), len(text) + 1] for text in texts], dtype=np.float32)


def test_caching_embedder_deduplicates_within_and_across_stages():
    backend = _CountingEmbedder()
    cached = CachingEmbedder(backend, max_items=8)

    first = cached.encode_batch(["alpha", "beta", "alpha"])
    second = cached.encode_batch(["beta", "gamma"])

    assert backend.calls == [["alpha", "beta"], ["gamma"]]
    np.testing.assert_array_equal(first[0], first[2])
    np.testing.assert_array_equal(second[0], first[1])


def test_caching_embedder_lru_is_bounded_without_changing_results():
    backend = _CountingEmbedder()
    cached = CachingEmbedder(backend, max_items=2)

    result = cached.encode_batch(["a", "bb", "ccc"])

    np.testing.assert_array_equal(
        result, np.asarray([[1, 2], [2, 3], [3, 4]], dtype=np.float32)
    )
    assert len(cached._cache) == 2
