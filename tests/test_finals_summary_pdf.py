from __future__ import annotations

import json

import pytest
from pypdf import PdfReader

from finals.models import Finding, ModuleDigest, ReportDigest
from finals.summary_pdf import (
    BODY_FONT_SIZE,
    SummaryPdfError,
    _combined_findings,
    _has_distinct_detail,
    _overview_lines,
    generate_summary_pdf,
    load_digests,
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


def test_summary_pdf_is_one_a4_page_without_links(tmp_path):
    output = tmp_path / "summary.pdf"
    result = generate_summary_pdf(_digests(tmp_path), output)
    assert result["pages"] == 1 and result["page_size"] == "A4" and result["links"] == 0
    reader = PdfReader(str(output))
    assert len(reader.pages) == 1
    assert not (reader.pages[0].get("/Annots") or [])
    text = reader.pages[0].extract_text()
    assert "优先复核" in text and "三份报告速览" in text
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
    overview = dict(_overview_lines(digests))
    assert "编译日志：失败" in overview["作品描述"]
    assert "运行日志：未提供" in overview["作品描述"]
