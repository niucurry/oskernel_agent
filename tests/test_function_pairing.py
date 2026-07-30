from __future__ import annotations

from src.embed.query import _identity_neighbor_candidates
from src.exact.identity import (compare_function_identity_features,
                                function_identity,
                                function_identity_features,
                                identity_relation,
                                is_trivial_constant_stub)
from src.models import FunctionRecord
from src.normalize.store import FunctionStore
from src.report import semantic_compare as SC


def _record(repo: str, path: str, start: int, name: str, code: str) -> FunctionRecord:
    return FunctionRecord(
        repo_id=repo,
        file_path=path,
        start_line=start,
        end_line=start + max(0, code.count("\n")),
        func_name=name,
        module_tag="other",
        lang="rust",
        raw_code=code,
        normalized_code="",
    )


def test_identity_score_prefers_specific_corresponding_function_over_neighbor_template():
    query = """fn remove_record(store: &mut Store, key: Key) -> Result<()> {
    let position = store.find(key)?;
    store.remove_at(position);
    store.flush()?;
    Ok(())
}"""
    correct = """fn remove_record(table: &mut Store, key: Key) -> Result<()> {
    let index = table.find(key)?;
    table.remove_at(index);
    table.flush()?;
    Ok(())
}"""
    neighbor = """fn update_record(table: &mut Store, key: Key, value: Value) -> Result<()> {
    let index = table.find(key)?;
    table.replace_at(index, value);
    table.flush()?;
    Ok(())
}"""

    same = function_identity("remove_record", query, "remove_record", correct)
    adjacent = function_identity("remove_record", query, "update_record", neighbor)

    assert same["score"] >= 0.8
    assert same["score"] > adjacent["score"] + 0.18


def test_precomputed_identity_features_are_exactly_equivalent_to_wrapper():
    query = "fn run_tasks(queue: &mut Queue) { while let Some(t) = queue.pop() { t.run(); } }"
    candidate = "fn run_tasks(tasks: &mut Queue) { while let Some(t) = tasks.pop() { t.run(); } }"

    direct = function_identity("run_tasks", query, "run_tasks", candidate)
    precomputed = compare_function_identity_features(
        function_identity_features("run_tasks", query),
        function_identity_features("run_tasks", candidate),
    )

    assert precomputed == direct


def test_parameter_count_ignores_gnu_annotations_before_c_function():
    no_args = """__attribute__((cold))
void flush(void) { drain(); }"""
    two_args = """__attribute__((section(\".text.fast\")))
int run_tasks(struct Queue *queue, size_t limit) { return limit; }"""

    assert function_identity_features("flush", no_args).parameter_count == 0
    assert function_identity_features("run_tasks", two_args).parameter_count == 2


def test_parameter_count_finds_rust_signature_after_cfg_and_generics():
    code = """#[cfg(any(feature = \"fast\", target_arch = \"riscv64\"))]
pub fn run<T: Handler<Result<A, B>>>(handler: T, limit: usize) {
    handler.run(limit);
}"""

    assert function_identity_features("run", code).parameter_count == 2


def test_parameter_count_ignores_commas_in_nested_parameter_types():
    code = """int install(
    void (*handler)(int, int),
    Pair<Map<int, int>, int> value
) { return handler(value); }"""

    assert function_identity_features("install", code).parameter_count == 2


def test_same_name_alone_does_not_claim_specific_function_correspondence():
    assert identity_relation(True, 0.55, 0.30) == "same_name_only"
    assert identity_relation(True, 0.55, 0.80) == "same_name_code_clone"
    assert identity_relation(True, 0.80, 0.30) == "exact_counterpart"


def test_identity_neighbor_expands_correct_function_from_recalled_file(tmp_path):
    db = tmp_path / "functions.db"
    correct_code = "fn remove_record(store: &mut Store, key: Key) { store.remove(key); }"
    neighbor_code = "fn update_record(store: &mut Store, key: Key) { store.update(key); }"
    with FunctionStore(db) as store:
        neighbor_id = store.add_function(
            _record("history/repo", "src/records.rs", 10, "update_record", neighbor_code), [])
        correct_id = store.add_function(
            _record("history/repo", "src/records.rs", 30, "remove_record", correct_code), [])
        store.conn.commit()

        query = {
            "func_name": "remove_record", "raw_code": correct_code,
            "lang": "rust",
        }
        seeds = [{"id": neighbor_id, "score": 0.88, "recall_source": "vector"}]
        added, scanned = _identity_neighbor_candidates(
            store.conn, query, seeds, "query/repo")

    assert scanned == 2
    assert [candidate["id"] for candidate in added] == [correct_id]
    assert added[0]["identity_expansion"] is True
    assert added[0]["identity_score"] >= 0.8


def test_identity_neighbor_reuses_seed_and_file_domain_queries(tmp_path):
    db = tmp_path / "functions.db"
    query_code = "fn remove_record(store: &mut Store) { store.remove(); }"
    with FunctionStore(db) as store:
        seed_id = store.add_function(
            _record("history/repo", "src/records.rs", 10, "update_record",
                    "fn update_record(store: &mut Store) { store.update(); }"), [])
        store.add_function(
            _record("history/repo", "src/records.rs", 30, "remove_record", query_code), [])
        store.conn.commit()
        selects = []
        store.conn.set_trace_callback(
            lambda sql: selects.append(sql) if sql.lstrip().upper().startswith("SELECT") else None
        )
        seed_cache = {}
        domain_cache = {}
        args = (
            store.conn,
            {"func_name": "remove_record", "raw_code": query_code, "lang": "rust"},
            [{"id": seed_id, "score": 0.8, "recall_source": "vector"}],
            "query/repo",
        )

        first, first_scanned = _identity_neighbor_candidates(
            *args, seed_location_cache=seed_cache, domain_rows_cache=domain_cache)
        first_select_count = len(selects)
        second, second_scanned = _identity_neighbor_candidates(
            *args, seed_location_cache=seed_cache, domain_rows_cache=domain_cache)

    assert first_scanned == second_scanned == 2
    assert [candidate["id"] for candidate in first] == [candidate["id"] for candidate in second]
    assert first_select_count == 2
    assert len(selects) == first_select_count


