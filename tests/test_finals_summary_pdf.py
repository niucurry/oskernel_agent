from __future__ import annotations

import io
import json

import pytest
from pypdf import PdfReader

from oskernel_agent.finals.models import Finding, ModuleDigest, ReportDigest
from oskernel_agent.finals import summary_pdf
from oskernel_agent.finals.summary_pdf import (
    AISummary,
    AISummaryIssue,
    BODY_FONT_SIZE,
    SummaryPdfError,
    _build_pdf_bytes,
    _combined_findings,
    _has_distinct_detail,
    _unattributed_issue_identifiers,
    generate_summary_pdf,
    load_digests,
    run_ai_summary_analysis,
    _validate_ai_summary_result,
)


def test_summary_omits_a_detail_that_only_repeats_the_title():
    assert not _has_distinct_detail("多核支持尚未启用", "多核支持尚未启用。")
    assert _has_distinct_detail("存在同源代码", "高置信同源函数比例为 13.3%。")


def test_identifier_with_conventional_syscall_prefix_is_attributed_to_corpus():
    corpus = "api/src/syscall/task/execve.rs execve 在多线程场景下返回错误。"
    issue = AISummaryIssue(
        source="description", source_finding=1,
        title="多线程执行新程序缺陷",
        judgment="sys_execve 在多线程场景直接返回错误。",
        severity="high", confidence=90,
    )

    assert _unattributed_issue_identifiers(issue, corpus) == []


def test_identifier_absent_from_corpus_is_still_flagged():
    corpus = "api/src/syscall/task/execve.rs execve 在多线程场景下返回错误。"
    issue = AISummaryIssue(
        source="description", source_finding=1,
        title="多线程执行新程序缺陷",
        judgment="fork 相关路径也返回错误。",
        severity="high", confidence=90,
    )

    assert _unattributed_issue_identifiers(issue, corpus) == ["fork"]


