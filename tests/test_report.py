"""oskernel_agent.comparison.report / oskernel_agent.comparison.pipeline 测试：语义对比报告 + 漏斗/resume。"""

from __future__ import annotations

from copy import deepcopy
import json
import pytest
import sqlite3
from types import SimpleNamespace

from oskernel_agent.comparison.pipeline.__main__ import (_finalize_comparison_output,
                                   _resolve_git_revision,
                                   _restore_semantic_cache)
from oskernel_agent.comparison.pipeline.steps import STEPS, tier_counts
from oskernel_agent.comparison.report import semantic_compare as SC
from oskernel_agent.comparison.report.audit import audit_reports
from oskernel_agent.comparison.report.label_normalize import normalize_labels
from oskernel_agent.comparison.retrieval_contract import build_retrieval_contract
from oskernel_agent.report_quality import IncompleteReportError


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
    ai_result = out / "target-repo_ai_detect.json"
    ai_result.write_text('{"status":"ok"}', encoding="utf-8")
    html = out / "target_comparison.html"
    html.write_text("<html></html>", encoding="utf-8")

    final_html = _finalize_comparison_output(
        out, repo_name, html, query_repo_id, intermediate, ai_result,
        preserve_paths=(ai_result,))

    archived = out / repo_name / ".semantic_cache" / cache.name
    archived_content = out / repo_name / ".semantic_cache" / "cache" / content_cache.name
    assert final_html.exists() and archived.exists() and archived_content.exists()
    assert (out / repo_name / ai_result.name).read_text(encoding="utf-8") == '{"status":"ok"}'
    assert not intermediate.exists() and not work_dir.exists()
    assert not ai_result.exists()

    restored = _restore_semantic_cache(out, repo_name, query_repo_id)

    assert restored == 2
    assert json.loads((work_dir / cache.name).read_text(encoding="utf-8"))["code-key"]["verdict"] == "疑似"
    assert (work_dir / "cache" / content_cache.name).read_text(encoding="utf-8") == "<section>cached</section>"


def test_resolve_git_revision_reads_git_metadata_without_running_git(tmp_path):
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("b" * 40 + "\n", encoding="utf-8")
    assert _resolve_git_revision(tmp_path) == "b" * 40


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


