"""生成由专用 AI 摘要智能体撰写的一页 A4 评审 PDF。"""

from __future__ import annotations

import copy
import html
import io
import json
import os
import re
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from oskernel_agent.engines.llm_batch import BatchTask, run_batch_task

from .models import Finding, ReportDigest, Severity
from .readability import explain_terms_on_first_use, readability_errors

BODY_FONT_SIZE = 10.5
_PAGE_MARGIN = 14 * mm
_SUMMARY_SOURCES = ("description", "development", "comparison")
_INTERNAL_HARDCODE_METRICS = {
    "hardcode_candidates",
    "hardcode_signals",
    "hardcode_scan_truncated",
    "hardcode_scanned_files",
    "hardcode_cleared",
}
_UNVERIFIED_TEST_SUCCESS_RE = re.compile(
    r"(?:LTP|\u6d4b\u8bd5\u5957\u4ef6|\u6d4b\u8bd5|\u7528\u4f8b).{0,18}(?:\u5168\u91cf|\u5168\u90e8)?.{0,6}(?:\u901a\u8fc7|\u8dd1\u901a|\u6210\u529f)(?!\u7387)"
    r"|(?:\u901a\u8fc7(?!\u7387)(?!\s*(?:\u811a\u672c|\u4fee\u6539|\u767d\u540d\u5355|\u8bbe\u7f6e|\u914d\u7f6e|\u6ce8\u5165|\u52ab\u6301|\u62e6\u622a|\u65c1\u8def|\u7ed5\u8fc7|\u5ffd\u7565|\u786c\u7f16\u7801|\u6ce8\u91ca|\u63a5\u53e3|\u7cfb\u7edf\u8c03\u7528|syscall))"
    r"|\u8dd1\u901a(?!\u7387)).{0,18}(?:LTP|\u6d4b\u8bd5\u5957\u4ef6|\u6d4b\u8bd5|\u7528\u4f8b)",
    re.I,
)
_UNVERIFIED_KERNEL_COMPLETENESS_RE = re.compile(
    r"(?:完整|完备|完善).{0,24}(?:双架构|操作系统|内核|内核框架)",
    re.I,
)
_COMPILE_TOPIC_RE = re.compile(
    r"编译|构建|make|kernel-rv|kernel-la",
    re.I,
)
_SOURCE_LABELS = {
    "description": "作品描述与运行质量",
    "development": "开发过程",
    "comparison": "历史作品对比",
}
_SEVERITY_LABELS = {
    "critical": "严重",
    "high": "高风险",
    "medium": "需关注",
    "low": "低风险",
    "info": "提示",
}
_SEVERITY_COLORS = {
    "critical": "#991b1b",
    "high": "#b42318",
    "medium": "#b54708",
    "low": "#475467",
    "info": "#475467",
}

SummarySource = Literal["description", "development", "comparison"]


class SummaryPdfError(RuntimeError):
    pass


def _single_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _complete_sentence(value: str) -> str:
    """Normalize generated prose without silently clipping unfinished content."""
    text = _single_line(value)
    if text and text[-1] not in "。！？；.!?;：:":
        text += "。"
    return text


def _complete_sentences_within(value: str, limit: int) -> str:
    """在完整句边界压缩到额度内；没有完整句可保留时按额度硬切，守住交付上限。"""
    text = _complete_sentence(value)
    if len(text) <= limit:
        return text
    sentences = re.findall(r".*?[。！？；.!?;](?:\s+|$)", text)
    kept: list[str] = []
    used = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if used + len(sentence) > limit:
            break
        kept.append(sentence)
        used += len(sentence)
    if kept:
        return "".join(kept)
    # 首句本身超额度或全文无句界：按额度硬切并补句号，保证通过交付校验
    hard = text[: limit - 1].rstrip("，,、；;：: ")
    return hard + "。" if hard else text[:limit]


class AISummaryIssue(BaseModel):
    """摘要智能体从来源报告中选出的一个评委复核项。"""

    model_config = ConfigDict(extra="forbid")

    source: SummarySource
    source_finding: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=80)
    judgment: str = Field(min_length=1, max_length=240)
    severity: Severity
    confidence: int = Field(ge=0, le=100)

    @field_validator("title", "judgment")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _single_line(value)


class AISummarySection(BaseModel):
    """对应一份后续报告的 AI 结论。"""

    model_config = ConfigDict(extra="forbid")

    source: SummarySource
    conclusion: str = Field(min_length=1, max_length=180)
    confidence: int = Field(ge=0, le=100)

    @field_validator("conclusion")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _single_line(value)


