"""src.metadata 测试：独特字符串过滤/召回、基线双侧扣除。"""

from __future__ import annotations

from pathlib import Path

from src.metadata.baseline import is_baseline_derived
from src.metadata.config import MetadataSettings
from src.metadata.runner import (channel_baseline, channel_common_code,
                                 channel_unique_strings, process_metadata)
from src.metadata.strings import build_reverse_index, string_hits_for_func
from src.models import FunctionRecord, is_baseline_repo
from src.normalize.store import FunctionStore

SETTINGS = MetadataSettings()

UNIQUE_STR = "very specific kernel boot banner xyzzy"
GENERIC_STR = "could not allocate memory block"


def _rec(repo_id, func_name, raw, start=10):
    return FunctionRecord(
        repo_id=repo_id, file_path="src/x.rs", start_line=start, end_line=start + raw.count("\n"),
        func_name=func_name, module_tag="other", lang="rust", raw_code=raw, normalized_code="",
    )


def _build_db(tmp_path) -> tuple[Path, int]:
    db = tmp_path / "functions.db"
    with FunctionStore(db) as s:
        # 历史函数：含独特字符串
        hist_id = s.add_function(_rec("2021/hist", "boot", "fn boot() {}"), [UNIQUE_STR], [])
        # 通用字符串：出现在 6 个不同仓库 → 应被过滤（阈值 5）
        for i in range(6):
            s.add_function(_rec(f"20{i}/r", f"f{i}", "fn f() {}"), [GENERIC_STR], [])
        s.conn.commit()
    return db, hist_id


# ---------- 通道 1：独特字符串 ----------

def test_generic_string_filtered_unique_kept(tmp_path):
    db, _ = _build_db(tmp_path)
    index = build_reverse_index(db, generic_threshold=5)
    assert UNIQUE_STR in index           # 仅 1 个仓库 → 保留
    assert GENERIC_STR not in index      # 6 个仓库 > 5 → 过滤


def test_string_hits_exclude_same_repo(tmp_path):
    db, hist_id = _build_db(tmp_path)
    index = build_reverse_index(db)
    # 来自其它仓库的查询 → 命中
    hits = string_hits_for_func([UNIQUE_STR], index, exclude_repo_id="2024/new")
    assert hits.get(hist_id) == ("2021/hist", 1)
    # 同仓库被排除
    assert string_hits_for_func([UNIQUE_STR], index, exclude_repo_id="2021/hist") == {}


def test_channel_creates_new_pair_via_string(tmp_path):
    db, hist_id = _build_db(tmp_path)
    # 一个已有嫌疑对（与字符串无关），其 query 函数 raw_code 含独特字符串
    qf = {
        "repo_id": "2024/new", "file_path": "k.rs", "start_line": 100, "end_line": 102,
        "func_name": "init", "module_tag": "other", "lang": "rust",
        "raw_code": f'fn init() {{ log("{UNIQUE_STR}"); }}', "normalized_code": "",
    }
    data = {"suspects": [{
        "tier": "weak", "final_score": 0.5,
        "query_func": qf,
        "candidate_func": {"repo_id": "2099/other", "file_path": "z.rs", "start_line": 1, "end_line": 3,
                           "func_name": "zzz", "module_tag": "other", "lang": "rust", "raw_code": "", "normalized_code": ""},
        "evidence": {}, "matched_spans": [], "match_type_per_span": [],
    }]}
    new_n = channel_unique_strings(data, db, SETTINGS)
    assert new_n == 1
    created = [s for s in data["suspects"] if s.get("source") == "string_channel"]
    assert len(created) == 1
    sp = created[0]
    assert sp["candidate_func"]["repo_id"] == "2021/hist"
    assert sp["evidence"]["unique_string_matches"] == 1
    assert sp["tier"] == "weak"
    assert sp["evidence"]["line_similarity"] == sp["final_score"]