def _actual_ai_model_result() -> dict:
    """最小但完整的实际模型产物，供低层 HTML 组装测试使用。"""
    return {
        "status": "ok",
        "model_id": "test/code-model",
        "scope": {
            "extracted_functions": 1,
            "borrowed_excluded": 0,
            "third_party_excluded": 0,
            "eligible_functions": 1,
            "analyzed_functions": 1,
            "truncated": False,
        },
        "aggregated": {
            "overall": {
                "total_functions": 1,
                "llm_count": 0,
                "human_count": 1,
                "uncertain_count": 0,
                "llm_ratio_by_count": 0.0,
                "llm_ratio_by_loc": 0.0,
            },
            "suspicious_functions": [],
        },
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


def test_review_text_rejects_truncation_instead_of_leaving_half_sentence():
    value = "证据" * 100
    with pytest.raises(ValueError, match="超过 120 字"):
        SC._bounded_review_text(value, 120)
    with pytest.raises(ValueError, match="省略号"):
        SC._bounded_review_text("依据尚未说明完整…", 120)
    with pytest.raises(IncompleteReportError, match="疑似在长度上限处截断"):
        SC._legacy_review_text_for_display("字" * 120, 120)


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
    from oskernel_agent import config as cfg

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
    assert "中间代码未纳入模型输入" in compact
    assert "…" not in compact and "..." not in compact


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


def test_library_classification_takes_priority_over_stale_baseline_tier():
    library = _sc_suspect(
        "os/src/fs/ext4_lw/inode.rs", "as_type",
        "data/repos/0/baseline_kernel", "fs/ext4.rs", "from_type",
        "baseline_derived", .9, exact=10,
    )
    library["reuse_library"] = "lwext4"
    library["evidence"]["baseline_query_scope"] = True

    assert SC._tag_query_level_baselines([library]) == 0
    assert SC.baseline_stats([library]) == []


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


def test_review_group_prefers_completed_candidate_over_deferred_secondary():
    deferred = _sc_suspect(
        "os/mm.rs", "from_elf", "2025/deferred", "mm.rs", "from_elf",
        "review", .95, exact=20,
    )
    deferred["model_review_selection"] = "deferred_secondary"
    deferred["model_review_note"] = "未进入本轮模型预算"
    reviewed = _sc_suspect(
        "os/mm.rs", "from_elf", "2024/reviewed", "mm.rs", "from_elf",
        "review", .80, exact=18,
    )
    reviewed.update({
        "model_review_selection": "selected",
        "review_verdict": "借鉴",
        "review_reason": "共享非必要的装载步骤与常量",
        "review_responsibility": "一致",
        "review_responsibility_reason": "都构造同一类用户地址空间",
        "review_evidence_anchors": ["from_elf", "fn"],
    })

    group = SC.collect_file_pairs(
        [deferred, reviewed], keep_tiers=("review", "weak"))[0]
    resolution = SC.finalize_secondary_review_candidates([deferred, reviewed])
    # 新分类器会纠正测试夹具中与 os/mm.rs 路径冲突的旧 fs 标签。
    stats = SC.compute_submodule_stats([deferred, reviewed], None)["mm"]
    _toc, section = SC._review_section([group], None, "2024/new")

    assert group["review_verdict"] == "借鉴"
    assert group["candidates"][0]["ref_repo"] == "2024/reviewed"
    assert group["overall_sim"] == .80
    assert resolution == {"supplemental": 1, "dismissed": 0}
    assert deferred["model_review_selection"] == "supplemental_source"
    assert stats["review"] == 1
    assert stats["review_pending"] == 0
    assert "模型认为借鉴（仍需人工确认）" in section
    assert "次级候选未送模型（人工核验）" not in section


def test_weak_deferred_secondary_is_removed_after_primary_is_cleared():
    cleared = _sc_suspect(
        "os/mm.rs", "map_page", "2025/cleared", "mm.rs", "map_page",
        "dismissed", .90, exact=18,
    )
    cleared["dismiss_reason"] = "review_非借鉴"
    cleared["review_verdict"] = "非借鉴"
    deferred = _sc_suspect(
        "os/mm.rs", "map_page", "2024/deferred", "mm.rs", "map_page",
        "review", .78, exact=15,
    )
    deferred["model_review_selection"] = "deferred_secondary"
    deferred["model_review_note"] = "未进入本轮模型预算"

    resolution = SC.finalize_secondary_review_candidates([cleared, deferred])

    assert resolution == {"supplemental": 0, "dismissed": 1}
    assert deferred["tier"] == "dismissed"
    assert deferred["dismiss_reason"] == "review_secondary_after_primary_cleared"
    assert SC.collect_file_pairs(
        [cleared, deferred], keep_tiers=("review", "weak")) == []


def test_independent_strong_secondary_is_selected_for_bounded_fallback_review():
    cleared = _sc_suspect(
        "os/mm.rs", "map_page", "2025/cleared", "mm.rs", "map_page",
        "dismissed", .92, exact=9,
    )
    cleared["dismiss_reason"] = "review_非借鉴"
    cleared["review_verdict"] = "非借鉴"
    deferred = _sc_suspect(
        "os/mm.rs", "map_page", "2024/deferred", "mm.rs", "map_page",
        "review", .90, exact=9,
    )
    deferred["query_func"]["raw_code"] = "\n".join(
        f"target_step_{line}();" for line in range(10)
    )
    deferred["query_func"]["end_line"] = 19
    deferred["candidate_func"]["end_line"] = 10
    deferred["candidate_func"]["raw_code"] = "\n".join(
        f"source_step_{line}();" for line in range(10)
    )
    deferred["model_review_selection"] = "deferred_secondary"
    deferred["evidence"].update({
        "line_similarity": .90,
        "function_identity_score": .90,
        "function_identity_relation": "exact_counterpart",
        "function_name_exact": True,
    })

    selected = SC.select_exceptional_secondary_review_pairs([cleared, deferred])

    assert len(selected) == 1
    assert selected[0]["query_func"] == "map_page"
    assert deferred["model_review_selection"] == "selected_secondary"


def test_review_judgment_hydrates_cached_deferred_pairs_without_model_calls(
        monkeypatch, tmp_path):
    from oskernel_agent import config as cfg

    query_code = "fn shared() { common(); target_step(); }"

    def group(repo, ref_code):
        return {
            "query_func": "shared", "query_file": "a.rs", "query_code": query_code,
            "query_repo": "2026/new", "query_start": 10,
            "overall_sim": .8, "clone_type": "renamed", "match_coverage": .7,
            "candidates": [{
                "ref_func": "shared", "ref_repo": repo, "ref_file": f"{repo}.rs",
                "ref_start": 20, "ref_code": ref_code,
            }],
        }

    selected = group("2025/selected", "fn shared() { common(); selected_step(); }")
    deferred = group("2024/deferred", "fn shared() { common(); deferred_step(); }")
    model = "test-model"

    def key(item):
        candidate = item["candidates"][0]
        return SC._cache_key(
            SC._REVIEW_PROMPT_VERSION, model, item["query_func"],
            candidate["ref_func"], item["query_code"], candidate["ref_code"],
        )

    def cached(verdict, reason):
        return {
            "responsibility": "一致",
            "responsibility_reason": "都执行 common 步骤",
            "verdict": verdict,
            "reason": reason,
            "evidence_anchors": ["shared", "common"],
        }

    cache = {
        key(selected): cached("疑似", "实现共享 common 步骤"),
        key(deferred): cached("借鉴", "实现共享 common 步骤及非必要结构"),
    }
    (tmp_path / f"review_judgment_{model}.json").write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setitem(cfg.api, "key", "test-key")
    monkeypatch.setitem(cfg.api, "base_url", "https://example.invalid/v1")
    monkeypatch.setattr(
        SC, "_review_one",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("有效缓存不得再次调用模型")),
    )

    resolved = SC.run_review_judgment(
        [selected], tmp_path, model=model, cache_lookup_pairs=[selected, deferred])

    assert len(resolved) == 2
    assert selected["review_verdict"] == "疑似"
    assert deferred["review_verdict"] == "借鉴"


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


