"""src.report / src.pipeline 测试：语义对比报告 + 漏斗/resume。"""

from __future__ import annotations

from copy import deepcopy
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from src.pipeline.__main__ import (_finalize_comparison_output,
                                   _resolve_git_revision,
                                   _restore_semantic_cache)
from src.pipeline.steps import STEPS, build_local_meta, tier_counts
from src.report import semantic_compare as SC
from src.report.audit import audit_reports
from src.report.label_normalize import normalize_labels
from src.retrieval_contract import build_retrieval_contract


# ---------- 语义对比报告（M2：U1-U8 + 文件级） ----------


def test_finalize_preserves_and_restores_content_addressed_review_cache(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    repo_name = "target-repo"
    query_repo_id = "target-id"
    work_dir = out / f"{query_repo_id}_semantic_work"
    work_dir.mkdir()
    cache = work_dir / "review_judgment_test-model.json"
    cache.write_text(json.dumps({"code-key": {"verdict": "疑似"}}), encoding="utf-8")
    content_cache = work_dir / "cache" / "content-key.html"
    content_cache.parent.mkdir()
    content_cache.write_text("<section>cached</section>", encoding="utf-8")
    intermediate = out / "target_recall.json"
    intermediate.write_text("{}", encoding="utf-8")
    html = out / "target_comparison.html"
    html.write_text("<html></html>", encoding="utf-8")

    final_html = _finalize_comparison_output(
        out, repo_name, html, query_repo_id, intermediate)

    archived = out / repo_name / ".semantic_cache" / cache.name
    archived_content = out / repo_name / ".semantic_cache" / "cache" / content_cache.name
    assert final_html.exists() and archived.exists() and archived_content.exists()
    assert not intermediate.exists() and not work_dir.exists()

    restored = _restore_semantic_cache(out, repo_name, query_repo_id)

    assert restored == 2
    assert json.loads((work_dir / cache.name).read_text(encoding="utf-8"))["code-key"]["verdict"] == "疑似"
    assert (work_dir / "cache" / content_cache.name).read_text(encoding="utf-8") == "<section>cached</section>"


def test_report_generation_metadata_records_wall_time_elapsed_time_and_revision(tmp_path):
    placeholder = SC._generation_metadata_html({"generated_at": "old"})
    stamped = SC.stamp_generation_metadata(placeholder, {
        "started_at": "2026-07-30T10:00:00+08:00",
        "generated_at": "2026-07-30T10:02:03+08:00",
        "total_elapsed_sec": 123,
        "report_elapsed_sec": 23.5,
        "orchestration_overhead_sec": 19.5,
        "target_revision": "a" * 40,
        "stage_timings": {"recall": 80, "report": 23.5},
    })

    assert "2026-07-30T10:02:03+08:00" in stamped
    assert "2 分 3 秒（123.00s）" in stamped
    assert "报告组装 23.50 秒" in stamped
    assert "编排/等待及初始化耗时" in stamped
    assert "19.50 秒" in stamped
    assert "a" * 40 in stamped

    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="utf-8")
    assert _resolve_git_revision(tmp_path) == "b" * 40

def test_find_opencode_ignores_inaccessible_candidates(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    def denied(_path):
        raise PermissionError("restricted user directory")

    monkeypatch.setattr(Path, "exists", denied)
    assert SC._find_opencode() == "opencode"


def _sc_suspect(qfile, qfunc, repo, cfile, cfunc, tier, score, module="fs",
                exact=0, renamed=0, mtypes=None):
    return {
        "tier": tier, "final_score": score,
        "query_func": {"repo_id": "2024/new", "file_path": qfile, "func_name": qfunc,
                       "start_line": 10, "end_line": 40, "module_tag": module,
                       "raw_code": "fn x(){}", "lang": "rust"},
        "candidate_func": {"repo_id": repo, "file_path": cfile, "func_name": cfunc,
                           "start_line": 1, "end_line": 31, "module_tag": module,
                           "raw_code": "fn x(){}", "lang": "rust"},
        "evidence": {"exact_match_lines": exact, "renamed_match_lines": renamed},
        "match_type_per_span": mtypes or [],
    }


def test_pair_sim_fallback_when_final_score_zero():
    # D1 报告侧双保险：final_score=0 但有匹配行 → 用 匹配行/函数行 兜底
    s = _sc_suspect("a.rs", "f", "2021/x", "b.rs", "g", "review", 0.0, exact=31)
    assert SC._pair_sim(s) == 1.0
    # 无匹配行时仍为 0
    assert SC._pair_sim(_sc_suspect("a.rs", "f", "2021/x", "b.rs", "g", "weak", 0.0)) == 0.0


def test_exact_label_is_scoped_to_matched_fragment_and_shows_function_coverage():
    suspect = _sc_suspect(
        "a.rs", "f", "2021/x", "b.rs", "g", "confirmed", 0.8, exact=10)

    group = SC.collect_file_pairs([suspect])[0]
    summary = SC._clone_summary(group)

    assert group["clone_type"] == "exact"
    assert group["match_coverage"] == 0.323
    assert summary == "匹配片段完全相同 · 覆盖目标函数 32.3%（10/31 行）"
    assert "完全相同 · 覆盖目标函数 100" not in summary


def test_review_payload_requires_role_reason_review_reason_and_real_code_anchor():
    query = "fn alloc_block() { bitmap.alloc(); }"
    ref = "fn alloc_block() { bitmap.find_free(); }"
    valid = json.dumps({
        "responsibility": "一致",
        "responsibility_reason": "双方都从位图中分配空闲块",
        "verdict": "疑似",
        "reason": "调用对象一致但具体分配接口不同",
        "evidence_anchors": ["bitmap", "alloc_block"],
    }, ensure_ascii=False)

    parsed = SC._parse_review_payload(valid, query, ref)

    assert parsed["verdict"] == "疑似"
    assert parsed["responsibility"] == "一致"
    assert parsed["evidence_anchors"] == ["bitmap", "alloc_block"]


def test_review_text_truncation_is_marked_instead_of_leaving_half_sentence():
    value = "证据" * 100
    bounded = SC._bounded_review_text(value, 120)
    assert len(bounded) == 120
    assert bounded.endswith("…")
    assert SC._legacy_review_text_for_display("字" * 120, 120).endswith("…")


def test_review_payload_format_or_unverifiable_reason_fails_instead_of_becoming_suspect():
    base = {
        "responsibility": "一致",
        "responsibility_reason": "双方都更新页表项标志",
        "verdict": "疑似",
        "reason": "都调用 set_flags 修改权限位",
        "evidence_anchors": ["set_flags"],
    }
    query, ref = "fn set_flags() {}", "fn set_flags() {}"

    for payload in (
        "说明文字 " + json.dumps(base, ensure_ascii=False),
        json.dumps({**base, "reason": ""}, ensure_ascii=False),
        json.dumps({**base, "evidence_anchors": ["not_in_code"]}, ensure_ascii=False),
        json.dumps({**base, "responsibility": "不一致", "verdict": "疑似"}, ensure_ascii=False),
    ):
        try:
            SC._parse_review_payload(payload, query, ref)
        except ValueError:
            pass
        else:
            raise AssertionError("无效模型结果不得通过严格复核协议")


def test_review_one_labels_invalid_model_output_as_failure():
    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"verdict":"疑似","reason":""}'))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **_kwargs: response)))
    group = {
        "query_func": "f", "query_file": "a.rs", "query_code": "fn f() {}",
        "overall_sim": .8, "clone_type": "similar", "match_coverage": None,
        "candidates": [{"ref_func": "g", "ref_repo": "2021/x", "ref_file": "b.rs",
                        "ref_code": "fn g() {}"}],
    }

    result = SC._review_one(client, "test-model", group, 1)

    assert result["verdict"] == "复核失败"
    assert "输出格式或证据字段无效" in result["reason"]


