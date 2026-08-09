"""建库：遍历 functions.db 的函数，嵌入 normalized_code 并写入 Qdrant。

支持：
- 去重——多个函数若 normalized_code 完全相同（改名等价、跨仓库公共代码），只算一次向量；
- 崩溃安全——本地模式每 flush_every 个点 close+落盘，意外中断只丢增量；
- 增量续跑——recreate=False 时跳过已落盘的 func_id 继续。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
from loguru import logger

from oskernel_agent.comparison.models import is_baseline_repo

from .embedder import BaseEmbedder
from .vector_store import VectorStore

_SELECT = (
    "SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag, lang, normalized_code "
    "FROM functions"
)


def year_of(repo_id: str) -> int | None:
    head = repo_id.split("/", 1)[0]
    return int(head) if head.isdigit() else None


def _payload(row: sqlite3.Row) -> dict:
    return {
        "repo_id": row["repo_id"],
        "year": year_of(row["repo_id"]),
        "file_path": row["file_path"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "func_name": row["func_name"],
        "module_tag": row["module_tag"],
        "lang": row["lang"],
        "is_baseline": is_baseline_repo(row["repo_id"]),
    }


def build_index(
    db_path: str | Path,
    store: VectorStore,
    embedder: BaseEmbedder,
    *,
    recreate: bool = False,
    code_chunk: int = 256,
    min_lines: int = 0,
    flush_every: int = 2000,
) -> dict:
    """把 functions.db 函数嵌入入库（去重 + 崩溃安全 + 增量）。

    - min_lines>0 时只嵌入行数 >= 该值的函数（functions.db 仍保留全部，候选解析不受影响）；
    - 相同 normalized_code 只算一次向量，再分发给共享该代码的所有 func_id；
    - 每 flush_every 个点落盘一次（本地模式）；recreate=False 时跳过已落盘的点续跑。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = _SELECT
    if min_lines and min_lines > 0:
        sql += f" WHERE (end_line - start_line + 1) >= {int(min_lines)}"
    rows = conn.execute(sql).fetchall()
    conn.close()

    store.ensure_collection(embedder.dim, recreate=recreate)
    existing = set() if recreate else store.existing_ids()
    todo = [r for r in rows if r["id"] not in existing]

    # 按 normalized_code 去重分组：一个唯一代码 → 共享该向量的所有行
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in todo:
        groups[r["normalized_code"]].append(r)
    uniq = list(groups.items())
    logger.info(
        "命中 {} 个函数（min_lines={}），待入库 {}（已存在 {}）；去重后需计算向量 {} 个",
        len(rows), min_lines, len(todo), len(rows) - len(todo), len(uniq),
    )

    added = 0
    since_flush = 0
    for start in range(0, len(uniq), code_chunk):
        batch = uniq[start : start + code_chunk]
        vecs = embedder.encode_batch([code for code, _ in batch])
        ids: list[int] = []
        vlist: list[np.ndarray] = []
        payloads: list[dict] = []
        for (_code, grp), v in zip(batch, vecs):
            for r in grp:
                ids.append(r["id"])
                vlist.append(v)
                payloads.append(_payload(r))
        store.upsert(ids, np.asarray(vlist), payloads)
        added += len(ids)
        since_flush += len(ids)
        if since_flush >= flush_every:
            store.flush()
            since_flush = 0
            logger.info("已入库 {}/{}（已落盘检查点）", added, len(todo))
        else:
            logger.info("已入库 {}/{}", added, len(todo))

    store.flush()
    logger.info("建库完成：collection 共 {} 个点", store.count())
    return {"total": len(rows), "added": added, "unique_vectors": len(uniq), "collection_count": store.count()}