def test_report_refines_legacy_other_modules_without_database_rebuild():
    suspects = [
        _sc_suspect("os/src/signal/types.rs", "default_op", "2024/ref",
                    "os/src/signal/types.rs", "default_op", "confirmed", .98,
                    module="other", exact=20),
        _sc_suspect("user/src/lib.rs", "get_time", "2024/ref",
                    "user/src/lib.rs", "get_time", "confirmed", .97,
                    module="other", exact=10),
    ]
    suspects[0]["query_func"]["raw_code"] = "fn default_op(){ SigSet::SIGHUP; SigSet::SIGKILL; }"
    suspects[1]["query_func"]["raw_code"] = "fn get_time(){ let x: TimeVal; gettimeofday(); }"

    changed = SC._refine_report_modules(suspects)
    groups = SC.collect_file_pairs(suspects)

    assert changed >= 2
    assert {group["module"] for group in groups} == {"signal", "time"}


def test_report_can_split_old_explicit_module_into_new_subsystem():
    record = {
        "file_path": "kernel/src/syscall/fs.rs", "func_name": "dispatch",
        "module_tag": "fs", "lang": "rust", "raw_code": "fn dispatch() {}",
    }
    assert SC._module_for_record(record) == "syscall"


def test_unknown_functions_do_not_collapse_into_one_other_cluster():
    suspects = [
        _sc_suspect("os/src/misc/a.rs", "alpha_helper", "2024/ref",
                    "misc/a.rs", "alpha_helper", "confirmed", .98,
                    module="other", exact=20),
        _sc_suspect("os/src/misc/b.rs", "beta_helper", "2024/ref",
                    "misc/b.rs", "beta_helper", "confirmed", .97,
                    module="other", exact=20),
    ]

    clusters = SC.build_similarity_clusters(SC.collect_file_pairs(suspects))

    assert len(clusters) == 2
    assert len({cluster["feature_key"] for cluster in clusters}) == 2
    assert all(cluster["function_count"] == 1 for cluster in clusters)