def test_metadata_string_channel_and_existing_pairs_are_same_language_only(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        c_rec = FunctionRecord(
            repo_id="2021/c-hist", file_path="src/x.c", start_line=1, end_line=3,
            func_name="init", module_tag="other", lang="c",
            raw_code="int init(void){return 0;}", normalized_code="",
        )
        store.add_function(c_rec, [UNIQUE_STR], [])
        store.conn.commit()
    qf = {
        "repo_id": "2024/new", "file_path": "k.rs", "start_line": 10, "end_line": 12,
        "func_name": "init", "module_tag": "other", "lang": "rust",
        "raw_code": f'fn init() {{ log!("{UNIQUE_STR}"); }}', "normalized_code": "",
    }
    data = {"suspects": [{
        "tier": "review", "final_score": 0.7, "query_func": qf,
        "candidate_func": {
            "repo_id": "2021/c-hist", "file_path": "src/x.c", "start_line": 1,
            "end_line": 3, "func_name": "init", "module_tag": "other", "lang": "c",
            "raw_code": "int init(void){return 0;}", "normalized_code": "",
        },
        "evidence": {}, "matched_spans": [], "match_type_per_span": [],
    }]}

    result = process_metadata(data, db, settings=SETTINGS)

    assert result["suspects"] == []
    assert result["metadata_summary"]["cross_language_filtered"] == 1
    assert result["metadata_summary"]["string_new_pairs"] == 0


def test_metadata_tags_verified_library_adapter_before_other_channels(tmp_path):
    repo = tmp_path / "repo"
    package = repo / "crates" / "renamed-lwext4"
    adapter = repo / "os" / "src" / "fs" / "ext4_lw"
    package.mkdir(parents=True)
    adapter.mkdir(parents=True)
    (package / "Cargo.toml").write_text(
        '[package]\nname = "lwext4_rust"\nversion = "0.1.0"\n', encoding="utf-8")
    (adapter / "inode.rs").write_text(
        "use lwext4_rust::Ext4File;\nfn as_inode_type() {}\n", encoding="utf-8")
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        store.conn.commit()
    suspect = {
        "tier": "confirmed",
        "query_func": {
            "repo_id": "2026/new", "file_path": "os/src/fs/ext4_lw/inode.rs",
            "start_line": 2, "func_name": "as_inode_type", "lang": "rust",
            "raw_code": "fn as_inode_type() {}", "normalized_code": "Q",
        },
        "candidate_func": {
            "repo_id": "2025/team", "file_path": "os/src/fs/ext4_lw/inode.rs",
            "start_line": 2, "func_name": "as_inode_type", "lang": "rust",
            "raw_code": "fn as_inode_type() {}", "normalized_code": "C",
        },
        "evidence": {},
    }

    result = process_metadata(
        {"suspects": [suspect]}, db, settings=SETTINGS, query_repo=repo)

    assert result["metadata_summary"]["library_reuse_pairs"] == 1
    assert result["metadata_summary"]["string_new_pairs"] == 0
    assert suspect["reuse_library"] == "lwext4"
    assert suspect["tier"] == "confirmed"


# ---------- D1：字符串通道新建对计算真实 final_score（不再硬编码 0） ----------

def test_string_channel_new_pair_scores_identical_copy(tmp_path):
    raw = (
        "fn parse_header(buf: &[u8]) -> Header {\n"
        "    let magic = read_u32(buf);\n"
        f'    log("{UNIQUE_STR}");\n'
        "    let version = read_u16(buf);\n"
        "    Header { magic, version }\n"
        "}"
    )
    db = tmp_path / "functions.db"
    with FunctionStore(db) as s:
        s.add_function(_rec("2021/hist", "parse_header", raw), [UNIQUE_STR], [])
        s.conn.commit()
    qf = {
        "repo_id": "2024/new", "file_path": "k.rs", "start_line": 100,
        "end_line": 105, "func_name": "parse_header", "module_tag": "fs", "lang": "rust",
        "raw_code": raw, "normalized_code": "",
    }
    data = {"suspects": [{
        "tier": "weak", "final_score": 0.5, "query_func": qf,
        "candidate_func": {"repo_id": "2099/other", "file_path": "z.rs", "start_line": 1, "end_line": 3,
                           "func_name": "zzz", "module_tag": "other", "lang": "rust",
                           "raw_code": "", "normalized_code": ""},
        "evidence": {}, "matched_spans": [], "match_type_per_span": [],
    }]}
    channel_unique_strings(data, db, SETTINGS)
    created = [s for s in data["suspects"] if s.get("source") == "string_channel"]
    assert len(created) == 1
    sp = created[0]
    assert sp["final_score"] == 1.0                       # 逐字节相同 → 真实 100%
    assert sp["tier"] == "confirmed"                      # 不再恒为 review
    assert sp["evidence"]["exact_match_lines"] > 0
    assert sp["evidence"]["renamed_match_lines"] == 0


# ---------- 通道 2：基线扣除（双侧同基线 或 单侧 query 命中） ----------

def test_baseline_both_sides_same_func():
    # 返回 (bool, 判据)。双侧同基线用 bilateral_threshold（默认 0.70，低于单侧 0.85）——
    # 双侧信号（两侧独立收敛到同一基线 id）远强于单侧，故门槛低，覆盖「4 队共同改造 rcore-v3
    # 原始函数、互相 1.0 但对原始版 sim<0.85」的因果倒置场景。
    assert is_baseline_derived((7, 0.9), (7, 0.92), threshold=0.85)[0] is True        # 双侧同基线
    assert is_baseline_derived((7, 0.9), (None, 0.0), threshold=0.85)[0] is True      # 单侧 query 命中（vendored 上游）
    assert is_baseline_derived((7, 0.9), (8, 0.95), threshold=0.85)[0] is True        # query 命中基线 7（candidate 命中别的，单侧）
    # 双侧同 id、两侧均 > 双侧阈值 0.70（即使 query < 单侧 0.85）→ 双侧路径接住（remove_timer 实测 0.826）
    assert is_baseline_derived((7, 0.80), (7, 0.9), threshold=0.85)[0] is True
    assert is_baseline_derived((7, 0.826), (7, 0.826), threshold=0.85, bilateral_threshold=0.75)[0] is True
    assert is_baseline_derived((None, 0.0), (7, 0.9), threshold=0.85)[0] is False     # query 未命中基线
    # 不同 id 且 query < 单侧阈值 → 双侧不成立、单侧也不够 → 不判
    assert is_baseline_derived((7, 0.80), (8, 0.9), threshold=0.85)[0] is False
    # 同 id 但 query < 双侧阈值 0.70 → 双侧也不够 → 不判
    assert is_baseline_derived((7, 0.60), (7, 0.9), threshold=0.85)[0] is False


class _FakeMatcher:
    def __init__(self, mapping):
        self.mapping = mapping

    def match(self, nc):
        return self.mapping.get(nc, (None, 0.0))


def test_channel_baseline_never_reclassifies_library_reuse():
    suspect = {
        "tier": "confirmed", "reuse_library": "lwext4",
        "query_func": {"normalized_code": "Q"},
        "candidate_func": {
            "repo_id": "data/repos/0/baseline_lwext4", "normalized_code": "B",
        },
        "evidence": {},
    }

    n = channel_baseline(
        {"suspects": [suspect]}, _FakeMatcher({"Q": (1, 1.0), "B": (1, 1.0)}),
        SETTINGS,
    )

    assert n == 0
    assert suspect["tier"] == "confirmed"
    assert "baseline_flag" not in suspect["evidence"]


def test_channel_baseline_does_not_exclude_on_vector_similarity_alone(tmp_path):
    s_both = {"tier": "review", "query_func": {"normalized_code": "Q1"},
              "candidate_func": {"normalized_code": "C1"}, "evidence": {}}
    s_one = {"tier": "review", "query_func": {"normalized_code": "Q2"},
             "candidate_func": {"normalized_code": "C2"}, "evidence": {}}
    # Q2 命中基线（vendored 上游），C2 不命中 → 单侧也扣
    matcher = _FakeMatcher({"Q1": (5, 0.9), "C1": (5, 0.91), "Q2": (5, 0.9), "C2": (None, 0.0)})
    n = channel_baseline(
        {"suspects": [s_both, s_one]}, matcher, SETTINGS,
        db_path=tmp_path / "missing.db",
    )
    assert n == 0
    assert s_both["tier"] == "review"
    assert s_one["tier"] == "review"
    assert s_both["evidence"]["baseline_vector_candidate_only"] is True


def test_channel_baseline_requires_direct_source_evidence(tmp_path):
    baseline_code = """fn common() {
    let a = prepare();
    let b = transform(a);
    commit(b);
}"""
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        baseline_id = store.add_function(
            _rec("0/baseline_kernel", "common", baseline_code), [], [],
        )
        store.conn.commit()
    query = {
        "repo_id": "2026/new", "file_path": "src/common.rs", "start_line": 1,
        "end_line": 5, "func_name": "common", "module_tag": "other", "lang": "rust",
        "raw_code": baseline_code, "normalized_code": "Q",
    }
    suspect = {
        "tier": "review", "final_score": 0.8, "query_func": query,
        "candidate_func": {
            "repo_id": "2025/team", "file_path": "src/common.rs", "start_line": 1,
            "end_line": 5, "func_name": "common", "module_tag": "other", "lang": "rust",
            "raw_code": baseline_code, "normalized_code": "C",
        },
        "evidence": {"line_similarity": 1.0, "exact_match_lines": 5},
    }
    matcher = _FakeMatcher({
        "Q": (baseline_id, 0.93), "C": (baseline_id, 0.94),
    })

    n = channel_baseline({"suspects": [suspect]}, matcher, SETTINGS, db_path=db)

    assert n == 1
    assert suspect["tier"] == "baseline_derived"
    assert suspect["evidence"]["baseline_reference"]["repo_id"] == "0/baseline_kernel"
    assert "直接逐行相似" in suspect["baseline_note"]


def test_low_direct_baseline_overlap_cannot_hide_stronger_history(tmp_path):
    baseline_code = """fn run_tasks() {
    shared_one();
    shared_two();
}"""
    query_code = """fn run_tasks() {
    shared_one();
    shared_two();
    team_step_one();
    team_step_two();
    team_step_three();
    team_step_four();
    team_step_five();
    finish();
}"""
    history_code = query_code
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        baseline_id = store.add_function(
            _rec("0/baseline_kernel", "run_tasks", baseline_code), [], [],
        )
        store.conn.commit()
    query = {
        "repo_id": "2026/new", "file_path": "src/task.rs", "start_line": 90,
        "end_line": 100, "func_name": "run_tasks", "module_tag": "sched", "lang": "rust",
        "raw_code": query_code, "normalized_code": "Q",
    }
    suspect = {
        "tier": "review", "final_score": 1.0, "query_func": query,
        "candidate_func": {
            "repo_id": "2025/team", "file_path": "src/task.rs", "start_line": 60,
            "end_line": 70, "func_name": "run_tasks", "module_tag": "sched", "lang": "rust",
            "raw_code": history_code, "normalized_code": "H",
        },
        "evidence": {"line_similarity": 1.0, "exact_match_lines": 10},
    }
    matcher = _FakeMatcher({
        "Q": (baseline_id, 0.92), "H": (baseline_id, 0.93),
    })

    n = channel_baseline({"suspects": [suspect]}, matcher, SETTINGS, db_path=db)

    assert n == 0
    assert suspect["tier"] == "review"
    assert suspect["evidence"]["baseline_vector_candidate_only"] is True


def test_explicit_baseline_candidate_without_direct_code_does_not_propagate_per_query():
    query = {"file_path": "src/mm.rs", "start_line": 20, "func_name": "map",
             "normalized_code": "Q"}
    direct = {
        "tier": "review", "query_func": query,
        "candidate_func": {
            "repo_id": r"data\repos\0\baseline_kernel", "normalized_code": "B",
        },
        "evidence": {},
    }
    historical = {
        "tier": "confirmed", "query_func": dict(query),
        "candidate_func": {"repo_id": "2025/team", "normalized_code": "H"},
        "evidence": {},
    }
    same_name_different_function = {
        "tier": "confirmed",
        "query_func": {**query, "start_line": 80},
        "candidate_func": {"repo_id": "2025/team", "normalized_code": "H2"},
        "evidence": {},
    }

    n = channel_baseline(
        {"suspects": [direct, historical, same_name_different_function]},
        _FakeMatcher({}), SETTINGS,
    )

    assert is_baseline_repo(r"data\repos\0\baseline_kernel") is True
    assert n == 1
    assert direct["tier"] == "baseline_derived"
    assert direct["evidence"]["baseline_source_substantive"] is False
    assert historical["tier"] == "confirmed"
    assert same_name_different_function["tier"] == "confirmed"


def test_explicit_baseline_candidate_with_direct_code_propagates_per_query():
    query = {"file_path": "src/mm.rs", "start_line": 20, "func_name": "map",
             "normalized_code": "Q"}
    direct = {
        "tier": "review", "query_func": query,
        "candidate_func": {
            "repo_id": "data/repos/0/baseline_kernel", "normalized_code": "B",
        },
        "evidence": {"line_similarity": 0.9, "exact_match_lines": 10},
    }
    historical = {
        "tier": "confirmed", "query_func": dict(query),
        "candidate_func": {"repo_id": "2025/team", "normalized_code": "H"},
        "evidence": {"line_similarity": 0.85, "exact_match_lines": 9},
    }

    n = channel_baseline(
        {"suspects": [direct, historical]}, _FakeMatcher({}), SETTINGS,
    )

    assert n == 2
    assert direct["evidence"]["baseline_source_substantive"] is True
    assert direct["tier"] == historical["tier"] == "baseline_derived"


def test_weak_explicit_baseline_does_not_hide_stronger_history_evidence():
    query = {
        "file_path": "src/task.rs", "start_line": 90, "end_line": 131,
        "func_name": "run_tasks", "normalized_code": "Q",
    }
    direct = {
        "tier": "weak", "query_func": query,
        "candidate_func": {
            "repo_id": "data/repos/0/baseline_kernel", "normalized_code": "B",
        },
        "evidence": {
            "line_similarity": 0.1951,
            "exact_match_lines": 5,
            "renamed_match_lines": 3,
        },
    }
    historical = {
        "tier": "weak", "query_func": dict(query),
        "candidate_func": {"repo_id": "2025/team", "normalized_code": "H"},
        "evidence": {
            "line_similarity": 0.4146,
            "exact_match_lines": 9,
            "renamed_match_lines": 8,
        },
    }

    n = channel_baseline(
        {"suspects": [direct, historical]}, _FakeMatcher({}), SETTINGS,
    )

    assert n == 1
    assert direct["tier"] == "baseline_derived"
    assert historical["tier"] == "weak"
    assert direct["evidence"]["baseline_source_substantive"] is False
    assert "baseline_incremental_evidence" not in historical["evidence"]


# ---------- 通道 4：公共/框架代码广度过滤 ----------

def _sp(qfp, qsl, crepo, tier, vec):
    return {"tier": tier, "query_func": {"file_path": qfp, "start_line": qsl},
            "candidate_func": {"repo_id": crepo}, "evidence": {"vector_similarity": vec}}


def test_common_code_breadth_only_annotates_without_downgrading():
    # 命中多个仓库只能证明广泛传播，无法排除 fork 链或多次复制，因此不单独降级。
    common = [_sp("a.rs", 1, f"2025/team{i}", "confirmed", 0.99) for i in range(6)]
    # review 档同样只标注广度。
    reviewmany = [_sp("d.rs", 1, f"2025/r{i}", "review", 0.95) for i in range(6)]
    # 独有函数 B：只命中 1 个仓库 → 保持 confirmed
    uniq = [_sp("b.rs", 1, "2025/teamX", "confirmed", 1.0)]
    # 函数 C：命中 6 个仓库但相似度都低于阈值 → 不算公共
    weakmany = [_sp("c.rs", 1, f"2025/w{i}", "weak", 0.6) for i in range(6)]
    data = {"suspects": common + reviewmany + uniq + weakmany}
    n = channel_common_code(data, SETTINGS)
    assert n == 12
    assert all(s["tier"] == "confirmed" for s in common)
    assert all("widespread_match_note" in s for s in common)
    assert common[0]["evidence"]["widespread_match_repos"] == 6
    assert all(s["tier"] == "review" for s in reviewmany)
    assert uniq[0]["tier"] == "confirmed"                   # 单仓库不降
    assert all(s["tier"] == "weak" for s in weakmany)       # 低相似不计入广度


def test_common_code_keeps_baseline_derived():
    s = _sp("d.rs", 1, "2025/t0", "baseline_derived", 0.99)
    others = [_sp("d.rs", 1, f"2025/t{i}", "confirmed", 0.99) for i in range(1, 6)]
    channel_common_code({"suspects": [s] + others}, SETTINGS)
    assert s["tier"] == "baseline_derived"  # 更具体的基线信号不被覆盖
