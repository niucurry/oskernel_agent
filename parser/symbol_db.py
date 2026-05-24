"""
基于 SQLite 的符号索引与代码全文检索后端。

替代 Level2Index 的内存三层字典实现，把符号表和源码全文落到磁盘，
配合 FTS5 让 search_code 工具从 O(N) 行扫描变为毫秒级查询。

存储位置：data/cache/<repo_hash>.db
缓存键由仓库绝对路径 + 源文件 mtime + size 组成。当源文件未变时，
build_repo_map 直接复用已有数据库，跳过 ctags 全量扫描。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Iterable

SCHEMA_VERSION = "1"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS symbols (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,
    kind      TEXT,
    file      TEXT NOT NULL,
    line      INTEGER,
    signature TEXT,
    typeref   TEXT,
    scope     TEXT,
    level     TEXT,
    subsystem TEXT
);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file);
CREATE INDEX IF NOT EXISTS idx_sym_sub  ON symbols(subsystem);

CREATE TABLE IF NOT EXISTS file_meta (
    file  TEXT PRIMARY KEY,
    mtime REAL,
    size  INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
    file UNINDEXED,
    line UNINDEXED,
    content,
    tokenize = 'unicode61 remove_diacritics 1'
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def repo_cache_path(repo_path: str, cache_dir: str | Path) -> Path:
    """根据仓库绝对路径计算缓存数据库文件位置。"""
    h = hashlib.sha1(str(Path(repo_path).resolve()).encode()).hexdigest()[:16]
    base = Path(cache_dir)
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{h}.db"


def fingerprint_files(files: Iterable[Path]) -> str:
    """对一组文件的 mtime + size 求哈希，用于缓存命中判断。"""
    hasher = hashlib.sha1()
    for f in sorted(files, key=lambda p: str(p)):
        try:
            st = f.stat()
            hasher.update(f"{f}|{st.st_mtime_ns}|{st.st_size}\n".encode())
        except OSError:
            continue
    return hasher.hexdigest()


class SymbolDB:
    """SQLite 持久化符号索引 + FTS5 全文检索。

    提供 _by_name / _by_file / _by_subsystem 三个属性，返回 dict-like view，
    让 tool_dispatcher 中既有的 self.level2_index._by_xxx[key] 调用保持兼容。
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self.open()

    # 连接管理

    def open(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA_SQL)
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn or self.open()

    # 元数据

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # 写入

    def populate(
        self,
        tags: list[dict],
        primary_lang: str,
        file_to_subsystem: Mapping[str, str],
        source_files: Iterable[Path],
        repo_path: str,
        classify_fn,
    ) -> dict[str, int]:
        """全量写入符号表和 FTS5 表。返回 {total, level1, level2, discarded, fts_rows}。"""
        from parser.code_parser import classify_symbol  # 防循环

        c = self.conn
        c.execute("DELETE FROM symbols")
        c.execute("DELETE FROM file_meta")
        c.execute("DELETE FROM code_fts")

        sym_rows: list[tuple] = []
        l1 = l2 = drop = 0
        for tag in tags:
            level = (classify_fn or classify_symbol)(tag, primary_lang)
            if level == "discard":
                drop += 1
                continue
            if level == "level1":
                l1 += 1
            else:
                l2 += 1
            sub = file_to_subsystem.get(tag["rel_path"])
            sym_rows.append((
                tag["name"],
                tag.get("kind", ""),
                tag["rel_path"],
                int(tag.get("line", 0) or 0),
                tag.get("signature", ""),
                tag.get("typeref", ""),
                tag.get("scope", ""),
                level,
                sub,
            ))
        c.executemany(
            "INSERT INTO symbols(name, kind, file, line, signature, typeref, "
            "scope, level, subsystem) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sym_rows,
        )

        fts_rows = self._populate_fts(source_files, repo_path)

        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.set_meta("schema_version", SCHEMA_VERSION)
        self.set_meta("repo_path", str(Path(repo_path).resolve()))
        self.set_meta("indexed_at", now)
        self.set_meta("primary_lang", primary_lang)
        c.commit()
        return {
            "total":     len(tags),
            "level1":    l1,
            "level2":    l2,
            "discarded": drop,
            "fts_rows":  fts_rows,
        }

    def _populate_fts(self, source_files: Iterable[Path], repo_path: str) -> int:
        repo_root = Path(repo_path).resolve()
        c = self.conn
        meta_rows: list[tuple] = []
        fts_rows: list[tuple] = []
        count = 0
        for f in source_files:
            try:
                st = f.stat()
                if st.st_size > 1_000_000:
                    continue
                text = f.read_text(errors="replace")
            except OSError:
                continue
            try:
                rel = str(f.resolve().relative_to(repo_root))
            except ValueError:
                rel = str(f)
            meta_rows.append((rel, st.st_mtime, st.st_size))
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                fts_rows.append((rel, lineno, line))
                count += 1
        c.executemany(
            "INSERT OR REPLACE INTO file_meta(file, mtime, size) VALUES (?, ?, ?)",
            meta_rows,
        )
        c.executemany(
            "INSERT INTO code_fts(file, line, content) VALUES (?, ?, ?)", fts_rows
        )
        return count

    # 查询

    def lookup_symbol(self, name: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE name=? ORDER BY file, line", (name,)
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def list_file_symbols(self, file_path: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE file=? ORDER BY line", (file_path,)
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def search_symbols(self, pattern: str, limit: int = 20) -> list[dict]:
        like = f"%{pattern.lower()}%"
        rows = self.conn.execute(
            "SELECT * FROM symbols WHERE LOWER(name) LIKE ? LIMIT ?",
            (like, limit),
        ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def fts_search(
        self,
        query: str,
        file_glob: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """FTS5 全文搜索。query 是 FTS5 MATCH 语法（关键词、短语、布尔）。"""
        sql = "SELECT file, line, content FROM code_fts WHERE code_fts MATCH ?"
        params: list = [query]
        if file_glob:
            sql += " AND file GLOB ?"
            params.append(file_glob)
        sql += " LIMIT ?"
        params.append(limit)
        return [
            {"file": r["file"], "line": r["line"], "content": r["content"]}
            for r in self.conn.execute(sql, params).fetchall()
        ]

    def all_files(self) -> list[str]:
        rows = self.conn.execute("SELECT file FROM file_meta ORDER BY file").fetchall()
        return [r["file"] for r in rows]

    def stats(self) -> dict[str, int | str | None]:
        c = self.conn
        sym_count = c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        fts_count = c.execute("SELECT COUNT(*) FROM code_fts").fetchone()[0]
        file_count = c.execute("SELECT COUNT(*) FROM file_meta").fetchone()[0]
        return {
            "symbols":      sym_count,
            "fts_rows":     fts_count,
            "files":        file_count,
            "db_path":      str(self.db_path),
            "db_size":      self.db_path.stat().st_size if self.db_path.exists() else 0,
            "indexed_at":   self.get_meta("indexed_at"),
            "primary_lang": self.get_meta("primary_lang"),
        }

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> dict:
        return {
            "name":      row["name"],
            "kind":      row["kind"] or "",
            "file":      row["file"],
            "line":      row["line"] or 0,
            "signature": row["signature"] or "",
            "typeref":   row["typeref"] or "",
            "scope":     row["scope"] or "",
            "level":     row["level"] or "",
        }

    # dict-like view（兼容旧的 self.level2_index._by_name[k] 等用法）

    @property
    def _by_name(self) -> "_NameView":
        return _NameView(self)

    @property
    def _by_file(self) -> "_FileView":
        return _FileView(self)

    @property
    def _by_subsystem(self) -> "_SubsystemView":
        return _SubsystemView(self)


class _MappingView(Mapping):
    """SQL 支持的只读 dict 视图，按需查询而非全表加载。"""

    def __init__(self, db: SymbolDB):
        self._db = db

    def __iter__(self):
        for r in self._db.conn.execute(self._distinct_sql()):
            yield r[0]

    def __len__(self) -> int:
        return self._db.conn.execute(self._count_sql()).fetchone()[0]

    def __contains__(self, key) -> bool:
        row = self._db.conn.execute(self._exists_sql(), (key,)).fetchone()
        return row is not None

    def items(self):
        for key in self:
            yield key, self[key]

    # 子类实现

    def _distinct_sql(self) -> str:
        raise NotImplementedError

    def _count_sql(self) -> str:
        raise NotImplementedError

    def _exists_sql(self) -> str:
        raise NotImplementedError


class _NameView(_MappingView):

    def _distinct_sql(self) -> str:
        return "SELECT DISTINCT name FROM symbols"

    def _count_sql(self) -> str:
        return "SELECT COUNT(DISTINCT name) FROM symbols"

    def _exists_sql(self) -> str:
        return "SELECT 1 FROM symbols WHERE name=? LIMIT 1"

    def __getitem__(self, name: str) -> list[dict]:
        rows = self._db.conn.execute(
            "SELECT * FROM symbols WHERE name=? ORDER BY file, line", (name,)
        ).fetchall()
        if not rows:
            raise KeyError(name)
        return [SymbolDB._row_to_entry(r) for r in rows]

    def get(self, name: str, default=None):
        try:
            return self[name]
        except KeyError:
            return default


class _FileView(_MappingView):

    def _distinct_sql(self) -> str:
        return "SELECT DISTINCT file FROM symbols"

    def _count_sql(self) -> str:
        return "SELECT COUNT(DISTINCT file) FROM symbols"

    def _exists_sql(self) -> str:
        return "SELECT 1 FROM symbols WHERE file=? LIMIT 1"

    def __getitem__(self, file_path: str) -> list[dict]:
        rows = self._db.conn.execute(
            "SELECT * FROM symbols WHERE file=? ORDER BY line", (file_path,)
        ).fetchall()
        if not rows:
            raise KeyError(file_path)
        return [SymbolDB._row_to_entry(r) for r in rows]

    def get(self, file_path: str, default=None):
        try:
            return self[file_path]
        except KeyError:
            return default


class _SubsystemView(_MappingView):

    def _distinct_sql(self) -> str:
        return "SELECT DISTINCT subsystem FROM symbols WHERE subsystem IS NOT NULL"

    def _count_sql(self) -> str:
        return "SELECT COUNT(DISTINCT subsystem) FROM symbols WHERE subsystem IS NOT NULL"

    def _exists_sql(self) -> str:
        return "SELECT 1 FROM symbols WHERE subsystem=? LIMIT 1"

    def __getitem__(self, subsystem: str) -> list[dict]:
        rows = self._db.conn.execute(
            "SELECT * FROM symbols WHERE subsystem=? ORDER BY file, line",
            (subsystem,),
        ).fetchall()
        if not rows:
            raise KeyError(subsystem)
        return [SymbolDB._row_to_entry(r) for r in rows]

    def get(self, subsystem: str, default=None):
        try:
            return self[subsystem]
        except KeyError:
            return default


# 工具函数：枚举仓库内的源文件（供 populate 的 FTS 入库使用）

_SRC_EXTS_FOR_FTS = frozenset({
    ".c", ".h", ".cc", ".cpp", ".hpp",
    ".rs",
    ".S", ".s", ".asm",
    ".py", ".sh",
    ".md", ".txt", ".rst",
    ".toml", ".lds", ".ld",
})

_FTS_SKIP_DIRS = frozenset({
    ".git", "target", "build", "node_modules",
    "__pycache__", ".cargo", "vendor",
    "third_party", "thirdparty", "external",
})


def iter_source_files(repo_path: str) -> list[Path]:
    """枚举仓库内可索引的源文件路径，跳过噪声目录、二进制和超大文件。"""
    root = Path(repo_path)
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _FTS_SKIP_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in _SRC_EXTS_FOR_FTS:
                continue
            results.append(p)
    return results