def test_every_function_cluster_renders_its_own_semantic_explanation():
    suspects = [
        _sc_suspect("os/src/fs/inode.rs", "read_inode", "2023/ref", "inode.rs",
                    "read_inode", "confirmed", .98, exact=20),
        _sc_suspect("os/src/fs/vfs.rs", "mount_vfs", "2023/ref", "vfs.rs",
                    "mount_vfs", "confirmed", .96, exact=18),
    ]
    groups = SC.collect_file_pairs(suspects)
    clusters = SC.build_similarity_clusters(groups)
    assert len(clusters) == 2
    analyses = []
    for cluster in clusters:
        analyses.append(
            f'<section data-cluster="{cluster["analysis_id"]}" '
            f'data-module="{cluster["module"]}"><h3>{cluster["feature"]}</h3>'
            f'<p>{cluster["feature"]}采用独立的功能簇分析正文。</p>'
            f'<ul><li>{cluster["analysis_id"]} 的具体语义证据。</li></ul></section>'
        )

    _toc, section = SC._cluster_section(groups, "".join(analyses), None, "2026/new")

    assert section.count("功能簇语义说明") == 2
    assert all(cluster["analysis_id"] in section for cluster in clusters)
    assert "Inode 与目录项采用独立的功能簇分析正文" in section
    assert "虚拟文件系统采用独立的功能簇分析正文" in section


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
    analysis = (
        '<section data-module="fs"><h3>文件系统语义分析</h3>'
        '<p><code>read</code> 的读取职责与历史实现一致，控制流和错误处理形成直接同源证据。</p>'
        '<ul><li>目标函数与来源函数共享完整的读取步骤。</li></ul></section>'
    )
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
        retrieval_contract=contract, ai_detect_data=_actual_ai_model_result())
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
    assert "整体结果分布（按全部解析函数，含复用库与基线衍生）" in html
    assert "占全部解析函数比例" in html
    assert "复用库（第三方库）" in html
    assert "基线衍生" in html
    assert 'class="toc-card"' in html                  # 左侧目录统一卡片
    assert "评审结论" in html and "同源判断" in html and "合规复用" in html and "附录" in html
    assert "共同上游判断" in html
    assert "系统约定比较运行时取得的最新代码" in html
    assert "时间方向判断" not in html
    assert "版本证据" not in html
    assert 'class="report-section"' in html            # 正文统一章节外壳
    # 九段式结构固定且顺序稳定
    section_ids = ["summary", "sec-lineage", "sec-clusters", "sec-review", "sec-files",
                   "sec-innovation", "sec-compliance", "sec-aidetect", "sec-original"]
    positions = [html.index(f'id="{sid}"') for sid in section_ids]
    assert positions == sorted(positions)
    assert 'id="sec-technical"' not in html
    assert "运行信息与统计口径" not in html
    # 所有 echarts JSON 必须可解析（前端 JSON.parse 不能炸）
    import re
    for blob in re.findall(r'<script type="application/json">(.*?)</script>', html, re.DOTALL):
        json.loads(blob)


def test_overall_distribution_separates_reuse_baseline_and_unmatched_ratios():
    rows = SC._overall_distribution_rows({
        "confirmed": 4,
        "review": 1,
        "review_incomplete": 1,
        "original": 5,
        "library": 3,
        "baseline": 2,
        "upstream": 1,
        "false_positive": 0,
        "common": 1,
    }, total=20)
    by_key = {row["key"]: row for row in rows}

    assert by_key["original"]["pct"] == 25.0
    assert by_key["library"]["pct"] == 15.0
    assert by_key["baseline"]["pct"] == 10.0
    assert by_key["unclassified"] == {
        "key": "unclassified", "label": "其他未归类", "color": "#cbd5e1",
        "count": 2, "pct": 10.0,
    }
    assert sum(row["count"] for row in rows) == 20

    table = SC._overall_distribution_table(rows, 20)
    donut = SC._echarts_overall_donut(rows)
    assert "暂未检出相似" in table and "25.0%" in table
    assert "复用库（第三方库）" in table and "15.0%" in table
    assert "基线衍生" in table and "10.0%" in table
    assert '"value": 3, "name": "复用库（第三方库）"' in donut
    assert '"value": 2, "name": "基线衍生"' in donut


