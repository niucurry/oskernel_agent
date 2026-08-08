"""生成决赛要求的一页 A4 摘要 PDF。"""

from __future__ import annotations

import html
import io
import os
from pathlib import Path

from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .models import Finding, ReportDigest
from .readability import clip_at_sentence, explain_terms_on_first_use, remove_ai_filler

BODY_FONT_SIZE = 10.5
_PAGE_MARGIN = 15 * mm


class SummaryPdfError(RuntimeError):
    pass


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
    missing = {"description", "development", "comparison"} - set(digests)
    if missing:
        raise SummaryPdfError("缺少摘要输入：" + "、".join(sorted(missing)))
    return digests


def _safe(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def _combined_findings(digests: dict[str, ReportDigest], limit: int) -> list[Finding]:
    rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
    grouped: dict[tuple[str, str], list[Finding]] = {}
    for kind in ("description", "development", "comparison"):
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
            "detail": clip_at_sentence(
                f"发现 {len(items)} 次同类线索；代表项：{representative.detail}",
                360,
            ),
        }))

    ordered = sorted(
        collapsed,
        key=lambda item: (-rank[item.severity], -item.confidence, item.title),
    )
    # 摘要不是问题数量排行榜。先让三份报告各保留一条代表判断，
    # 再按风险补齐，避免同一类开发历史线索挤掉代码或对比结论。
    selected: list[Finding] = []
    for kind in ("description", "development", "comparison"):
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


def _executive_conclusion(digests: dict[str, ReportDigest], repo_id: str) -> str:
    development = digests["development"].metrics
    comparison = digests["comparison"].metrics
    high = sum(
        1 for item in _combined_findings(digests, 24)
        if item.severity in ("high", "critical")
    )
    source = str(comparison.get("closest_source") or "未确定")
    pct = comparison.get("overall_similarity_pct", 0)
    commits = development.get("commit_count", 0)
    text = (
        f"{repo_id} 有 {high} 类高风险线索需要优先复核。"
        f"代码与 {source} 最接近，高置信同源函数比例为 {pct}%；"
        f"当前可见开发历史包含 {commits} 次提交。"
    )
    return clip_at_sentence(explain_terms_on_first_use(remove_ai_filler(text)), 190)


def _overview_lines(digests: dict[str, ReportDigest]) -> list[tuple[str, str]]:
    desc = digests["description"]
    dev = digests["development"]
    comp = digests["comparison"]
    desc_metrics, dev_metrics, comp_metrics = desc.metrics, dev.metrics, comp.metrics
    top_modules = "、".join(
        f"{module.name} {module.similarity_pct:.1f}%"
        for module in comp.modules[:3]
        if module.similarity_pct is not None
    ) or "未形成模块级比例"
    status_text = {
        "passed": "通过",
        "failed": "失败",
        "unknown": "未能确认",
        "not_provided": "未提供",
        "missing": "文件缺失",
        "skipped": "不适用",
    }
    build_status = status_text.get(
        str(desc_metrics.get("build_log_status", "not_provided")), "未能确认"
    )
    run_status = status_text.get(
        str(desc_metrics.get("run_log_status", "not_provided")), "未能确认"
    )
    return [
        (
            "作品描述",
            clip_at_sentence(
                f"{desc.conclusion} 编译日志：{build_status}；"
                f"运行日志：{run_status}；"
                f"硬编码线索 {desc_metrics.get('hardcode_signals', 0)} 条。",
                170,
            ),
        ),
        (
            "开发过程",
            clip_at_sentence(
                f"{dev_metrics.get('commit_count', 0)} 次提交，"
                f"时间跨度 {dev_metrics.get('start_date') or '未知'} 至 {dev_metrics.get('end_date') or '未知'}；"
                f"大规模提交 {dev_metrics.get('large_commit_count', 0)} 次，"
                f"阈值 {dev_metrics.get('large_commit_threshold', 0)} 代码行（LOC）。",
                170,
            ),
        ),
        (
            "对比分析",
            clip_at_sentence(
                f"最近历史作品为 {comp_metrics.get('closest_source') or '未确定'}；"
                f"整体高置信同源函数比例 {comp_metrics.get('overall_similarity_pct', 0)}%；"
                f"模块前三项：{top_modules}。",
                170,
            ),
        ),
    ]


def _styles(font: str, bold: str, *, detail_limit: int) -> dict[str, ParagraphStyle]:
    return {
        "title": ParagraphStyle("title", fontName=bold, fontSize=18, leading=22,
                                textColor=colors.HexColor("#172033"), spaceAfter=3),
        "meta": ParagraphStyle("meta", fontName=font, fontSize=8.5, leading=11,
                               textColor=colors.HexColor("#667085")),
        "lead": ParagraphStyle("lead", fontName=bold, fontSize=11.5, leading=17,
                               textColor=colors.HexColor("#172033")),
        "section": ParagraphStyle("section", fontName=bold, fontSize=12.5, leading=16,
                                  textColor=colors.HexColor("#1d4ed8"), spaceBefore=3, spaceAfter=4),
        "body": ParagraphStyle("body", fontName=font, fontSize=BODY_FONT_SIZE, leading=15,
                               textColor=colors.HexColor("#27364b"), alignment=TA_LEFT),
        "small": ParagraphStyle("small", fontName=font, fontSize=8.5, leading=12,
                                textColor=colors.HexColor("#667085")),
        "label": ParagraphStyle("label", fontName=bold, fontSize=BODY_FONT_SIZE, leading=15,
                                textColor=colors.HexColor("#172033")),
    }


