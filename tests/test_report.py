"""src.report / src.pipeline 测试：章节数据、表格、后置校验删句、档案、漏斗/resume。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.pipeline.steps import STEPS, build_local_meta, tier_counts
from src.report import sections as S
from src.report.generate import generate_report
from src.report.postcheck import add_allowed, extract_refs, scrub


# ---------- 后置校验 ----------

def test_extract_refs():
    refs = extract_refs("见 src/a.rs:10-20 和 kernel/b.c:5，以及 x:3（非引用）")
    assert ("src/a.rs", 10, 20) in refs
    assert ("kernel/b.c", 5, 5) in refs
    assert all(r[0] != "x" for r in refs)  # 无扩展名不算引用


def test_scrub_deletes_unverifiable_sentence():
    allowed: dict = {}
    add_allowed(allowed, "a.rs:100-110")
    text = "函数 a.rs:105 与历史一致。但 fake.rs:999 是编造的引用。结尾无引用句子。"
    cleaned, deleted = scrub(text, allowed)
    assert deleted == 1
    assert "fake.rs:999" not in cleaned
    assert "a.rs:105" in cleaned and "结尾无引用句子" in cleaned


def test_scrub_out_of_range_deleted():
    allowed: dict = {}
    add_allowed(allowed, "a.rs:100-110")
    cleaned, deleted = scrub("越界引用 a.rs:200。", allowed)  # 200 不在 100-110
    assert deleted == 1 and "a.rs:200" not in cleaned


# ---------- 章节数据 ----------

def _suspect(qfile, qs, qe, repo, cfile, cs, ce, tier, score, module="sched", verdict=None, reasoning=""):
    s = {
        "tier": tier, "final_score": score,
        "query_func": {"repo_id": "2024/new", "file_path": qfile, "start_line": qs, "end_line": qe,
                       "func_name": "f", "module_tag": module},
        "candidate_func": {"repo_id": repo, "file_path": cfile, "start_line": cs, "end_line": ce,
                           "func_name": "g", "module_tag": module},
        "evidence": {},
    }
    if verdict:
        s["review"] = {"verdict": verdict, "clone_type": "renamed", "reasoning": reasoning}
    return s


def test_trace_top_repos_weighted():
    suspects = [
        _suspect("a.rs", 1, 9, "2021/x", "g.rs", 1, 9, "confirmed", 0.97),
        _suspect("b.rs", 1, 9, "2021/x", "h.rs", 1, 9, "review", 0.8),
        _suspect("c.rs", 1, 9, "2022/y", "k.rs", 1, 9, "weak", 0.6),
    ]
    top = S.trace_top_repos(suspects)
    assert top[0]["repo_id"] == "2021/x"          # 3+2=5 权重最高
    assert top[0]["confirmed"] == 1 and top[0]["review"] == 1


def test_module_table_and_high_sim():
    suspects = [_suspect("a.rs", 1, 9, "2021/x", "g.rs", 10, 19, "confirmed", 0.97, module="mm")]
    rows = S.module_table_rows(suspects)
    mm = [r for r in rows if r["module"] == "mm"][0]
    assert mm["repo_id"] == "2021/x" and mm["sim"] == 0.97
    assert "| mm |" in S.render_module_table(rows)
    pairs = S.high_similarity_pairs(suspects)
    assert len(pairs) == 1 and pairs[0]["new_ref"] == "a.rs:1-9"


def test_innovation_and_annotations():
    recall = {"results": [
        {"query": {"file_path": "big.rs", "start_line": 1, "end_line": 40, "func_name": "novel", "raw_code": "x"},
         "candidates": [{"score": 0.3}]},   # 低相似 + 40 行 → 创新
        {"query": {"file_path": "s.rs", "start_line": 1, "end_line": 5, "func_name": "tiny", "raw_code": "y"},
         "candidates": [{"score": 0.2}]},   # 行数不足
    ]}
    innov = S.innovation_functions(recall)
    assert len(innov) == 1 and innov[0]["ref"] == "big.rs:1-40"

    suspects = [_suspect("a.rs", 1, 9, "0/base", "g.rs", 1, 9, "baseline_derived", 0.9)]
    suspects[0]["baseline_note"] = "同基线"
    ann = S.annotations(suspects)
    assert ann["baseline_count"] == 1


# ---------- 生成（模板兜底 + LLM 后置校验） ----------

class _FakeLLM:
    async def complete(self, messages, temperature):
        sysmsg = messages[0]["content"]
        if "溯源结论" in sysmsg:
            return "本作品疑似借鉴 2021/x（a.rs:1-9 与历史一致）。另有 fake.rs:999-1000 的雷同陈述。"
        if "压缩" in sysmsg or "数组" in sysmsg:
            return '["逐行一致","结构雷同"]'
        if "独立实现" in sysmsg:
            return "- big.rs:1-40 实现了独立逻辑。"
        return "x"


def _data():
    suspects = [_suspect("a.rs", 1, 9, "2021/x", "g.rs", 1, 9, "confirmed", 0.97,
                         verdict="likely_clone", reasoning="两处逐行对应")]
    recall = {"query_repo_id": "2024/new", "results": [
        {"query": {"file_path": "big.rs", "start_line": 1, "end_line": 40, "func_name": "novel", "raw_code": "fn novel(){}"},
         "candidates": [{"score": 0.3}]}]}
    return {"suspects": suspects}, recall


def test_generate_template_fallback_no_llm():
    reviewed, recall = _data()
    md, deleted = generate_report(reviewed, recall, client=None)
    for h in ["一、溯源结论", "二、模块级对照表", "三、高相似代码段清单", "四、创新点分析",
              "五、附注信号", "六、AI 生成代码检测"]:
        assert h in md
    assert "未启用 LLM" in md
    assert "未运行 AI 生成代码检测" in md  # 未提供 ai_report 时章六给出说明


def test_generate_with_llm_scrubs_hallucinated_ref():
    reviewed, recall = _data()
    md, deleted = generate_report(reviewed, recall, client=_FakeLLM())
    assert deleted >= 1                       # fake.rs:999-1000 句被删
    assert "fake.rs:999" not in md
    assert "a.rs:1-9" in md                   # 真实引用保留
    assert "已删除" in md


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