def _pair(query: dict, candidate: dict, identity: float, line: float,
          tier: str = "review") -> dict:
    matched = 3
    return {
        "tier": tier,
        "final_score": line,
        "query_func": query,
        "candidate_func": candidate,
        "evidence": {
            "line_similarity": line,
            "function_identity_score": identity,
            "exact_match_lines": matched,
            "renamed_match_lines": 0,
        },
        "matched_spans": [],
        "match_type_per_span": ["exact"],
    }


def test_dominated_neighbor_pair_is_replaced_by_better_function_in_same_file():
    query = {
        "repo_id": "query/repo", "file_path": "src/records.rs", "start_line": 20,
        "end_line": 25, "func_name": "remove_record", "module_tag": "other",
        "lang": "rust", "raw_code": "\n".join(["step();"] * 6),
    }
    correct_ref = {
        **query, "repo_id": "history/repo", "start_line": 100,
        "func_name": "remove_record",
    }
    wrong_ref = {
        **query, "repo_id": "history/repo", "start_line": 140,
        "func_name": "update_record",
    }
    correct = _pair(query, correct_ref, identity=0.92, line=0.42)
    wrong = _pair(query, wrong_ref, identity=0.38, line=0.44)

    removed = SC._suppress_dominated_candidate_mismatches([wrong, correct])

    assert removed == 1
    assert correct["tier"] == "review"
    assert wrong["tier"] == "dismissed"
    assert wrong["pairing_replacement"]["func_name"] == "remove_record"


def test_high_line_similarity_renamed_copy_is_not_suppressed_by_same_name_candidate():
    query = {
        "repo_id": "query/repo", "file_path": "src/records.rs", "start_line": 20,
        "end_line": 25, "func_name": "remove_record", "module_tag": "other",
        "lang": "rust", "raw_code": "\n".join(["step();"] * 6),
    }
    same_name = {**query, "repo_id": "history/repo", "start_line": 100}
    renamed = {
        **query, "repo_id": "history/repo", "start_line": 140,
        "func_name": "erase_entry",
    }
    identity_match = _pair(query, same_name, identity=0.9, line=0.4)
    strong_renamed = _pair(query, renamed, identity=0.45, line=0.82)

    removed = SC._suppress_dominated_candidate_mismatches(
        [identity_match, strong_renamed])

    assert removed == 0
    assert strong_renamed["tier"] == "review"


def test_family_neighbor_is_removed_but_compatible_renamed_pair_is_kept():
    query = {
        "repo_id": "query/repo", "file_path": "src/records.rs", "start_line": 20,
        "end_line": 25, "func_name": "remove_record", "module_tag": "other",
        "lang": "rust", "raw_code": "\n".join(["step();"] * 6),
    }
    family_ref = {
        **query, "repo_id": "history/repo", "start_line": 100,
        "func_name": "update_record",
    }
    renamed_ref = {
        **query, "repo_id": "history/repo", "file_path": "src/other.rs",
        "start_line": 200, "func_name": "erase_entry",
    }
    family = _pair(query, family_ref, identity=0.58, line=0.55)
    renamed = _pair(query, renamed_ref, identity=0.8, line=0.5)
    same_name_only = _pair(query, {**family_ref, "func_name": "remove_record"},
                           identity=0.55, line=0.3)
    family["evidence"]["function_identity_relation"] = "family_neighbor"
    renamed["evidence"]["function_identity_relation"] = "compatible_renamed"
    same_name_only["evidence"]["function_identity_relation"] = "same_name_only"

    removed = SC._suppress_family_neighbor_mismatches(
        [family, renamed, same_name_only])

    assert removed == 2
    assert family["tier"] == "dismissed"
    assert same_name_only["tier"] == "dismissed"
    assert renamed["tier"] == "review"


def test_different_named_constant_stubs_have_no_behavior_identity():
    query_code = """pub fn sys_alpha(a: usize, b: usize) -> isize {
    -38 // not implemented
}"""
    candidate_code = """pub fn sys_beta(a: usize, b: usize) -> isize {
    1
}"""
    query = {
        "repo_id": "query/repo", "file_path": "src/sys.rs", "start_line": 20,
        "end_line": 22, "func_name": "sys_alpha", "module_tag": "other",
        "lang": "rust", "raw_code": query_code,
    }
    candidate = {
        **query, "repo_id": "history/repo", "start_line": 100,
        "func_name": "sys_beta", "raw_code": candidate_code,
    }
    pair = _pair(query, candidate, identity=0.4, line=0.8)
    pair["evidence"]["function_identity_relation"] = "nonsemantic_stub"

    assert is_trivial_constant_stub(query_code) is True
    assert is_trivial_constant_stub(candidate_code) is True
    assert SC._suppress_nonsemantic_stub_mismatches([pair]) == 1
    assert pair["tier"] == "dismissed"
