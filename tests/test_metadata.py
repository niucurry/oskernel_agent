"""src.metadata 测试：独特字符串过滤/召回、基线双侧扣除、git blame 定位、commit 信号。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.metadata.baseline import is_baseline_derived
from src.metadata.commits import (
    detect_commit_signals,
    find_introducing_commit,
    parse_blame,
)
from src.metadata.config import MetadataSettings
from src.metadata.runner import channel_baseline, channel_unique_strings
from src.metadata.strings import build_reverse_index, string_hits_for_func
from src.models import FunctionRecord
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
    assert sp["tier"] == "review"


# ---------- 通道 2：基线双侧才扣 ----------

def test_baseline_both_sides_same_func():
    assert is_baseline_derived((7, 0.9), (7, 0.92), threshold=0.85) is True
    assert is_baseline_derived((7, 0.9), (None, 0.0), threshold=0.85) is False   # 仅一侧
    assert is_baseline_derived((7, 0.9), (8, 0.95), threshold=0.85) is False     # 不同基线函数
    assert is_baseline_derived((7, 0.9), (7, 0.80), threshold=0.85) is False     # 一侧低于阈值


class _FakeMatcher:
    def __init__(self, mapping):
        self.mapping = mapping

    def match(self, nc):
        return self.mapping.get(nc, (None, 0.0))


def test_channel_baseline_downgrades_only_both_sides():
    s_both = {"tier": "review", "query_func": {"normalized_code": "Q1"},
              "candidate_func": {"normalized_code": "C1"}, "evidence": {}}
    s_one = {"tier": "review", "query_func": {"normalized_code": "Q2"},
             "candidate_func": {"normalized_code": "C2"}, "evidence": {}}
    matcher = _FakeMatcher({"Q1": (5, 0.9), "C1": (5, 0.91), "Q2": (5, 0.9), "C2": (None, 0.0)})
    n = channel_baseline({"suspects": [s_both, s_one]}, matcher, SETTINGS)
    assert n == 1
    assert s_both["tier"] == "baseline_derived" and s_both["evidence"]["baseline_flag"] is True
    assert s_one["tier"] == "review"  # 仅一侧命中基线，不扣除


# ---------- 通道 3：git blame 定位 + 信号 ----------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def test_find_introducing_commit_locates_right_commit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    f = repo / "foo.rs"
    f.write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(repo, "add", "foo.rs")
    _git(repo, "commit", "-q", "-m", "init")
    sha1 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    f.write_text("line1\nline2\nline3\nfn added() {\n    do_work();\n}\n", encoding="utf-8")
    _git(repo, "add", "foo.rs")
    _git(repo, "commit", "-q", "-m", "add scheduler function")
    sha2 = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    assert find_introducing_commit(repo, "foo.rs", 4, 6) == sha2   # 新增的函数行
    assert find_introducing_commit(repo, "foo.rs", 1, 3) == sha1   # 初始行


def test_detect_commit_signals():
    big = detect_commit_signals({"additions": 3000, "message": "stuff", "date": "2023-01-01"}, 10, SETTINGS)
    assert "large_commit" in big

    vague = detect_commit_signals({"additions": 5, "message": "init", "date": "2023-01-01"}, 10, SETTINGS)
    assert "vague_message" in vague

    early = detect_commit_signals(
        {"additions": 100, "message": "add full scheduler", "date": "2024-05-02T10:00:00Z"}, 40, SETTINGS
    )
    assert "early_complete_impl" in early   # 距 2024-05-01 < 3 天且 40 行

    none = detect_commit_signals({"additions": 5, "message": "fix bug in scheduler", "date": "2024-08-01"}, 10, SETTINGS)
    assert none == []