def _write(tmp_path, digest: ReportDigest):
    path = tmp_path / f"{digest.kind}.json"
    path.write_text(json.dumps(digest.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")
    return path


def _digests(tmp_path):
    description = ReportDigest(
        repo_id="T2026-demo", kind="description",
        conclusion="存在需要核查的硬编码线索，且发现页表回收实现缺陷。",
        findings=[Finding(title="页表回收路径不完整",
                          detail="页表释放路径遗漏了中间页目录，可能造成内存泄漏。",
                          severity="high", confidence=.95, source="description")],
        metrics={"hardcode_signals": 1},
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
        "overall_judgment": "作品当前最影响评审的是页表回收实现缺陷；开发历史存在集中提交，对比结果显示部分实现与历史作品接近，三项均需按证据顺序复核。",
        "confidence": 80,
        "sections": [
            {
                "source": "description",
                "conclusion": "描述报告显示页表回收路径存在遗漏，且硬编码线索需要核查，功能完成度需要结合源码继续确认。",
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
                "title": "页表回收缺陷",
                "judgment": "页表释放路径遗漏中间页目录，可能造成内存泄漏，直接影响内存管理正确性。",
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
    assert "AI 自动生成 · 未经人工修改" not in text
    assert "使用说明" in text
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


def test_summary_rejects_any_compile_claim_in_issue_judgment(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0]["judgment"] = "内核产物未完成正式构建，结果未单独说明。"

    with pytest.raises(SummaryPdfError, match="不得包含编译或构建"):
        _validate_ai_summary_result(payload, digests)


def test_summary_rejects_environment_interruption_claim_wording(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["sections"][0]["conclusion"] = "双架构因环境限制未通过编译。"

    with pytest.raises(SummaryPdfError, match="不得包含编译或构建"):
        _validate_ai_summary_result(payload, digests)


def test_summary_rejects_code_identifier_not_in_referenced_finding(tmp_path):
    digests = load_digests(_digests(tmp_path))
    digests["description"].findings = [Finding(
        title="人工智能（AI）复核硬编码线索：faccessat 路径分支",
        detail="sys_faccessat2 对 /musl/ 路径返回成功。",
        severity="medium",
        confidence=.8,
        source="description",
    )]
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0].update({
        "source_finding": 1,
        "severity": "medium",
        "confidence": 80,
        "title": "faccessat/fcntl 路径分支",
        "judgment": "sys_faccessat2 与 fcntl 均改写错误返回。",
    })

    with pytest.raises(SummaryPdfError, match="不存在的代码标识符.*fcntl"):
        _validate_ai_summary_result(payload, digests)


def test_summary_allows_identifier_from_related_finding_in_same_report(tmp_path):
    """同一根因拆成多条 finding（一条给错误输出、一条给根因）时，跨 finding
    引用真实标识符的合并是合法表达，不应判为幻觉。"""
    digests = load_digests(_digests(tmp_path))
    digests["description"].findings = [
        Finding(title="源码实现问题：页表释放",
                detail="sys_free_pages 对 /musl/ 路径返回成功。",
                severity="high", confidence=.95, source="description"),
        Finding(title="源码实现问题：页表遍历",
                detail="free_pagetable 递归深度未设上限。",
                severity="high", confidence=.95, source="description"),
    ]
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0].update({
        "source_finding": 1,
        "severity": "high",
        "confidence": 95,
        "title": "页表释放缺陷",
        "judgment": "sys_free_pages 与 free_pagetable 均改写错误返回。",
    })

    summary = _validate_ai_summary_result(payload, digests)

    assert summary.issues[0].title == "页表释放缺陷"


def test_summary_rejects_suspected_hardcode_rewritten_as_confirmed(tmp_path):
    digests = load_digests(_digests(tmp_path))
    digests["description"].findings = [Finding(
        title="人工智能（AI）复核硬编码线索：按测试名分支",
        detail="缺失路径可能回退为固定程序。",
        severity="medium",
        confidence=.8,
        source="description",
    )]
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0].update({
        "source_finding": 1,
        "severity": "medium",
        "confidence": 80,
        "judgment": "人工智能（AI）复核确认该实现构成硬编码行为。",
    })

    with pytest.raises(SummaryPdfError, match="疑似硬编码"):
        _validate_ai_summary_result(payload, digests)


def test_summary_rejects_complete_kernel_claim_without_dual_arch_build(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["overall_judgment"] = "作品具有完整的 RISC-V 与 LoongArch 双架构操作系统内核框架。"

    with pytest.raises(SummaryPdfError, match="未经编译验证"):
        _validate_ai_summary_result(payload, digests)


def test_summary_normalizes_contiguous_zero_based_finding_refs(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0]["source_finding"] = 0

    summary = _validate_ai_summary_result(payload, digests)

    assert summary.issues[0].source_finding == 1
    assert summary.issues[1].source_finding == 1



def test_summary_normalizes_sparse_zero_based_finding_refs(tmp_path):
    digests = load_digests(_digests(tmp_path))
    digests["description"].findings.extend([
        Finding(title="第二项", detail="第二项证据。", severity="high",
                confidence=.95, source="description"),
        Finding(title="第三项", detail="第三项证据。", severity="high",
                confidence=.95, source="description"),
    ])
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0]["source_finding"] = 0
    payload["issues"].append({
        "source": "description", "source_finding": 2,
        "title": "第三项风险", "judgment": "第三项证据需要进一步核查。",
        "severity": "high", "confidence": 95,
    })

    summary = _validate_ai_summary_result(payload, digests)
    description_refs = [
        issue.source_finding for issue in summary.issues
        if issue.source == "description"
    ]

    assert description_refs == [1, 3]


def test_summary_compacts_only_at_complete_sentence_boundary(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["sections"][1]["conclusion"] = (
        "第一句说明提交历史。" * 12
        + "最后一句不应被截断。"
    )

    summary = _validate_ai_summary_result(payload, digests)

    assert len(summary.sections[1].conclusion) <= 180
    assert summary.sections[1].conclusion.endswith("。")
    assert "…" not in summary.sections[1].conclusion


def test_summary_preserves_complete_section_within_schema_limit(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    original = payload["sections"][1]["conclusion"]
    payload["sections"][1]["conclusion"] = original * 4

    summary = _validate_ai_summary_result(payload, digests)

    assert summary.sections[1].conclusion == original * 4
    assert len(summary.sections[1].conclusion) <= 180
    assert summary.sections[1].conclusion.endswith("。")


def test_summary_rejects_ai_ellipsis_instead_of_rendering_it(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["sections"][1]["conclusion"] = "提交历史显示移植工作仍在继续…"

    with pytest.raises(SummaryPdfError, match="省略号"):
        _validate_ai_summary_result(payload, digests)

def test_summary_rejects_test_success_claim_without_runtime_evidence(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["sections"][1]["conclusion"] = (
        "LTP \u5168\u91cf\u6d4b\u8bd5\u5df2\u901a\u8fc7\u3002"
    )

    with pytest.raises(SummaryPdfError, match="\u672a\u7ecf\u6b63\u5f0f\u8fd0\u884c\u65e5\u5fd7"):
        _validate_ai_summary_result(payload, digests)


def test_summary_allows_explicit_runtime_evidence_limitation(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["sections"][1]["conclusion"] = (
        "提交历史不能证明当前版本的功能测例全部通过。"
    )

    summary = _validate_ai_summary_result(payload, digests)

    assert "不能证明" in summary.sections[1].conclusion


def test_summary_allows_warning_about_tests_passing_unexpectedly(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0]["judgment"] = (
        "条件回退可能使 LTP 测试非预期通过，需要评委核对实际返回值。"
    )

    summary = _validate_ai_summary_result(payload, digests)

    assert "非预期通过" in summary.issues[0].judgment


def test_summary_allows_pass_rate_metric_without_runtime_log(tmp_path):
    """“测试通过率依赖白名单排除”是比率/限制陈述，不是“测试已通过”的断言。"""
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["sections"][1]["conclusion"] = (
        "冲刺阶段测试通过率依赖白名单排除而非全部修复。"
    )

    summary = _validate_ai_summary_result(payload, digests)

    assert "通过率" in summary.sections[1].conclusion


def test_summary_allows_via_phrasing_before_test_word(tmp_path):
    """“通过脚本忽略测试退出码”的“通过”是方式介词，不是测试通过断言。"""
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["issues"][0]["judgment"] = (
        "可疑实现通过脚本忽略测试退出码来绕过失败。"
    )

    summary = _validate_ai_summary_result(payload, digests)

    assert "通过脚本" in summary.issues[0].judgment


def test_summary_explains_term_before_length_validation(tmp_path):
    digests = load_digests(_digests(tmp_path))
    payload = _ai_summary().model_dump(mode="json")
    payload["sections"][0]["conclusion"] = "LTP result is unavailable."

    summary = _validate_ai_summary_result(payload, digests)

    assert "Linux \u6d4b\u8bd5\u9879\u76ee\uff08LTP\uff09" in summary.sections[0].conclusion
    assert summary.sections[0].conclusion.endswith(".")

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
