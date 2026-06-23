"""Qdrant 向量库封装。

- collection 名取自 settings（默认 os_functions），距离 cosine，维度按模型实际输出；
- payload：repo_id, year, file_path, start_line, end_line, func_name, module_tag, is_baseline；
- 点 id 用 functions.db 的 func_id（保证增量可去重）；
- 支持 http 服务（docker）或本地路径 / 内存模式（无 docker 时测试用）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from qdrant_client import QdrantClient, models


class VectorStore:
    def __init__(self, collection: str, *, url: str | None = None, path: str | None = None, in_memory: bool = False):
        self.collection = collection
        self._path = path
        self._in_memory = in_memory
        self._url = url
        if in_memory:
            self.client = QdrantClient(location=":memory:")
        elif path:
            Path(path).mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=path)
        else:
            self.client = QdrantClient(url=url or "http://localhost:6333")

    def flush(self) -> None:
        """本地路径模式下，close+重开以强制落盘（qdrant 本地模式默认内存缓冲，仅 close 时写盘）。

        建库长任务的崩溃安全检查点：意外中断只丢上次 flush 之后的增量，重跑（recreate=False）
        会跳过已落盘的点继续。http/内存模式无需此操作。
        """
        if not self._path:
            return
        self.client.close()
        self.client = QdrantClient(path=self._path)

    # ---- collection 生命周期 ----
    def ensure_collection(self, dim: int, *, recreate: bool = False) -> None:
        exists = self.client.collection_exists(self.collection)
        if exists and recreate:
            self.client.delete_collection(self.collection)
            exists = False
        if not exists:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
            )

    def existing_ids(self) -> set[int]:
        """返回已入库的点 id 集合（用于增量跳过）。"""
        ids: set[int] = set()
        if not self.client.collection_exists(self.collection):
            return ids
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection,
                limit=10000,
                with_payload=False,
                with_vectors=False,
                offset=offset,
            )
            ids.update(int(p.id) for p in points)
            if offset is None:
                break
        return ids

    def count(self) -> int:
        if not self.client.collection_exists(self.collection):
            return 0
        return self.client.count(self.collection, exact=True).count

    # ---- 写入 ----
    def upsert(self, ids: list[int], vectors: np.ndarray, payloads: list[dict]) -> None:
        points = [
            models.PointStruct(id=int(i), vector=v.tolist(), payload=p)
            for i, v, p in zip(ids, vectors, payloads)
        ]
        self.client.upsert(collection_name=self.collection, points=points)

    # ---- 检索 ----
    def search(
        self,
        vector: np.ndarray,
        top_k: int,
        *,
        exclude_repo_id: str | None = None,
        module_tag: str | None = None,
        candidate_ids: list[int] | None = None,
        baseline_only: bool = False,
    ) -> list[dict]:
        """检索 top_k；可排除某 repo_id、限定 module_tag、限定候选 id 集合（SimHash 粗筛）、
        或仅检索基线库（is_baseline=true，供 metadata 扣除）。

        返回含 score+payload 的 dict 列表。candidate_ids 为空列表时不会命中任何点。
        """
        must_not = []
        must = []
        if exclude_repo_id is not None:
            must_not.append(
                models.FieldCondition(key="repo_id", match=models.MatchValue(value=exclude_repo_id))
            )
        if module_tag is not None:
            must.append(
                models.FieldCondition(key="module_tag", match=models.MatchValue(value=module_tag))
            )
        if baseline_only:
            must.append(models.FieldCondition(key="is_baseline", match=models.MatchValue(value=True)))
        if candidate_ids is not None:
            must.append(models.HasIdCondition(has_id=list(candidate_ids)))
        flt = models.Filter(must=must or None, must_not=must_not or None) if (must or must_not) else None
        hits = self.client.query_points(
            collection_name=self.collection,
            query=vector.tolist(),
            limit=top_k,
            query_filter=flt,
            with_payload=True,
        ).points
        return [{"id": int(h.id), "score": float(h.score), "payload": h.payload} for h in hits]
