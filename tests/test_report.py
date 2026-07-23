"""src.report / src.pipeline 测试：语义对比报告 + 漏斗/resume。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from src.pipeline.steps import STEPS, build_local_meta, tier_counts
from src.report import semantic_compare as SC
from src.report.audit import audit_reports
from src.report.label_normalize import normalize_labels
from src.retrieval_contract import build_retrieval_contract


# ---------- 语义对比报告（M2：U1-U8 + 文件级） ----------

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


def test_review_groups_do_not_repeat_a_function_already_confirmed():
    confirmed = [{"query_file": "os/fs.rs", "query_func": "read", "query_start": 10}]
    review = [
        {"query_file": "os/fs.rs", "query_func": "read", "query_start": 80},
        {"query_file": "os/mm.rs", "query_func": "alloc", "query_start": 20},
        {"query_file": "os/mm.rs", "query_func": "alloc", "query_start": 120},
    ]
    kept = SC._exclude_confirmed_review_groups(review, confirmed)
    assert [(g["query_file"], g["query_func"]) for g in kept] == [("os/mm.rs", "alloc")]


def test_generate_comparison_html_has_m2_elements():
    suspects = [
        _sc_suspect("os/src/fs/inode.rs", "read", "2021/a", "i.rs", "read", "confirmed", 0.98,
                    exact=30, mtypes=["exact"]),
        _sc_suspect("os/src/sched/task.rs", "pick", "2021/a", "t.rs", "pick", "review", 0.82,
                    renamed=20, module="sched", mtypes=["renamed"]),
    ]
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
                                  "line_count": 88, "func_count": 4}]}]
    contract = build_retrieval_contract({
        "complete": True, "configured": 167, "covered": 167,
        "missing_repo_ids": [],
    }, complete=True)
    html = SC.generate_comparison_html(
        "2024/new", suspects, stats, groups, analysis, original_funcs=[],
        review_pairs=review_groups, file_matches=file_matches, file_similar=[],
        retrieval_contract=contract)
    assert "报告导读（请先阅读）" in html              # 导读卡（面向老师的语境引导）
    assert "初步体检" in html                          # 体检结论
    assert "高度疑似借鉴清单" in html                  # 红色高相似清单独立展示
    assert 'id="sec-review"' in html
    assert "模型复核后仍存疑" in html                  # 模型不确定清单不是“尚未审核”
    assert "结构相似但上下文不足" in html
    assert "候选来源（全部）" in html                  # U6 全候选
    assert 'id="sec-files"' in html                  # 文件级清单
    assert "整文件相同" in html
    assert "暂未检出相似（函数）" in html
    assert "相对参考 repo 的创新实现地图" in html
    assert 'data-retrieval-contract-version="3"' in html
    assert "历史作品覆盖 167/167" in html
    assert "仅比较同一编程语言" in html
    assert "自研/原创（函数）" not in html
    assert "各模块高度疑似借鉴函数数" in html           # tier 分布图
    assert 'class="toc-card"' in html                  # 左侧目录统一卡片
    assert "报告概览" in html and "相似性证据" in html and "实现差异" in html
    assert 'class="report-section"' in html            # 正文统一章节外壳
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


def test_generate_report_renders_innovation_code_map():
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
    html = SC.generate_comparison_html(
        "2026/new", [], SC.compute_submodule_stats([], None), [], "", [],
        innovation_points=[point],
    )

    assert 'id="sec-innovation"' in html
    assert "参考实现基线" in html and "本作品代码变化" in html
    assert "实现复杂度（静态估算）" in html
    assert "kernel/mlfq.rs:10" in html
    assert "kernel/sched.rs:20" in html


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


def test_full_legacy_html_is_marked_stale_idempotently():
    old = "<html><body><h2>原创代码</h2></body></html>"
    new = normalize_labels(old)
    assert 'data-retrieval-complete="false"' in new
    assert "必须按完整召回链重跑" in new
    assert normalize_labels(new) == new


def test_low_level_report_without_contract_is_visibly_stale():
    html = SC.generate_comparison_html(
        "2024/new", [], SC.compute_submodule_stats([], None), [], "", [])
    assert 'data-retrieval-complete="false"' in html
    assert "已失效，必须重跑" in html


def test_report_audit_distinguishes_complete_stale_and_unmarked(tmp_path):
    for name, body in (
        ("complete", '<div data-retrieval-contract-version="3" data-retrieval-complete="true"></div>'),
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