def _story(
    digests: dict[str, ReportDigest],
    repo_id: str,
    font: str,
    bold: str,
    *,
    finding_limit: int,
    detail_limit: int,
) -> list:
    style = _styles(font, bold, detail_limit=detail_limit)
    confidences = [digest.confidence for digest in digests.values()]
    confidence = round(min(confidences) * 100)
    story: list = [
        Paragraph("决赛评审摘要", style["title"]),
        Paragraph(f"作品：{_safe(repo_id)}　综合置信度：{confidence}%", style["meta"]),
        Spacer(1, 5),
        Table([[Paragraph(_safe(_executive_conclusion(digests, repo_id)), style["lead"])]],
              colWidths=[A4[0] - 2 * _PAGE_MARGIN], style=TableStyle([
                  ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#eef4ff")),
                  ("BOX", (0, 0), (-1, -1), .7, colors.HexColor("#9bb8ef")),
                  ("LEFTPADDING", (0, 0), (-1, -1), 10),
                  ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                  ("TOPPADDING", (0, 0), (-1, -1), 8),
                  ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
              ])),
        Spacer(1, 7),
        Paragraph("优先复核", style["section"]),
    ]
    severity_color = {
        "critical": "#991b1b", "high": "#b91c1c", "medium": "#b45309",
        "low": "#4b5563", "info": "#4b5563",
    }
    findings = _combined_findings(digests, finding_limit)
    for index, finding in enumerate(findings, start=1):
        detail = clip_at_sentence(explain_terms_on_first_use(finding.detail), detail_limit)
        title = explain_terms_on_first_use(finding.title)
        story.append(KeepTogether([
            Paragraph(
                f'<font color="{severity_color[finding.severity]}"><b>{index}. {_safe(title)}</b></font>'
                f'　<font color="#667085">置信度 {round(finding.confidence * 100)}%</font>',
                style["body"],
            ),
            Paragraph(_safe(detail), style["body"]),
            Spacer(1, 3),
        ]))

    story.extend([Paragraph("三份报告速览", style["section"])])
    rows = [
        [Paragraph(_safe(label), style["label"]), Paragraph(_safe(text), style["body"])]
        for label, text in _overview_lines(digests)
    ]
    story.append(Table(rows, colWidths=[26 * mm, A4[0] - 2 * _PAGE_MARGIN - 26 * mm],
                       style=TableStyle([
                           ("VALIGN", (0, 0), (-1, -1), "TOP"),
                           ("GRID", (0, 0), (-1, -1), .45, colors.HexColor("#d8e0ea")),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f6fa")),
                           ("LEFTPADDING", (0, 0), (-1, -1), 7),
                           ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                           ("TOPPADDING", (0, 0), (-1, -1), 5),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                       ])))
    story.extend([
        Spacer(1, 6),
        Paragraph(
            "判断边界：本摘要用于安排评委核查顺序。代码相似、硬编码和人工智能（AI）生成代码信号均需结合源码、"
            "正式编译运行日志、比赛章程与现场说明复核，不能单独作为违规或扣分依据。",
            style["small"],
        ),
    ])
    return story


def _build_pdf_bytes(digests: dict[str, ReportDigest], repo_id: str) -> bytes:
    font, bold = _register_fonts()
    attempts = [(5, 120), (4, 105), (4, 85), (3, 75)]
    last_pages = 0
    for finding_limit, detail_limit in attempts:
        buffer = io.BytesIO()
        document = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=_PAGE_MARGIN, rightMargin=_PAGE_MARGIN,
            topMargin=13 * mm, bottomMargin=12 * mm,
            title=f"{repo_id} 决赛评审摘要", author="OS 内核代码分析 Agent",
        )
        document.build(_story(
            digests, repo_id, font, bold,
            finding_limit=finding_limit, detail_limit=detail_limit,
        ))
        data = buffer.getvalue()
        last_pages = len(PdfReader(io.BytesIO(data)).pages)
        if last_pages == 1:
            return data
    raise SummaryPdfError(f"摘要内容无法在 10.5 磅正文字号下压缩到一页（当前 {last_pages} 页）")


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
    if len(text.strip()) < 80:
        errors.append("PDF 可提取正文过短，可能渲染失败")
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
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_build_pdf_bytes(digests, resolved_repo_id))
    validation = validate_summary_pdf(target)
    return {"pdf_path": str(target), "repo_id": resolved_repo_id, **validation}