def test_ai_detect_section_rejects_model_failure_instead_of_rendering_placeholder():
    with pytest.raises(RuntimeError, match="参考模型加载失败"):
        SC._ai_detect_section(
            {"status": "skipped", "reason": "参考模型加载失败"}, None, "2026/new")


def test_ai_detect_section_accepts_verified_not_applicable_scope():
    toc, section = SC._ai_detect_section({
        "status": "skipped",
        "reason": "排除借鉴代码后无可归属函数",
        "scope": {
            "extracted_functions": 8,
            "borrowed_excluded": 8,
            "third_party_excluded": 0,
            "eligible_functions": 0,
            "analyzed_functions": 0,
        },
    }, None, "2026/new")

    assert "不适用" in toc
    assert "符合归属口径的函数为 <b>0</b>" in section
    assert "模型检测未完成" not in section


def test_ai_detect_section_renders_actual_model_result():
    ai_data = {
        "status": "ok",
        "model_id": "bigcode/starcoder2-3b",
        "scope": {
            "third_party_excluded": 3,
            "eligible_functions": 4,
            "analyzed_functions": 2,
            "truncated": True,
        },
        "aggregated": {
            "overall": {
                "total_functions": 2,
                "llm_count": 1,
                "human_count": 1,
                "uncertain_count": 0,
                "llm_ratio_by_count": .5,
                "llm_ratio_by_loc": .4,
            },
            "suspicious_functions": [{
                "file_path": "os/src/main.rs", "start_line": 10, "end_line": 40,
                "function_name": "generated", "loc": 31, "confidence": .91,
                "log_rank": .2, "stage": "fast_filter",
            }],
        },
    }

    toc, section = SC._ai_detect_section(ai_data, None, "2026/new")

    assert "AI 代码检测" in toc
    assert "bigcode/starcoder2-3b" in section
    assert "generated" in section
    assert "疑似 AI 生成（函数）" in section
    assert "排除 <b>3</b> 个已识别第三方复用库函数" in section
    assert "固定检测配额" in section
    assert "可判定函数中的疑似 AI 占比" in section
    assert "披露材料" not in section


def test_original_section_lists_every_unmatched_function_without_original_claim():
    functions = [
        {"module": "mm", "file": "os/mm.rs", "start": 20, "end": 34,
         "func": "map_private", "lines": 15, "max_sim": .2},
        {"module": "fs", "file": "os/fs.rs", "start": 8, "end": 17,
         "func": "open_special", "lines": 10, "max_sim": .1},
    ]

    toc, section = SC._original_section(functions, None, "2026/new")

    assert "os/mm.rs:20-34" in section and "map_private" in section
    assert "os/fs.rs:8-17" in section and "open_special" in section
    assert section.count("<tr>") == 3  # 表头 + 全部两个函数
    assert "完整列出这些函数" in section
    assert "不等于原创认定" in section
    assert ">2<" in toc


