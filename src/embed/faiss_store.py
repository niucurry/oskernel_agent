"""faiss HNSW 向量检索（query 路径专用，替代 qdrant-local SQLite 暴力扫描）。

构建：从 qdrant-local 一次性读出向量，建 HNSW 索引保存到磁盘；
查询：亚毫秒级 ANN，不需要 docker。
payload（repo_id/file_path 等）仍从 functions.db 读取（与 qdrant 路径相同）。

典型性能（21.5万向量，dim=256，HNSW M=32 efSearch=64）：
  - index 构建：~11s（一次性）
  - 单次 query：~0.02ms（比 qdrant-local 快约 10000×）
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import faiss
import numpy as np

from loguru import logger

from src.models import is_baseline_repo
from src.buildlib.coverage import db_mapping_signature

from .settings import EmbeddingSettings
from .vector_store import VectorStore

DEFAULT_INDEX = "data/db/faiss_hnsw.index"
DEFAULT_IDS   = "data/db/faiss_ids.npy"
_HNSW_M          = 32
_HNSW_EF_BUILD   = 200
_HNSW_EF_SEARCH  = 64


def _meta_path(ids_path: str | Path) -> Path:
    return Path(f"{ids_path}.meta.json")


def build_faiss_index(
    store: VectorStore,
    index_path: str | Path = DEFAULT_INDEX,
    ids_path: str | Path = DEFAULT_IDS,
    db_path: str | Path = "data/db/functions.db",
) -> tuple["faiss.Index", np.ndarray]:
    """从 VectorStore 读所有向量，建 HNSW 索引并保存。返回 (index, ids_array)。"""
    logger.info("[faiss] 从 qdrant 读取向量…（首次构建，约 30s）")
    offset = None
    ids: list[int] = []
    vecs: list[list[float]] = []
    while True:
        pts, offset = store.client.scroll(
            store.collection, limit=10000,
            with_payload=False, with_vectors=True, offset=offset,
        )
        for p in pts:
            ids.append(int(p.id))
            vecs.append(p.vector)
        if offset is None:
            break

    arr = np.array(vecs, dtype="float32")
    ids_arr = np.array(ids, dtype="int64")
    faiss.normalize_L2(arr)
    n, d = arr.shape
    logger.info("[faiss] 构建 HNSW（{} 向量，dim={}，M={})…", n, d, _HNSW_M)
    idx = faiss.IndexHNSWFlat(d, _HNSW_M, faiss.METRIC_INNER_PRODUCT)
    idx.hnsw.efConstruction = _HNSW_EF_BUILD
    idx.add(arr)
    faiss.write_index(idx, str(index_path))
    np.save(str(ids_path), ids_arr)
    meta = db_mapping_signature(db_path)
    meta["index_vectors"] = int(idx.ntotal)
    meta["ids_count"] = len(ids_arr)
    _meta_path(ids_path).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[faiss] 索引保存 → {} / {}", index_path, ids_path)
    return idx, ids_arr


def load_faiss_index(
    index_path: str | Path = DEFAULT_INDEX,
    ids_path: str | Path = DEFAULT_IDS,
    db_path: str | Path | None = None,
    db_signature: dict | None = None,
) -> tuple["faiss.Index", np.ndarray] | None:
    """加载已保存的 HNSW 索引；文件不存在返回 None。"""
    ip, isp = Path(index_path), Path(ids_path)
    if not ip.exists() or not isp.exists():
        return None
    if db_path is not None:
        mp = _meta_path(ids_path)
        if not mp.exists():
            logger.warning("[faiss] 缺少数据库代际签名 {}，拒绝使用可能过期的索引", mp)
            return None
        try:
            recorded = json.loads(mp.read_text(encoding="utf-8"))
            current = db_signature if db_signature is not None else db_mapping_signature(db_path)
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            logger.warning("[faiss] 索引一致性核验失败：{}", exc)
            return None
        if any(recorded.get(k) != current.get(k) for k in current):
            logger.warning("[faiss] functions.db 已变化，拒绝使用旧索引；请重建历史库")
            return None
    idx = faiss.read_index(str(ip))
    idx.hnsw.efSearch = _HNSW_EF_SEARCH
    ids_arr = np.load(str(isp))
    if idx.ntotal != len(ids_arr):
        logger.warning("[faiss] 索引向量数 {} 与 ID 数 {} 不一致，拒绝加载", idx.ntotal, len(ids_arr))
        return None
    return idx, ids_arr


class FaissVectorStore:
    """faiss HNSW 检索封装，接口子集与 VectorStore.search 兼容。

    只实现 query 路径所需的 search()；建库路径仍走原 VectorStore。
    payload 从 functions.db 读取。
    """

    supports_language_filter = True

    def __init__(self, index: "faiss.Index", ids_arr: np.ndarray,
                 db_path: str | Path = "data/db/functions.db") -> None:
        self._idx = index
        self._ids = ids_arr          # faiss 内部行号 → func_id
        self._id_to_row: dict[int, int] = {int(fid): row for row, fid in enumerate(ids_arr)}
        self._db_path = str(db_path)
        self._payload_cache: dict[int, dict] = {}

    def _fetch_payload(self, func_ids: list[int]) -> dict[int, dict]:
        missing = [fid for fid in func_ids if fid not in self._payload_cache]
        if missing:
            placeholders = ",".join("?" * len(missing))
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag, lang "
                f"FROM functions WHERE id IN ({placeholders})",
                missing,
            ).fetchall()
            conn.close()
            for r in rows:
                self._payload_cache[r["id"]] = dict(r)
        return {fid: self._payload_cache[fid] for fid in func_ids if fid in self._payload_cache}

    def search(
        self,
        vector: np.ndarray,
        top_k: int,
        *,
        exclude_repo_id: str | None = None,
        module_tag: str | None = None,
        candidate_ids: list[int] | None = None,
        baseline_only: bool = False,
        lang: str | None = None,
    ) -> list[dict]:
        """ANN 检索 top_k；lang 给定时只返回同一编程语言的函数。"""
        q = vector.reshape(1, -1).astype("float32")
        faiss.normalize_L2(q)

        def eligible(payload: dict) -> bool:
            if exclude_repo_id and payload["repo_id"] == exclude_repo_id:
                return False
            if module_tag and payload["module_tag"] != module_tag:
                return False
            if lang and (payload.get("lang") or "").lower() != lang.lower():
                return False
            if baseline_only and not is_baseline_repo(payload["repo_id"]):
                return False
            return True

        if candidate_ids is not None:
            # candidate_ids 限定时：用 IndexFlatIP 在子集上暴力搜（子集通常很小）
            if not candidate_ids:
                return []
            row_func_ids = [int(fid) for fid in candidate_ids if fid in self._id_to_row]
            payloads = self._fetch_payload(row_func_ids)
            row_func_ids = [fid for fid in row_func_ids
                            if fid in payloads and eligible(payloads[fid])]
            rows = np.array([self._id_to_row[fid] for fid in row_func_ids], dtype="int64")
            if len(rows) == 0:
                return []
            sub_vecs = self._idx.reconstruct_batch(rows)  # type: ignore[attr-defined]
            flat = faiss.IndexFlatIP(q.shape[1])
            flat.add(sub_vecs)
            k = min(top_k, len(rows))
            D, I = flat.search(q, k)
            found = [
                (row_func_ids[int(i)], float(D[0][pos]))
                for pos, i in enumerate(I[0]) if i >= 0
            ]
        else:
            # 全局 HNSW 检索。过滤条件可能使首批结果不足 top_k，逐步扩大搜索窗，
            # 确保“同语言 top_k”不会被排名靠前的其他语言候选挤掉。
            search_k = min(max(top_k + 10, 64), self._idx.ntotal)
            found = []
            while search_k > 0:
                D, I = self._idx.search(q, search_k)
                pairs = [
                    (int(self._ids[i]), float(D[0][pos]))
                    for pos, i in enumerate(I[0]) if i >= 0
                ]
                payloads = self._fetch_payload([fid for fid, _ in pairs])
                found = [(fid, score) for fid, score in pairs
                         if fid in payloads and eligible(payloads[fid])]
                if len(found) >= top_k or search_k >= self._idx.ntotal:
                    break
                search_k = min(self._idx.ntotal, search_k * 2)

        payloads = self._fetch_payload([fid for fid, _ in found])
        results = []
        for fid, score in found:
            p = payloads.get(fid)
            if p is None or not eligible(p):
                continue
            results.append({
                "id": fid,
                "score": float(score),
                "payload": {
                    "repo_id": p["repo_id"],
                    "year": int(p["repo_id"].split("/", 1)[0]) if p["repo_id"].split("/", 1)[0].isdigit() else None,
                    "file_path": p["file_path"],
                    "start_line": p["start_line"],
                    "end_line": p["end_line"],
                    "func_name": p["func_name"],
                    "module_tag": p["module_tag"],
                    "lang": p["lang"],
                    "is_baseline": is_baseline_repo(p["repo_id"]),
                },
            })
            if len(results) >= top_k:
                break
        return results
