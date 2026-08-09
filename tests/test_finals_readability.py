from __future__ import annotations

from finals.models import EvidenceRef, Finding, ModuleDigest, ReportDigest
from finals.readability import (
    concise_module_summary,
    explain_terms_in_html,
    explain_terms_on_first_use,
    readability_errors,
    remove_ai_filler,
)


def test_filler_is_removed_without_changing_fact():
    assert remove_ai_filler("综上所述，该仓库只有 3 次提交。") == "该仓库只有 3 次提交"


def test_module_summary_stops_at_sentence_and_obeys_limit():
    raw = "职责明确。" + "实现细节很多，" * 80
    summary = concise_module_summary(raw)
    assert len(summary) <= 300
    assert summary.endswith(("。", "…"))
    assert not summary.endswith("，")


def test_term_expansion_cannot_push_module_summary_over_limit():
    raw = "模块采用 COW VFS ELF IPC ABI SMP，" + "映射路径稳定，" * 37 + "确保正确。"
    assert len(raw) <= 300
    summary = concise_module_summary(raw)
    assert len(summary) <= 300
    assert not summary.endswith("，")


def test_common_term_is_explained_only_once():
    text = explain_terms_on_first_use("COW 用于缺页处理，后续 COW 路径复用同一映射。")
    assert text.count("写时复制") == 1
    assert text.count("COW") == 2
    assert not readability_errors(text)


def test_competition_report_terms_are_defined_on_first_use():
    text = explain_terms_on_first_use("vendor 依赖引入后开展 LTP 测试。")
    assert "仓库内置第三方（vendor）依赖" in text
    assert "Linux 测试项目（LTP）" in text


def test_html_term_gate_ignores_code_and_defines_first_visible_use():
    source = "<html><head><title>COW</title></head><body><nav>COW</nav><code>COW</code><p>COW 缺页。</p><p>COW 复用。</p></body></html>"
    rendered = explain_terms_in_html(source)
    assert "<title>COW</title>" in rendered and "<nav>COW</nav>" in rendered
    assert "<code>COW</code>" in rendered
    assert rendered.count("写时复制（Copy-on-Write，COW）") == 1


def test_unexplained_term_and_ai_filler_are_reported():
    errors = readability_errors("值得注意的是，VFS 提供统一接口。")
    assert any("模板语" in error for error in errors)
    assert any("VFS" in error for error in errors)


def test_short_term_inside_a_larger_standard_name_is_not_a_false_first_use():
    assert not any("术语 OS" in error for error in readability_errors("符合 POSIX 接口约定。"))


def test_digest_selects_only_decision_relevant_findings():
    digest = ReportDigest(
        repo_id="T2026-demo",
        kind="description",
        conclusion="存在一个高风险问题。",
        findings=[
            Finding(title="普通提示", detail="提示。", severity="info", confidence=1,
                    source="description"),
            Finding(title="高风险", detail="问题。", severity="high", confidence=.8,
                    source="description", evidence=[EvidenceRef(path="src/main.c", line=3)]),
        ],
        modules=[ModuleDigest(name="内存管理", summary="负责页表映射。")],
    )
    assert [item.title for item in digest.decision_findings(1)] == ["高风险"]
