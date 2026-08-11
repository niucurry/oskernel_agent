from __future__ import annotations

import io
import json

import pytest
from pypdf import PdfReader

from oskernel_agent.finals.models import Finding, ModuleDigest, ReportDigest
from oskernel_agent.finals import summary_pdf
from oskernel_agent.finals.summary_pdf import (
    AISummary,
    BODY_FONT_SIZE,
    SummaryPdfError,
    _build_pdf_bytes,
    _combined_findings,
    _has_distinct_detail,
    generate_summary_pdf,
    load_digests,
    run_ai_summary_analysis,
)


def test_summary_omits_a_detail_that_only_repeats_the_title():
    assert not _has_distinct_detail("多核支持尚未启用", "多核支持尚未启用。")
    assert _has_distinct_detail("存在同源代码", "高置信同源函数比例为 13.3%。")


def _write(tmp_path, digest: ReportDigest):
    path = tmp_path / f"{digest.kind}.json"
    path.write_text(json.dumps(digest.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
    return path


def _digests(tmp_path):
    description = ReportDigest(
        repo_id="T2026-demo", kind="description", conclusion="编译失败，需先修复链接错误。",
        findings=[Finding(title="编译失败", detail="链接器找不到入口符号。", severity="high",
                          confidence=.95, source="description")],
        metrics={"build_log_status": "failed", "run_log_status": "not_provided",
                 "hardcode_signals": 1},
    )
    development = ReportDigest(
        repo_id="T2026-demo", kind="development", conclusion="可见历史包含 8 次提交。",
        findings=[Finding(title="单次大规模代码提交", detail="一次提交变更 1300 LOC。",
                          severity="high", confidence=.95, source="development")],
        metrics={"commit_count": 8, "start_date": "2026-07-01", "end_date": "2026-08-01",
                 "large_commit_count": 1, "large_commit_threshold": 1000},
    )
    comparison = ReportDigest(
        repo_id="T2026-demo", kind="comparison", conclusion="与 2025/A 最接近。",
        findings=[Finding(title="发现同源代码", detail="整体比例 25%。", severity="medium",
                          confidence=.9, source="comparison")],
        modules=[ModuleDigest(name="文件系统", summary="2/8 个函数同源。", similarity_pct=25)],
        metrics={"closest_source": "2025/A", "overall_similarity_pct": 25,
                 "ai_llm_functions": 0},
    )
    return [_write(tmp_path, item) for item in (description, development, comparison)]


def _ai_summary():
    return AISummary.model_validate({
        "overall_judgment": "作品当前最影响评审的是编译链路失败；开发历史存在集中提交，对比结果显示部分实现与历史作品接近，三项均需按证据顺序复核。",
        "confidence": 80,
        "sections": [
            {
                "source": "description",
                "conclusion": "现有日志显示链接阶段失败，且缺少可确认的运行结果，功能完成度暂不能按通过评价。",
                "confidence": 80,
            },
            {
                "source": "development",
                "conclusion": "可见历史包含八次提交，其中一次达到大规模提交阈值；该事实需要结合提交内容判断过程连续性。",
                "confidence": 80,
            },
            {
                "source": "comparison",
                "conclusion": "与 2025/A 最接近，整体高置信同源函数比例为 25%，相似性结论仍需结合源码语义复核。",
                "confidence": 80,
            },
        ],
        "issues": [
            {
                "source": "description",
                "source_finding": 1,
                "title": "编译链路失败",
                "judgment": "链接器找不到入口符号，当前产物无法完成正式构建，直接影响功能验证。",
                "severity": "high",
                "confidence": 95,
            },
            {
                "source": "development",
                "source_finding": 1,
                "title": "存在集中式大提交",
                "judgment": "一次提交变更 1300 代码行，开发连续性需要结合该提交的文件分布和目标进一步核查。",
                "severity": "high",
                "confidence": 95,
            },
            {
                "source": "comparison",
                "source_finding": 1,
                "title": "发现同源代码线索",
                "judgment": "整体比例为 25%，足以安排源码语义复核，但不能仅凭比例认定违规。",
                "severity": "medium",
                "confidence": 90,
            },
        ],
    })


def test_summary_pdf_is_one_a4_page_without_links(tmp_path, monkeypatch):
    monkeypatch.setattr(
        summary_pdf,
        "run_ai_summary_analysis",
        lambda digests, repo_id, output_path: _ai_summary(),
    )
    output = tmp_path / "summary.pdf"
    result = generate_summary_pdf(_digests(tmp_path), output)
    assert result["pages"] == 1 and result["page_size"] == "A4" and result["links"] == 0
    reader = PdfReader(str(output))
    assert len(reader.pages) == 1
    assert not (reader.pages[0].get("/Annots") or [])
    text = reader.pages[0].extract_text()
    assert "AI 总体判断" in text and "AI 检出问题与判断" in text
    assert "AI 自动生成 · 未经人工修改" in text
    assert BODY_FONT_SIZE == 10.5


def test_summary_requires_all_three_source_digests(tmp_path):
    paths = _digests(tmp_path)[:2]
    with pytest.raises(SummaryPdfError, match="comparison"):
        generate_summary_pdf(paths, tmp_path / "bad.pdf")


def test_summary_merges_repeated_findings_and_localizes_status(tmp_path):
    paths = _digests(tmp_path)
    digests = load_digests(paths)
    repeated = Finding(
        title="单次大规模代码提交",
        detail="另一次提交变更 1600 LOC。",
        severity="high",
        confidence=.9,
        source="development",
    )
    digests["development"].findings.append(repeated)

    findings = _combined_findings(digests, 5)
    assert len([item for item in findings if item.source == "development"]) == 1
    assert next(item for item in findings if item.source == "development").title.endswith("（2次）")
    assert {item.source for item in findings} == {"description", "development", "comparison"}


def test_summary_runs_dedicated_ai_agent_and_validates_references(tmp_path, monkeypatch):
    digests = load_digests(_digests(tmp_path))
    digests["description"].metrics.update({
        "hardcode_candidates": 46,
        "hardcode_signals": 46,
        "hardcode_scan_truncated": False,
        "hardcode_scanned_files": 220,
        "hardcode_cleared": 39,
        "hardcode_confirmed": 0,
        "hardcode_suspected": 7,
    })
    captured = {}

    def fake_run(task, *, schema_hint, timeout):
        captured["agent_name"] = task.agent_name
        captured["input_files"] = task.input_files
        captured["schema_hint"] = schema_hint
        captured["timeout"] = timeout
        captured["input"] = json.loads(task.input_files[0].read_text(encoding="utf-8"))
        return _ai_summary().model_dump(mode="json")

    monkeypatch.setattr(summary_pdf, "run_batch_task", fake_run)
    result = run_ai_summary_analysis(digests, "T2026-demo", tmp_path / "summary.pdf")

    assert result.overall_judgment.startswith("作品当前最影响评审")
    assert captured["agent_name"] == "os-kernel-summary"
    assert captured["input_files"] == (tmp_path / "summary.input.json",)
    assert "source_finding" in captured["schema_hint"]
    assert captured["timeout"] == 300
    description_metrics = captured["input"]["reports"]["description"]["metrics"]
    assert description_metrics["hardcode_confirmed"] == 0
    assert description_metrics["hardcode_suspected"] == 7
    assert not ({
        "hardcode_candidates",
        "hardcode_signals",
        "hardcode_scan_truncated",
        "hardcode_scanned_files",
        "hardcode_cleared",
    } & description_metrics.keys())


def test_summary_rejects_ai_confidence_above_source(tmp_path, monkeypatch):
    digests = load_digests(_digests(tmp_path))
    invalid = _ai_summary().model_dump(mode="json")
    invalid["issues"][2]["confidence"] = 100
    monkeypatch.setattr(summary_pdf, "run_batch_task", lambda *args, **kwargs: invalid)

    with pytest.raises(SummaryPdfError, match="置信度高于来源"):
        run_ai_summary_analysis(digests, "T2026-demo", tmp_path / "summary.pdf")


def test_five_long_ai_issues_still_fit_one_page(tmp_path):
    payload = _ai_summary().model_dump(mode="json")
    payload["overall_judgment"] = (
        "编译与运行证据仍不完整，开发过程存在需要解释的集中变更，历史作品对比也出现需进一步核对的实现线索；"
        "评委应依次核查构建日志、提交内容与源码语义后再形成结论。"
    )
    payload["sections"][0]["conclusion"] = (
        "正式构建未完成且缺少运行结果，多个功能点无法通过现有材料验证；当前只能确认失败位置，不能推断未执行部分的实际状态。"
    )
    payload["sections"][1]["conclusion"] = (
        "提交历史能够呈现主要开发跨度，但集中变更削弱了过程可解释性；需要结合文件分布、提交目标和相邻提交确认是否连续开发。"
    )
    payload["sections"][2]["conclusion"] = (
        "函数级对比显示与最近历史作品存在相似实现；比例用于确定核查范围，不能脱离公共上游、接口约束和源码语义直接定性。"
    )
    payload["issues"].extend([
        {
            "source": "description",
            "source_finding": 2,
            "title": "运行证据不足",
            "judgment": "未提供可确认的正式运行结果，评委无法判断失败仅限构建阶段还是同时影响核心功能；应以现场复现和完整日志作为最终依据。",
            "severity": "medium",
            "confidence": 80,
        },
        {
            "source": "development",
            "source_finding": 2,
            "title": "提交过程需要解释",
            "judgment": "集中变更覆盖多个文件且时间接近，现有摘要不足以证明各功能的形成顺序；该线索影响过程可信度评价，但不单独构成负面定性。",
            "severity": "medium",
            "confidence": 78,
        },
    ])
    summary = AISummary.model_validate(payload)

    data = _build_pdf_bytes({}, "T2026-stress", summary)
    (tmp_path / "stress-summary.pdf").write_bytes(data)

    assert len(PdfReader(io.BytesIO(data)).pages) == 1
