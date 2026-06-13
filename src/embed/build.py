"""建库：遍历 functions.db 的函数，嵌入 normalized_code 并写入 Qdrant。支持增量。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from loguru import logger

from .embedder import BaseEmbedder
from .vector_store import VectorStore

_SELECT = (
    "SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag, normalized_code "
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
        "is_baseline": False,
    }


def build_index(
    db_path: str | Path,
    store: VectorStore,
    embedder: BaseEmbedder,
    *,
    recreate: bool = False,
    upsert_chunk: int = 256,
) -> dict:
    """把 functions.db 全部函数嵌入入库（增量：已入库的 func_id 跳过）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(_SELECT).fetchall()
    conn.close()

    store.ensure_collection(embedder.dim, recreate=recreate)
    existing = set() if recreate else store.existing_ids()
    todo = [r for r in rows if r["id"] not in existing]
    logger.info("functions.db 共 {} 个函数，待入库 {}（已存在 {}）", len(rows), len(todo), len(rows) - len(todo))

    added = 0
    for start in range(0, len(todo), upsert_chunk):
        chunk = todo[start : start + upsert_chunk]
        vecs = embedder.encode_batch([r["normalized_code"] for r in chunk])
        store.upsert([r["id"] for r in chunk], vecs, [_payload(r) for r in chunk])
        added += len(chunk)
        logger.info("已入库 {}/{}", added, len(todo))

    logger.info("建库完成：collection 共 {} 个点", store.count())
    return {"total": len(rows), "added": added, "collection_count": store.count()}