def test_innovation_candidates_bind_target_to_reference_code(tmp_path):
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.execute(
            "INSERT INTO functions VALUES (7, '2025/ref-os', 'kernel/sched.rs', 20, 30, "
            "'pick_next', 'sched', 'rust', "
            "'fn pick_next(){ for t in tasks() { if ready(t) { age(t); run(t); } } }')"
        )

    recall = {
        "results": [{
            "query": {
                "file_path": "kernel/mlfq.rs", "start_line": 10, "end_line": 42,
                "func_name": "pick_mlfq", "module_tag": "sched", "lang": "rust",
                "raw_code": "fn pick_mlfq(){ for t in tasks() { if ready(t) { age(t); run(t); } } }",
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
                (8, "2025/rust-ref", "kernel/sched.rs", "pick_next", "rust",
                 "fn pick_next(){ for t in tasks() { run(t); } }"),
            ],
        )
    recall = {"results": [{
        "query": {"file_path": "kernel/new.rs", "start_line": 1, "end_line": 20,
                  "func_name": "pick_next", "module_tag": "sched", "lang": "rust",
                  "raw_code": "fn pick_next(){ for t in tasks() { run(t); } }"},
        "candidates": [
            {"id": 7, "score": 0.99, "payload": {"repo_id": "2025/c-ref", "file_path": "kernel/sched.c"}},
            {"id": 8, "score": 0.40, "payload": {"repo_id": "2025/rust-ref", "file_path": "kernel/sched.rs"}},
        ],
    }]}

    candidates = SC.build_innovation_candidates(recall, [], functions_db_path=db)

    assert candidates[0]["references"][0]["repo"] == "2025/rust-ref"
    assert candidates[0]["references"][0]["lang"] == "rust"


def test_innovation_candidates_skip_higher_scored_family_neighbor(tmp_path):
    """向量分更高的同功能族邻居不能压过职责可对应的具体参考函数。"""
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.executemany(
            "INSERT INTO functions VALUES (?, '2025/ref-os', ?, 1, 40, ?, 'sched', 'rust', ?)",
            [
                (7, "kernel/legacy_task.rs", "clone_task",
                 "fn clone_task(flags: Flags) { legacy_queue(); allocate_task(); publish_tid(); }"),
                (8, "kernel/process.rs", "clone_process",
                 "fn clone_process(flags: Flags) { copy_address_space(); register_child(); }"),
            ],
        )
    recall = {"results": [{
        "query": {
            "file_path": "kernel/process.rs", "start_line": 1, "end_line": 40,
            "func_name": "clone_process", "module_tag": "sched", "lang": "rust",
            "raw_code": (
                "fn clone_process(flags: Flags) { meta_lock(); inner_lock(); "
                "copy_address_space(); register_child(); }"
            ),
        },
        "candidates": [
            {"id": 7, "score": 0.82, "payload": {"repo_id": "2025/ref-os"}},
            {"id": 8, "score": 0.55, "payload": {"repo_id": "2025/ref-os"}},
        ],
    }]}

    candidates = SC.build_innovation_candidates(recall, [], functions_db_path=db)

    assert len(candidates) == 1
    assert candidates[0]["references"][0]["func"] == "clone_process"
    assert candidates[0]["references"][0]["score"] == 0.55


def test_innovation_candidates_target_primary_repo_when_global_topk_misses_it(tmp_path):
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.executemany(
            "INSERT INTO functions VALUES (?, ?, 'kernel/process.rs', 1, 30, "
            "'clone_process', 'sched', 'rust', ?)",
            [
                (7, "2025/secondary", "fn clone_process(){ copy_vm(); register_child(); }"),
                (8, "2025/primary", "fn clone_process(){ copy_vm(); register_child(); audit(); }"),
            ],
        )
    recall = {"results": [{
        "query": {
            "file_path": "kernel/process.rs", "start_line": 1, "end_line": 30,
            "func_name": "clone_process", "module_tag": "sched", "lang": "rust",
            "raw_code": "fn clone_process(){ copy_vm(); register_child(); secure(); }",
        },
        # 全库 Top-K 只有 secondary；primary 必须由定向仓库检索补回。
        "candidates": [{
            "id": 7, "score": 0.91,
            "payload": {"repo_id": "2025/secondary", "file_path": "kernel/process.rs"},
        }],
    }]}
    suspects = [_sc_suspect(
        "kernel/base.rs", "schedule", "2025/primary", "kernel/base.rs", "schedule",
        "confirmed", 0.95, module="sched", exact=12,
    )]

    candidates = SC.build_innovation_candidates(
        recall, suspects, functions_db_path=db,
    )

    reference = candidates[0]["references"][0]
    assert reference["repo"] == "2025/primary"
    assert reference["selection_source"] == "主要参考仓库定向检索"
    assert reference["score"] is None


def test_innovation_candidate_pool_replenishes_after_early_targets_lack_baselines(tmp_path):
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.execute(
            "INSERT INTO functions VALUES (9, '2025/ref-os', 'kernel/fifth.rs', 1, 60, "
            "'fifth_candidate', 'sched', 'rust', 'fn fifth_candidate(){ schedule(); wake(); }')"
        )
    results = []
    names = ("prepare_memory", "route_interrupt", "flush_inode", "probe_device",
             "fifth_candidate")
    for index, (lines, name) in enumerate(zip((100, 90, 80, 70, 60), names), start=1):
        results.append({
            "query": {
                "file_path": f"kernel/{index}.rs", "start_line": 1, "end_line": lines,
                "func_name": name, "module_tag": "sched", "lang": "rust",
                "raw_code": f"fn {name}(){{ schedule(); wake(); }}",
            },
            "candidates": ([{"id": 9, "score": 0.5,
                              "payload": {"repo_id": "2025/ref-os"}}]
                           if index == 5 else []),
        })

    candidates = SC.build_innovation_candidates(
        {"results": results}, [], functions_db_path=db,
    )

    assert [candidate["func"] for candidate in candidates] == ["fifth_candidate"]


def test_targeted_reference_search_reads_legacy_module_for_new_category(tmp_path):
    db = tmp_path / "functions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE functions (id INTEGER PRIMARY KEY, repo_id TEXT, file_path TEXT, "
            "start_line INTEGER, end_line INTEGER, func_name TEXT, module_tag TEXT, lang TEXT, raw_code TEXT)"
        )
        conn.execute(
            "INSERT INTO functions VALUES (9, '2025/ref-os', 'kernel/syscall/fs.rs', "
            "1, 30, 'dispatch', 'fs', 'rust', 'fn dispatch(){ syscall_number(); }')"
        )
    query = {
        "file_path": "kernel/syscall/fs.rs", "start_line": 1, "end_line": 30,
        "func_name": "dispatch", "module_tag": "syscall", "lang": "rust",
        "raw_code": "fn dispatch(){ syscall_number(); audit(); }",
    }
    recall_items = [(30, {"query": query}, {}, "syscall")]

    found = SC._targeted_reference_ids(
        db, recall_items, {"syscall": ["2025/ref-os"]},
    )

    assert found[SC._query_key(query)][0][0] == 9


def test_innovation_comparability_rejects_unrelated_generic_name_and_module():
    query = {
        "func_name": "new", "module_tag": "sched", "lang": "rust",
        "raw_code": "fn new() -> Self { Self { priority: 0, vruntime: 0 } }",
    }
    unrelated = {
        "func_name": "new", "module_tag": "fs", "lang": "rust",
        "raw_code": "fn new() -> Self { Self { block_size: 512, files: 0 } }",
    }

    assert not SC._innovation_reference_is_comparable(query, unrelated)


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
            "impact_scope": "影响调度选择与任务等待时间",
            "counterevidence": "尚缺少运行时调度轨迹验证收益",
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
    assert points[0]["complexity"]["analyzed_functions"] == 1
    assert points[0]["complexity"]["max_cyclomatic_complexity"] >= 3
    assert points[0]["complexity"]["tool"] == "Lizard 1.23.0"


def test_innovation_map_requires_reference_for_every_target():
    candidates = [{
        "key": "t0001", "module": "sched", "module_display": "进程调度",
        "reference_repo": "2025/ref-os", "file": "kernel/process.rs",
        "start": 1, "end": 20, "func": "clone_process", "lines": 20,
        "raw_code": "fn clone_process() {}",
        "references": [{
            "key": "r0001", "repo": "2025/ref-os", "file": "kernel/process.rs",
            "start": 1, "end": 20, "func": "clone_process", "score": 0.5,
            "raw_code": "fn clone_process() {}",
        }],
    }]
    raw = {"innovations": [{
        "title": "没有绑定参考函数的结论", "kind": "机制改良",
        "baseline": "参考实现较简单", "delta": "目标实现增加机制",
        "why_it_matters": "提高稳定性", "impact_scope": "进程创建",
        "counterevidence": "尚无运行时验证", "confidence": "medium",
        "target_keys": ["t0001"], "reference_keys": [],
    }]}

    assert SC._normalize_innovation_points(raw, candidates) == []


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
    assert complexity["max_cyclomatic_complexity"] >= 3
    assert complexity["total_nloc"] == 184
    assert complexity["unavailable_functions"] == 0


def test_innovation_complexity_marks_unsupported_language_unavailable():
    complexity = SC._innovation_complexity([{
        "file": "arch/context.asm", "func": "switch_to", "lines": 4,
        "raw_code": ".global switch_to\nswitch_to:\n  beq a0, a1, done\ndone:\n  ret",
    }])

    assert complexity["analyzed_functions"] == 0
    assert complexity["unavailable_functions"] == 1
    assert complexity["max_cyclomatic_complexity"] is None


def test_generate_report_renders_innovation_code_map():
    from oskernel_agent.comparison.report.gitlab_links import GitLabLinker

    point = {
        "title": "多级反馈队列与老化", "kind": "机制改良", "confidence": "high",
        "reference_repo": "2025/ref-os", "baseline": "参考实现采用单队列轮转",
        "delta": "目标实现增加多级队列与老化路径", "why_it_matters": "缓解饥饿",
        "targets": [{"file": "kernel/mlfq.rs", "start": 10, "end": 42,
                     "func": "pick_mlfq", "lines": 33}],
        "references": [{"repo": "2025/ref-os", "file": "kernel/sched.rs",
                        "start": 20, "end": 30, "func": "pick_next", "score": 0.42,
                        "identity_score": 0.78, "selection_source": "主要参考仓库定向检索"}],
        "complexity": {
            "method": "McCabe cyclomatic complexity", "tool": "Lizard 1.23.0",
            "analyzed_functions": 1, "unavailable_functions": 0,
            "max_cyclomatic_complexity": 4, "mean_cyclomatic_complexity": 4.0,
            "total_nloc": 30, "total_token_count": 120, "max_parameter_count": 1,
            "functions": [{"file": "kernel/mlfq.rs", "func": "pick_mlfq",
                           "cyclomatic_complexity": 4, "nloc": 30,
                           "token_count": 120, "parameter_count": 1}],
        },
    }
    linker = GitLabLinker(
        {"2025/ref-os": "https://gitlab.example.com/history/ref-os"}, {},
        query_repo_url="https://gitlab.example.com/current/new.git",
        query_sha="d" * 40,
    )
    linker.mark_query_repo("2026/new")
    html = SC.generate_comparison_html(
        "2026/new", [], SC.compute_submodule_stats([], None), [], "", [],
        innovation_points=[point], innovation_candidates=[{"key": "t0001"}],
        linker=linker,
        ai_detect_data=_actual_ai_model_result(),
    )

    assert 'id="sec-innovation"' in html
    assert "参考实现基线" in html and "本作品代码变化" in html
    assert "函数级代码度量" in html
    assert "McCabe 圈复杂度" in html
    assert "Lizard 1.23.0" in html
    assert "McCabe CCN 4" in html
    assert "自定义高/中/低分" in html
    assert "1 组可比较代码基线 → 1 个候选创新" in html
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
        "2024/new", [], SC.compute_submodule_stats([], None), [], "", [],
        ai_detect_data=_actual_ai_model_result())
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