class AISummary(BaseModel):
    """直接进入 PDF 的全部作品相关文字。"""

    model_config = ConfigDict(extra="forbid")

    overall_judgment: str = Field(min_length=1, max_length=280)
    confidence: int = Field(ge=0, le=100)
    sections: list[AISummarySection] = Field(min_length=3, max_length=3)
    issues: list[AISummaryIssue] = Field(default_factory=list, max_length=5)

    @field_validator("overall_judgment")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _single_line(value)

    @model_validator(mode="after")
    def validate_structure(self) -> "AISummary":
        sources = [section.source for section in self.sections]
        if sources != list(_SUMMARY_SOURCES):
            raise ValueError("sections 必须按 description、development、comparison 排列")
        refs = [(issue.source, issue.source_finding) for issue in self.issues]
        if len(refs) != len(set(refs)):
            raise ValueError("issues 不得重复引用同一来源问题")
        return self


def _font_candidates() -> tuple[list[Path], list[Path]]:
    regular = [
        Path(os.getenv("FINALS_CJK_FONT", "")),
        Path("C:/Windows/Fonts/Deng.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    ]
    bold = [
        Path(os.getenv("FINALS_CJK_BOLD_FONT", "")),
        Path("C:/Windows/Fonts/Dengb.ttf"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    ]
    return regular, bold


def _register_fonts() -> tuple[str, str]:
    regular_candidates, bold_candidates = _font_candidates()
    regular = next((path for path in regular_candidates if str(path) and path.is_file()), None)
    if regular is None:
        raise SummaryPdfError(
            "未找到中文字体；请用 FINALS_CJK_FONT 指向可嵌入的 TTF/TTC 字体"
        )
    bold = next((path for path in bold_candidates if str(path) and path.is_file()), regular)
    if "FinalsSans" not in pdfmetrics.getRegisteredFontNames():
        regular_args = {"subfontIndex": 0} if regular.suffix.lower() == ".ttc" else {}
        pdfmetrics.registerFont(TTFont("FinalsSans", str(regular), **regular_args))
    if "FinalsSansBold" not in pdfmetrics.getRegisteredFontNames():
        bold_args = {"subfontIndex": 0} if bold.suffix.lower() == ".ttc" else {}
        pdfmetrics.registerFont(TTFont("FinalsSansBold", str(bold), **bold_args))
    return "FinalsSans", "FinalsSansBold"


def load_digests(paths: list[str | Path]) -> dict[str, ReportDigest]:
    digests: dict[str, ReportDigest] = {}
    for path in paths:
        source = Path(path)
        if not source.is_file():
            raise SummaryPdfError(f"摘要输入不存在：{source}")
        digest = ReportDigest.model_validate_json(source.read_text(encoding="utf-8"))
        if digest.kind == "summary":
            continue
        if digest.kind in digests:
            raise SummaryPdfError(f"摘要输入重复：{digest.kind}")
        digests[digest.kind] = digest
    missing = set(_SUMMARY_SOURCES) - set(digests)
    if missing:
        raise SummaryPdfError("缺少摘要输入：" + "、".join(sorted(missing)))
    return digests


def _safe(value: object) -> str:
    # 与 development._esc 同一约定：仅 None 归一为空串，数值 0 原样渲染。
    return html.escape("" if value is None else str(value), quote=False)


def _summary_input_digest(digest: ReportDigest) -> dict:
    """Hide scanner noise; the judge summary only uses reviewed hardcode results."""
    payload = digest.model_dump(mode="json")
    metrics = dict(payload.get("metrics") or {})
    if digest.kind == "description":
        for key in _INTERNAL_HARDCODE_METRICS:
            metrics.pop(key, None)
    elif digest.kind == "comparison":
        for key in list(metrics):
            if key == "ai_llm_functions" or key.startswith("history_"):
                metrics.pop(key, None)
        payload["modules"] = [
            module for module in (payload.get("modules") or [])
            if int(module.get("evidence_count") or 0) > 0
        ]
    payload["metrics"] = metrics
    return payload


def _combined_findings(digests: dict[str, ReportDigest], limit: int) -> list[Finding]:
    """保留旧的跨报告候选归并能力，供事实准备与兼容调用使用。"""
    rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
    grouped: dict[tuple[str, str], list[Finding]] = {}
    for kind in _SUMMARY_SOURCES:
        for finding in digests[kind].findings:
            grouped.setdefault((kind, finding.title), []).append(finding)

    collapsed: list[Finding] = []
    for (_kind, title), items in grouped.items():
        representative = sorted(
            items,
            key=lambda item: (-rank[item.severity], -item.confidence, item.detail),
        )[0]
        if len(items) == 1:
            collapsed.append(representative)
            continue
        collapsed.append(representative.model_copy(update={
            "title": f"{title}（{len(items)}次）",
            "detail": (
                f"发现 {len(items)} 次同类线索；代表项：{representative.detail}"
            ),
        }))

    ordered = sorted(
        collapsed,
        key=lambda item: (-rank[item.severity], -item.confidence, item.title),
    )
    selected: list[Finding] = []
    for kind in _SUMMARY_SOURCES:
        candidate = next((item for item in ordered if item.source == kind), None)
        if candidate is not None and len(selected) < limit:
            selected.append(candidate)
    for item in ordered:
        if len(selected) >= limit:
            break
        if item not in selected:
            selected.append(item)
    return sorted(
        selected,
        key=lambda item: (-rank[item.severity], -item.confidence, item.title),
    )


def _has_distinct_detail(title: str, detail: str) -> bool:
    """避免问题标题在判断行中原样重复。"""
    def normalized(value: str) -> str:
        return re.sub(r"[\s，。；：、,.!?！？:;]+", "", value).casefold()

    return bool(normalized(detail)) and normalized(detail) != normalized(title)


def _render_order_text(summary: AISummary) -> str:
    parts = [summary.overall_judgment]
    by_source: dict[str, list[AISummaryIssue]] = {source: [] for source in _SUMMARY_SOURCES}
    for issue in summary.issues:
        by_source[issue.source].append(issue)
    for section in summary.sections:
        parts.append(section.conclusion)
        for issue in by_source[section.source]:
            parts.extend((issue.title, issue.judgment))
    return "\n".join(parts)

def _compact_ai_summary_fields(value: dict) -> dict:
    compacted = copy.deepcopy(value)
    compacted["overall_judgment"] = _complete_sentences_within(
        explain_terms_on_first_use(compacted.get("overall_judgment", "")), 280
    )
    for section in compacted.get("sections") or []:
        if isinstance(section, dict):
            section["conclusion"] = _complete_sentences_within(
                explain_terms_on_first_use(section.get("conclusion", "")), 180
            )
    for issue in compacted.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        issue["title"] = _single_line(
            explain_terms_on_first_use(issue.get("title", ""))
        )
        issue["judgment"] = _complete_sentences_within(
            explain_terms_on_first_use(issue.get("judgment", "")), 240
        )
    return compacted

def _normalize_zero_based_finding_refs(
    value: dict,
    digests: dict[str, ReportDigest],
) -> dict:
    """Repair a clearly zero-based sequence; later checks still verify every mapping."""
    normalized = copy.deepcopy(value)
    issues = normalized.get("issues") or []
    for source in _SUMMARY_SOURCES:
        source_issues = [
            issue for issue in issues
            if isinstance(issue, dict) and issue.get("source") == source
        ]
        refs = [issue.get("source_finding") for issue in source_issues]
        if not refs or not all(isinstance(ref, int) for ref in refs) or 0 not in refs:
            continue
        # 零基序号可以是稀疏选择（如 0、2、18），不要求连续；只要每个值都能
        # 在来源 finding 数组中合法映射，统一加一。后续仍校验严重度和置信度，
        # 混用零基/一基的结果会因引用事实不一致而被拒绝。
        if any(ref < 0 or ref >= len(digests[source].findings) for ref in refs):
            continue
        for issue in source_issues:
            issue["source_finding"] += 1
    return normalized



_SUMMARY_IDENTIFIER_ALLOWLIST = {
    "code", "file", "files", "function", "functions", "hardcode", "input",
    "kernel", "linux", "module", "output", "path", "return", "runtime", "standard",
    "status", "system", "test", "tests",
}


def _source_report_corpus(source: str, digests: dict[str, ReportDigest]) -> str:
    """整份来源报告的可核对语料：全部 finding 及其证据。

    跨 finding 引用同一真实标识符是合法合并（如同一根因的构建失败常拆成多条
    finding，一条给错误输出、一条给根因），护栏只应拦截在整份报告里都不存在的
    幻觉标识符。
    """
    digest = digests[source]
    parts: list[str] = []
    for finding in digest.findings:
        parts.append(finding.title)
        parts.append(finding.detail)
        for evidence in finding.evidence:
            parts.append(f"{evidence.path} {evidence.excerpt}")
    return " ".join(parts)


def _unattributed_issue_identifiers(issue: AISummaryIssue, corpus: str) -> list[str]:
    """发现摘要问题中没有出现在来源报告语料的代码标识符。

    同一标识符带约定命名前缀（sys_execve / syscall_xxx / do_xxx 等）视为来源
    标识符的规范写法，不算改写或补造；只有整份来源报告都查不到的标识符才拦截。
    """
    generated = f"{issue.title} {issue.judgment}"
    source = corpus.casefold()
    identifiers = set(re.findall(
        r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_]{3,}(?![A-Za-z0-9_])",
        generated,
    ))

    def attributed(token: str) -> bool:
        folded = token.casefold()
        if folded in _SUMMARY_IDENTIFIER_ALLOWLIST or folded in source:
            return True
        stripped = re.sub(r"^(?:sys|syscall|__sys|__do|do|ax|k)_", "", folded)
        return len(stripped) >= 4 and stripped in source

    return sorted(token for token in identifiers if not attributed(token))


def _validate_ai_summary_result(
    value: dict,
    digests: dict[str, ReportDigest],
) -> AISummary:
    if not isinstance(value, dict) or value.get("_error"):
        raise SummaryPdfError("摘要智能体未返回可用结果")
    value = _normalize_zero_based_finding_refs(
        _compact_ai_summary_fields(value), digests
    )
    try:
        summary = AISummary.model_validate(value)
    except ValidationError as exc:
        raise SummaryPdfError(f"摘要智能体输出结构无效：{exc}") from exc

    available = sum(len(digests[source].findings) for source in _SUMMARY_SOURCES)
    if available and not summary.issues:
        raise SummaryPdfError("摘要智能体遗漏了三份报告中的问题")

    overall_cap = min(round(digests[source].confidence * 100) for source in _SUMMARY_SOURCES)
    if summary.confidence > overall_cap:
        raise SummaryPdfError("摘要智能体总体置信度高于三份来源报告的共同上限")
    for section in summary.sections:
        source_cap = round(digests[section.source].confidence * 100)
        if section.confidence > source_cap:
            raise SummaryPdfError(f"摘要智能体的 {section.source} 结论置信度高于来源报告")

    refs = {(issue.source, issue.source_finding) for issue in summary.issues}
    source_corpora = {
        source: _source_report_corpus(source, digests)
        for source in _SUMMARY_SOURCES
    }
    for issue in summary.issues:
        findings = digests[issue.source].findings
        if issue.source_finding > len(findings):
            raise SummaryPdfError(
                f"摘要智能体引用了不存在的问题：{issue.source}#{issue.source_finding}"
            )
        source_finding = findings[issue.source_finding - 1]
        if issue.severity != source_finding.severity:
            raise SummaryPdfError(
                f"摘要智能体改变了来源严重度：{issue.source}#{issue.source_finding}"
                f"（来源为 {source_finding.severity}）"
            )
        source_confidence = round(source_finding.confidence * 100)
        if issue.confidence > source_confidence:
            raise SummaryPdfError(
                f"摘要智能体置信度高于来源：{issue.source}#{issue.source_finding}"
                f"（来源上限 {source_confidence}）"
            )
        if not _has_distinct_detail(issue.title, issue.judgment):
            raise SummaryPdfError("摘要问题的标题与判断重复")
        unattributed = _unattributed_issue_identifiers(issue, source_corpora[issue.source])
        if unattributed:
            raise SummaryPdfError(
                "摘要问题引入了来源 finding 中不存在的代码标识符："
                + "、".join(unattributed)
            )
        if issue.source == "description" and "硬编码线索" in source_finding.title:
            confirm = re.search(
                r"(?:已|人工智能（AI）复核)?确认|构成(?:作弊|硬编码)",
                issue.judgment,
            )
            if confirm:
                # 只看上一个标点之后的上下文：否定/待定修饰（尚未确认、无法确认、
                # 无确认项、待确认、疑似构成等）与「与/有确认项区分」这类类别引用
                # 都属于审慎表述，不算把疑似线索改写为确认结论；裸「确认」、
                # 「已确认」「构成硬编码」等断言仍拒绝。
                tail = re.split(r"[，。；！？、]", issue.judgment[: confirm.start()])[-1]
                negated = re.search(
                    r"(尚未|还未|未能|无法|不能|不足以|有待|待|未|没有|无|与|和|把|将|有|共)$",
                    tail,
                )
                hedged = confirm.group(0).startswith("构成") and re.search(
                    r"(疑似|推测|可能|或可)$", tail)
                if not (negated or hedged):
                    raise SummaryPdfError("摘要把疑似硬编码线索改写成了确认结论")

    for source in _SUMMARY_SOURCES:
        critical = [
            index for index, finding in enumerate(digests[source].findings, start=1)
            if finding.severity == "critical"
        ]
        if critical and not any((source, index) in refs for index in critical):
            raise SummaryPdfError(f"摘要智能体遗漏了 {source} 的严重问题")

    rendered_text = _render_order_text(summary)

    if _COMPILE_TOPIC_RE.search(rendered_text):
        raise SummaryPdfError("摘要不得包含编译或构建分析内容")

    if _UNVERIFIED_KERNEL_COMPLETENESS_RE.search(summary.overall_judgment):
        raise SummaryPdfError("摘要把未经编译验证的源码框架写成了完整内核")

    test_claim_text = re.sub(
        r"(?:非预期|错误地|异常地|不应|本应失败却)通过",
        "异常命中",
        rendered_text,
    )
    test_claim_text = re.sub(
        r"(?:不能|无法|不足以|不代表|不得)[^。！？；]{0,80}",
        "",
        test_claim_text,
    )
    if _UNVERIFIED_TEST_SUCCESS_RE.search(test_claim_text):
        raise SummaryPdfError(
            "摘要把开发提交信息改写成了未经正式运行日志证明的测试通过结论"
        )
    writing_errors = readability_errors(_render_order_text(summary), max_chars=1450)
    if writing_errors:
        raise SummaryPdfError("摘要智能体文字未通过可读性检查：" + "；".join(writing_errors))
    return summary


def _collect_ai_summary_issue_errors(
    summary: AISummary,
    digests: dict[str, ReportDigest],
) -> list[str]:
    """收集全部 issue 级交付错误，供定向修复任务使用。

    覆盖校验器逐 issue 检查的超集：引用、严重度、置信度、标题判断重复、标识符归属、
    硬编码确认化。结构性错误（遗漏全部问题、总体/分节置信度上限）不在此列，
    由调用方直接抛出。
    """
    errors: list[str] = []
    source_corpora = {
        source: _source_report_corpus(source, digests)
        for source in _SUMMARY_SOURCES
    }
    for issue in summary.issues:
        findings = digests[issue.source].findings
        if issue.source_finding > len(findings):
            errors.append(
                f"{issue.source}#{issue.source_finding}：引用了不存在的问题"
            )
            continue
        source_finding = findings[issue.source_finding - 1]
        if issue.severity != source_finding.severity:
            errors.append(
                f"{issue.source}#{issue.source_finding}：severity 应为 "
                f"{source_finding.severity}（来源 finding「{source_finding.title}」），"
                f"实际为 {issue.severity}；若该 issue 的内容实际来自其他 finding，"
                "请改引用并同步修正严重度与置信度"
            )
        source_confidence = round(source_finding.confidence * 100)
        if issue.confidence > source_confidence:
            errors.append(
                f"{issue.source}#{issue.source_finding}：confidence {issue.confidence} "
                f"高于来源 {source_confidence}"
            )
        if not _has_distinct_detail(issue.title, issue.judgment):
            errors.append(f"{issue.source}#{issue.source_finding}：标题与判断重复")
        unattributed = _unattributed_issue_identifiers(
            issue, source_corpora[issue.source]
        )
        if unattributed:
            errors.append(
                f"{issue.source}#{issue.source_finding}：引入来源不存在的代码标识符 "
                + "、".join(unattributed)
            )
        if (
            issue.source == "description"
            and "硬编码线索" in source_finding.title
            and re.search(
                r"(?:已|人工智能（AI）复核)?确认|构成(?:作弊|硬编码)", issue.judgment
            )
        ):
            errors.append(
                f"{issue.source}#{issue.source_finding}：把疑似硬编码线索改写成了确认结论"
            )
    return errors


def _repair_ai_summary_analysis(
    summary: AISummary,
    digests: dict[str, ReportDigest],
    ai_path: Path,
    input_path: Path,
    repo_id: str,
    schema_hint: str,
    errors: list[str],
) -> dict:
    """定向修复 issue 级交付错误：只允许按来源 finding 修正引用、严重度、置信度与表述。

    修复任务是只读式小范围修改（与 tree_builder 的 verdict 定向修复同构）：
    未列入错误清单的 issue 与字段一律保持原样，随后由调用方整体重跑交付校验。
    """
    repair_path = ai_path.with_name(ai_path.stem + ".repair.json")
    issues_dump = json.dumps(
        [issue.model_dump() for issue in summary.issues],
        ensure_ascii=False,
        indent=2,
    )
    request = (
        "上次生成的最终评审摘要未通过交付校验。请先读取随消息附加的 summary input JSON，"
        "逐一核对每个 issue 与其 source_finding 的实际内容，只修复下面列出的错误：\n"
        "错误清单：\n"
        + "\n".join("- " + error for error in errors)
        + "\n\n"
        "当前 issues：\n"
        + issues_dump
        + "\n\n"
        "修复规则：source_finding 必须指向真实存在的 finding（一基序号）；severity 必须"
        "与来源 finding 完全一致；confidence 不得高于来源；issue 中的函数名、路径名和"
        "代码标识符必须来自其引用的 finding，禁止改写或补造相近名称；不得把疑似硬编码"
        "线索写成确认结论；标题与判断不得重复。未列入错误清单的 issue 和字段一律保持"
        "原样，overall_judgment 与 sections 保持原样，禁止改动。\n"
        f"repo_id: {repo_id}\n"
        f"input_file: {input_path.resolve()}\n"
        f"expected_schema: {schema_hint}\n"
        f"output_path: {repair_path.resolve()}\n"
        "只调用 write_report；content 为修复后的完整合法 JSON（overall_judgment、"
        "sections 与全部 issues），output_path 必须使用上面的绝对路径。"
    )
    task = BatchTask(
        batch_id=f"summary-repair-{re.sub(r'[^A-Za-z0-9_.-]+', '-', repo_id)[:80]}",
        agent_name="os-kernel-summary-repair",
        user_request=request,
        output_path=repair_path,
        cache_dir=ai_path.parent,
        cache_key="",
        cache_enabled=False,
        fallback={},
        input_files=(input_path,),
    )
    repaired = run_batch_task(task, schema_hint=schema_hint, timeout=300)
    if not repaired or repaired.get("_error"):
        raise SummaryPdfError("摘要修复智能体未返回可用结果：" + "；".join(errors))
    return repaired


def run_ai_summary_analysis(
    digests: dict[str, ReportDigest],
    repo_id: str,
    output_path: Path,
) -> AISummary:
    """让专用智能体完成取舍、判断和全部作品相关表述。"""
    ai_path = output_path.with_suffix(".ai.json")
    input_path = output_path.with_suffix(".input.json")
    input_payload = {
        "repo_id": repo_id,
        "reports": {
            source: _summary_input_digest(digests[source])
            for source in _SUMMARY_SOURCES
        },
    }
    input_path.write_text(
        json.dumps(input_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    schema_hint = (
        '{"overall_judgment":str,"confidence":0-100,'
        '"sections":[{"source":"description|development|comparison",'
        '"conclusion":str,"confidence":0-100}],'
        '"issues":[{"source":"description|development|comparison",'
        '"source_finding":int,"title":str,"judgment":str,'
        '"severity":"info|low|medium|high|critical","confidence":0-100}]}'
    )
    request = (
        "请站在操作系统内核赛题评委角度，完整阅读随消息附加的 summary input JSON，"
        "生成可直接进入一页 A4 PDF 的最终评审摘要。摘要的实质性文字必须全部由你生成，"
        "参赛队不会做人工 review 或修改。先给总体判断，再按作品描述、开发过程、历史作品"
        "对比的顺序适度展开；选择不超过五个最影响评审的问题，写清发现、影响、AI 判断和"
        "置信度。只能使用输入事实，不能编造数字、日期、作品名或结论；每个 issue 必须引用"
        "真实的 source_finding 一基序号，severity 与来源一致，confidence 不得高于来源 finding。"
        "总体 confidence 不得高于三份报告 confidence 的最低值，每个 section confidence 不得高于"
        "对应来源报告 confidence。"
        "证据不足时降低置信度并使用审慎表述。文字要简洁、自然、无模板腔。\n"
        "硬编码部分只使用 AI 复核后的 hardcode_confirmed 与 hardcode_suspected；确认项与疑似项必须分开表述，不得把疑似证据并入确认结论；"
        "不得在摘要中引用原始扫描候选数、扫描命中数、排除数或扫描文件数。\n"
        "开发过程报告只证明提交历史；不得写 LTP 或其他测试已通过、跑通或成功。\n"
        "本摘要不分析编译与构建：不得写编译通过、编译失败、构建入口、双架构编译或镜像编译等表述；"
        "不得称作品为完整、完备、可运行或已经验证的操作系统内核。\n"
        "所有文字必须使用完整句子，禁止使用‘…’或‘...’省略内容；不得提交被截断的词组。"
        "overall_judgment 不超过 280 字，每个 section conclusion 不超过 180 字，"
        "每个 issue title 不超过 80 字、judgment 不超过 240 字；issue 中的函数名、路径名和代码标识符必须来自其引用的 source_finding，"
        "禁止改写或补造相近名称（例如不得把 finding 中的 execve 改写成 sys_execve，"
        "也不得引入 finding 中不存在的其他函数名如 fork），否则会导致交付失败。\n"
        "不要在 issue 文字中裸写与代码标识符写法相同的专有缩写或术语（如 POSIX、ABI、MMIO、ELF、syscall）；"
        "表达标准或接口语义时用自然语言描述，必要时只使用来源 finding 明文中确实出现的写法。"
        "全篇任何英文术语（如 syscall）首次出现时必须给出中文解释（如 系统调用（syscall）），否则无法通过可读性校验。\n"
        "输出 JSON 必须严格符合 expected_schema 的字段，禁止任何额外字段（尤其不得回传 "
        "repo_id 或输入文件路径）；多出的字段会导致交付失败。\n"
        f"repo_id: {repo_id}\n"
        f"input_file: {input_path.resolve()}\n"
        f"expected_schema: {schema_hint}\n"
        f"output_path: {ai_path.resolve()}\n"
        "只调用 write_report；content 为合法 JSON 字符串，output_path 必须使用上面的绝对路径。"
    )

    def _delivery_complete(value: dict) -> bool | str:
        try:
            _validate_ai_summary_result(value, digests)
        except SummaryPdfError as exc:
            return str(exc)
        return True

    task = BatchTask(
        batch_id=f"summary-{re.sub(r'[^A-Za-z0-9_.-]+', '-', repo_id)[:80]}",
        agent_name="os-kernel-summary",
        user_request=request,
        output_path=ai_path,
        cache_dir=output_path.parent,
        cache_key="",
        cache_enabled=False,
        fallback={},
        input_files=(input_path,),
        cache_validator=_delivery_complete,
    )
    result = run_batch_task(task, schema_hint=schema_hint, timeout=480)
    try:
        return _validate_ai_summary_result(result, digests)
    except SummaryPdfError:
        if not isinstance(result, dict) or result.get("_error"):
            raise
        normalized = _normalize_zero_based_finding_refs(
            _compact_ai_summary_fields(result), digests
        )
        try:
            summary = AISummary.model_validate(normalized)
        except ValidationError:
            raise  # 结构无效（缺字段/错类型），无法定向修复
        errors = _collect_ai_summary_issue_errors(summary, digests)
        if not errors:
            raise  # 结构性错误（遗漏全部问题、置信度上限等）不在修复范围
        print(f"[summary] {repo_id} 存在 {len(errors)} 条 issue 级交付错误，触发定向修复")
        repaired = _repair_ai_summary_analysis(
            summary, digests, ai_path, input_path, repo_id,
            schema_hint, errors,
        )
        return _validate_ai_summary_result(repaired, digests)


def _styles(font: str, bold: str, *, compact: bool) -> dict[str, ParagraphStyle]:
    body_leading = 13.5 if compact else 14.4
    return {
        "title": ParagraphStyle(
            "title", fontName=bold, fontSize=16.5, leading=20,
            textColor=colors.HexColor("#101828"),
        ),
        "meta": ParagraphStyle(
            "meta", fontName=font, fontSize=8.5, leading=10.5,
            textColor=colors.HexColor("#667085"), alignment=TA_RIGHT,
        ),
        "meta_left": ParagraphStyle(
            "meta_left", fontName=font, fontSize=8.5, leading=10.5,
            textColor=colors.HexColor("#667085"),
        ),
        "lead": ParagraphStyle(
            "lead", fontName=font, fontSize=11, leading=15.5 if compact else 16.2,
            textColor=colors.HexColor("#172033"),
        ),
        "section": ParagraphStyle(
            "section", fontName=bold, fontSize=11.5, leading=14,
            textColor=colors.HexColor("#173b6c"),
        ),
        "body": ParagraphStyle(
            "body", fontName=font, fontSize=BODY_FONT_SIZE, leading=body_leading,
            textColor=colors.HexColor("#27364b"), alignment=TA_LEFT,
        ),
        "issue_title": ParagraphStyle(
            "issue_title", fontName=font, fontSize=BODY_FONT_SIZE, leading=body_leading,
            textColor=colors.HexColor("#172033"),
        ),
        "confidence": ParagraphStyle(
            "confidence", fontName=font, fontSize=8.5, leading=body_leading,
            textColor=colors.HexColor("#667085"), alignment=TA_RIGHT,
        ),
        "small": ParagraphStyle(
            "small", fontName=font, fontSize=8.5, leading=10.5,
            textColor=colors.HexColor("#667085"),
        ),
    }


def _issue_card(
    issue: AISummaryIssue,
    style: dict[str, ParagraphStyle],
    available_width: float,
    *,
    compact: bool,
) -> Table:
    accent = colors.HexColor(_SEVERITY_COLORS[issue.severity])
    header = Paragraph(
        f'<font color="{_SEVERITY_COLORS[issue.severity]}"><b>'
        f'{_safe(_SEVERITY_LABELS[issue.severity])}</b></font>　'
        f'<b>{_safe(issue.title)}</b>',
        style["issue_title"],
    )
    confidence = Paragraph(f"置信度 {issue.confidence}%", style["confidence"])
    judgment = Paragraph(_safe(issue.judgment), style["body"])
    table = Table(
        [[header, confidence], [judgment, ""]],
        colWidths=[available_width - 28 * mm, 28 * mm],
        style=TableStyle([
            ("SPAN", (0, 1), (1, 1)),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
            ("LINEBEFORE", (0, 0), (0, -1), 2.2, accent),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ("TOPPADDING", (0, 0), (-1, 0), 3 if compact else 4),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
            ("TOPPADDING", (0, 1), (-1, 1), 0),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 4 if compact else 5),
        ]),
    )
    table.hAlign = "LEFT"
    return table


def _story(
    summary: AISummary,
    repo_id: str,
    font: str,
    bold: str,
    *,
    compact: bool,
) -> list:
    style = _styles(font, bold, compact=compact)
    available_width = A4[0] - 2 * _PAGE_MARGIN
    vertical = 3 if compact else 4
    issues_by_source: dict[str, list[AISummaryIssue]] = {
        source: [] for source in _SUMMARY_SOURCES
    }
    for issue in summary.issues:
        issues_by_source[issue.source].append(issue)

    header = Table(
        [[Paragraph("人工智能（AI）评审摘要", style["title"]),
          Paragraph("单页决策视图", style["meta"])]],
        colWidths=[available_width - 40 * mm, 40 * mm],
        style=TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]),
    )
    story: list = [
        header,
        Spacer(1, 2),
        HRFlowable(width="100%", thickness=1.2, color=colors.HexColor("#1d4ed8")),
        Spacer(1, 3),
        Table(
            [[Paragraph(f"作品编号：{_safe(repo_id)}", style["meta_left"])]],
            colWidths=[available_width],
            style=TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]),
        ),
        Spacer(1, 5 if compact else 6),
        Table(
            [[Paragraph(
                f'<b>AI 总体判断</b>　<font color="#475467" size="8.5">'
                f'置信度 {summary.confidence}%</font><br/>{_safe(summary.overall_judgment)}',
                style["lead"],
            )]],
            colWidths=[available_width],
            style=TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef4ff")),
                ("BOX", (0, 0), (-1, -1), .7, colors.HexColor("#9bb8ef")),
                ("LEFTPADDING", (0, 0), (-1, -1), 9),
                ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                ("TOPPADDING", (0, 0), (-1, -1), 6 if compact else 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6 if compact else 7),
            ]),
        ),
        Spacer(1, 5 if compact else 7),
        Paragraph("AI 检出问题与判断", style["section"]),
        Spacer(1, 2),
    ]

    for index, section in enumerate(summary.sections, start=1):
        source = section.source
        section_heading: list = [
            Table(
                [[Paragraph(
                    f'<b>{index}. {_safe(_SOURCE_LABELS[source])}</b>', style["section"]
                ), Paragraph(f"结论置信度 {section.confidence}%", style["meta"])]],
                colWidths=[available_width - 34 * mm, 34 * mm],
                style=TableStyle([
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f1f5f9")),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]),
            ),
            Spacer(1, 2),
            Paragraph(_safe(section.conclusion), style["body"]),
        ]
        story.append(KeepTogether(section_heading))
        for issue in issues_by_source[source]:
            story.extend([
                Spacer(1, 2 if compact else 3),
                _issue_card(issue, style, available_width, compact=compact),
            ])
        story.append(Spacer(1, vertical))

    story.extend([
        Spacer(1, 1),
        HRFlowable(width="100%", thickness=.6, color=colors.HexColor("#d0d5dd")),
        Spacer(1, 3),
        Paragraph(
            "使用说明：内容仅用于安排评委核查顺序，不替代源码与正式日志、比赛章程与现场说明。",
            style["small"],
        ),
    ])
    return story


def _build_pdf_bytes(digests: dict[str, ReportDigest], repo_id: str, summary: AISummary) -> bytes:
    font, bold = _register_fonts()
    last_pages = 0
    for compact in (False, True):
        buffer = io.BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=_PAGE_MARGIN,
            rightMargin=_PAGE_MARGIN,
            topMargin=11 * mm if compact else 12 * mm,
            bottomMargin=9 * mm if compact else 10 * mm,
            title=f"{repo_id} AI 评审摘要",
            author="OS 内核代码分析 Agent",
        )
        document.build(_story(summary, repo_id, font, bold, compact=compact))
        data = buffer.getvalue()
        last_pages = len(PdfReader(io.BytesIO(data)).pages)
        if last_pages == 1:
            return data
    raise SummaryPdfError(
        f"AI 摘要无法在 {BODY_FONT_SIZE} 磅正文字号下完整排入一页（当前 {last_pages} 页）"
    )


def validate_summary_pdf(path: str | Path) -> dict:
    source = Path(path)
    reader = PdfReader(str(source))
    errors: list[str] = []
    if len(reader.pages) != 1:
        errors.append(f"页数为 {len(reader.pages)}，要求 1 页")
    for page in reader.pages:
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if abs(width - A4[0]) > 1 or abs(height - A4[1]) > 1:
            errors.append(f"页面不是 A4：{width:.1f}×{height:.1f}")
        for ref in page.get("/Annots") or []:
            annotation = ref.get_object()
            if str(annotation.get("/Subtype") or "") == "/Link" or annotation.get("/A"):
                errors.append("PDF 含超链接注释")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text.strip()) < 120:
        errors.append("PDF 可提取正文过短，可能渲染失败")
    required = ("AI 总体判断", "AI 检出问题与判断", "作品描述与运行质量", "开发过程", "历史作品对比")
    missing = [label for label in required if label not in text]
    if missing:
        errors.append("PDF 缺少评审层级：" + "、".join(missing))
    if re.search(r"…|(?<!\.)\.{3}(?!\.)", text):
        errors.append("PDF 正文含省略号截断")
    if errors:
        raise SummaryPdfError("；".join(errors))
    return {"pages": 1, "page_size": "A4", "links": 0, "text_chars": len(text.strip())}


def generate_summary_pdf(
    digest_paths: list[str | Path],
    output_path: str | Path,
    *,
    repo_id: str | None = None,
) -> dict:
    digests = load_digests(digest_paths)
    resolved_repo_id = repo_id or digests["description"].repo_id
    target = Path(output_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    summary = run_ai_summary_analysis(digests, resolved_repo_id, target)
    target.write_bytes(_build_pdf_bytes(digests, resolved_repo_id, summary))
    validation = validate_summary_pdf(target)
    return {
        "pdf_path": str(target),
        "ai_path": str(target.with_suffix(".ai.json")),
        "repo_id": resolved_repo_id,
        **validation,
    }
