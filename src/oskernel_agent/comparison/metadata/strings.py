"""通道 1：独特字符串信号。

基于 functions.db 的 unique_strings 表建反向索引 string_value -> [(repo_id, func_id)]，
过滤出现在 > 阈值个不同仓库的通用字符串。提供「新作品字符串 → 命中历史函数」查询。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from oskernel_agent.comparison.normalize.store import DEFAULT_DB


def build_reverse_index(db_path: str | Path = DEFAULT_DB, *, generic_threshold: int = 5) -> dict[str, list[tuple[str, int]]]:
    """string_value -> [(repo_id, func_id), ...]；出现在 > generic_threshold 个仓库的字符串剔除。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT string_value, repo_id, func_id FROM unique_strings").fetchall()
    conn.close()

    postings: dict[str, list[tuple[str, int]]] = defaultdict(list)
    repos: dict[str, set[str]] = defaultdict(set)
    for s, repo_id, func_id in rows:
        postings[s].append((repo_id, func_id))
        repos[s].add(repo_id)
    return {s: lst for s, lst in postings.items() if len(repos[s]) <= generic_threshold}


def string_hits_for_func(
    strings: list[str],
    index: dict[str, list[tuple[str, int]]],
    *,
    exclude_repo_id: str | None = None,
) -> dict[int, tuple[str, int]]:
    """给定一个函数的字符串列表，返回 {hist_func_id: (hist_repo_id, 命中字符串数)}。"""
    count: dict[int, int] = defaultdict(int)
    repo_of: dict[int, str] = {}
    for s in set(strings):
        for repo_id, func_id in index.get(s, ()):
            if exclude_repo_id is not None and repo_id == exclude_repo_id:
                continue
            count[func_id] += 1
            repo_of[func_id] = repo_id
    return {fid: (repo_of[fid], c) for fid, c in count.items()}


def fetch_function(db_path: str | Path, func_id: int) -> dict | None:
    """按 func_id 取历史函数完整信息（用于构造候选 FunctionRecord）。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT repo_id, file_path, start_line, end_line, func_name, module_tag, lang, raw_code, normalized_code "
        "FROM functions WHERE id=?",
        (func_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None
