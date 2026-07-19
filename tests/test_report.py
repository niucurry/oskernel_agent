"""src.report / src.pipeline 测试：语义对比报告 + 漏斗/resume。"""

from __future__ import annotations

import json
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
        file_matches=file_matches, file_similar=[], retrieval_contract=contract)
    assert "报告导读（请先阅读）" in html              # 导读卡（面向老师的语境引导）
    assert "初步体检" in html                          # 体检结论
    assert "疑似借鉴清单（待人工判定）" in html         # 分类清单（措辞与「辅助参考」定位一致）
    assert "候选来源（全部）" in html                  # U6 全候选
    assert 'id="sec-files"' in html                  # 文件级清单
    assert "整文件相同" in html
    assert "暂未检出相似（函数）" in html
    assert 'data-retrieval-contract-version="2"' in html
    assert "历史作品覆盖 167/167" in html
    assert "自研/原创（函数）" not in html
    assert "各模块疑似借鉴函数数（待人工判定）" in html  # tier 分布图
    # 所有 echarts JSON 必须可解析（前端 JSON.parse 不能炸）
    import re
    for blob in re.findall(r'<script type="application/json">(.*?)</script>', html, re.DOTALL):
        json.loads(blob)


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
        ("complete", '<div data-retrieval-contract-version="2" data-retrieval-complete="true"></div>'),
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