def test_resume_step_order():
    assert STEPS.index("recall") < STEPS.index("report")
    assert tier_counts([{"tier": "review"}, {"tier": "review"}, {"tier": "weak"}]) == {"review": 2, "weak": 1}


def test_verified_library_adapter_is_excluded_from_original_and_module_totals():
    context = SC.LibraryContext(roots=(("os/src/fs/ext4_lw", "lwext4"),))
    recall = {"results": [
        {"query": {
            "repo_id": "new", "file_path": "os/src/fs/ext4_lw/inode.rs",
            "start_line": 10, "end_line": 20, "func_name": "as_inode_type",
            "module_tag": "fs",
        }, "candidates": []},
        {"query": {
            "repo_id": "new", "file_path": "os/src/fs/vfs.rs",
            "start_line": 30, "end_line": 40, "func_name": "mount",
            "module_tag": "fs",
        }, "candidates": []},
    ]}

    originals = SC._original_functions(
        recall, [], library_context=context)
    stats = SC.compute_submodule_stats(
        [], recall, library_context=context)
    excluded = SC._exclusion_totals(
        [], recall, library_context=context)

    assert [item["func"] for item in originals] == ["mount"]
    assert stats["fs"]["original"] == 1
    assert stats["fs"]["total"] == 1
    assert excluded["library"] == 1
