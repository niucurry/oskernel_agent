"""归一化结果落盘到 SQLite（data/db/functions.db）。

两张表：
  functions(id, repo_id, file_path, start_line, end_line, func_name,
            module_tag, lang, raw_code, normalized_code)
  unique_strings(repo_id, func_id, string_value)   -- func_id 外键指向 functions.id

按 repo_id 幂等写入：重跑同一仓库会先删除其旧记录再插入。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.models import FunctionRecord

DEFAULT_DB = "data/db/functions.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS functions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id         TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    func_name       TEXT NOT NULL,
    module_tag      TEXT NOT NULL,
    lang            TEXT NOT NULL,
    raw_code        TEXT NOT NULL,
    normalized_code TEXT NOT NULL,
    feature_tokens  TEXT NOT NULL DEFAULT '[]'   -- JSON 列表，供 Layer1 SimHash
);
CREATE INDEX IF NOT EXISTS idx_functions_repo ON functions(repo_id);
CREATE INDEX IF NOT EXISTS idx_functions_module ON functions(module_tag);

CREATE TABLE IF NOT EXISTS unique_strings (
    repo_id      TEXT NOT NULL,
    func_id      INTEGER NOT NULL,
    string_value TEXT NOT NULL,
    FOREIGN KEY (func_id) REFERENCES functions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_strings_repo ON unique_strings(repo_id);
CREATE INDEX IF NOT EXISTS idx_strings_func ON unique_strings(func_id);
"""


class FunctionStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """为旧库补 feature_tokens 列（幂等）。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(functions)")}
        if "feature_tokens" not in cols:
            self.conn.execute("ALTER TABLE functions ADD COLUMN feature_tokens TEXT NOT NULL DEFAULT '[]'")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "FunctionStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def clear_repo(self, repo_id: str) -> None:
        """删除某仓库的旧记录（幂等重跑）。"""
        cur = self.conn.execute("SELECT id FROM functions WHERE repo_id=?", (repo_id,))
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            self.conn.executemany("DELETE FROM unique_strings WHERE func_id=?", [(i,) for i in ids])
        self.conn.execute("DELETE FROM unique_strings WHERE repo_id=?", (repo_id,))
        self.conn.execute("DELETE FROM functions WHERE repo_id=?", (repo_id,))

    def add_function(self, rec: FunctionRecord, strings: list[str], feature_tokens: list[str] | None = None) -> int:
        """插入一条函数记录及其字符串/特征 token，返回新行 id。"""
        cur = self.conn.execute(
            """INSERT INTO functions
               (repo_id, file_path, start_line, end_line, func_name, module_tag, lang, raw_code, normalized_code, feature_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.repo_id,
                rec.file_path,
                rec.start_line,
                rec.end_line,
                rec.func_name,
                rec.module_tag.value,
                rec.lang,
                rec.raw_code,
                rec.normalized_code,
                json.dumps(feature_tokens or [], ensure_ascii=False),
            ),
        )
        func_id = cur.lastrowid
        if strings:
            self.conn.executemany(
                "INSERT INTO unique_strings (repo_id, func_id, string_value) VALUES (?,?,?)",
                [(rec.repo_id, func_id, s) for s in strings],
            )
        return func_id

    def write_repo(self, repo_id: str, records: list[tuple[FunctionRecord, list[str], list[str]]]) -> int:
        """替换式写入一个仓库的全部函数记录（rec, strings, feature_tokens），返回写入条数。"""
        self.clear_repo(repo_id)
        for rec, strings, *rest in records:
            feature_tokens = rest[0] if rest else []
            self.add_function(rec, strings, feature_tokens)
        self.conn.commit()
        return len(records)

    def module_distribution(self, repo_id: str | None = None) -> dict[str, int]:
        """按 module_tag 统计函数数量（可限定仓库）。"""
        if repo_id is None:
            cur = self.conn.execute("SELECT module_tag, COUNT(*) FROM functions GROUP BY module_tag")
        else:
            cur = self.conn.execute(
                "SELECT module_tag, COUNT(*) FROM functions WHERE repo_id=? GROUP BY module_tag",
                (repo_id,),
            )
        return dict(cur.fetchall())
