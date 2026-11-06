"""
HTML 渲染：tree.json → 多层 Alpine.js 折叠树报告。

顶部：固定 verdict 卡片（多维度评分 + 亮点 / 槽点 + 一句话总评）
下方：递归折叠树，每个节点可点击展开 / 折叠，状态存 localStorage
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from oskernel_agent.finals.digests import (
    description_digest_from_tree,
    description_priority_findings,
    description_review_sections,
    normalize_description_claim,
)
from oskernel_agent.finals.readability import (
    clip_at_sentence,
    concise_module_summary,
    explain_terms_in_html,
    is_dependency_scope_only_issue,
    html_to_text,
    remove_ai_filler,
)

from ..report_quality import IncompleteReportError, assert_report_complete, sanitize_code_ellipses
from .html import (
    _BROKEN_PREFIX,
    _CDN_HEAD,
    assert_toc_resolves,
    derive_repo_web_base,
    linkify_html,
    make_file_link_resolver,
    repository_url_to_web_base,
)

# 架构图/流程图已弃用：剥离正文里残留的 Mermaid 块（防 LLM 偶发输出）
_MERMAID_RE = re.compile(
    r'<pre\b[^>]*class="[^"]*\bmermaid\b[^"]*"[^>]*>.*?</pre>',
    re.DOTALL | re.IGNORECASE,
)
_ECHARTS_CHART_RE = re.compile(
    r'<div\b(?=[^>]*class="[^"]*\becharts-chart\b)[^>]*>\s*'
    r'<script\b[^>]*type=["\']application/json["\'][^>]*>.*?</script>\s*</div>',
    re.DOTALL | re.IGNORECASE,
)


def _strip_diagrams(content: str) -> str:
    return _MERMAID_RE.sub("", content) if content else content


def _strip_echarts_charts(content: str) -> str:
    return _ECHARTS_CHART_RE.sub("", content) if content else content


_ID_ATTR_RE = re.compile(r'\s+id\s*=\s*(?:"[^"]*"|\'[^\']*\')', re.IGNORECASE)


def _strip_html_ids(content: str) -> str:
    """剥离 LLM 正文里自带的 id 属性，避免与报告结构锚点（如 id="verdict"）撞车，
    导致 assert_toc_resolves 判重复锚点而整份报告渲染失败。"""
    return _ID_ATTR_RE.sub("", content) if content else content


# 复用 html.py 的 CDN 头：已含 Tailwind + ECharts + Mermaid + Alpine 及其初始化。
# 折叠节点展开时让其中的 ECharts 重新计算尺寸（初次在 display:none 下 init 会是 0 尺寸）。
_TREE_HEAD = _CDN_HEAD

# 评委速读版没有图表和交互树，不再加载 ECharts / Alpine.js。
_BRIEF_HEAD = '<script src="https://cdn.tailwindcss.com"></script>'

_TREE_RESIZE_ON_OPEN = """
<script>
document.addEventListener('section:opened', function () {
  setTimeout(function () {
    document.querySelectorAll('.echarts-chart[data-rendered]').forEach(function (el) {
      if (el.__chart) el.__chart.resize();
    });
  }, 60);
});
</script>
"""

_TREE_CSS = """
:root { color-scheme: light dark; }
html { scroll-behavior: smooth; }
html, body {
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
               Helvetica, Arial, sans-serif;
}
/* 锚点跳转时与视口顶部留出间距，避免标题贴边 */
[id] { scroll-margin-top: 1rem; }
/* 左侧目录 */
.toc-nav { scrollbar-width: thin; }
.toc-nav a.toc-link {
  display: block; padding: 0.2rem 0.6rem; border-radius: 0.375rem;
  font-size: 0.8rem; line-height: 1.4; color: rgb(100 116 139);
  border-left: 2px solid transparent; text-decoration: none;
  overflow-wrap: anywhere; white-space: normal;
}
.toc-nav a.toc-link:hover { color: rgb(15 23 42); background: rgb(148 163 184 / 0.12); }
.toc-nav a.toc-link.toc-sub { padding-left: 1.4rem; font-size: 0.75rem; }
.toc-nav a.toc-link.toc-sub2 {
  padding-left: 2.2rem; font-size: 0.72rem; color: rgb(148 163 184);
}
.toc-nav a.toc-active {
  color: rgb(37 99 235); font-weight: 600;
  border-left-color: rgb(37 99 235); background: rgb(37 99 235 / 0.08);
}
@media (prefers-color-scheme: dark) {
  .toc-nav a.toc-link { color: rgb(148 163 184); }
  .toc-nav a.toc-link:hover { color: rgb(241 245 249); }
  .toc-nav a.toc-active { color: rgb(96 165 250); border-left-color: rgb(96 165 250); }
}
a.file-jump { border-bottom: 1px dashed currentColor; text-decoration: none; }
a.file-jump:hover { background: rgba(9, 105, 218, 0.08); }
.score-pill {
  display: inline-block; padding: 0 0.5rem; border-radius: 9999px;
  font-size: 0.75rem; font-weight: 600; margin-right: 0.5rem;
}
.score-green  { background: rgb(34 197 94 / 0.18); color: rgb(21 128 61); }
.score-yellow { background: rgb(234 179 8 / 0.20); color: rgb(133 77 14); }
.score-red    { background: rgb(239 68 68 / 0.18); color: rgb(153 27 27); }
@media (prefers-color-scheme: dark) {
  .score-green  { color: rgb(134 239 172); }
  .score-yellow { color: rgb(253 224 71); }
  .score-red    { color: rgb(252 165 165); }
}
.tree-node { padding-left: 1.25rem; border-left: 1px dashed rgb(148 163 184 / 0.3); }
.tree-toggle { cursor: pointer; user-select: none; }
.tree-toggle:hover { color: rgb(59 130 246); }
.severity-low    { color: rgb(234 179 8); }
.severity-medium { color: rgb(249 115 22); }
.severity-high   { color: rgb(220 38 38); }
.severity-critical { color: rgb(153 27 27); font-weight: 700; }
.finding-card { border-left: 3px solid rgb(249 115 22); padding: .65rem .8rem; background: rgb(255 247 237); }
.finding-card.high, .finding-card.critical { border-left-color: rgb(220 38 38); background: rgb(254 242 242); }
.detail-toggle > summary { cursor: pointer; color: rgb(71 85 105); font-size: .82rem; font-weight: 600; }
.detail-toggle[open] > summary { margin-bottom: .75rem; }
@media (prefers-color-scheme: dark) {
  .finding-card { background: rgb(120 53 15 / .16); }
  .finding-card.high, .finding-card.critical { background: rgb(127 29 29 / .16); }
  .detail-toggle > summary { color: rgb(148 163 184); }
}
/* 正文标题压制：agent 写的 h1-h6 一律小于所在节点标题，避免下级标题比上级大。
   .node-content hN 特异性 (0,1,1) 高于 Tailwind prose 的 (0,1,0)，无论加载顺序都生效。 */
