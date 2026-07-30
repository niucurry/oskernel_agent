"""归一化结果落盘到 SQLite（data/db/functions.db）。

三张表：
  functions(id, repo_id, file_path, start_line, end_line, func_name,
            module_tag, lang, raw_code, normalized_code)
  unique_strings(repo_id, func_id, string_value)   -- func_id 外键指向 functions.id
  files(id, repo_id, file_path, lang, line_count, func_count, norm_hash, raw_hash)
            -- L0 文件指纹层：整文件规范化哈希，供 fastpath 检测整文件复制

按 repo_id 幂等写入：重跑同一仓库会先删除其旧记录再插入。
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from pathlib import Path

from src.models import FunctionRecord

DEFAULT_DB = "data/db/functions.db"


def normalized_code_hash(code: str) -> str:
    """稳定代码指纹；用于不受 ANN top-k 限制的完全归一化召回通道。"""
    return hashlib.sha256(code.encode("utf-8", "replace")).hexdigest()

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
    normalized_hash TEXT NOT NULL DEFAULT '',
    feature_tokens  TEXT NOT NULL DEFAULT '[]'   -- JSON 列表，供 Layer1 SimHash
);
CREATE INDEX IF NOT EXISTS idx_functions_repo ON functions(repo_id);
CREATE INDEX IF NOT EXISTS idx_functions_module ON functions(module_tag);
CREATE INDEX IF NOT EXISTS idx_functions_name_lang_repo
    ON functions(func_name, lang, repo_id);
CREATE INDEX IF NOT EXISTS idx_functions_repo_file_lang_line
    ON functions(repo_id, file_path, lang, start_line);

CREATE TABLE IF NOT EXISTS unique_strings (
    repo_id      TEXT NOT NULL,
    func_id      INTEGER NOT NULL,
    string_value TEXT NOT NULL,
    FOREIGN KEY (func_id) REFERENCES functions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_strings_repo ON unique_strings(repo_id);
CREATE INDEX IF NOT EXISTS idx_strings_func ON unique_strings(func_id);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id     TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    lang        TEXT NOT NULL,
    line_count  INTEGER NOT NULL,
    func_count  INTEGER NOT NULL,
    norm_hash   TEXT NOT NULL,   -- 去注释+折叠空白+去空行后 sha1（消化格式差异）
    raw_hash    TEXT NOT NULL    -- 原文 sha1（逐字节相同判定）
);
CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo_id);
CREATE INDEX IF NOT EXISTS idx_files_norm_hash ON files(norm_hash);
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
        """为旧库补召回字段与索引（幂等）。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(functions)")}
        if "feature_tokens" not in cols:
            self.conn.execute("ALTER TABLE functions ADD COLUMN feature_tokens TEXT NOT NULL DEFAULT '[]'")
        if "normalized_hash" not in cols:
            self.conn.execute("ALTER TABLE functions ADD COLUMN normalized_hash TEXT NOT NULL DEFAULT ''")
        while True:
            missing = self.conn.execute(
                "SELECT id, normalized_code FROM functions WHERE normalized_hash='' LIMIT 1000"
            ).fetchall()
            if not missing:
                break
            self.conn.executemany(
                "UPDATE functions SET normalized_hash=? WHERE id=?",
                [(normalized_code_hash(code), func_id) for func_id, code in missing],
            )
            self.conn.commit()
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_functions_norm_hash ON functions(normalized_hash)"
        )
        # 召回阶段会为每个目标函数执行同名查找，并在命中候选的文件内扩展身份邻域。
        # 缺少以下复合索引时，SQLite 会按仓库或全表重复扫描，耗时随历史库线性恶化。
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_functions_name_lang_repo "
            "ON functions(func_name, lang, repo_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_functions_repo_file_lang_line "
            "ON functions(repo_id, file_path, lang, start_line)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_functions_norm_hash_lang_repo "
            "ON functions(normalized_hash, lang, repo_id)"
        )
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
        self.conn.execute("DELETE FROM files WHERE repo_id=?", (repo_id,))

    def add_function(self, rec: FunctionRecord, strings: list[str], feature_tokens: list[str] | None = None) -> int:
        """插入一条函数记录及其字符串/特征 token，返回新行 id。"""
        cur = self.conn.execute(
            """INSERT INTO functions
               (repo_id, file_path, start_line, end_line, func_name, module_tag, lang, raw_code,
                normalized_code, normalized_hash, feature_tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
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
                normalized_code_hash(rec.normalized_code),
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

    def add_file(self, repo_id: str, file_path: str, lang: str, line_count: int,
                 func_count: int, norm_hash: str, raw_hash: str) -> None:
        """插入一条文件指纹记录（L0 文件层）。"""
        self.conn.execute(
            """INSERT INTO files
               (repo_id, file_path, lang, line_count, func_count, norm_hash, raw_hash)
               VALUES (?,?,?,?,?,?,?)""",
            (repo_id, file_path, lang, line_count, func_count, norm_hash, raw_hash),
        )

    def write_repo(
        self,
        repo_id: str,
        records: list[tuple[FunctionRecord, list[str], list[str]]],
        file_records: list[dict] | None = None,
    ) -> int:
        """替换式写入一个仓库的全部函数记录（rec, strings, feature_tokens），返回写入条数。

        file_records 给定时同步写入 files 表（L0 文件指纹）：每条
        {file_path, lang, line_count, func_count, norm_hash, raw_hash}。
        """
        self.clear_repo(repo_id)
        for rec, strings, *rest in records:
            feature_tokens = rest[0] if rest else []
            self.add_function(rec, strings, feature_tokens)
        for fr in file_records or []:
            self.add_file(repo_id, fr["file_path"], fr["lang"], fr["line_count"],
                          fr["func_count"], fr["norm_hash"], fr["raw_hash"])
        self.conn.commit()
        return len(records)

    def find_files_by_norm_hash(self, norm_hash: str, *, exclude_repo_id: str | None = None) -> list[dict]:
        """按规范化哈希查历史文件（用于 fastpath 整文件复制检测）。"""
        self.conn.row_factory = sqlite3.Row
        rows = self.conn.execute(
            "SELECT repo_id, file_path, lang, line_count, func_count, norm_hash, raw_hash "
            "FROM files WHERE norm_hash=?",
            (norm_hash,),
        ).fetchall()
        return [dict(r) for r in rows if r["repo_id"] != exclude_repo_id]

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
