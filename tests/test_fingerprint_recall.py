from __future__ import annotations

import sqlite3

from src.embed.query import _fingerprint_candidates, _name_candidates
from src.models import FunctionRecord, ModuleTag
from src.normalize.store import FunctionStore


def test_fingerprint_recall_returns_every_matching_history_repo(tmp_path):
    db = tmp_path / "functions.db"
    normalized = "fn FUNC_0 ( ) { FUNC_1 ( ) ; }"
    with FunctionStore(db) as store:
        for repo_id in ("2024/a", "2025/b", "2025/b"):
            rec = FunctionRecord(
                repo_id=repo_id, file_path=f"{repo_id[-1]}.rs", start_line=1, end_line=10,
                func_name="run_tasks", module_tag=ModuleTag.SCHED, lang="rust",
                raw_code="fn run_tasks() {}", normalized_code=normalized,
            )
            store.add_function(rec, [])
        store.conn.commit()

    conn = sqlite3.connect(db)
    got = _fingerprint_candidates(conn, normalized, "2026/new")
    conn.close()
    assert {c["payload"]["repo_id"] for c in got} == {"2024/a", "2025/b"}
    assert all(c["fingerprint_match"] and c["score"] == 1.0 for c in got)


def test_function_name_recall_is_per_repo_and_skips_vector_gate(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        for repo_id in ("2024/a", "2025/b"):
            rec = FunctionRecord(
                repo_id=repo_id, file_path="processor.rs", start_line=1, end_line=40,
                func_name="run_tasks", module_tag=ModuleTag.SCHED, lang="rust",
                raw_code="fn run_tasks() {}", normalized_code=f"different {repo_id}",
            )
            store.add_function(rec, [])
        store.conn.commit()
    conn = sqlite3.connect(db)
    got = _name_candidates(conn, "run_tasks", "2026/new")
    conn.close()
    assert {c["payload"]["repo_id"] for c in got} == {"2024/a", "2025/b"}
    assert all(c["name_match"] and c["score"] == 0.0 for c in got)


def test_hard_recall_channels_filter_cross_language(tmp_path):
    db = tmp_path / "functions.db"
    normalized = "FUNC_0 ( ) { FUNC_1 ( ) ; }"
    with FunctionStore(db) as store:
        for repo_id, lang, suffix in (("2024/rust", "rust", "rs"), ("2024/c", "c", "c")):
            rec = FunctionRecord(
                repo_id=repo_id, file_path=f"run.{suffix}", start_line=1, end_line=10,
                func_name="run_tasks", module_tag=ModuleTag.SCHED, lang=lang,
                raw_code="same text", normalized_code=normalized,
            )
            store.add_function(rec, [])
        store.conn.commit()
    conn = sqlite3.connect(db)
    fingerprints = _fingerprint_candidates(conn, normalized, "2026/new", "rust")
    names = _name_candidates(conn, "run_tasks", "2026/new", "rust")
    conn.close()

    assert {c["payload"]["repo_id"] for c in fingerprints} == {"2024/rust"}
    assert {c["payload"]["repo_id"] for c in names} == {"2024/rust"}