def test_review_one_stops_after_role_mismatch_without_similarity_call():
    calls = []
    role_json = json.dumps({
        "responsibility": "不一致",
        "responsibility_reason": "左侧分配数据块，右侧分配索引节点",
        "evidence_anchors": ["alloc_block", "alloc_inode"],
    }, ensure_ascii=False)

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=role_json))])

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    group = {
        "query_func": "alloc_block", "query_file": "a.rs",
        "query_code": "fn alloc_block() { bitmap.alloc(); }",
        "overall_sim": .44, "clone_type": "similar", "match_coverage": None,
        "candidates": [{"ref_func": "alloc_inode", "ref_repo": "2025/x",
                        "ref_file": "b.rs", "ref_code": "fn alloc_inode() { inode.alloc(); }"}],
    }

    result = SC._review_one(client, "test-model", group, 1)

    assert len(calls) == 1
    assert result["responsibility"] == "不一致"
    assert result["verdict"] == "非借鉴"
    assert "候选配对不成立" in result["reason"]


def test_review_one_runs_similarity_only_after_role_gate_passes():
    outputs = iter([
        json.dumps({
            "responsibility": "一致",
            "responsibility_reason": "双方都修改页表项中的权限标志",
            "evidence_anchors": ["set_flags"],
        }, ensure_ascii=False),
        json.dumps({
            "verdict": "疑似",
            "reason": "都读取旧标志后合并新权限位",
            "evidence_anchors": ["set_flags", "flags"],
        }, ensure_ascii=False),
    ])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=next(outputs)))])

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    group = {
        "query_func": "set_flags", "query_file": "a.rs",
        "query_code": "fn set_flags() { flags |= new_flags; }",
        "overall_sim": .88, "clone_type": "near_dup", "match_coverage": .8,
        "matched_lines": 8, "query_lines": 10,
        "candidates": [{"ref_func": "set_flags", "ref_repo": "2025/x",
                        "ref_file": "b.rs", "ref_code": "fn set_flags() { flags |= bits; }"}],
    }

    result = SC._review_one(client, "test-model", group, 1)

    assert len(calls) == 2
    assert result["responsibility"] == "一致"
    assert result["verdict"] == "疑似"
    assert result["evidence_anchors"] == ["set_flags", "flags"]


def test_review_one_forces_json_mode_and_retries_invalid_role_output():
    outputs = iter([
        "先说明一下职责，再给 JSON",
        json.dumps({
            "responsibility": "一致",
            "responsibility_reason": "双方都修改页表项中的权限标志",
            "evidence_anchors": ["set_flags"],
        }, ensure_ascii=False),
        json.dumps({
            "verdict": "疑似",
            "reason": "双方都读取旧标志并合并新的权限位",
            "evidence_anchors": ["set_flags", "flags"],
        }, ensure_ascii=False),
    ])
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=next(outputs)))])

    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    group = {
        "query_func": "set_flags", "query_file": "a.rs",
        "query_code": "fn set_flags() { flags |= new_flags; }",
        "overall_sim": .88, "clone_type": "near_dup", "match_coverage": .8,
        "matched_lines": 8, "query_lines": 10,
        "candidates": [{"ref_func": "set_flags", "ref_repo": "2025/x",
                        "ref_file": "b.rs", "ref_code": "fn set_flags() { flags |= bits; }"}],
    }

    result = SC._review_one(client, "test-model", group, 1)

    assert len(calls) == 3
    assert all(call["response_format"] == {"type": "json_object"} for call in calls)
    assert "上次输出未通过机器校验" in calls[1]["messages"][-1]["content"]
    assert result["verdict"] == "疑似"


def test_review_judgment_submits_identical_code_pair_only_once(monkeypatch, tmp_path):
    from src.oskernel_agent import config as cfg

    query_code = "fn shared() { common_step(); }"
    ref_code = "fn shared() { common_step(); }"

    def group(repo, ref_file):
        return {
            "query_func": "shared", "query_file": "a.rs", "query_code": query_code,
            "overall_sim": 1.0, "clone_type": "exact", "match_coverage": 1.0,
            "candidates": [{
                "ref_func": "shared", "ref_repo": repo, "ref_file": ref_file,
                "ref_code": ref_code,
            }],
        }

    groups = [group("2021/source", "one.rs"), group("2022/mirror", "two.rs")]
    calls = []

    def fake_review(_client, _model, review_group, _timeout):
        calls.append(review_group)
        return {
            "responsibility": "一致",
            "responsibility_reason": "双方都执行 common_step",
            "verdict": "疑似",
            "reason": "共同实现包含相同的 common_step 调用",
            "evidence_anchors": ["common_step"],
        }

    monkeypatch.setitem(cfg.api, "key", "test-key")
    monkeypatch.setitem(cfg.api, "base_url", "https://example.invalid/v1")
    monkeypatch.setattr(SC, "_review_one", fake_review)

    SC.run_review_judgment(groups, tmp_path, model="test-model", workers=2)

    assert len(calls) == 1
    assert [g["review_verdict"] for g in groups] == ["疑似", "疑似"]


