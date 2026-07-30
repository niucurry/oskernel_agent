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
        code = f"fn {fn}(ptr: usize) -> isize {{ copy_abi_value(ptr as *const usize) }}"
        s = _pair(_q("api/src/task.rs", fn, code=code), _q("2025/o/api/src/task.rs", fn))
        assert UB.is_abi_constrained(s) is True, f"{fn} 应被判 ABI 受限"


def test_abi_path_glob_no_leading_slash_bug():
    # 路径无前导 /，glob `api/src/file/` 应命中（修前因前导斜杠 bug 漏判）
    s = _pair(_q("api/src/file/fs.rs", "metadata_to_kstat"),
              _q("2025/o/api/src/file/fs.rs", "metadata_to_kstat"))
    assert UB.is_abi_constrained(s) is True


def test_abi_syscall_shim_path():
    code = "fn sys_mmap(addr: usize) -> isize { mmap_adapter(addr as *mut u8) }"
    s = _pair(_q("api/src/syscall/mm/mmap.rs", "sys_mmap", code=code),
              _q("2025/o/api/src/syscall/mm/mmap.rs", "sys_mmap"))
    assert UB.is_abi_constrained(s) is True


# ── confirmed 全量逐对复核（不依赖函数名白名单或代码长度） ─────────────────────

def test_collect_review_pairs_does_not_depend_on_name_or_function_length():
    short = _pair(
        _q("src/a.rs", "convert", code="fn convert(x:u8)->u8{x}"),
        _q("src/b.rs", "convert", code="fn convert(x:u8)->u8{x}"),
    )
    long_code = "\n".join(f"let value_{i} = compute({i});" for i in range(40))
    long = _pair(
        _q("src/c.rs", "domain_specific_operation", start=50, code=long_code),
        _q("src/d.rs", "renamed_operation", start=70, code=long_code),
    )

    groups = SC.collect_review_pairs([short, long], keep_tiers=("confirmed",))

    assert len(groups) == 2
    assert all(len(group["candidates"]) == 1 for group in groups)


def test_apply_review_verdicts_downgrades_confirmed_boilerplate():
    # confirmed 样板候选 LLM 判非借鉴 → 降为 dismissed
    s = _pair(_q("core/src/task/stat.rs", "fmt", code="fn fmt(){}", start=103),
              _q("2025/o/core/src/task/stat.rs", "fmt", code="fn fmt(){}", start=103),
              tier="confirmed", score=1.0)
    groups = SC.collect_review_pairs([s], keep_tiers=("confirmed",))
    groups[0].update({"review_verdict": "非借鉴", "review_reason": "接口约束字段拼接"})
    up, dn = SC._apply_review_verdicts([s], groups)
    assert up == 0 and dn == 1
    assert s["tier"] == "dismissed"
    assert "非借鉴" in s["dismiss_reason"]


def test_apply_review_verdicts_keeps_confirmed_when_llm_says_borrow():
    # confirmed 样板候选 LLM 判借鉴 → 保持 confirmed
    s = _pair(_q("a.rs", "from", code="fn from(x:u8)->Self{Self(x)}", start=10),
              _q("b.rs", "from", code="fn from(x:u8)->Self{Self(x)}", start=10),
              tier="confirmed", score=1.0)
    groups = SC.collect_review_pairs([s], keep_tiers=("confirmed",))
    groups[0].update({"review_verdict": "借鉴", "review_reason": "逐字相同"})
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


def test_apply_review_verdicts_never_drops_uncertain_signal():
    code = "\n".join(f"step_{i}();" for i in range(10))
    s = _pair(_q("a.rs", "run_tasks", code=code, start=10),
              _q("b.rs", "run_tasks", code=code), tier="weak", score=0.61)
    s["evidence"].update({
        "line_similarity": .61, "exact_match_lines": 8,
        "function_identity_score": .8,
    })
    groups = SC.collect_review_pairs([s], keep_tiers=("weak",))
    groups[0].update({"review_verdict": "疑似", "review_reason": "证据不足"})
    up, dn = SC._apply_review_verdicts([s], groups)
    assert up == 0 and dn == 0
    assert s["tier"] == "weak"


def test_apply_review_verdict_is_scoped_to_exact_candidate_pair():
    query_code = "\n".join(f"target_step_{i}();" for i in range(10))
    query = _q("src/target.rs", "operation", start=30, module="other", code=query_code)
    unrelated = _pair(
        query,
        _q("history/unrelated.rs", "different_operation", start=11, module="other",
           code="\n".join(f"unrelated_step_{i}();" for i in range(10))),
        tier="review", score=0.81,
    )
    plausible = _pair(
        query,
        _q("history/plausible.rs", "operation_variant", start=71, module="other",
           code="\n".join(f"plausible_step_{i}();" for i in range(10))),
        tier="review", score=0.79,
    )
    for item, line in ((unrelated, .81), (plausible, .79)):
        item["evidence"].update({
            "line_similarity": line, "exact_match_lines": 8,
            "function_identity_score": .8,
        })
    groups = SC.collect_review_pairs([unrelated, plausible], keep_tiers=("review",))
    for group in groups:
        ref_file = group["candidates"][0]["ref_file"]
        group["review_verdict"] = "非借鉴" if "unrelated" in ref_file else "疑似"
        group["review_reason"] = "逐对测试"

    up, dn = SC._apply_review_verdicts([unrelated, plausible], groups)

    assert up == 0 and dn == 1
    assert unrelated["tier"] == "dismissed"
    assert plausible["tier"] == "review"
    assert plausible["review_verdict"] == "疑似"
