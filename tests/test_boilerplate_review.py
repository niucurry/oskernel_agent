"""文件级路径过滤 + 上游后缀匹配 + confirmed 样板候选复核 测试。

对应评审实测：build.rs/macros.rs/C 库/examples 仍出现在文件级清单（aggregate 忽略排除标签）、
其他队 vendored 到不同目录结构（AstrancE/api/...）、confirmed 的 from/fmt 短函数跳过 LLM 复核。
"""

from __future__ import annotations

from src.report import upstream_baselines as UB
from src.report import semantic_compare as SC


def _q(fp, fn, lang="rust", start=10, module="arch", code="fn f(){}"):
    return {"repo_id": "2026/new", "file_path": fp, "func_name": fn,
            "start_line": start, "end_line": start + 9, "module_tag": module,
            "lang": lang, "raw_code": code}


def _pair(qf, cf, tier="confirmed", score=0.96):
    return {"tier": tier, "final_score": score, "query_func": qf, "candidate_func": cf, "evidence": {}}


# ── 文件级路径过滤 ────────────────────────────────────────────────────────────

def test_is_excluded_file_path_upstream_root():
    assert UB.is_excluded_file_path("arceos/ulib/axlibc/build.rs") == "upstream_vendored"
    assert UB.is_excluded_file_path("arceos/api/arceos_api/src/macros.rs") == "upstream_vendored"


def test_is_excluded_file_path_abi_build_rs():
    # build.rs 无论是否在 arceos 下都判 ABI（构建脚本）
    assert UB.is_excluded_file_path("os/build.rs") == "abi_constrained"
    assert UB.is_excluded_file_path("scripts/x.rs") == "abi_constrained"


def test_is_excluded_file_path_preserves_team_original():
    # 队伍自研代码路径不命中
    assert UB.is_excluded_file_path("os/src/sched/run_queue.rs") is None
    assert UB.is_excluded_file_path("core/src/task/stat.rs") is None


# ── 上游后缀匹配（其他队 vendored 到不同目录结构） ─────────────────────────────

def test_upstream_vendored_suffix_match_different_structure():
    # query: arceos/api/arceos_api/src/macros.rs  (rel = api/arceos_api/src/macros.rs)
    # candidate: AstrancE/api/arceos_api/src/macros.rs  (无 arceos 段，但后缀相同)
    s = _pair(_q("arceos/api/arceos_api/src/macros.rs", "define_api_type"),
              _q("AstrancE/api/arceos_api/src/macros.rs", "define_api_type"))
    assert UB.is_upstream_vendored_pair(s) == "arceos"


def test_upstream_vendored_suffix_does_not_false_positive():
    # 候选后缀不同 → 不命中（保留为真实借鉴信号）
    s = _pair(_q("arceos/modules/asynctask/src/task.rs", "from"),
              _q("2024/x/crates/taskctx/src/task.rs", "from"))
    assert UB.is_upstream_vendored_pair(s) is None


# ── ABI 模式拓宽：robust/futex + 路径 glob 修复 ─────────────────────────────────

def test_abi_robust_futex_name_patterns():
    for fn in ["exit_robust_list", "handle_futex_death", "get_robust_list"]:
        s = _pair(_q("api/src/task.rs", fn), _q("2025/o/api/src/task.rs", fn))
        assert UB.is_abi_constrained(s) is True, f"{fn} 应被判 ABI 受限"


def test_abi_path_glob_no_leading_slash_bug():
    # 路径无前导 /，glob `api/src/file/` 应命中（修前因前导斜杠 bug 漏判）
    s = _pair(_q("api/src/file/fs.rs", "metadata_to_kstat"),
              _q("2025/o/api/src/file/fs.rs", "metadata_to_kstat"))
    assert UB.is_abi_constrained(s) is True


def test_abi_syscall_shim_path():
    s = _pair(_q("api/src/syscall/mm/mmap.rs", "sys_mmap"),
              _q("2025/o/api/src/syscall/mm/mmap.rs", "sys_mmap"))
    assert UB.is_abi_constrained(s) is True


# ── confirmed 样板候选复核 ────────────────────────────────────────────────────

def test_boilerplate_candidate_generic_name():
    # from/fmt/new 等通用 trait 方法名 → 候选
    g = {"query_func": "from", "query_code": "fn from(x: u8) -> Self { Self(x as u16) }"}
    assert SC._is_boilerplate_candidate(g) is True
    g2 = {"query_func": "fmt", "query_code": "fn fmt(...) {...}"}
    assert SC._is_boilerplate_candidate(g2) is True


def test_boilerplate_candidate_short_body():
    # 非 trait 名但函数体 ≤12 非空行 → 候选
    code = "fn f() {\n    let x = 1;\n    x + 1\n}\n"
    g = {"query_func": "custom_func", "query_code": code}
    assert SC._is_boilerplate_candidate(g) is True


def test_boilerplate_candidate_not_long_real_logic():
    # 真实长逻辑函数 → 不是候选（不送复核，保留为 confirmed 借鉴信号）
    code = "\n".join(f"    let v{i} = do_something({i});" for i in range(20))
    g = {"query_func": "complex_scheduler", "query_code": code}
    assert SC._is_boilerplate_candidate(g) is False


def test_apply_review_verdicts_downgrades_confirmed_boilerplate():
    # confirmed 样板候选 LLM 判非借鉴 → 降为 dismissed
    s = _pair(_q("core/src/task/stat.rs", "fmt", code="fn fmt(){}", start=103),
              _q("2025/o/core/src/task/stat.rs", "fmt", code="fn fmt(){}", start=103),
              tier="confirmed", score=1.0)
    groups = [{"query_file": "core/src/task/stat.rs", "query_func": "fmt",
               "query_start": 103, "review_verdict": "非借鉴", "review_reason": "ABI 字段拼接"}]
    up, dn = SC._apply_review_verdicts([s], groups)
    assert up == 0 and dn == 1
    assert s["tier"] == "dismissed"
    assert "非借鉴" in s["dismiss_reason"]


def test_apply_review_verdicts_keeps_confirmed_when_llm_says_borrow():
    # confirmed 样板候选 LLM 判借鉴 → 保持 confirmed
    s = _pair(_q("a.rs", "from", code="fn from(x:u8)->Self{Self(x)}", start=10),
              _q("b.rs", "from", code="fn from(x:u8)->Self{Self(x)}", start=10),
              tier="confirmed", score=1.0)
    groups = [{"query_file": "a.rs", "query_func": "from", "query_start": 10,
               "review_verdict": "借鉴", "review_reason": "逐字相同"}]
    up, dn = SC._apply_review_verdicts([s], groups)
    assert up == 0 and dn == 0
    assert s["tier"] == "confirmed"


def test_apply_review_verdicts_untouched_confirmed_not_in_review():
    # confirmed 但不在 groups（非样板候选）→ 不受复核影响
    s = _pair(_q("a.rs", "real_borrow", code="fn real_borrow(){...}"),
              _q("b.rs", "real_borrow"), tier="confirmed", score=0.98)
    up, dn = SC._apply_review_verdicts([s], [])
    assert up == 0 and dn == 0
    assert s["tier"] == "confirmed"