def test_positive_review_anchors_must_exist_on_both_sides():
    payload = json.dumps({
        "responsibility": "一致",
        "responsibility_reason": "双方都把输入记录转换为输出记录",
        "verdict": "疑似",
        "reason": "共享转换步骤，但仍需排除接口约束",
        "evidence_anchors": ["shared_step", "query_only_marker"],
    }, ensure_ascii=False)

    try:
        SC._parse_review_payload(
            payload,
            "fn transform() { shared_step(); query_only_marker(); }",
            "fn transform() { shared_step(); reference_only_marker(); }",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("正向同源结论不得使用只存在于单侧代码的锚点")


def test_identifier_anchor_must_match_whole_token_not_substring():
    payload = json.dumps({
        "responsibility": "一致",
        "responsibility_reason": "双方都处理资源页面",
        "verdict": "疑似",
        "reason": "候选锚点需要按完整标识符核验",
        "evidence_anchors": ["allocate", "page"],
    }, ensure_ascii=False)

    try:
        SC._parse_review_payload(
            payload,
            "fn allocate_page() { page.commit(); }",
            "fn deallocate_page() { page.release(); }",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("标识符子串巧合不得通过共同锚点校验")


def test_review_failure_is_separate_from_model_uncertain_stats():
    failed = _sc_suspect("a.rs", "f", "2021/x", "b.rs", "g", "review", .81)
    failed["review_verdict"] = "复核失败"
    failed["review_reason"] = "复核失败：输出格式无效"

    stats = SC.compute_submodule_stats([failed], None)["fs"]

    assert stats["review"] == 0
    assert stats["review_failed"] == 1
    assert stats["review_incomplete"] == 1
    assert stats["review_pct"] == 0.0

    group = SC.collect_file_pairs([failed], keep_tiers=("review", "weak"))[0]
    group.update({
        "review_verdict": "复核失败",
        "review_reason": "复核失败：输出格式或证据字段无效",
        "review_responsibility": "未判定",
        "review_responsibility_reason": "",
        "review_evidence_anchors": [],
    })
    _toc, section = SC._review_section([group], None, "2024/new")
    assert "复核失败（不计为存疑）" in section
    assert "模型有效复核后仍存疑（1 个函数）" not in section


def test_review_section_distinguishes_same_name_functions_at_different_lines():
    first = _sc_suspect("a.rs", "lookup", "2021/x", "b.rs", "lookup", "review", .81)
    second = _sc_suspect("a.rs", "lookup", "2021/y", "c.rs", "lookup", "review", .80)
    second["query_func"]["start_line"] = 80
    second["query_func"]["end_line"] = 100
    groups = SC.collect_file_pairs([first, second], keep_tiers=("review", "weak"))
    for group in groups:
        group.update({
            "review_verdict": "疑似",
            "review_reason": "共享了可核验的非平凡实现步骤",
            "review_responsibility": "一致",
            "review_responsibility_reason": "双方处理同一类目录查找",
            "review_evidence_anchors": ["lookup", "fn"],
        })

    _toc, section = SC._review_section(groups, None, "2024/new")

    assert '返回“疑似” <b>2</b> 个' in section


def test_review_section_summarizes_model_cleared_pairs_without_expanding_irrelevant_code():
    suspect = _sc_suspect(
        "os/task.rs", "run_tasks", "2025/history", "task.rs", "run_tasks",
        "review", .72, exact=8,
    )
    suspect["query_func"]["raw_code"] = "\n".join(f"target_{i}();" for i in range(10))
    suspect["candidate_func"]["raw_code"] = "\n".join(f"source_{i}();" for i in range(10))
    suspect["evidence"]["line_similarity"] = .72
    group = SC.collect_review_pairs([suspect])[0]
    group.update({
        "review_verdict": "非借鉴",
        "review_reason": "职责相近但关键调度状态维护机制不同",
        "review_responsibility": "部分一致",
        "review_responsibility_reason": "都处理调度循环",
        "review_evidence_anchors": ["fn"],
    })

    _toc, section = SC._review_section([], None, "2024/new", [group])

    assert "已被模型明确排除" in section
    assert "run_tasks" not in section
    assert "关键调度状态维护机制不同" not in section


def test_baseline_section_lists_every_excluded_function_for_audit():
    funcs = [
        {"name": f"baseline_{index}", "file": f"src/{index}.rs", "start": index,
         "source": "baseline", "note": "共同公共基线"}
        for index in range(55)
    ]
    funcs[-1]["name"] = "must_remain_searchable"

    _toc, section = SC._baseline_section(funcs, None, "2024/new")

    assert "must_remain_searchable" in section
    assert "列出前 50 个" not in section


def test_collect_file_pairs_groups_all_candidates():
    # 同一 query 函数命中两个来源 → 聚合为一个 group、两个候选
    suspects = [
        _sc_suspect("os/src/fs/inode.rs", "read", "2021/a", "i.rs", "read", "confirmed", 0.98, exact=30),
        _sc_suspect("os/src/fs/inode.rs", "read", "2022/b", "j.rs", "rd", "review", 0.82, renamed=20),
    ]
    groups = SC.collect_file_pairs(suspects)
    assert len(groups) == 1
    g = groups[0]
    assert g["candidate_count"] == 2
    assert g["overall_tier"] == "confirmed"      # 取最强档位
    assert g["overall_sim"] == 0.98


def test_candidate_summary_and_code_panel_show_reference_function_name():
    suspect = _sc_suspect(
        "os/src/fs/inode.rs", "read", "2021/a", "i.rs", "read_inode",
        "confirmed", 0.98, exact=30,
    )
    group = SC.collect_file_pairs([suspect])[0]

    cell = SC._candidates_cell(group, None)
    _toggle, panel = SC._code_evidence(group)

    assert "read_inode" in cell
    assert "read_inode" in panel


def test_collect_file_pairs_preserves_code_beyond_old_prefix_limit():
    suspect = _sc_suspect(
        "src/large.rs", "transform", "history/ref", "src/large.rs", "transform",
        "confirmed", 0.99, exact=30,
    )
    marker = "TAIL_CONTEXT_MUST_SURVIVE"
    suspect["query_func"]["raw_code"] = "x" * 900 + marker
    suspect["candidate_func"]["raw_code"] = "y" * 900 + marker

    group = SC.collect_file_pairs([suspect])[0]

    assert marker in group["query_code"]
    assert marker in group["candidates"][0]["ref_code"]


def test_group_explanation_uses_candidate_that_establishes_final_tier():
    review = _sc_suspect(
        "src/target.rs", "operation", "history/review", "src/review.rs", "operation",
        "review", 0.94, exact=20,
    )
    confirmed = _sc_suspect(
        "src/target.rs", "operation", "history/confirmed", "src/confirmed.rs", "operation",
        "confirmed", 0.72, exact=18,
    )

    group = SC.collect_file_pairs([review, confirmed])[0]

    assert group["overall_tier"] == "confirmed"
    assert group["candidates"][0]["ref_repo"] == "history/confirmed"
    assert group["overall_sim"] == 0.72


def test_long_review_context_retains_matched_region_and_function_tail():
    lines = [f"statement_{index}();" for index in range(240)]
    code = "\n".join(lines)

    compact = SC._compact_code_for_review(
        code, start_line=100, absolute_ranges=[(218, 222)], max_chars=1800,
    )

    assert "statement_120();" in compact
    assert "statement_239();" in compact
    assert "代码已省略" in compact


def test_review_evidence_gate_requires_multiple_code_signals_not_single_literal():
    vector_only = _sc_suspect(
        "src/a.rs", "operation", "history/a", "src/b.rs", "operation",
        "review", 0.91,
    )
    vector_only["evidence"].update({"line_similarity": 0.12, "vector_similarity": 0.98})
    literal_only = _sc_suspect(
        "src/c.rs", "operation", "history/b", "src/d.rs", "operation",
        "weak", 0.18,
    )
    literal_only["evidence"].update({
        "line_similarity": 0.18, "unique_string_matches": 1,
    })
    supported = _sc_suspect(
        "src/e.rs", "operation", "history/c", "src/f.rs", "operation",
        "weak", 0.64, exact=8,
    )
    supported["query_func"]["raw_code"] = "\n".join(f"q{i}();" for i in range(12))
    supported["candidate_func"]["raw_code"] = "\n".join(f"c{i}();" for i in range(11))
    supported["evidence"].update({
        "line_similarity": 0.64,
        "function_identity_score": 0.8,
        "segment_hits": {"hits": 2, "q_total": 4, "c_total": 4},
    })

    removed = SC._apply_review_evidence_gate([vector_only, literal_only, supported])

    assert removed == 2
    assert vector_only["tier"] == "dismissed"
    assert literal_only["tier"] == "dismissed"
    assert supported["tier"] == "weak"
    assert supported["review_evidence_basis"] == "multi_signal_code_similarity"


def test_review_evidence_gate_keeps_substantive_partial_copy_below_half_ratio():
    suspect = _sc_suspect(
        "src/expanded.rs", "operation", "history/base", "src/base.rs", "operation",
        "weak", 0.43, exact=12,
    )
    query_lines = [f"query_step_{index}();" for index in range(30)]
    candidate_lines = [f"candidate_step_{index}();" for index in range(20)]
    suspect["query_func"]["raw_code"] = "\n".join(query_lines)
    suspect["candidate_func"]["raw_code"] = "\n".join(candidate_lines)
    suspect["evidence"].update({
        "line_similarity": 0.4,
        "function_identity_score": 0.8,
        "segment_hits": {"hits": 3, "q_total": 5, "c_total": 5},
    })

    removed = SC._apply_review_evidence_gate([suspect])

    assert removed == 0
    assert suspect["tier"] == "weak"
    assert suspect["review_evidence_basis"] == "supported_substantive_partial_match"


def test_review_evidence_gate_keeps_large_identity_supported_partial_copy_without_segments():
    suspect = _sc_suspect(
        "src/expanded.rs", "operation", "history/base", "src/base.rs", "operation",
        "weak", 0.4146, exact=9, renamed=8,
    )
    suspect["query_func"]["raw_code"] = "\n".join(
        f"query_step_{index}();" for index in range(41)
    )
    suspect["candidate_func"]["raw_code"] = "\n".join(
        f"candidate_step_{index}();" for index in range(38)
    )
    suspect["evidence"].update({
        "line_similarity": 0.4146,
        "function_name_exact": True,
        "function_identity_score": 0.842,
        "function_identity_relation": "exact_counterpart",
    })

    removed = SC._apply_review_evidence_gate([suspect])

    assert removed == 0
    assert suspect["review_evidence_basis"] == "identity_supported_large_partial_match"
    assert SC._model_negative_requires_human_review(suspect, "一致") is True


def test_source_metrics_deduplicate_candidate_pairs_and_use_effective_loc():
    a = _sc_suspect("os/fs.rs", "read", "2023/ref", "a.rs", "read", "confirmed", .98,
                    exact=10)
    b = _sc_suspect("os/fs.rs", "read", "2023/ref", "b.rs", "read2", "confirmed", .96,
                    exact=7)
    c = _sc_suspect("os/mm.rs", "alloc", "2023/ref", "m.rs", "alloc", "confirmed", .97,
                    module="mm", exact=5)
    a["query_func"]["raw_code"] = b["query_func"]["raw_code"] = "\n".join(["x"] * 10)
    c["query_func"]["raw_code"] = "\n".join(["x"] * 5)

    metrics = SC._source_metrics([a, b, c])

    assert metrics == [{"repo": "2023/ref", "functions": 2, "effective_loc": 15,
                        "files": 2, "modules": 2, "multi_repo_functions": 0}]


def test_source_metrics_marks_non_unique_multi_repo_attribution():
    a = _sc_suspect("os/fs.rs", "read", "2023/a", "a.rs", "read", "confirmed", .98,
                    exact=10)
    b = _sc_suspect("os/fs.rs", "read", "2024/b", "b.rs", "read", "confirmed", .97,
                    exact=9)
    metrics = {item["repo"]: item for item in SC._source_metrics([a, b])}

    assert metrics["2023/a"]["functions"] == 1
    assert metrics["2023/a"]["multi_repo_functions"] == 1
    assert metrics["2024/b"]["multi_repo_functions"] == 1


def test_function_identity_key_distinguishes_same_name_in_same_file_by_line():
    first = _sc_suspect(
        "os/fs.rs", "lookup", "2023/ref", "a.rs", "lookup", "confirmed", .98,
        exact=8,
    )
    second = _sc_suspect(
        "os/fs.rs", "lookup", "2023/ref", "b.rs", "lookup", "confirmed", .97,
        exact=7,
    )
    second["query_func"]["start_line"] = 90
    second["query_func"]["end_line"] = 110

    metrics = SC._source_metrics([first, second])

    assert metrics[0]["functions"] == 2


def test_report_boundary_removes_explicit_baseline_from_history_sources():
    baseline = _sc_suspect(
        "os/mm.rs", "map", "data/repos/0/baseline_kernel", "mm.rs", "map",
        "confirmed", .9, exact=10,
    )
    history = _sc_suspect(
        "os/mm.rs", "map", "2025/team", "mm.rs", "map", "confirmed", .9,
        exact=9,
    )

    changed = SC._tag_query_level_baselines([baseline, history])

    assert changed == 2
    assert baseline["tier"] == history["tier"] == "baseline_derived"
    assert SC._source_metrics([baseline, history]) == []


def test_report_boundary_keeps_history_that_materially_exceeds_weak_baseline():
    baseline = _sc_suspect(
        "os/task.rs", "run_tasks", "data/repos/0/baseline_kernel",
        "task.rs", "run_tasks", "weak", .1951, exact=5, renamed=3,
    )
    baseline["evidence"]["line_similarity"] = .1951
    history = _sc_suspect(
        "os/task.rs", "run_tasks", "2025/team", "task.rs", "run_tasks",
        "weak", .4146, exact=9, renamed=8,
    )
    history["evidence"]["line_similarity"] = .4146

    changed = SC._tag_query_level_baselines([baseline, history])

    assert changed == 1
    assert baseline["tier"] == "baseline_derived"
    assert history["tier"] == "weak"
    assert baseline["evidence"]["baseline_source_substantive"] is False
    assert "baseline_incremental_evidence" not in history["evidence"]


def test_model_review_selection_skips_confirmed_and_defers_weaker_secondary():
    confirmed = _sc_suspect(
        "os/a.rs", "dispatch", "2025/a", "a.rs", "dispatch",
        "confirmed", .99, exact=20,
    )
    confirmed_secondary = _sc_suspect(
        "os/a.rs", "dispatch", "2024/b", "b.rs", "dispatch",
        "confirmed", .97, exact=18,
    )
    unresolved = []
    for idx, similarity in enumerate((.72, .70, .60), start=1):
        item = _sc_suspect(
            "os/b.rs", "run", f"202{idx}/team", f"b{idx}.rs", "run",
            "review" if idx < 3 else "weak", similarity, exact=12,
        )
        item["query_func"]["start_line"] = 100
        item["query_func"]["end_line"] = 114
        item["query_func"]["raw_code"] = "\n".join(
            f"target_step_{line}();" for line in range(15)
        )
        item["candidate_func"]["start_line"] = idx * 10
        item["candidate_func"]["raw_code"] = "\n".join(
            f"source_{idx}_step_{line}();" for line in range(15)
        )
        item["evidence"]["line_similarity"] = similarity
        item["evidence"]["function_identity_score"] = .8
        item["evidence"]["segment_hits"] = {
            "hits": 2, "q_total": 4, "c_total": 4,
        }
        unresolved.append(item)
    suspects = [confirmed, confirmed_secondary, *unresolved]

    selected, stats = SC.select_model_review_pairs(
        suspects, max_unresolved_candidates=2,
    )

    assert stats == {
        "targets": 1,
        "eligible_pairs": 3,
        "eligible_unique_content_pairs": 3,
        "selected_pairs": 2,
        "selected_source_pairs": 2,
        "selected_unique_content_pairs": 2,
        "deferred_secondary_pairs": 1,
        "max_unresolved_candidates": 2,
    }
    assert len(selected) == 2
    assert sum(g["query_func"] == "dispatch" for g in selected) == 0
    assert sum(g["query_func"] == "run" for g in selected) == 2
    assert confirmed["tier"] == confirmed_secondary["tier"] == "confirmed"
    assert unresolved[-1]["tier"] == "weak"
    assert unresolved[-1]["model_review_selection"] == "deferred_secondary"


def test_model_review_duplicate_mirrors_do_not_consume_distinct_candidate_budget():
    suspects = []
    for idx, code in enumerate(("fn run(){ shared(); }", "fn run(){ shared(); }",
                                "fn run(){ distinct(); }"), start=1):
        item = _sc_suspect(
            "os/run.rs", "run", f"202{idx}/team", f"r{idx}.rs", "run",
            "review", .8 - idx / 100, exact=8,
        )
        item["query_func"]["start_line"] = 100
        item["query_func"]["end_line"] = 114
        item["query_func"]["raw_code"] = "\n".join(
            f"target_{line}();" for line in range(15)
        )
        item["candidate_func"]["start_line"] = idx * 10
        item["candidate_func"]["raw_code"] = "\n".join([code] * 15)
        item["evidence"]["line_similarity"] = .8 - idx / 100
        item["evidence"]["function_identity_score"] = .8
        item["evidence"]["segment_hits"] = {
            "hits": 2, "q_total": 4, "c_total": 4,
        }
        suspects.append(item)

    selected, stats = SC.select_model_review_pairs(
        suspects, max_unresolved_candidates=2,
    )

    assert len(selected) == 2
    assert stats["selected_unique_content_pairs"] == 2
    assert stats["selected_source_pairs"] == 3
    assert stats["deferred_secondary_pairs"] == 0


def test_model_review_budget_does_not_change_evidence_tiers_or_originality():
    base = []
    for idx, similarity in enumerate((.78, .77, .76), start=1):
        item = _sc_suspect(
            "os/sched.rs", "run_tasks", f"202{idx}/team",
            f"sched{idx}.rs", "run_tasks", "review", similarity, exact=12,
        )
        item["query_func"].update({
            "start_line": 100, "end_line": 114,
            "raw_code": "\n".join(f"target_step_{line}();" for line in range(15)),
        })
        item["candidate_func"].update({
            "start_line": idx * 10,
            "raw_code": "\n".join(
                f"source_{idx}_step_{line}();" for line in range(15)
            ),
        })
        item["evidence"].update({
            "line_similarity": similarity,
            "function_identity_score": .8,
            "function_name_exact": True,
            "function_identity_relation": "exact_counterpart",
            "segment_hits": {"hits": 2, "q_total": 4, "c_total": 4},
        })
        base.append(item)

    one = deepcopy(base)
    all_candidates = deepcopy(base)
    selected_one, _ = SC.select_model_review_pairs(
        one, max_unresolved_candidates=1,
    )
    selected_all, _ = SC.select_model_review_pairs(
        all_candidates, max_unresolved_candidates=3,
    )

    assert len(selected_one) == 1
    assert len(selected_all) == 3
    assert [item["tier"] for item in one] == ["review"] * 3
    assert [item["tier"] for item in all_candidates] == ["review"] * 3
    assert SC.compute_submodule_stats(one, None) == SC.compute_submodule_stats(
        all_candidates, None,
    )
    recall = {"results": [{"query": deepcopy(base[0]["query_func"]), "candidates": []}]}
    assert SC._original_functions(recall, one) == []
    assert SC._original_functions(recall, all_candidates) == []


def test_strong_direct_evidence_survives_model_negative_and_keeps_secondary_source():
    candidates = []
    for idx, similarity in enumerate((.80, .70), start=1):
        item = _sc_suspect(
            "os/sched.rs", "run_tasks", f"202{idx}/team",
            f"sched{idx}.rs", "run_tasks", "review", similarity, exact=12,
        )
        item["query_func"].update({
            "start_line": 100, "end_line": 114,
            "raw_code": "\n".join(f"target_step_{line}();" for line in range(15)),
        })
        item["candidate_func"].update({
            "start_line": idx * 10,
            "raw_code": "\n".join(
                f"source_{idx}_step_{line}();" for line in range(15)
            ),
        })
        item["evidence"].update({
            "line_similarity": similarity,
            "function_identity_score": .8,
            "function_name_exact": True,
            "function_identity_relation": "exact_counterpart",
            "segment_hits": {"hits": 2, "q_total": 4, "c_total": 4},
        })
        candidates.append(item)

    selected, _ = SC.select_model_review_pairs(
        candidates, max_unresolved_candidates=1,
    )
    selected[0].update({
        "review_verdict": "非借鉴",
        "review_reason": "实现机制不同",
        "review_responsibility": "部分一致",
        "review_responsibility_reason": "只共享调度职责",
        "review_evidence_anchors": ["run_tasks"],
    })

    _up, down = SC._apply_review_verdicts(candidates, selected)

    assert down == 0
    assert candidates[0]["tier"] == "review"
    assert candidates[0]["review_verdict"] == "规则保留"
    assert candidates[0]["review_raw_verdict"] == "非借鉴"
    assert candidates[1]["tier"] == "review"
    assert candidates[1]["model_review_selection"] == "deferred_secondary"
    groups = SC.collect_file_pairs(candidates, keep_tiers=("review", "weak"))
    assert len(groups) == 1
    assert {item["ref_repo"] for item in groups[0]["candidates"]} == {
        "2021/team", "2022/team",
    }
    recall = {"results": [{
        "query": deepcopy(candidates[0]["query_func"]), "candidates": [],
    }]}
    assert SC._original_functions(recall, candidates) == []


def test_internal_arch_duplicate_is_included_in_exclusion_accounting():
    duplicate = _sc_suspect(
        "os/arch/riscv/trap.rs", "dispatch", "2025/a", "trap.rs", "dispatch",
        "confirmed", .99, exact=20,
    )
    duplicate["internal_arch_dup"] = "os/arch/common/trap.rs"

    totals = SC._exclusion_totals([duplicate])

    assert totals["false_positive"] == 1
    assert totals["total_excluded"] == 1


def test_exclusion_accounting_is_target_exclusive_and_includes_unmatched_libraries():
    valid = _sc_suspect(
        "os/task.rs", "run", "2025/a", "task.rs", "run",
        "confirmed", .9, exact=10,
    )
    same_target_upstream = _sc_suspect(
        "os/task.rs", "run", "2024/base", "task.rs", "run",
        "confirmed", .8, exact=8,
    )
    same_target_upstream["abi_constrained"] = True
    upstream_only = _sc_suspect(
        "os/abi.rs", "layout", "2024/base", "abi.rs", "layout",
        "confirmed", .8, exact=8,
    )
    upstream_only["query_func"]["start_line"] = 50
    upstream_only["abi_constrained"] = True
    recall = {"results": [
        {"query": valid["query_func"]},
        {"query": upstream_only["query_func"]},
        {"query": {
            "repo_id": "2024/new", "file_path": "vendor/spin/src/lib.rs",
            "start_line": 1, "func_name": "lock", "module_tag": "other",
        }},
    ]}

    totals = SC._exclusion_totals(
        [valid, same_target_upstream, upstream_only], recall,
    )

    assert totals["library"] == 1
    assert totals["upstream"] == 1
    assert totals["total_excluded"] == 2


def test_similarity_clusters_aggregate_functions_into_one_feature_event():
    suspects = [
        _sc_suspect("os/src/fs/inode.rs", "read_inode", "2023/ref", "inode.rs", "read_inode",
                    "confirmed", .98, exact=20),
        _sc_suspect("os/src/fs/inode_cache.rs", "write_inode", "2023/ref", "inode.rs", "write_inode",
                    "confirmed", .96, exact=18),
    ]
    clusters = SC.build_similarity_clusters(SC.collect_file_pairs(suspects))

    assert len(clusters) == 1
    assert clusters[0]["feature"] == "Inode 与目录项"
    assert clusters[0]["function_count"] == 2
    assert clusters[0]["source"] == "2023/ref"


def test_review_priority_is_explainable_and_favors_core_large_match():
    core = {"overall_sim": .91, "module": "mm", "query_code": "\n".join(["x"] * 100),
            "clone_type": "near_dup", "candidates": [{"ref_repo": "2023/a"}]}
    peripheral = {"overall_sim": .72, "module": "other", "query_code": "x",
                  "clone_type": "similar", "candidates": [{"ref_repo": "2023/a"}]}

    a, b = SC._review_priority(core), SC._review_priority(peripheral)

    assert a["score"] > b["score"]
    assert a["level"] == "高"
    assert any("核心子系统" in reason for reason in a["reasons"])


def test_review_groups_only_remove_the_same_function_location_already_confirmed():
    confirmed = [{"query_file": "os/fs.rs", "query_func": "read", "query_start": 10}]
    review = [
        {"query_file": "os/fs.rs", "query_func": "read", "query_start": 10},
        {"query_file": "os/fs.rs", "query_func": "read", "query_start": 80},
        {"query_file": "os/mm.rs", "query_func": "alloc", "query_start": 20},
        {"query_file": "os/mm.rs", "query_func": "alloc", "query_start": 120},
    ]
    kept = SC._exclude_confirmed_review_groups(review, confirmed)
    assert [
        (g["query_file"], g["query_start"], g["query_func"]) for g in kept
    ] == [
        ("os/fs.rs", 80, "read"),
        ("os/mm.rs", 20, "alloc"),
        ("os/mm.rs", 120, "alloc"),
    ]


def test_generate_comparison_html_has_m2_elements():
    suspects = [
        _sc_suspect("os/src/fs/inode.rs", "read", "2021/a", "i.rs", "read", "confirmed", 0.98,
                    exact=30, mtypes=["exact"]),
        _sc_suspect("os/src/sched/task.rs", "pick", "2021/a", "t.rs", "pick", "review", 0.82,
                    renamed=20, module="sched", mtypes=["renamed"]),
    ]
    suspects[1]["review_verdict"] = "疑似"
    suspects[1]["review_reason"] = "结构相似但上下文不足"
    recall = {"query_repo_id": "2024/new", "results": [
        {"query": {"file_path": "os/src/fs/inode.rs", "func_name": "read", "module_tag": "fs"}},
        {"query": {"file_path": "os/src/sched/task.rs", "func_name": "pick", "module_tag": "sched"}},
    ]}
    stats = SC.compute_submodule_stats(suspects, recall)
    groups = SC.collect_file_pairs(suspects)
    review_groups = SC.collect_file_pairs(suspects, keep_tiers=("review", "weak"))
    review_groups[0]["review_verdict"] = "疑似"
    review_groups[0]["review_reason"] = "结构相似但上下文不足"
    analysis = SC._fallback_analysis(groups, stats)
    file_matches = [{"query_file": "os/src/driver/uart.rs", "line_count": 88,
                     "matches": [{"repo_id": "2021/a", "file_path": "drv/uart.rs",
                                  "line_count": 88, "func_count": 4}]},
                    {"query_file": "user/src/bin/libctest/malloc.rs", "line_count": 40,
                     "matches": [{"repo_id": "2021/a", "file_path": "tests/malloc.rs",
                                  "line_count": 40, "func_count": 2}]}]
    contract = build_retrieval_contract({
        "complete": True, "configured": 167, "covered": 167,
        "missing_repo_ids": [],
    }, complete=True)
    html = SC.generate_comparison_html(
        "2024/new", suspects, stats, groups, analysis, original_funcs=[],
        review_pairs=review_groups, file_matches=file_matches, file_similar=[],
        retrieval_contract=contract)
    assert "报告导读（请先阅读）" in html              # 导读卡（面向老师的语境引导）
    assert "证据概览" in html                          # 中性、非自动扣分的概览
    assert "高置信同源功能簇" in html                  # 函数聚合为功能级同源事件
    assert 'id="sec-review"' in html
    assert "模型复核难例" in html                        # 只展示值得人工判断的难例
    assert "结构相似但上下文不足" in html
    assert "候选来源（全部）" in html                  # U6 全候选
    assert "匹配片段完全相同" in html
    assert "覆盖目标函数" in html
    assert 'id="sec-files"' in html                  # 文件级清单
    assert "libctest" not in html                    # 测试套件不进入评委证据
    assert "cdn.jsdelivr.net/npm/alpine" not in html # 断网时不依赖 Alpine 展开内容
    assert "cdn.tailwindcss.com" not in html          # 页面排版完全自带，不依赖 Tailwind
    assert "页面布局不依赖 Tailwind CDN" in html
    assert "chartAttempts<20" in html                # ECharts 加载失败有界重试
    assert "内核赛道评分边界" in html
    assert "查看官方评分说明" in html
    assert "整文件相同" in html
    assert "暂未检出相似（函数）" in html
    assert "相对参考实现的候选创新" in html
    assert 'data-retrieval-contract-version="4"' in html
    assert 'data-retrieval-complete="true"' in html
    assert "召回完整性已核验" not in html
    assert "历史作品覆盖 167/167" not in html
    assert "候选禁止静默截断" not in html
    assert "自研/原创（函数）" not in html
    assert "各模块高置信同源函数数" in html             # tier 分布图
    assert 'class="toc-card"' in html                  # 左侧目录统一卡片
    assert "评审结论" in html and "同源判断" in html and "合规复用" in html and "附录" in html
    assert "共同上游判断" in html
    assert "系统约定比较运行时取得的最新代码" in html
    assert "时间方向判断" not in html
    assert "版本证据" not in html
    assert 'class="report-section"' in html            # 正文统一章节外壳
    # 九段式结构固定且顺序稳定
    section_ids = ["summary", "sec-lineage", "sec-clusters", "sec-review", "sec-files",
                   "sec-innovation", "sec-compliance", "sec-aidetect", "sec-original",
                   "sec-technical"]
    positions = [html.index(f'id="{sid}"') for sid in section_ids]
    assert positions == sorted(positions)
    # 所有 echarts JSON 必须可解析（前端 JSON.parse 不能炸）
    import re
    for blob in re.findall(r'<script type="application/json">(.*?)</script>', html, re.DOTALL):
        json.loads(blob)


def test_innovation_candidates_bind_target_to_reference_code(tmp_path):
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.execute(
            "INSERT INTO functions VALUES (7, '2025/ref-os', 'kernel/sched.rs', 20, 30, "
            "'pick_next', 'sched', 'rust', 'fn pick_next(){ for t in tasks { run(t); } }')"
        )

    recall = {
        "results": [{
            "query": {
                "file_path": "kernel/mlfq.rs", "start_line": 10, "end_line": 42,
                "func_name": "pick_mlfq", "module_tag": "sched", "lang": "rust",
                "raw_code": "fn pick_mlfq(){ loop { if ready() { age(); break; } } }",
            },
            "candidates": [{
                "id": 7, "score": 0.42,
                "payload": {"repo_id": "2025/ref-os", "file_path": "kernel/sched.rs",
                            "start_line": 20, "end_line": 30, "func_name": "pick_next",
                            "module_tag": "sched", "is_baseline": False},
            }],
        }],
    }
    # 另一函数的有效命中只用来确定该模块主要参考 repo；pick_mlfq 本身未命中。
    suspects = [_sc_suspect(
        "kernel/base.rs", "schedule", "2025/ref-os", "kernel/sched.rs", "schedule",
        "confirmed", 0.95, module="sched", exact=20,
    )]

    candidates = SC.build_innovation_candidates(recall, suspects, functions_db_path=db)

    assert len(candidates) == 1
    assert candidates[0]["reference_repo"] == "2025/ref-os"
    assert candidates[0]["references"][0]["raw_code"].startswith("fn pick_next")
    assert candidates[0]["references"][0]["key"].startswith("r")


def test_innovation_candidates_ignore_cross_language_reference(tmp_path):
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.executemany(
            "INSERT INTO functions VALUES (?, ?, ?, 1, 10, ?, 'sched', ?, ?)",
            [
                (7, "2025/c-ref", "kernel/sched.c", "pick_next", "c", "int pick_next(void){return 0;}"),
                (8, "2025/rust-ref", "kernel/sched.rs", "pick_next", "rust", "fn pick_next(){}"),
            ],
        )
    recall = {"results": [{
        "query": {"file_path": "kernel/new.rs", "start_line": 1, "end_line": 20,
                  "func_name": "pick_new", "module_tag": "sched", "lang": "rust",
                  "raw_code": "fn pick_new(){ loop {} }"},
        "candidates": [
            {"id": 7, "score": 0.99, "payload": {"repo_id": "2025/c-ref", "file_path": "kernel/sched.c"}},
            {"id": 8, "score": 0.40, "payload": {"repo_id": "2025/rust-ref", "file_path": "kernel/sched.rs"}},
        ],
    }]}

    candidates = SC.build_innovation_candidates(recall, [], functions_db_path=db)

    assert candidates[0]["references"][0]["repo"] == "2025/rust-ref"
    assert candidates[0]["references"][0]["lang"] == "rust"


def test_innovation_map_rejects_invented_keys_and_adds_complexity():
    candidates = [{
        "key": "t0001", "module": "sched", "module_display": "进程调度",
        "reference_repo": "2025/ref-os", "file": "kernel/mlfq.rs", "start": 10,
        "end": 42, "func": "pick_mlfq", "lines": 33,
        "raw_code": "fn pick_mlfq(){ loop { if ready() && aging() { break; } } }",
        "references": [{
            "key": "r0001", "repo": "2025/ref-os", "file": "kernel/sched.rs",
            "start": 20, "end": 30, "func": "pick_next", "score": 0.42,
            "raw_code": "fn pick_next(){}",
        }],
    }]
    raw = {"innovations": [
        {
            "title": "多级反馈队列与老化",
            "kind": "机制改良",
            "baseline": "参考实现采用单队列轮转",
            "delta": "目标实现增加多级队列与老化路径",
            "why_it_matters": "缓解饥饿但增加状态维护成本",
            "confidence": "high",
            "target_keys": ["t0001"],
            "reference_keys": ["r0001"],
        },
        {
            "title": "模型编造条目", "baseline": "x", "delta": "y",
            "target_keys": ["t9999"], "reference_keys": [],
        },
    ]}

    points = SC._normalize_innovation_points(raw, candidates)

    assert len(points) == 1
    assert points[0]["targets"][0]["key"] == "t0001"
    assert points[0]["references"][0]["key"] == "r0001"
    assert points[0]["complexity"]["code_lines"] == 33
    assert points[0]["complexity"]["branch_points"] >= 3


def test_innovation_complexity_uses_full_function_beyond_model_excerpt(tmp_path):
    query_code = (
        "fn clone_process() {\n"
        + "    let padding = prepare();\n" * 180
        + "    if ready() { run(); }\n"
        + "    for task in tasks() { schedule(task); }\n"
        + "}\n"
    )
    assert query_code.index("if ready") > 1800
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.execute(
            "INSERT INTO functions VALUES (7, '2025/ref-os', 'kernel/process.rs', 1, 20, "
            "'clone_process', 'sched', 'rust', 'fn clone_process() {}')"
        )
    recall = {"results": [{
        "query": {"file_path": "kernel/process.rs", "start_line": 1, "end_line": 184,
                  "func_name": "clone_process", "module_tag": "sched", "lang": "rust",
                  "raw_code": query_code},
        "candidates": [{"id": 7, "score": 0.42,
                        "payload": {"repo_id": "2025/ref-os", "file_path": "kernel/process.rs"}}],
    }]}

    candidates = SC.build_innovation_candidates(recall, [], functions_db_path=db)
    complexity = SC._innovation_complexity(candidates)

    assert len(candidates[0]["raw_code"]) == len(query_code)
    assert len(candidates[0]["analysis_code"]) <= 12000
    assert complexity["branch_points"] >= 2


def test_generate_report_renders_innovation_code_map():
    from src.report.gitlab_links import GitLabLinker

    point = {
        "title": "多级反馈队列与老化", "kind": "机制改良", "confidence": "high",
        "reference_repo": "2025/ref-os", "baseline": "参考实现采用单队列轮转",
        "delta": "目标实现增加多级队列与老化路径", "why_it_matters": "缓解饥饿",
        "targets": [{"file": "kernel/mlfq.rs", "start": 10, "end": 42,
                     "func": "pick_mlfq", "lines": 33}],
        "references": [{"repo": "2025/ref-os", "file": "kernel/sched.rs",
                        "start": 20, "end": 30, "func": "pick_next", "score": 0.42}],
        "complexity": {"level": "中", "file_count": 1, "symbol_count": 1,
                       "code_lines": 33, "branch_points": 4},
    }
    linker = GitLabLinker(
        {"2025/ref-os": "https://gitlab.example.com/history/ref-os"}, {},
        query_repo_url="https://gitlab.example.com/current/new.git",
        query_sha="d" * 40,
    )
    linker.mark_query_repo("2026/new")
    html = SC.generate_comparison_html(
        "2026/new", [], SC.compute_submodule_stats([], None), [], "", [],
        innovation_points=[point],
        linker=linker,
    )

    assert 'id="sec-innovation"' in html
    assert "参考实现基线" in html and "本作品代码变化" in html
    assert "实现复杂度（静态估算）" in html
    assert "调用/引用入口" in html
    assert "测试 / Benchmark 证据" not in html
    assert "建议验证" not in html
    assert "限制与反证" in html
    assert "待人工确认" in html
    assert "kernel/mlfq.rs:10" in html
    assert "kernel/sched.rs:20" in html
    assert "https://gitlab.example.com/current/new/-/blob/" in html
    assert "https://gitlab.example.com/history/ref-os/-/blob/HEAD/" in html


def test_legacy_original_section_is_migrated_to_non_claiming_language():
    old = (
        '<a class="toc-link" href="#sec-original">原创代码</a>'
        '<h2 class="x">原创代码</h2>'
        '<p>共 <b>12</b> 个函数未与历史代码库构成借鉴（完全未命中，'
        '或虽有中等相似命中但经 AI 模型复核判为疑似 / 非借鉴、即独立实现的通用写法），'
        '从设计维度看属于该作品的原创 / 自研实现（按规模降序，全部列出）：</p>'
        '<span>自研/原创（函数）</span>'
    )
    new = normalize_labels(old)
    assert "暂未检出历史相似（不等于原创）" in new
    assert "不等于原创认定" in new
    assert "暂未检出相似（函数）" in new
    assert normalize_labels(new) == new


def test_legacy_review_label_is_migrated_to_model_uncertain_idempotently():
    old = '<span>疑似借鉴（待复核）（函数）</span><span>待复核</span>'
    new = normalize_labels(old)
    assert "模型复核后仍存疑（函数）" in new
    assert "待复核" not in new
    assert normalize_labels(new) == new


def test_legacy_high_suspicion_label_is_migrated_to_high_confidence_lineage():
    old = '<span>高度疑似借鉴（函数）</span><span>高度疑似占纳入统计函数</span>'
    new = normalize_labels(old)
    assert "高置信同源代码（函数）" in new
    assert "高置信同源占纳入统计函数" in new
    assert "高度疑似" not in new
    assert normalize_labels(new) == new


def test_full_legacy_html_is_marked_stale_idempotently():
    old = "<html><body><h2>原创代码</h2></body></html>"
    new = normalize_labels(old)
    assert 'data-retrieval-complete="false"' in new
    assert "必须按完整召回链重跑" in new
    assert normalize_labels(new) == new


def test_low_level_report_without_contract_keeps_hidden_stale_audit_marker():
    html = SC.generate_comparison_html(
        "2024/new", [], SC.compute_submodule_stats([], None), [], "", [])
    assert 'data-retrieval-complete="false"' in html
    assert 'id="retrieval-stale" hidden' in html
    assert "已失效，必须重跑" not in html


def test_report_audit_distinguishes_complete_stale_and_unmarked(tmp_path):
    for name, body in (
        ("complete", '<div data-retrieval-contract-version="4" data-retrieval-complete="true"></div>'),
        ("stale", '<div data-retrieval-contract-version="missing" data-retrieval-complete="false"></div>'),
        ("unmarked", "<html></html>"),
    ):
        d = tmp_path / name
        d.mkdir()
        (d / "comparison.html").write_text(body, encoding="utf-8")
    result = audit_reports(tmp_path)
    assert result["comparison_reports"] == 3
    assert result["complete_reports"] == 1
    assert result["stale_reports"] == 1
    assert result["unmarked_reports"] == ["unmarked/comparison.html"]


def test_report_audit_finds_pipeline_named_comparison_report(tmp_path):
    report_dir = tmp_path / "T2026-demo"
    report_dir.mkdir()
    (report_dir / "T2026-demo_comparison.html").write_text(
        '<div data-retrieval-contract-version="4" data-retrieval-complete="true"></div>',
        encoding="utf-8",
    )

    result = audit_reports(tmp_path)

    assert result["comparison_reports"] == 1
    assert result["complete_reports"] == 1


# ---------- pipeline 辅助 ----------

def test_build_local_meta(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    def g(*a): subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True)
    g("init", "-q"); g("config", "user.email", "t@t.com"); g("config", "user.name", "t")
    (repo / "f.rs").write_text("a\nb\nc\n", encoding="utf-8")
    g("add", "f.rs"); g("commit", "-q", "-m", "init kernel")
    commits = build_local_meta(repo)
    assert len(commits) == 1
    assert commits[0]["additions"] == 3 and commits[0]["message"] == "init kernel"
    assert (repo / "_meta.json").exists()


def test_resume_step_order():
    assert STEPS.index("recall") < STEPS.index("report")
    assert tier_counts([{"tier": "review"}, {"tier": "review"}, {"tier": "weak"}]) == {"review": 2, "weak": 1}