.node-content h1, .node-content h2 { font-size: 0.95rem; font-weight: 700; margin: 0.7em 0 0.35em; line-height: 1.35; }
.node-content h3 { font-size: 0.9rem; font-weight: 600; margin: 0.6em 0 0.3em; }
.node-content h4, .node-content h5, .node-content h6 { font-size: 0.85rem; font-weight: 600; margin: 0.5em 0 0.25em; }
/* verdict 是头部重点分析，标题略大、可读，但仍低于"代码树"区块标题(text-xl=1.25rem) */
.verdict-content h1, .verdict-content h2 { font-size: 1.1rem; font-weight: 700; margin: 0.8em 0 0.4em; line-height: 1.35; }
.verdict-content h3 { font-size: 1rem; font-weight: 600; margin: 0.7em 0 0.35em; }
.verdict-content h4, .verdict-content h5, .verdict-content h6 { font-size: 0.9rem; font-weight: 600; margin: 0.5em 0 0.25em; }
"""

_BRIEF_CSS = """
:root { color-scheme: light dark; }
html { scroll-behavior: smooth; }
html, body { font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
[id] { scroll-margin-top: 1rem; }
a.file-jump { color: rgb(37 99 235); border-bottom: 1px dashed currentColor; text-decoration: none; }
a.file-jump:hover { background: rgb(37 99 235 / 0.08); }
.brief-card { border: 1px solid rgb(203 213 225); border-radius: 0.75rem; background: white; }
.status-ok { color: rgb(21 128 61); background: rgb(220 252 231); }
.status-warn { color: rgb(180 83 9); background: rgb(254 243 199); }
.status-bad { color: rgb(185 28 28); background: rgb(254 226 226); }
.status-info { color: rgb(71 85 105); background: rgb(226 232 240); }
.status-pill { display: inline-block; padding: 0.1rem 0.55rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 700; }
.finding-row { border-left: 3px solid rgb(245 158 11); padding-left: 0.8rem; }
.core-card { border-top: 1px solid rgb(226 232 240); padding: 0.9rem 0; }
.core-card:first-child { border-top: 0; padding-top: 0; }
.toc-nav a { display: block; padding: 0.25rem 0.6rem; color: rgb(100 116 139); text-decoration: none; border-left: 2px solid transparent; }
.toc-nav a:hover { color: rgb(30 64 175); border-left-color: rgb(96 165 250); }
@media (prefers-color-scheme: dark) {
  .brief-card { border-color: rgb(51 65 85); background: rgb(30 41 59); }
  .core-card { border-color: rgb(51 65 85); }
  .status-ok { color: rgb(134 239 172); background: rgb(20 83 45); }
  .status-warn { color: rgb(253 230 138); background: rgb(120 53 15); }
  .status-bad { color: rgb(254 202 202); background: rgb(127 29 29); }
  .status-info { color: rgb(203 213 225); background: rgb(51 65 85); }
  a.file-jump { color: rgb(147 197 253); }
}
@media print {
  .toc-nav { display: none !important; }
  main { max-width: none !important; }
  a.file-jump { color: inherit; border-bottom: 0; }
}
"""


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _score_pill(score) -> str:
    s = int(score or 0)
    cls = "score-green" if s >= 85 else ("score-yellow" if s >= 70 else "score-red")
    return f'<span class="score-pill {cls}">{s}</span>'


def _render_verdict_radar(verdict: dict) -> str:
    dims = [d for d in verdict.get("dimensions", []) if isinstance(d, dict)]
    points: list[tuple[str, int]] = []
    for d in dims:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        try:
            score = int(round(float(d.get("score") or 0)))
        except (TypeError, ValueError):
            score = 0
        points.append((name, max(0, min(100, score))))
    if not points:
        return ""

    option = {
        "title": {"text": f"{len(points)}维雷达图", "left": "center"},
        "tooltip": {},
        "radar": {
            "indicator": [{"name": name, "max": 100} for name, _ in points],
            "center": ["50%", "55%"],
            "radius": "65%",
        },
        "series": [{
            "name": "评分",
            "type": "radar",
            "data": [{"name": "评分", "value": [score for _, score in points]}],
            "areaStyle": {"opacity": 0.18},
            "lineStyle": {"width": 2},
        }],
    }
    option_json = json.dumps(option, ensure_ascii=False).replace("</", "<\\/")
    return (
        '<div class="echarts-chart" style="height:380px">'
        f'<script type="application/json">{option_json}</script>'
        '</div>'
    )


def _resolve_path_anchor(path_with_line: str, resolver) -> str:
    """形如 'kernel/trap.c:42' 的字符串解析为链接。"""
    if not path_with_line:
        return ""
    # 支持单行与行号范围两种写法：`file.c:84`、`file.c:84-88`、`file.c#L84-88`
    m = re.search(r"(?::|#L)(\d+(?:-L?\d+)?)$", path_with_line)
    if m:
        f = path_with_line[:m.start()]
        line = m.group(1).replace("L", "")
    else:
        # Evidence should always land on a stable source location.  Model output
        # occasionally names only a file; line 1 is an honest, deterministic
        # fallback and is more useful to reviewers than a file-level link.
        f, line = path_with_line, "1"
        path_with_line = f"{path_with_line}:1"
    url = resolver(f, line) if resolver else None
    if url:
        broken = url.startswith(_BROKEN_PREFIX)
        href = url[len(_BROKEN_PREFIX):] if broken else url
        cls = "file-jump file-broken" if broken else "file-jump"
        tgt = ' target="_blank" rel="noopener"' if href.startswith("http") else ""
        return f'<a class="{cls}" href="{_esc(href)}"{tgt}>{_esc(path_with_line)}</a>'
    return f'<span class="file-jump">{_esc(path_with_line)}</span>'


def _render_findings(items: list, resolver, *, title: str, color_cls: str,
                     is_issue: bool) -> str:
    """把节点级 highlights / issues 渲染成清晰的逐条列表（标题 + 项目符号）。

    每条一行：路径锚点 [+ severity 徽章] — 评判语。空列表返回 ""。
    """
    if not items:
        return ""
    lis: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        anchor = _resolve_path_anchor(it.get("path", ""), resolver)
        badge = ""
        if is_issue:
            sev = _esc(it.get("severity", ""))
            if sev:
                badge = f' <span class="severity-{sev} font-medium">[{sev}]</span>'
        quote = _esc(it.get("quote", ""))
        sep = " — " if quote else ""
        lis.append(
            f'<li class="leading-snug">{anchor}{badge}'
            f'<span class="text-slate-600 dark:text-slate-400">{sep}{quote}</span></li>'
        )
    return (
        f'<div class="mt-2">'
        f'<div class="text-xs font-semibold {color_cls} mb-1">{title}</div>'
        f'<ul class="text-xs list-disc pl-5 space-y-1 marker:text-slate-400">'
        f'{"".join(lis)}</ul>'
        f'</div>'
    )


def _render_similarity(sim: dict, resolver) -> str:
    """顶层「与借鉴 OS 相似度分析」卡片。数据来自 verdict.similarity。

    sim 为空、或没有 reference_os 时返回 ""（不渲染，兼容老报告与无参考 OS 的场景）。
    """
    if not isinstance(sim, dict):
        return ""
    ref = _esc(sim.get("reference_os") or "")
    if not ref:
        return ""

    pct = sim.get("overlap_pct")
    level = _esc(sim.get("level") or "")
    # 相似度越高 = 原创性越低，故色阶与 score-pill 相反：低相似=绿，高相似=红
    pct_badge = ""
    if isinstance(pct, (int, float)):
        p = int(pct)
        cls = "score-red" if p >= 70 else ("score-yellow" if p >= 40 else "score-green")
        pct_badge = f'<span class="score-pill {cls}">{p}%</span>'

    summary = _esc(sim.get("summary") or "")
    borrowed = _render_findings(
        sim.get("borrowed") or [], resolver,
        title="沿用 / 借鉴", color_cls="text-amber-700 dark:text-amber-400",
        is_issue=False)
    original = _render_findings(
        sim.get("original") or [], resolver,
        title="改造 / 原创", color_cls="text-cyan-700 dark:text-cyan-400",
        is_issue=False)

    summary_html = (
        f'<p class="text-sm text-slate-700 dark:text-slate-300 mb-3">{summary}</p>'
        if summary else ""
    )
    return f"""
<section id="similarity" data-section-id="similarity" class="mb-8 p-6 rounded-lg border border-slate-300 dark:border-slate-700
                             bg-white dark:bg-slate-800 shadow-sm">
  <h2 class="text-xl font-semibold mb-3">与借鉴 OS 的相似度分析</h2>
  <div class="flex items-baseline gap-3 mb-3 text-sm">
    <span>参考实现：<strong>{ref}</strong></span>
    <span>相似度：{pct_badge}{(" " + level) if level else ""}</span>
  </div>
  {summary_html}
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div>{borrowed or '<div class="text-xs text-slate-500">沿用 / 借鉴：(无)</div>'}</div>
    <div>{original or '<div class="text-xs text-slate-500">改造 / 原创：(无)</div>'}</div>
  </div>
</section>
"""


def _render_priority_summary(tree_json: dict, resolver) -> str:
    """决赛要求的首屏：先给结论和问题，细节再下钻。"""
    digest = description_digest_from_tree(tree_json)
    cards: list[str] = []
    severity_text = {
        "critical": "严重", "high": "高", "medium": "中", "low": "低", "info": "提示",
    }
    for item in description_priority_findings(digest, 8):
        refs: list[str] = []
        for evidence in item.evidence[:2]:
            if not evidence.path:
                continue
            location = evidence.path + (f":{evidence.line}" if evidence.line else "")
            refs.append(_resolve_path_anchor(location, resolver))
        evidence_html = (
            '<div class="text-xs text-slate-500 mt-1">证据：' + "、".join(refs) + '</div>'
            if refs else ""
        )
        confidence = round(item.confidence * 100)
        normalized_title = re.sub(r"[\s，。；：、,.!?！？:;]+", "", item.title).casefold()
        normalized_detail = re.sub(r"[\s，。；：、,.!?！？:;]+", "", item.detail).casefold()
        detail_html = (
            f'<p class="text-sm mt-1">{_esc(item.detail)}</p>'
            if normalized_detail and normalized_detail != normalized_title else ""
        )
        cards.append(
            f'<li class="finding-card {item.severity} rounded">'
            f'<div class="flex flex-wrap items-baseline gap-2"><strong>{_esc(item.title)}</strong>'
            f'<span class="text-xs severity-{_esc(item.severity)}">风险：{_esc(severity_text[item.severity])}</span>'
            f'<span class="text-xs text-slate-500">置信度 {confidence}%</span></div>'
            f'{detail_html}{evidence_html}</li>'
        )
    if not cards:
        cards.append(
            '<li class="finding-card rounded"><strong>未形成高置信问题</strong>'
            '<p class="text-sm mt-1">当前源码分析没有形成可直接报告的高风险问题；'
            '本结论只覆盖已经采集并可回溯的证据。</p></li>'
        )

    metrics = digest.metrics
    coverage = (
        f'AI 确认 {_esc(metrics.get("hardcode_confirmed", 0))} 条、'
        f'疑似 {_esc(metrics.get("hardcode_suspected", 0))} 条。'
    )
    hardcode_scope = (
        "硬编码专项检查范围：仓库第一方源码与测试/评测脚本中的按测试名或被加载的 ELF 文件名分支、"
        "针对测试的不合理缓存替换策略、直接打印预期输出、修改测试脚本绕过失败用例；"
        "vendor、third_party、external 等第三方依赖目录不计入作品作弊候选。"
    )
    return f"""
<section id="verdict" data-section-id="verdict" class="mb-6 p-6 rounded-lg border border-slate-300 dark:border-slate-700
                             bg-white dark:bg-slate-800 shadow-sm">
  <div class="text-xs font-semibold tracking-wider text-blue-700 dark:text-blue-300 mb-2">先看结论</div>
  <h2 class="text-xl font-semibold mb-2">结论与问题</h2>
  <p class="text-base leading-relaxed mb-4">{_esc(digest.conclusion)}</p>
  <ol class="space-y-3">{"".join(cards)}</ol>
  <p class="text-xs text-slate-500 mt-4">证据覆盖：{coverage}</p>
  <p class="text-xs text-slate-500 mt-1">{_esc(hardcode_scope)}</p>
</section>
"""


def _render_hardcode_reviews(verdict: dict, resolver) -> str:
    reviews = verdict.get("hardcode_reviews") or []
    if not reviews:
        return ""
    status_text = {
        "confirmed": "确认问题",
        "suspected": "疑似问题",
        "cleared": "已排除",
    }
    items: list[str] = []
    for review in reviews:
        location = str(review.get("path") or "")
        if review.get("line"):
            location += f':{int(review["line"])}'
        evidence = _resolve_path_anchor(location, resolver) if location else "无路径"
        status = status_text.get(str(review.get("status") or ""), "待复核")
        confidence = round(float(review.get("confidence") or 0))
        items.append(
            '<li class="py-2 border-b border-slate-200 dark:border-slate-700">'
            f'<div><strong>{_esc(review.get("category") or "未分类线索")}</strong> · '
            f'{_esc(status)} · 置信度 {confidence}%</div>'
            f'<div class="text-xs mt-1">证据：{evidence}</div>'
            f'<p class="text-sm mt-1"><strong>实现方法：</strong>'
            f'{_esc(review.get("method") or "未提供")}</p>'
            f'<p class="text-sm mt-1"><strong>AI 分析：</strong>'
            f'{_esc(review.get("reason") or "未提供判断理由")}</p>'
            f'<pre class="text-xs mt-1 overflow-x-auto"><code>{_esc(review.get("excerpt") or "")}</code></pre>'
            '</li>'
        )
    return (
        '<details class="detail-toggle mt-4"><summary>展开逐条硬编码复核'
        f'（{len(reviews)} 条）</summary><ol class="mt-2">{"".join(items)}</ol></details>'
    )


def _render_verdict(verdict: dict, resolver) -> str:
    score_total = int(verdict.get("score_total") or 0)
    cls = "score-green" if score_total >= 85 else \
          ("score-yellow" if score_total >= 70 else "score-red")
    one_line = _esc(verdict.get("one_line", ""))

    dims_html = ""
    for d in verdict.get("dimensions", []):
        s = int(d.get("score") or 0)
        dims_html += (
            f'<tr class="border-b border-slate-200 dark:border-slate-700">'
            f'<td class="py-2 px-3 font-medium">{_esc(d.get("name",""))}</td>'
            f'<td class="py-2 px-3">{_score_pill(s)}</td>'
            f'<td class="py-2 px-3 text-sm text-slate-700 dark:text-slate-300">'
            f'{_esc(d.get("reason",""))}</td></tr>'
        )

    hi_items = "".join(
        f'<li class="py-1">{_resolve_path_anchor(h.get("path",""), resolver)} — '
        f'{_esc(h.get("quote",""))}</li>'
        for h in verdict.get("highlights", [])
    )
    is_items = "".join(
        f'<li class="py-1">{_resolve_path_anchor(i.get("path",""), resolver)} '
        f'<span class="severity-{_esc(i.get("severity",""))} font-semibold">'
        f'[{_esc(i.get("severity",""))}]</span> — '
        f'{_esc(i.get("quote",""))}</li>'
        for i in verdict.get("issues", [])
    )

    radar_html = _render_verdict_radar(verdict)
    hardcode_html = _render_hardcode_reviews(verdict, resolver)

    # verdict 详细正文（agent 直出的 HTML）——剥离架构图和 LLM 手写图表后嵌入并链接化。
    # 雷达图统一由结构化 dimensions 确定性生成，避免模型写出占位 0 分或尺度错误。
    content = _strip_html_ids(_strip_echarts_charts(_strip_diagrams(verdict.get("content") or "")))
    content_html = ""
    if content.strip():
        content_html = (
            f'<div class="verdict-content prose prose-sm dark:prose-invert max-w-none mt-2 mb-4">'
            f'{linkify_html(content, resolver)}</div>'
        )

    return f"""
<section id="evaluation" data-section-id="evaluation" class="mb-8 p-5 rounded-lg border border-slate-300 dark:border-slate-700
                             bg-white dark:bg-slate-800 shadow-sm">
  <details class="detail-toggle">
  <summary>展开评分与详细综合分析（综合分 {score_total}）</summary>
  <div class="flex items-baseline gap-3 mb-3 mt-3">
    <span class="text-3xl font-bold {cls}">{score_total}</span>
    <span class="text-base text-slate-700 dark:text-slate-300">{one_line}</span>
  </div>
  <table class="w-full text-sm mb-4">
    <thead><tr class="text-xs uppercase tracking-wider text-slate-500 border-b">
      <th class="py-2 px-3 text-left">维度</th>
      <th class="py-2 px-3 text-left">得分</th>
      <th class="py-2 px-3 text-left">评语</th>
    </tr></thead>
    <tbody>{dims_html}</tbody>
  </table>
  {radar_html}
  {content_html}
  {hardcode_html}
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div>
      <div class="text-xs uppercase tracking-wider text-green-700 dark:text-green-400 mb-2">
        亮点
      </div>
      <ul class="text-sm list-disc pl-5">{hi_items or '<li class="text-slate-500">(无)</li>'}</ul>
    </div>
    <div>
      <div class="text-xs uppercase tracking-wider text-red-700 dark:text-red-400 mb-2">
        问题
      </div>
      <ul class="text-sm list-disc pl-5">{is_items or '<li class="text-slate-500">(无)</li>'}</ul>
    </div>
  </div>
  </details>
</section>
"""


_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
_CORRECTNESS_RISK_TERMS = (
    "仅返回成功", "占位实现", "行为不明确", "溢出", "丢弃", "旁路", "伪造",
    "竞态", "死锁", "越界", "权限", "错误结果",
)
_COMPLETENESS_RISK_TERMS = ("未实现", "不支持", "仅实现", "ENOSYS", "Unsupported")
_PERFORMANCE_RISK_TERMS = ("性能", "浪费", "热点", "锁竞争", "瓶颈", "全量")


def _collect_report_issues(tree_json: dict) -> list[dict]:
    """收集 verdict 与全部一级子系统问题，按源码位置去重但不做数量截断。"""
    root = tree_json.get("tree") or {}
    subsystem_nodes = [
        node for node in (root.get("children") or []) if isinstance(node, dict)
    ]
    subsystem_by_path: dict[str, list[str]] = {}
    for node in subsystem_nodes:
        name = str(node.get("name") or "未分类")
        for item in node.get("issues") or []:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            if path:
                subsystem_by_path.setdefault(path, []).append(name)

    merged: dict[str, dict] = {}
    order = 0

    def _add(item: dict, subsystem: str | None = None, review_node: dict | None = None) -> None:
        nonlocal order
        path = str(item.get("path") or "").strip()
        quote = normalize_description_claim(
            str(item.get("quote") or ""), path, tree_json.get("facts") or {},
        )
        if is_dependency_scope_only_issue(quote):
            return
        if not path or not quote or not re.search(r"(?::|#L)\d+(?:-L?\d+)?$", path):
            return
        from ..analysis.claim_review import claim_warning
        quote = claim_warning(review_node or {}, str(item.get("quote") or ""), path) + quote
        severity = str(item.get("severity") or "medium")
        if severity not in _SEVERITY_RANK:
            severity = "medium"
        # 纯重复/复用问题属于维护性，不应与正确性和可用性风险并列为中风险。
        if severity == "medium" and any(
            term in quote for term in ("重复代码", "缺乏复用", "重复定义")
        ) and not any(term in quote for term in _CORRECTNESS_RISK_TERMS):
            severity = "low"
        if path not in merged:
            names = list(dict.fromkeys(subsystem_by_path.get(path, [])))
            if subsystem and subsystem not in names:
                names.append(subsystem)
            merged[path] = {
                "path": path,
                "quote": quote,
                "severity": severity,
                "subsystems": names,
                "order": order,
            }
            order += 1
            return
        current = merged[path]
        if _SEVERITY_RANK[severity] > _SEVERITY_RANK[current["severity"]]:
            current["severity"] = severity
        if subsystem and subsystem not in current["subsystems"]:
            current["subsystems"].append(subsystem)

    for item in (tree_json.get("verdict") or {}).get("issues") or []:
        if isinstance(item, dict):
            _add(item, review_node=tree_json.get("verdict") or {})
    for node in subsystem_nodes:
        name = str(node.get("name") or "未分类")
        for item in node.get("issues") or []:
            if isinstance(item, dict):
                _add(item, name, node)

    issues = list(merged.values())
    for item in issues:
        text = item["quote"]
        correctness = any(term in text for term in _CORRECTNESS_RISK_TERMS)
        incomplete = any(term in text for term in _COMPLETENESS_RISK_TERMS)
        performance = any(term in text for term in _PERFORMANCE_RISK_TERMS)
        # 重要问题严格服从模型严重级；low/info 即使描述“未实现”，也属于模块内的
        # 局部完整性问题。只有明显的正确性/安全性误分级才从 low 提升到 medium。
        if item["severity"] == "low" and correctness:
            item["severity"] = "medium"
        item["major"] = item["severity"] in {"critical", "high", "medium"}
        if correctness:
            item["impact_group"] = 0
        elif item["severity"] in {"critical", "high", "medium"} and not performance:
            item["impact_group"] = 1
        elif incomplete:
            item["impact_group"] = 2
        elif performance:
            item["impact_group"] = 3
        else:
            item["impact_group"] = 4
    issues.sort(key=lambda item: (
        not item["major"],
        -_SEVERITY_RANK[item["severity"]],
        item["impact_group"],
        item["order"],
    ))
    for index, item in enumerate(issues, start=1):
        item["number"] = index
        item["anchor"] = f"issue-{index}"
    return issues


def _render_issue_row(item: dict, resolver, *, main: bool) -> str:
    severity = str(item.get("severity") or "medium")
    severity_text = {
        "critical": "严重", "high": "高风险", "medium": "中风险", "low": "低风险",
    }.get(severity, "提示")
    severity_cls = "status-bad" if severity in {"critical", "high"} else (
        "status-warn" if severity == "medium" else "status-info"
    )
    subsystem_text = " / ".join(item.get("subsystems") or []) or "跨模块"
    evidence = _resolve_path_anchor(str(item.get("path") or ""), resolver)
    data_attr = "data-main-finding" if main else "data-other-finding"
    return (
        f'<li id="{_esc(item["anchor"])}" class="finding-row" '
        f'{data_attr}="{item["number"]}">'
        '<div class="flex flex-wrap items-center gap-2 mb-1">'
        f'<strong>{item["number"]}.</strong>'
        f'<span class="status-pill {severity_cls}">{severity_text}</span>'
        f'<span class="text-xs text-slate-500">{_esc(subsystem_text)}</span></div>'
        f'<p class="text-sm leading-relaxed">{_esc(item["quote"])}</p>'
        f'<p class="text-xs text-slate-500 mt-1">证据：{evidence}</p></li>'
    )


def _render_judge_conclusion(tree_json: dict, resolver) -> str:
    digest = description_digest_from_tree(tree_json)
    issues = _collect_report_issues(tree_json)
    major_rows = [
        _render_issue_row(item, resolver, main=True) for item in issues if item["major"]
    ]
    if not major_rows:
        major_rows.append(
            '<li class="finding-row"><p class="text-sm">静态分析未形成需要优先报告的高置信代码问题；'
            '这不等同于功能测试通过。</p></li>'
        )
    return f"""
<section id="verdict" data-section-id="verdict" class="brief-card p-5 mb-5">
  <div class="text-xs font-semibold tracking-wider text-blue-700 dark:text-blue-300 mb-2">先看结论</div>
  <h2 class="text-xl font-bold mb-2">AI 分析结论</h2>
  <p class="text-base leading-relaxed mb-5">{_esc(digest.conclusion)}</p>
  <div id="findings" data-section-id="findings">
    <h3 class="font-semibold mb-1">AI 检测到的重要问题</h3>
    <p class="text-xs text-slate-500 mb-3">严格按严重程度排序；全部高、中风险及影响语义正确性的缺失均直接展示，不设数量上限。低风险局部问题下沉到对应模块，不在这里重复。</p>
    <ol class="space-y-3">{"".join(major_rows)}</ol>
  </div>
</section>
"""




def _render_hardcode_brief(tree_json: dict, resolver) -> str:
    integrity = ((tree_json.get("facts") or {}).get("integrity") or {})
    hardcode = integrity.get("hardcode") or {}
    reviews = (tree_json.get("verdict") or {}).get("hardcode_reviews") or []
    confirmed = [item for item in reviews if isinstance(item, dict) and item.get("status") == "confirmed"]
    suspected = [item for item in reviews if isinstance(item, dict) and item.get("status") == "suspected"]
    truncated = bool(hardcode.get("truncated"))
    category_coverage = hardcode.get("category_coverage") or {}
    scope_complete = len(category_coverage) >= 4 and all(
        isinstance(value, dict) and value.get("scanned") is True
        for value in category_coverage.values()
    )

    if not scope_complete:
        conclusion = "四类硬编码方法的规则扫描范围不完整，不能给出无作弊结论。"
        status_text, status_cls = "检查不完整", "status-warn"
    elif truncated:
        conclusion = (
            "规则扫描输出发生截断，尚未完成全部结构化 AI 复核，不能据此给出无作弊结论。"
        )
        status_text, status_cls = "范围不完整", "status-warn"
    elif confirmed or suspected:
        conclusion = (
            f"AI 复核确认 {len(confirmed)} 条、疑似 {len(suspected)} 条；"
            "正文仅展示需要评委关注的确认和疑似证据。"
        )
        status_text, status_cls = "需要核查", "status-bad"
    elif len(reviews) >= len(hardcode.get("findings") or []):
        conclusion = (
            "AI 复核确认 0 条、疑似 0 条；未发现作弊型硬编码。"
        )
        status_text, status_cls = "未发现", "status-ok"
    else:
        conclusion = "硬编码候选尚未全部完成结构化 AI 复核，不能下结论。"
        status_text, status_cls = "复核不完整", "status-warn"

    raw_by_id = {
        str(s.get("signal_id") or ""): s
        for s in (hardcode.get("findings") or []) if isinstance(s, dict)
    }
    risky_rows: list[str] = []
    for item in (confirmed + suspected):
        location = str(item.get("path") or "")
        if item.get("line"):
            location += f':{int(item["line"])}'
        evidence = _resolve_path_anchor(location, resolver) if location else ""
        method = str(item.get("method") or "未说明实现方法。")
        reason = str(item.get("reason") or "证据需要复核。")
        review_status = "确认问题" if item.get("status") == "confirmed" else "疑似问题"
        # 低置信聚合项的代表行外还有其余命中行，一并列出供评委回溯全部位置。
        raw = raw_by_id.get(str(item.get("signal_id") or ""), {})
        hit_lines = raw.get("lines") or []
        others = sorted(
            {int(ln) for ln in hit_lines if int(ln) != int(raw.get("line") or 0)}
        ) if hit_lines else []
        extra = (
            f'（同类命中另 {len(others)} 处：{"、".join(f"L{ln}" for ln in others[:8])}）'
            if others else ""
        )
        risky_rows.append(
            '<li class="finding-row text-sm">'
            f'<strong>{_esc(item.get("category") or "硬编码线索")} · {review_status}</strong>：{_esc(method)} '
            f'{_esc(reason)}{extra}{(" · 证据：" + evidence) if evidence else ""}</li>'
        )
    return f"""
<section id="hardcode" data-section-id="hardcode" class="brief-card p-5 mb-5">
  <div class="flex flex-wrap items-center gap-3 mb-2">
    <h2 class="text-xl font-bold">硬编码 / 作弊复核</h2>
    <span class="status-pill {status_cls}">{status_text}</span>
  </div>
  <p class="text-sm leading-relaxed">{_esc(conclusion)}</p>
  {('<ol class="space-y-3 mt-3">' + ''.join(risky_rows) + '</ol>') if risky_rows else ''}
  <p class="text-xs text-slate-500 mt-3">覆盖四类方法：按测试名或可执行文件名分支、测试专用缓存策略、直接打印预期结果、修改测试脚本旁路失败；已排除候选不在主报告逐条展开。</p>
</section>
"""


def _path_without_line(value: str) -> str:
    return re.sub(r"(?::|#L)\d+(?:-L?\d+)?$", "", str(value or "").strip())


def _paths_in_text(value: str) -> list[str]:
    return re.findall(
        r"(?:[A-Za-z0-9_.+@-]+/)+(?:[A-Za-z0-9_.+@-]+)(?::|#L)\d+(?:-L?\d+)?",
        str(value or ""),
    )


def _direct_node_evidence(node: dict) -> list[str]:
    refs: list[str] = []
    for item in node.get("highlights") or []:
        if isinstance(item, dict) and item.get("path"):
            refs.append(str(item["path"]))
    for field in ("brief", "summary", "content"):
        refs.extend(_paths_in_text(str(node.get(field) or "")))
    refs.extend(str(path) for path in (node.get("file_paths") or []) if path)
    return list(dict.fromkeys(refs))


def _section_evidence(node: dict, parent: dict | None = None) -> list[str]:
    """覆盖子系统自身和每个真实子模块，避免只给一条代表链接。"""
    refs = _direct_node_evidence(node)
    children = [
        child for child in (node.get("children") or []) if isinstance(child, dict)
    ]
    if children:
        for child in children:
            child_refs = _direct_node_evidence(child)
            exact = next(
                (path for path in child_refs if re.search(r"(?::|#L)\d+", path)),
                child_refs[0] if child_refs else "",
            )
            if exact:
                refs.append(exact)
    if parent is not None:
        scope = _section_scope(node)
        for item in parent.get("highlights") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            if _path_without_line(str(item["path"])) in scope:
                refs.append(str(item["path"]))
    unique = list(dict.fromkeys(refs))
    precise_bases = {
        _path_without_line(path) for path in unique
        if re.search(r"(?::|#L)\d+", path)
    }
    # 同一文件已有精确行号时不再追加文件级链接；保留同文件的不同精确位置。
    return [
        path for path in unique
        if re.search(r"(?::|#L)\d+", path) or _path_without_line(path) not in precise_bases
    ]


def _section_scope(node: dict) -> set[str]:
    refs = _direct_node_evidence(node)
    for child in node.get("children") or []:
        if isinstance(child, dict):
            refs.extend(_direct_node_evidence(child))
    return {_path_without_line(path) for path in refs if path}


def _clean_capability_claim(value: str, tree_json: dict) -> str:
    text = normalize_description_claim(value, "", tree_json.get("facts") or {})
    return (
        text.replace("实现了完整的", "覆盖")
        .replace("实现完整", "覆盖")
        .replace("覆盖完整", "覆盖主要路径")
        .replace("完整的 TCP/IP 网络协议栈", "TCP/IP 网络能力")
        .replace("完整定义", "集中定义")
        .replace("等全部", "等多类")
        .replace("与 Linux 主线 UAPI 头文件保持一致", "以兼容 Linux UAPI 为目标")
        .replace("确保 ", "用于 ")
        .replace("确保用户程序二进制兼容", "为用户程序二进制兼容提供接口基础")
        .replace("完全解耦", "解耦")
    )


def _statement_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" 。；;！？")


def _section_statements(
    node: dict, parent: dict | None, tree_json: dict,
    assigned_issues: list[dict], major_quotes: set[str],
    *, limit: int = 300,
) -> tuple[list[dict], list[dict]]:
    """按完整陈述选取概览；超预算陈述放入详情，文本与自身引用一起移动。

    这只保证展示阶段不截断输入陈述，不证明输入本身正确，也不证明不同陈述
    彼此独立。摘要的语义核验属于上游分析，不能由字符预算代替。
    """
    entries: list[dict] = []
    children = [child for child in node.get("children") or [] if isinstance(child, dict)]
    excluded = {_statement_key(quote) for quote in major_quotes}
    excluded.update(_statement_key(str(item.get("quote") or "")) for item in assigned_issues)

    def add(value: str, kind: str, path: str = "", label: str = "") -> None:
        # summary 是完整输入；brief 可能早已截断，只用于旧产物没有 summary 时。
        # summary/quote 是纯文本。按 HTML 剥标签会把 fd<0、fd>=NOFILE 中的
        # 比较条件吃掉；渲染边界统一 _esc，而不把代码运算符当作标签。
        text = _clean_capability_claim(value, tree_json)
        if not text or _statement_key(text) in excluded:
            return
        entries.append({"text": f"{label}：{text}" if label else text, "kind": kind, "path": path})

    add(str(node.get("summary") or node.get("brief") or "未形成可靠的静态模块摘要。"), "implementation")
    for child in children:
        add(str(child.get("summary") or child.get("brief") or ""), "implementation",
            label=str(child.get("name") or "子模块"))
    for source in [node, *children]:
        for item in source.get("highlights") or []:
            if isinstance(item, dict):
                add(str(item.get("quote") or ""), "highlight", str(item.get("path") or ""))
    if parent is not None:
        scope = _section_scope(node)
        for item in parent.get("highlights") or []:
            if isinstance(item, dict) and item.get("path"):
                if _path_without_line(str(item["path"])) in scope:
                    add(str(item.get("quote") or ""), "highlight", str(item["path"]))

    visible: list[dict] = []
    deferred: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    remaining = max(0, limit)
    for entry in entries:
        key = (entry["text"], entry["kind"], entry["path"])
        if key in seen:
            continue
        seen.add(key)
        if len(entry["text"]) <= remaining:
            visible.append(entry)
            remaining -= len(entry["text"])
        else:
            deferred.append(entry)
    return visible, deferred


def _render_statement_items(entries: list[dict], resolver) -> str:
    lines: list[str] = []
    for entry in entries:
        path = entry["path"]
        citation = (
            f' <span class="text-xs text-slate-400">（{_resolve_path_anchor(path, resolver)}）</span>'
            if path else ""
        )
        lines.append(f'<li>{_esc(entry["text"])}{citation}</li>')
    return '<ul class="list-disc pl-4 mt-1 space-y-0.5">' + "".join(lines) + '</ul>'


def _render_evidence_list(label: str, paths: list[str], resolver) -> str:
    unique = list(dict.fromkeys(path for path in paths if path))
    if not unique:
        return ""
    links = "".join(
        f'<li class="inline mr-3">{_resolve_path_anchor(path, resolver)}</li>'
        for path in unique
    )
    return (
        f'<div class="text-xs text-slate-500 mt-2"><strong>{_esc(label)}：</strong>'
        f'<ul class="inline" data-evidence-count="{len(unique)}">{links}</ul></div>'
    )


def _render_all_subsystems(tree_json: dict, resolver) -> str:
    issues = _collect_report_issues(tree_json)
    major_quotes = {str(item.get("quote") or "") for item in issues if item.get("major")}
    sections = [
        {
            "name": name,
            "node": node,
            "parent": parent,
            "scope": _section_scope(node),
            "issues": [],
        }
        for name, node, parent in description_review_sections(tree_json)
    ]

    for issue in issues:
        issue_path = _path_without_line(str(issue.get("path") or ""))
        exact = [section for section in sections if issue_path in section["scope"]]
        if exact:
            exact[0]["issues"].append(issue)
            continue
        named = [
            section for section in sections
            if section["parent"] is None and section["name"] in (issue.get("subsystems") or [])
        ]
        if named:
            named[0]["issues"].append(issue)
            continue
        parent_named = [
            section for section in sections
            if section["parent"] is not None
            and str(section["parent"].get("name") or "") in (issue.get("subsystems") or [])
        ]
        if parent_named:
            # 结构化模块未精确覆盖该文件时，仍放在其父类下最接近的并列子模块，避免静默丢失。
            parent_named[0]["issues"].append(issue)

    cards: list[str] = []
    for section in sections:
        name = section["name"]
        node = section["node"]
        local_issues = section["issues"]
        visible, deferred = _section_statements(
            node, section["parent"], tree_json, local_issues, major_quotes,
        )
        paragraphs: list[str] = []
        for kind, label in (("implementation", "静态实现"), ("highlight", "实现亮点")):
            entries = [entry for entry in visible if entry["kind"] == kind]
            if entries:
                paragraphs.append(
                    f'<div class="text-sm leading-relaxed"><strong>{label}：</strong>'
                    f'{_render_statement_items(entries, resolver)}</div>'
                )
        if deferred:
            paragraphs.append(
                '<details class="text-sm mt-2" data-description-overflow="true">'
                f'<summary>补充实现与亮点（{len(deferred)} 条完整说明）</summary>'
                f'{_render_statement_items(deferred, resolver)}</details>'
            )
        if local_issues:
            local_lines = []
            for item in local_issues:
                if item.get("major"):
                    continue
                quote = str(item.get("quote") or "")
                path_str = str(item.get("path") or "")
                if not quote:
                    continue
                path_html = (
                    f' <span class="text-xs text-slate-400">（{_resolve_path_anchor(path_str, resolver)}）</span>'
                    if path_str else ""
                )
                local_lines.append(f"<li>{_esc(quote)}{path_html}</li>")
            if local_lines:
                paragraphs.append(
                    '<div class="text-sm leading-relaxed"><strong>局部问题：</strong>'
                    f'<ul class="list-disc pl-4 mt-1 space-y-0.5">{"".join(local_lines)}</ul></div>'
                )
        implementation_evidence = _section_evidence(node, section["parent"])
        analysis_chars = sum(len(entry["text"]) for entry in visible)
        detail_chars = sum(len(entry["text"]) for entry in deferred)
        issue_chars = sum(
            len(str(item.get("quote") or "")) for item in local_issues if not item.get("major")
        )
        cards.append(
            f'<article class="core-card" data-subsystem="{_esc(name)}" '
            f'data-analysis-chars="{analysis_chars}" data-detail-chars="{detail_chars}" '
            f'data-issue-chars="{issue_chars}">'
            f'<h3 class="font-bold mb-1">{_esc(name)}</h3>'
            f'{"".join(paragraphs)}'
            f'{_render_evidence_list("实现依据", implementation_evidence, resolver)}'
            '</article>'
        )
    return f"""
<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">按仓库实际设计拆分并列模块；笼统“其他”会展开为真实子模块。每项概览正文不超过 300 字；超出篇幅的实现与亮点可展开查看完整说明。问题陈述完整保留，重要问题集中列示，低风险局部问题在所属模块内说明；实现依据覆盖各子模块的代表位置。第三方库本身不计作作品缺陷或自研亮点，只评价项目适配代码及系统可见行为。</p>
  <div>{"".join(cards)}</div>
  {_render_description_scope(tree_json)}
</section>
"""


def _render_description_scope(tree_json: dict) -> str:
    rows = []
    for node in (tree_json.get("tree") or {}).get("children") or []:
        scope = node.get("description_scope")
        if not isinstance(scope, dict):
            continue
        values = [scope.get(key) for key in (
            "indexed_files", "prompted_files", "representative_files", "unrepresented_files",
        )]
        if any(value is not None and (not isinstance(value, int) or value < 0) for value in values):
            continue
        rows.append(f'<tr><td>{_esc(node.get("name", ""))}</td>'
                    + "".join(f"<td>{value if value is not None else '未知'}</td>" for value in values) + "</tr>")
    if not rows:
        return _render_recall_check(tree_json) + _render_evaluation_assertions(tree_json) + _render_evidence_plan(tree_json) + _render_read_audit(tree_json)
    return (
        '<div class="text-xs mt-4" data-description-scope="true">'
        '<h3 class="font-bold">源码范围说明</h3>'
        '<p>索引和提示清单只用于定位源码；模块代表文件只表示报告列出的来源。'
        '未列为代表文件不等于功能遗漏，列为代表文件也不证明全文已读或描述正确。'
        '实际源码阅读覆盖尚未测量；恢复旧阶段产物时提示文件数量保持未知。</p>'
        '<table><thead><tr><th>子系统</th><th>索引文件</th><th>提示文件</th>'
        '<th>索引内代表文件</th><th>未列代表文件</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table></div>'
        + _render_recall_check(tree_json) + _render_evaluation_assertions(tree_json) + _render_evidence_plan(tree_json) + _render_read_audit(tree_json)
    )


_RECALL_LABELS = {'retained_recalled': '标记保留且全部答对', 'dropped': '已读标记不在最终请求中',
                  'present_not_recalled': '标记仍在，未准确召回', 'misattributed': '张冠李戴',
                  'not_observed': '未确认标记曾返回', 'unknown': '无法确认', 'unmarked': '未布置标记'}
_RECALL_LABELS['attribution_target'] = '本函数标记被错误归给另一函数'


def _recall_dependency_text(items):
    text = []
    for item in items:
        history = ' → '.join(_RECALL_LABELS.get(s, s) for s in item.get('history', []))
        text.append(f"{item['path']}:{item['start_line']} {item['symbol']}：{history}")
    return '<br>依据函数核对：' + _esc('；'.join(text)) if text else ''


def _render_recall_check(tree_json: dict) -> str:
    sections = []
    nodes = list((tree_json.get('tree') or {}).get('children') or [])
    nodes.append(dict(tree_json.get('verdict') or {}, name='总评'))
    for node in nodes:
        check = node.get('recall_check')
        if not check:
            continue
        rows = []
        for round_number, attempt in enumerate(check.get('attempts', [check])):
            for function in attempt.get('functions', []):
                markers = '；'.join(f"{m['position']}：present={m['present']}，{_RECALL_LABELS.get(m['status'], m['status'])}" for m in function['markers'])
                actions = '、'.join({'reread': '补读', 'claim_review': '相关断言复核'}.get(a, a) for a in function['actions']) or '无'
                rows.append(f"<tr><td>{round_number}</td><td>{_esc(function['path'])}:{function['start_line']} {_esc(function['symbol'])}</td>"
                            f"<td>{_esc(function['coverage'])}；最终请求中完整源码范围：{function['source_present']}</td>"
                            f"<td>{_esc(markers)}；{_esc(_RECALL_LABELS.get(function['status'], function['status']))}</td><td>{_esc(actions)}</td></tr>")
        metrics = check.get('metrics', {})
        ratio = lambda key: '未知' if metrics.get(key) is None else f"{metrics[key]:.1%}"
        calls = check.get('calls', [])
        costs = [(a.get('metrics') or {}).get('prompt_tokens') for a in check.get('attempts', [check])]
        cost = sum(costs) if all(c is not None for c in costs) else '记录不完整'
        selection = check.get('selection', {})
        state = {'checked': '已核对', 'unknown': '无法确认：缺少本轮会话记录', 'probe_invalid': '提问无效，召回结果不采信',
                 'no_candidates': '没有可核对的函数'}.get(check.get('status'), '无法确认')
        controls = '；'.join(f"{c['path']} {c['symbol']}：" + {'control_correct': '正确返回 null', 'confabulation': '对照误报', 'unknown': '无法确认'}.get(c['status'], c['status']) for c in check.get('controls', []))
        sections.append(f"<h4>{_esc(node.get('name', ''))}</h4><p>{_esc(state)}。最终标记保留率：{ratio('retention_rate')}；完整函数保留率：{ratio('function_retention_rate')}；"
                        f"present 样本召回率：{ratio('recall_rate')}；张冠李戴：{metrics.get('misattributed', '未知')}；"
                        f"对照题误报：{metrics.get('confabulations', '未知')}；各轮提问输入 token 合计：{cost}。"
                        f"调用列表题：{sum(c['correct'] for c in calls)}/{len(calls)}；"
                        f"补读轮数：{check.get('total_repair_rounds', 0)}；超预算未核对：{len(selection.get('omitted', []))}；"
                        f"提问时上下文变化：{metrics.get('probe_context_changed', 0)}；"
                        f"未标记对照缺额：{selection.get('control_shortfall', 2)}；报告标记 echo：{any(a.get('report_echo') for a in check.get('attempts', [check]))}。</p>"
                        '<table><thead><tr><th>轮次</th><th>函数</th><th>读取与源码保留</th><th>标记与召回</th><th>触发动作</th></tr></thead><tbody>'
                        + ''.join(rows) + '</tbody></table><p>对照题：' + _esc(controls or '无可用对照') + '</p>')
    if not sections:
        return ''
    return ('<section data-recall-check="true"><h3>源码保留与召回核对</h3>'
            '<p>标记保留不等于完整源码保留，召回答对不等于语义正确。标记缺失不单独证明发生了压缩；'
            '调用列表题单独统计。补读预算耗尽仍未通过的状态保留，核对本身不改变评分。</p>'
            + ''.join(sections) + '</section>')


def _render_evaluation_assertions(tree_json: dict) -> str:
    nodes = list((tree_json.get("tree") or {}).get("children") or [])
    nodes.append(dict(tree_json.get("verdict") or {}, name="总评"))
    labels = {"supported": "两次源码复核支持", "adjudicated_supported": "分歧后源码复核支持",
              "contradicted": "两次复核反驳", "adjudicated_contradicted": "分歧后复核反驳",
              "disputed": "复核有分歧", "insufficient": "证据不足，尚不能确认",
              "ready_for_review": "来源核对通过，待语义复核", "not_reviewed": "未完成独立复核"}
    reasons = {"invalid_assertion_structure": "断言格式或项目对应无效",
               "incomplete_author_session": "生成会话记录不完整",
               "required_evidence_not_returned": "必要范围未完整返回或源码发生变化",
               "invalid_evidence": "引文格式无效", "unverified_evidence": "原文或读取记录未通过核对",
               "quote_not_in_declared_target": "引文未对应指定证据", "missing_target_quotes": "部分必要证据没有有效引文"}
    rows = []
    for node in nodes:
        for item in (node.get("evaluation_assertions") or {}).get("items", []):
            evidence = "".join(f"<li>{_esc(e['evidence_id'])}：{_esc(e['ref'])}</li>" for e in item.get("evidence", []))
            errors = "；".join(reasons.get(e, e) for e in item.get("errors", []))
            rows.append(f"<tr><td>{_esc(node.get('name', ''))} / {_esc(item['question'])}</td>"
                f"<td>{_esc(item['statement'])}<br>范围：{_esc(item['scope'])}"
                f"<br>条件：{_esc('；'.join(item.get('conditions', [])))}"
                f"<br>待查反例：{_esc('；'.join(item.get('counterevidence', [])) or '未提出')}</td>"
                f"<td><ul>{evidence}</ul>{_esc(labels.get(item['status'], '尚未确认'))}"
                f"<br>{_esc(errors or item.get('limitations', ''))}"
                + _recall_dependency_text(item.get("recall_dependencies", [])) +
                "<br>执行验证：未运行；分数：未计算</td></tr>")
    if not rows:
        return ""
    return ('<div class="text-xs mt-4" data-evaluation-assertions="true"><h3 class="font-bold">按评价项核对的事实断言</h3>'
            '<p>适用范围与成立条件是结论的一部分。引文真实、材料齐全以及多次模型支持，'
            '都不能替代独立执行验证。仍有反驳、分歧或证据不足的条目不能作为确定评分依据。</p>'
            '<table><thead><tr><th>评价项</th><th>断言及成立条件</th><th>证据与验证状态</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div>')


def _render_evidence_plan(tree_json: dict) -> str:
    nodes = list((tree_json.get("tree") or {}).get("children") or [])
    nodes.append(dict(tree_json.get("verdict") or {}, name="总评"))
    rows = []
    labels = {"returned": "目标范围已返回", "needs_evidence": "仍缺读取证据",
              "unknown": "记录不完整，无法确认", "unresolved_location": "尚未定位，需核对清单"}
    reasons = {"missing_or_unreadable": "文件缺失或不可读取", "unreliable_parse": "无法可靠解析函数",
               "ambiguous_symbol": "同名函数不能唯一定位", "symbol_not_found": "指定函数未定位",
               "invalid_range": "行范围超出文件"}
    for node in nodes:
        plan = node.get("evidence_plan")
        if not isinstance(plan, dict):
            continue
        for item in plan.get("items", []):
            parts = []
            for target in item.get("evidence", []):
                locations = [f"{r['path']}:{r['start_line']}-{r['end_line']}"
                             for r in target.get("requirements", [])] or [target.get("path", "")]
                state = labels.get(target.get("status"), labels["unknown"])
                detail = reasons.get(target.get("resolution"), "")
                if any(r.get("status") == "source_changed_or_unavailable" for r in target.get("checked_ranges", [])):
                    detail = "源码已变化或不可读取，本轮原记录不适用"
                parts.append(f"<li>{_esc(target.get('purpose', ''))}：{_esc('、'.join(locations))}；"
                             f"{state}{'（' + detail + '）' if detail else ''}</li>")
            rows.append(f"<tr><td>{_esc(node.get('name', ''))}</td>"
                        f"<td>{_esc(item.get('dimension', ''))}：{_esc(item.get('question', ''))}"
                        f"<br>适用范围：{_esc(item.get('scope', ''))}</td>"
                        f"<td><ul>{''.join(parts)}</ul></td>"
                        f"<td>{item.get('returned_count', 0)}/{item.get('required_count', 0)} 项已返回；"
                        "行为尚未验证，本步骤未计算分数</td></tr>")
    if not rows:
        return ""
    return ('<div class="text-xs mt-4" data-evidence-plan="true"><h3 class="font-bold">评价项的必要证据</h3>'
            '<p>清单由评测方在运行前提供。此处只核对清单要求的材料是否由工具返回，'
            '不能据此认定所有相关代码已查全或评价正确。缺证据的项目待核对，不据此扣分或给满分。'
            '本表尚未形成按细则核验的分项分数；原有六维分数不因此成为已验证分数。</p>'
            '<table><thead><tr><th>会话</th><th>评价项</th><th>必要证据与缺口</th><th>状态</th></tr>'
            '</thead><tbody>' + ''.join(rows) + '</tbody></table></div>')


def _render_read_audit(tree_json: dict) -> str:
    rows = []
    coverage_rows = []
    nodes = list((tree_json.get("tree") or {}).get("children") or [])
    nodes.append(dict(tree_json.get("verdict") or {}, name="总评"))
    for node in nodes:
        audit = node.get("read_audit")
        if not isinstance(audit, dict):
            continue
        coverage = audit.get("coverage") or {}
        for req in coverage.get("requirements") or []:
            location = f"{req['path']}:{req['start_line']}-{req['end_line']}"
            if req.get("status") == "returned":
                label = "目标范围完整返回"
            else:
                missing = "、".join(f"{lo}—{hi}" for lo, hi in req.get("missing_ranges", []))
                label = "仍缺可核对的返回证据" + (f"（{missing} 行）" if missing else "（源码变化或无法定位）")
            scope = {"function": "函数", "declared_lines": "评测方指定范围"}.get(
                req.get("scope"), "引用范围（未确认函数边界）")
            coverage_rows.append(f'<tr><td>{_esc(node.get("name", ""))}</td>'
                                 f'<td>{_esc(location)} {_esc(req.get("symbol", ""))}</td>'
                                 f'<td>{scope}：{_esc(label)}；补读 {coverage.get("repair_rounds", 0)} 轮</td></tr>')
        if not audit.get("run_id"):
            continue
        labels = {
            "returned": "对应源码行已由工具返回",
            "not_observed_in_tool_outputs": "未在本次工具返回中找到，需核对其他输入或补读",
            "unverified_line": "返回行不完整或与当前源码不符，需核对",
            "unknown_source_changed": "源码已变化，原记录不适用",
            "unknown": "记录不足，无法确认",
        }
        for ref in audit.get("references") or []:
            if not isinstance(ref, dict):
                continue
            location = f"{ref.get('path', '')}:{ref.get('line', '')}"
            if ref.get("end_line", ref.get("line")) != ref.get("line"):
                location += f"-{ref['end_line']}"
            label = labels.get(ref.get("status"), labels["unknown"])
            rows.append(f'<tr><td>{_esc(node.get("name", ""))}</td>'
                        f'<td>{_esc(location)}</td><td>{label}</td></tr>')
    if not rows and not coverage_rows:
        return ""
    return (
        '<div class="text-xs mt-4" data-read-audit="true">'
        '<h3 class="font-bold">源码引用核对</h3>'
        '<p>核对 OpenCode 原生工具与 MCP 工具实际返回的源码行。'
        '已返回不代表理解或判断正确；提示正文、附件、子任务和无法定位的工具输出未完整计入，'
        '未记录的引用不直接判为虚构。旧阶段恢复结果保持未知。'
        '引用函数最多自动补读两轮；目标范围返回完整也不代表相关函数没有遗漏。</p>'
        '<table><thead><tr><th>子系统</th><th>引用</th><th>核对结果</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table>'
        + ('<h4 class="font-bold mt-2">函数范围与补读结果</h4><table><thead><tr>'
           '<th>子系统</th><th>目标</th><th>核对结果</th></tr></thead><tbody>'
           + "".join(coverage_rows) + '</tbody></table>' if coverage_rows else '')
        + '</div>'
    )


def _render_brief_toc() -> str:
    items = (
        ("verdict", "结论与关键问题"),
        ("hardcode", "硬编码复核"),
        ("modules", "全部模块概览"),
    )
    links = "".join(
        f'<a class="toc-link" href="#{anchor}">{label}</a>' for anchor, label in items
    )
    return (
        '<nav class="toc-nav hidden lg:block w-48 shrink-0 self-start sticky top-6 py-10">'
        '<div class="text-xs font-semibold tracking-wider text-slate-400 mb-2 px-2">精简审阅</div>'
        f'{links}</nav>'
    )


def _render_claim_reviews(tree_json: dict) -> str:
    labels = {"supported": "两次复核支持（不等于正确性证明）",
              "adjudicated_supported": "分歧后复核支持，仍需审慎使用",
              "contradicted": "两次复核反驳，不宜作为确定结论",
              "adjudicated_contradicted": "分歧后复核反驳，不宜作为确定结论",
              "disputed": "复核存在分歧，尚未确认",
              "insufficient": "证据不足，尚未确认"}
    nodes = list((tree_json.get("tree") or {}).get("children") or [])
    nodes.append(dict(tree_json.get("verdict") or {}, name="总评"))
    rows = []
    notes = []
    for node in nodes:
        review = node.get("claim_review")
        if not isinstance(review, dict):
            continue
        name = _esc(node.get("name", ""))
        if review.get("status") == "unknown":
            notes.append(f"{name}：没有本次有效复核记录。")
            continue
        notes.append(f"{name}：选中 {review.get('selected_count', 0)} 条，"
                     f"未复核 {len(review.get('unreviewed') or [])} 条；"
                     + ("已修订一轮。" if (review.get("revision") or {}).get("attempted") else "未触发修订。"))
        for old in (review.get("revision") or {}).get("remaining_original_wording") or []:
            notes.append(f"{name}：正文仍包含先前未确认的原表述，需核对其上下文：{_esc(old['text'])}")
        for claim in review.get("claims") or []:
            reasons = "；".join(str(r.get("reason") or "") for r in claim.get("reviews") or [])
            evidence = "、".join(sorted({str(e.get("ref", "")) for r in claim.get("reviews") or []
                                        for e in r.get("evidence") or []}))
            context = claim.get("assertion_context") or {}
            scope = (f"<br>范围：{_esc(context['scope'])}<br>条件：{_esc('；'.join(context['conditions']))}"
                     if context else "")
            rows.append(f"<tr><td>{name}</td><td>{_esc(claim.get('text', ''))}{scope}</td>"
                        f"<td>{_esc(labels.get(claim.get('status'), '尚未确认'))}</td>"
                        f"<td>{_esc(reasons)}<br>{_esc(evidence)}"
                        + _recall_dependency_text(claim.get("recall_dependencies", [])) + "</td></tr>")
    if not notes:
        return ""
    return ('<section class="brief-card p-5 mb-5" data-claim-review="true">'
            '<h2 class="text-xl font-bold mb-2">关键结论复核</h2>'
            '<p>本节只复核选中的评价断言、结构化问题、亮点与摘要，不代表全文已验证。'
            '下列存在反驳、分歧或证据不足的说法不得当作确定事实，依赖这些说法的评分也需人工核对。'
            '同一模型的多次复核可能重复犯错；源码证据不能代替运行测试。</p>'
            + '<p>' + '<br>'.join(notes) + '</p>'
            + '<table><thead><tr><th>范围</th><th>结论</th><th>结果</th><th>理由与证据</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></section>')


# 渲染入口

def render_tree_html(tree_json: dict, title: str = "代码树报告",
                     resolver=None) -> str:
    """渲染“问题完整、文字精简”的评委作品描述报告。"""
    assert_report_complete("", structured=tree_json)
    meta = tree_json.get("meta", {})
    conclusion_html = _render_judge_conclusion(tree_json, resolver)
    review_html = _render_claim_reviews(tree_json)
    hardcode_html = _render_hardcode_brief(tree_json, resolver)
    modules_html = _render_all_subsystems(tree_json, resolver)
    toc_html = _render_brief_toc()

    title_safe = _esc(title)
    if meta.get("language_incomplete"):
        raise RuntimeError(
            "AI 生成内容仍有未完成中文化的正文或标题；拒绝生成需要人工修改的交付报告"
        )
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title_safe}</title>
{_BRIEF_HEAD}
<style>{_BRIEF_CSS}</style>
</head>
<body class="bg-slate-50 dark:bg-slate-900 text-slate-900 dark:text-slate-100">
<div class="max-w-6xl mx-auto flex gap-6 px-4 md:px-6">
  {toc_html}
  <main class="flex-1 min-w-0 max-w-4xl py-6 md:py-10">
    <header class="mb-6">
      <div class="flex flex-wrap items-baseline gap-3">
        <h1 class="text-2xl font-bold">{_esc(meta.get('repo','?'))} 作品描述报告</h1>
      </div>
      <div class="text-sm text-slate-500 mt-2">队伍：{_esc(meta.get('team_id','未记录'))} · 分析时间：{_esc(meta.get('ts',''))} · 索引源文件：{_esc(meta.get('indexed_files',0))} 个</div>
      <div class="text-xs text-slate-500 mt-1 ai-disclaimer">分析依据：源码结构分析与硬编码线索复核。相关判断仅供评委参考，不构成违规认定。</div>
    </header>
    {review_html}
    {conclusion_html}
    {hardcode_html}
    {modules_html}
    <footer class="text-xs text-slate-500 pb-8">源码链接用于核对引用；未复核及证据不足的结论以本报告标注为准。</footer>
  </main>
</div>
</body>
</html>
"""
    doc = explain_terms_in_html(doc)
    # 正文里含省略号的代码式片段包成 <code>（代码引述不是正文截断），
    # 避免模型描述代码差异时的 ... 缩写触发省略号门禁。
    doc = sanitize_code_ellipses(doc)
    assert_toc_resolves(doc)
    assert_report_complete(doc, structured=tree_json)
    return doc


def _render_toc(verdict_html: str, similarity_html: str,
                toc_subs: list[tuple[str, str, int]]) -> str:
    """左侧粘性目录：评判 / 相似度 / 代码树 + 子系统（含模块），点击平滑定位。

    toc_subs 每项为 (anchor_id, label, level)：level 1 = 子系统，2 = 模块（缩进更深）。
    """
    items: list[str] = []
    if verdict_html.strip():
        items.append('<a class="toc-link" href="#verdict">结论与问题</a>')
    if similarity_html.strip():
        items.append('<a class="toc-link" href="#similarity">相似度分析</a>')
    items.append('<a class="toc-link" href="#tree">代码树</a>')
    for sid, label, level in toc_subs:
        sub_cls = "toc-sub2" if level >= 2 else "toc-sub"
        items.append(
            f'<a class="toc-link {sub_cls}" href="#{_esc(sid)}" '
            f'title="{_esc(label)}">{_esc(label)}</a>'
        )
    links = "\n      ".join(items)
    return f"""<nav class="toc-nav hidden lg:block w-56 shrink-0 self-start sticky top-0
              max-h-screen overflow-y-auto py-6 md:py-10">
    <div class="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2 px-2">目录</div>
    <div class="space-y-0.5">
      {links}
    </div>
  </nav>"""


def _render_tree_node_static(node: dict, depth: int, resolver,
                             anchor_ids: dict[int, str] | None = None) -> str:
    """静态渲染单个节点，Alpine 控制折叠状态。

    节点类型由 pipeline 产出：root / subsystem / module（历史上还有 dir / file）。
    渲染策略与 type 解耦——按节点实际携带的字段渲染，任何带 children 的节点都递归，
    避免 type 取值与渲染分支不一致导致整棵子树被丢弃。

    anchor_ids：{id(node): 锚点 id}，给左侧目录指向的顶层子系统挂上 HTML id。
    """
    anchor_ids = anchor_ids or {}
    typ = node.get("type", "")
    # root 只是仓库名占位（与页眉 h1 重复），直接渲染子树，少一层冗余缩进
    if typ == "root":
        return "".join(
            _render_tree_node_static(c, 1, resolver, anchor_ids)
            for c in (node.get("children") or [])
        )
    is_container = typ in ("dir", "root", "subsystem")
    path = node.get("path", "")
    name = _esc(node.get("name") or path or "/")
    role = _esc(node.get("role", ""))
    summary_text = concise_module_summary(
        node.get("brief") or node.get("summary") or node.get("content") or "",
        input_is_html=not bool(node.get("brief") or node.get("summary")),
    )
    summary = linkify_html(_esc(summary_text), resolver)
    open_default = "true" if depth <= 1 else "false"
    node_key = path or "__root__"

    # 节点标题字号随树深度递减：root > 子系统 > 模块 > 更深
    title_size = {0: "text-xl", 1: "text-lg", 2: "text-base"}.get(depth, "text-sm")

    # 子系统/模块不打分，评分只在顶层 verdict 卡片展示
    # NOTE: role 角标 HTML 提到局部变量，避免 f-string 表达式内出现反斜杠（Python 3.11 兼容）。
    _role_badge = '<span class="text-xs text-slate-500">(' + role + ')</span>' if role else ""
    head = (
        f'<div class="tree-toggle flex items-baseline gap-2 py-1" @click="open = !open; '
        f'localStorage.setItem(\'tree:{_esc(node_key)}\', open ? \'1\' : \'0\'); '
        f'if (open) $dispatch(\'section:opened\')">'
        f'<span class="text-slate-400 w-4 text-center" x-text="open ? \'▾\' : \'▸\'"></span>'
        f'<span class="font-semibold {title_size} {("text-cyan-700 dark:text-cyan-400" if is_container else "")}">'
        f'{name}{"/" if typ == "dir" else ""}</span>'
        f'{_role_badge}'
        f'</div>'
    )

    body_parts: list[str] = []
    if summary:
        body_parts.append(
            f'<div class="module-analysis text-sm text-slate-700 dark:text-slate-300 mt-1 mb-2" '
            f'data-analysis-chars="{len(summary_text)}">'
            f'{summary}</div>'
        )

    if typ == "module" and node.get("file_paths"):
        source_links = [
            _resolve_path_anchor(str(path), resolver)
            for path in (node.get("file_paths") or [])[:8]
            if str(path or "").strip()
        ]
        if source_links:
            body_parts.append(
                '<div class="text-xs text-slate-500 mb-2">源码证据：'
                + "、".join(source_links) + '</div>'
            )

    # 决赛交付中，子系统和模块只保留不超过 300 字的分析及源码证据入口；
    # LLM 的长正文不再嵌入报告，避免“折叠了但仍然交付大量文字”的形式合规。
    content = _strip_html_ids(_strip_diagrams(node.get("content") or ""))
    if content.strip() and typ not in {"subsystem", "module"}:
        body_parts.append(
            f'<details class="detail-toggle mt-2 mb-2"><summary>展开详细证据</summary>'
            f'<div class="node-content prose prose-sm dark:prose-invert max-w-none mt-1">'
            f'{linkify_html(content, resolver)}</div></details>'
        )

    if typ in {"subsystem", "module"}:
        # 之前只渲染路径、累计 4 条截断，quote/severity 全丢。改回与顶层节点
        # 一致的 _render_findings：逐条输出 路径 + severity 徽章 + 评判语。
        body_parts.append(_render_findings(
            node.get("highlights") or [], resolver,
            title="本节点亮点", color_cls="text-green-700 dark:text-green-400",
            is_issue=False))
        body_parts.append(_render_findings(
            node.get("issues") or [], resolver,
            title="本节点问题", color_cls="text-red-700 dark:text-red-400",
            is_issue=True))
    else:
        body_parts.append(_render_findings(
            node.get("highlights") or [], resolver,
            title="本节点亮点", color_cls="text-green-700 dark:text-green-400",
            is_issue=False))
        body_parts.append(_render_findings(
            node.get("issues") or [], resolver,
            title="本节点问题", color_cls="text-red-700 dark:text-red-400",
            is_issue=True))

    # 任何带 children 的节点都递归渲染子树
    children = node.get("children") or []
    if children:
        child_html = "".join(
            _render_tree_node_static(c, depth + 1, resolver, anchor_ids)
            for c in children
        )
        body_parts.append(f'<div class="ml-4 mt-2">{child_html}</div>')

    body = "".join(body_parts)
    body_div = (
        f'<div class="tree-body" x-show="open" x-cloak>{body}</div>'
        if body else ""
    )
    # 顶层子系统挂锚点 id + data-section-id，供目录跳转与滚动高亮
    sid = anchor_ids.get(id(node))
    anchor_attr = f' id="{_esc(sid)}" data-section-id="{_esc(sid)}"' if sid else ""
    return (
        f'<div class="tree-node mb-1"{anchor_attr} x-data="{{ open: {open_default} }}" '
        f'x-init="(function(){{ const s = localStorage.getItem(\'tree:{_esc(node_key)}\'); '
        f'if (s !== null) open = (s === \'1\'); }})()">'
        f'{head}{body_div}</div>'
    )


def write_tree_html(out_path: Path, tree_json: dict,
                    repo_roots: list[Path] | None = None,
                    title: str | None = None) -> tuple[Path, set[str]]:
    """把 tree.json 渲染为 HTML 并写入 out_path。返回 (path, 断链路径集合)。"""
    broken: set[str] = set()
    # 报告元数据中的目标仓库 URL 是证据链接的权威来源。下载归档通常没有 .git，
    # 此时绝不能让 git 向上穿透并继承报告生成器仓库的 remote。
    web_bases = None
    if repo_roots:
        meta = tree_json.get("meta") or {}
        repository_url = str(meta.get("repository_url") or "")
        explicit_ref = str(
            meta.get("repository_ref") or meta.get("revision") or meta.get("commit") or ""
        )
        web_bases = []
        for index, root in enumerate(repo_roots):
            derived_base = derive_repo_web_base(Path(root))
            ref = explicit_ref
            if not ref:
                archive_match = re.search(r"-([0-9a-fA-F]{40})$", Path(root).name)
                derived_ref = re.search(r"/([0-9a-fA-F]{40})$", derived_base or "")
                ref = (
                    archive_match.group(1) if archive_match else
                    (derived_ref.group(1) if derived_ref else "main")
                )
            explicit_base = (
                repository_url_to_web_base(repository_url, ref)
                if index == 0 and repository_url else None
            )
            web_bases.append(explicit_base or derived_base)
    resolver = make_file_link_resolver(
        repo_roots, scheme="vscode", broken_paths=broken,
        repo_web_bases=web_bases,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_tree_html(
        tree_json,
        title=title or f"{tree_json.get('meta',{}).get('repo','?')} 作品描述报告",
        resolver=resolver,
    )
    if broken:
        sample = "、".join(sorted(broken)[:8])
        raise IncompleteReportError(
            f"报告包含 {len(broken)} 个无法回溯到源码的文件引用：{sample}"
        )
    out_path.write_text(html_text, encoding="utf-8")
    return out_path, broken
