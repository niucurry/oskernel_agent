from __future__ import annotations

import sqlite3

from src.embed.query import (_fingerprint_candidates, _name_candidates,
                             _same_language_candidates)
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


def test_function_name_recall_keeps_every_concrete_implementation(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        for index, repo_id in enumerate(("2024/a", "2025/b", "2025/b"), start=1):
            rec = FunctionRecord(
                repo_id=repo_id, file_path="processor.rs", start_line=index * 50,
                end_line=index * 50 + 39,
                func_name="run_tasks", module_tag=ModuleTag.SCHED, lang="rust",
                raw_code="fn run_tasks() {}", normalized_code=f"different {repo_id}",
            )
            store.add_function(rec, [])
        store.conn.commit()
    conn = sqlite3.connect(db)
    got = _name_candidates(conn, "run_tasks", "2026/new")
    conn.close()
    assert {c["payload"]["repo_id"] for c in got} == {"2024/a", "2025/b"}
    assert len(got) == 3
    assert sum(c["payload"]["repo_id"] == "2025/b" for c in got) == 2
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


def test_language_filter_cache_queries_each_candidate_id_once(tmp_path):
    db = tmp_path / "functions.db"
    ids = {}
    with FunctionStore(db) as store:
        for lang in ("rust", "c"):
            ids[lang] = store.add_function(FunctionRecord(
                repo_id=f"2024/{lang}", file_path=f"run.{lang}",
                start_line=1, end_line=2, func_name="run_tasks",
                module_tag=ModuleTag.SCHED, lang=lang,
                raw_code="same text", normalized_code=f"normalized {lang}",
            ), [])
        store.conn.commit()
        selects = []
        store.conn.set_trace_callback(
            lambda sql: selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
        )
        candidates = [{"id": ids["rust"]}, {"id": ids["c"]}]
        cache = {}

        rust = _same_language_candidates(
            store.conn, candidates, "rust", language_cache=cache)
        first_select_count = len(selects)
        c_lang = _same_language_candidates(
            store.conn, candidates, "c", language_cache=cache)

    assert [candidate["id"] for candidate in rust] == [ids["rust"]]
    assert [candidate["id"] for candidate in c_lang] == [ids["c"]]
    assert first_select_count == 1
    assert len(selects) == first_select_count


def test_recall_hot_queries_use_composite_indexes(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        name_plan = store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM functions "
            "WHERE func_name=? AND repo_id<>? AND lang=? ORDER BY repo_id, id",
            ("run_tasks", "2026/new", "rust"),
        ).fetchall()
        domain_plan = store.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM functions "
            "WHERE repo_id=? AND file_path=? AND lang=? ORDER BY start_line, id",
            ("2025/history", "src/task.rs", "rust"),
        ).fetchall()

    assert any("idx_functions_name_lang_repo" in row[-1] for row in name_plan)
    assert any("idx_functions_repo_file_lang_line" in row[-1] for row in domain_plan)
