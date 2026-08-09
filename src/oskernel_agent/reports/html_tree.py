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

from finals.digests import (
    DESCRIPTION_SUBSYSTEM_ORDER,
    description_digest_from_tree,
    description_priority_findings,
    normalize_description_claim,
)
from finals.readability import clip_at_sentence, concise_module_summary, explain_terms_in_html

from ..report_quality import IncompleteReportError, assert_report_complete
from .html import (
    _BROKEN_PREFIX,
    _CDN_HEAD,
    _INIT_SCRIPT,
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
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
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
        f, line = path_with_line, None
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
            '未提供的编译或运行日志不在此结论范围内。</p></li>'
        )

    metrics = digest.metrics
    status_text = {
        "passed": "通过", "failed": "失败", "unknown": "未能确认",
        "not_provided": "未提供", "missing": "文件缺失", "skipped": "不适用",
    }
    build_status = status_text.get(str(metrics.get("build_log_status")), "未能确认")
    run_status = status_text.get(str(metrics.get("run_log_status")), "未能确认")
    candidates = int(metrics.get("hardcode_candidates", metrics.get("hardcode_signals", 0)) or 0)
    selected_signals = int(metrics.get("hardcode_signals", 0) or 0)
    hardcode_count_text = (
        f"硬编码规则命中 {candidates} 条候选，选取 {selected_signals} 条进入 AI 复核，"
        if candidates != selected_signals else
        f"硬编码规则命中 {selected_signals} 条候选，"
    )
    coverage = (
        f'编译日志：{build_status}；运行日志：{run_status}；'
        f'{hardcode_count_text}'
        f'AI 确认 {_esc(metrics.get("hardcode_confirmed", 0))} 条、'
        f'疑似 {_esc(metrics.get("hardcode_suspected", 0))} 条、'
        f'排除 {_esc(metrics.get("hardcode_cleared", 0))} 条。'
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


def _status_label(status: str) -> tuple[str, str]:
    labels = {
        "passed": ("已验证通过", "status-ok"),
        "failed": ("失败", "status-bad"),
        "skipped": ("未执行", "status-warn"),
        "not_provided": ("未提供", "status-warn"),
        "missing": ("文件缺失", "status-bad"),
        "unknown": ("结果不明确", "status-warn"),
        "configured": ("配置存在，未实测", "status-warn"),
        "warning": ("配置不一致", "status-bad"),
        "partial": ("仅部分配置", "status-warn"),
    }
    return labels.get(str(status or "unknown"), ("未核验", "status-warn"))


def _first_evidence_link(evidence_items: list, resolver) -> str:
    for item in evidence_items or []:
        if isinstance(item, dict):
            path = str(item.get("path") or "")
            line = item.get("line")
        else:
            path = str(getattr(item, "path", "") or "")
            line = getattr(item, "line", None)
        if not path:
            continue
        if line:
            return _resolve_path_anchor(f"{path}:{line}", resolver)
        if re.search(r"(?::|#L)\d+(?:-L?\d+)?$", path):
            return _resolve_path_anchor(path, resolver)
    return ""


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

    def _add(item: dict, subsystem: str | None = None) -> None:
        nonlocal order
        path = str(item.get("path") or "").strip()
        quote = clip_at_sentence(normalize_description_claim(
            str(item.get("quote") or ""), path, tree_json.get("facts") or {},
        ), 150)
        if not path or not quote or not re.search(r"(?::|#L)\d+(?:-L?\d+)?$", path):
            return
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
            _add(item)
    for node in subsystem_nodes:
        name = str(node.get("name") or "未分类")
        for item in node.get("issues") or []:
            if isinstance(item, dict):
                _add(item, name)

    issues = list(merged.values())
    for item in issues:
        text = item["quote"]
        correctness = any(term in text for term in _CORRECTNESS_RISK_TERMS)
        incomplete = any(term in text for term in _COMPLETENESS_RISK_TERMS)
        performance = any(term in text for term in _PERFORMANCE_RISK_TERMS)
        item["major"] = (
            item["severity"] in {"critical", "high", "medium"}
            or correctness or incomplete
        )
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
        item["impact_group"],
        -_SEVERITY_RANK[item["severity"]],
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
    other_rows = [
        _render_issue_row(item, resolver, main=False) for item in issues if not item["major"]
    ]
    if not major_rows:
        major_rows.append(
            '<li class="finding-row"><p class="text-sm">静态分析未形成需要优先报告的高置信代码问题；'
            '这不等同于功能测试通过。</p></li>'
        )
    other_html = (
        '<details class="mt-4"><summary class="cursor-pointer text-sm font-semibold">'
        f'其他已定位的低风险问题（{len(other_rows)} 项）</summary>'
        f'<ol class="space-y-3 mt-3">{"".join(other_rows)}</ol></details>'
        if other_rows else ""
    )
    return f"""
<section id="verdict" data-section-id="verdict" class="brief-card p-5 mb-5">
  <div class="text-xs font-semibold tracking-wider text-blue-700 dark:text-blue-300 mb-2">先看结论</div>
  <h2 class="text-xl font-bold mb-2">AI 分析结论</h2>
  <p class="text-base leading-relaxed mb-5">{_esc(clip_at_sentence(digest.conclusion, 180))}</p>
  <div id="findings" data-section-id="findings">
    <h3 class="font-semibold mb-1">AI 检测到的重要问题</h3>
    <p class="text-xs text-slate-500 mb-3">全部高、中风险及影响语义正确性的缺失均直接展示，不设数量上限。</p>
    <ol class="space-y-3">{"".join(major_rows)}</ol>
    {other_html}
  </div>
</section>
"""


def _render_usability(tree_json: dict, resolver) -> str:
    integrity = ((tree_json.get("facts") or {}).get("integrity") or {})
    rows: list[str] = []
    for key, label in (("build_log", "构建"), ("run_log", "启动 / 运行")):
        fact = integrity.get(key) or {}
        status = str(fact.get("status") or "not_provided")
        status_text, status_cls = _status_label(status)
        note = str(fact.get("note") or "").strip()
        if not note:
            if fact.get("errors"):
                note = "；".join(str(value) for value in fact.get("errors")[:2])
            elif status == "not_provided":
                note = "没有正式日志，不能判断结果。"
            else:
                note = "当前材料不足以核验。"
        evidence = ""
        if fact.get("path"):
            evidence = _resolve_path_anchor(str(fact.get("path")), resolver)
        rows.append(
            '<div class="grid grid-cols-1 md:grid-cols-[7rem_8rem_1fr] gap-2 py-2 border-b '
            'border-slate-200 dark:border-slate-700">'
            f'<strong class="text-sm">{label}</strong>'
            f'<span><span class="status-pill {status_cls}">{status_text}</span></span>'
            f'<p class="text-sm text-slate-600 dark:text-slate-300">{_esc(clip_at_sentence(note, 150))}'
            f'{(" · 日志：" + evidence) if evidence else ""}</p></div>'
        )

    reproducibility = integrity.get("reproducibility") or {}
    repro_status = str(reproducibility.get("status") or "unknown")
    repro_text, repro_cls = _status_label(repro_status)
    repro_summary = str(reproducibility.get("summary") or "未采集容器复现配置。")
    repro_evidence = _first_evidence_link(reproducibility.get("evidence") or [], resolver)
    rows.append(
        '<div class="grid grid-cols-1 md:grid-cols-[7rem_8rem_1fr] gap-2 py-2">'
        '<strong class="text-sm">自动评测复现</strong>'
        f'<span><span class="status-pill {repro_cls}">{repro_text}</span></span>'
        f'<p class="text-sm text-slate-600 dark:text-slate-300">'
        f'{_esc(clip_at_sentence(repro_summary, 160))}'
        f'{(" · 证据：" + repro_evidence) if repro_evidence else ""}</p></div>'
    )
    return f"""
<section id="usability" data-section-id="usability" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-3">真实可用性</h2>
  <div>{"".join(rows)}</div>
  <p class="text-xs text-slate-500 mt-3">分析边界：构建、启动和测试均未通过实测；静态代码不能证明评测通过或性能达标。</p>
</section>
"""


def _render_hardcode_brief(tree_json: dict, resolver) -> str:
    integrity = ((tree_json.get("facts") or {}).get("integrity") or {})
    hardcode = integrity.get("hardcode") or {}
    reviews = (tree_json.get("verdict") or {}).get("hardcode_reviews") or []
    confirmed = [item for item in reviews if isinstance(item, dict) and item.get("status") == "confirmed"]
    suspected = [item for item in reviews if isinstance(item, dict) and item.get("status") == "suspected"]
    cleared = [item for item in reviews if isinstance(item, dict) and item.get("status") == "cleared"]
    scanned = int(hardcode.get("scanned_files") or 0)
    candidates = int(hardcode.get("candidate_count") or len(hardcode.get("findings") or []))
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
            f"扫描 {scanned} 个文件并抽取 {len(hardcode.get('findings') or [])}/{candidates} 条候选；"
            "候选输出被截断，不能据此给出完整的无作弊结论。"
        )
        status_text, status_cls = "范围不完整", "status-warn"
    elif confirmed or suspected:
        conclusion = (
            f"扫描 {scanned} 个文件、命中 {candidates} 条候选；AI 复核确认 {len(confirmed)} 条、"
            f"疑似 {len(suspected)} 条、排除 {len(cleared)} 条。"
        )
        status_text, status_cls = "需要核查", "status-bad"
    elif len(reviews) >= len(hardcode.get("findings") or []):
        conclusion = (
            f"未发现作弊型硬编码。扫描 {scanned} 个文件、命中 {candidates} 条候选，"
            f"AI 逐条复核后排除 {len(cleared)} 条。"
        )
        status_text, status_cls = "未发现", "status-ok"
    else:
        conclusion = "硬编码候选尚未全部完成结构化 AI 复核，不能下结论。"
        status_text, status_cls = "复核不完整", "status-warn"

    risky_rows: list[str] = []
    for item in (confirmed + suspected):
        location = str(item.get("path") or "")
        if item.get("line"):
            location += f':{int(item["line"])}'
        evidence = _resolve_path_anchor(location, resolver) if location else ""
        method = clip_at_sentence(str(item.get("method") or "未说明实现方法。"), 100)
        reason = clip_at_sentence(str(item.get("reason") or "证据需要复核。"), 100)
        review_status = "确认问题" if item.get("status") == "confirmed" else "疑似问题"
        risky_rows.append(
            '<li class="finding-row text-sm">'
            f'<strong>{_esc(item.get("category") or "硬编码线索")} · {review_status}</strong>：{_esc(method)} '
            f'{_esc(reason)}{(" · 证据：" + evidence) if evidence else ""}</li>'
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


def _render_all_subsystems(tree_json: dict, resolver) -> str:
    root = tree_json.get("tree") or {}
    nodes = [node for node in (root.get("children") or []) if isinstance(node, dict)]
    by_name = {str(node.get("name") or ""): node for node in nodes}
    ordered_names = [name for name in DESCRIPTION_SUBSYSTEM_ORDER if name in by_name]
    ordered_names.extend(
        str(node.get("name") or "未命名模块")
        for node in nodes
        if str(node.get("name") or "") not in DESCRIPTION_SUBSYSTEM_ORDER
    )
    issue_by_path = {
        str(item.get("path") or ""): item for item in _collect_report_issues(tree_json)
    }
    cards: list[str] = []
    for name in ordered_names:
        node = by_name[name]
        capability = clip_at_sentence(normalize_description_claim(
            str(node.get("brief") or node.get("summary") or "未形成可靠的静态模块摘要。"),
            "", tree_json.get("facts") or {},
        ), 90)
        capability = (
            capability.replace("实现了完整的", "覆盖")
            .replace("实现完整", "覆盖")
            .replace("确保 ", "用于 ")
            .replace("完全解耦", "解耦")
        )
        implementation_link = _first_evidence_link(node.get("highlights") or [], resolver)
        evidence_html = (
            f'<p class="text-xs text-slate-500 mt-1">实现依据：{implementation_link}</p>'
            if implementation_link else ""
        )
        issue_refs: list[str] = []
        for node_issue in node.get("issues") or []:
            if not isinstance(node_issue, dict):
                continue
            issue = issue_by_path.get(str(node_issue.get("path") or ""))
            if not issue:
                continue
            issue_refs.append(
                f'<a class="file-jump" href="#{_esc(issue["anchor"])}" '
                f'title="{_esc(issue["quote"])}">#{issue["number"]}</a>'
            )
        issue_html = (
            '<p class="text-sm leading-relaxed"><strong>已定位问题：</strong>'
            + "、".join(issue_refs) + '（见上方问题清单与证据）</p>'
            if issue_refs else
            '<p class="text-sm leading-relaxed"><strong>已定位问题：</strong>'
            '未形成带源码位置的结构化问题；运行状态仍以“真实可用性”为准。</p>'
        )
        cards.append(
            f'<article class="core-card" data-subsystem="{_esc(name)}" '
            f'data-analysis-chars="{len(capability)}">'
            f'<h3 class="font-bold mb-1">{_esc(name)}</h3>'
            f'<p class="text-sm leading-relaxed"><strong>静态实现：</strong>{_esc(capability)}</p>'
            f'{issue_html}'
            f'{evidence_html}</article>'
        )
    return f"""
<section id="modules" data-section-id="modules" class="brief-card p-5 mb-5">
  <h2 class="text-xl font-bold mb-1">模块概览</h2>
  <p class="text-xs text-slate-500 mb-3">覆盖仓库识别出的全部一级子系统；每项只保留实现判断、源码入口和上方问题编号。</p>
  <div>{"".join(cards)}</div>
</section>
"""


def _render_brief_toc() -> str:
    items = (
        ("verdict", "结论与关键问题"),
        ("usability", "真实可用性"),
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


# 渲染入口

def render_tree_html(tree_json: dict, title: str = "代码树报告",
                     resolver=None) -> str:
    """渲染“问题完整、文字精简”的评委作品描述报告。"""
    assert_report_complete("", structured=tree_json)
    meta = tree_json.get("meta", {})
    conclusion_html = _render_judge_conclusion(tree_json, resolver)
    usability_html = _render_usability(tree_json, resolver)
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
        <span class="status-pill status-ok">准确性优先 · 精简呈现</span>
      </div>
      <div class="text-sm text-slate-500 mt-2">队伍：{_esc(meta.get('team_id','未记录'))} · 分析时间：{_esc(meta.get('ts',''))} · 索引源文件：{_esc(meta.get('indexed_files',0))} 个</div>
      <div class="text-xs text-slate-500 mt-1">本报告完全由 AI 分析工具生成，参赛队未参与修改。</div>
    </header>
    {conclusion_html}
    {usability_html}
    {hardcode_html}
    {modules_html}
    <footer class="text-xs text-slate-500 pb-8">结论所需证据均已收录在本报告的源码链接中。</footer>
  </main>
</div>
</body>
</html>
"""
    doc = explain_terms_in_html(doc)
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
        node.get("brief") or node.get("summary") or node.get("content") or ""
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
