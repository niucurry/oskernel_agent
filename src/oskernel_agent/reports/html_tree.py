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

from .html import (
    _BROKEN_PREFIX,
    _CDN_HEAD,
    _INIT_SCRIPT,
    assert_toc_resolves,
    derive_repo_web_base,
    linkify_html,
    make_file_link_resolver,
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
<section id="verdict" data-section-id="verdict" class="mb-8 p-6 rounded-lg border border-slate-300 dark:border-slate-700
                             bg-white dark:bg-slate-800 shadow-sm">
  <div class="flex items-baseline gap-3 mb-3">
    <span class="text-3xl font-bold {cls}">{score_total}</span>
    <span class="text-lg italic text-slate-700 dark:text-slate-300">{one_line}</span>
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
  <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
    <div>
      <div class="text-xs uppercase tracking-wider text-green-700 dark:text-green-400 mb-2">
        亮点 highlights
      </div>
      <ul class="text-sm list-disc pl-5">{hi_items or '<li class="text-slate-500">(无)</li>'}</ul>
    </div>
    <div>
      <div class="text-xs uppercase tracking-wider text-red-700 dark:text-red-400 mb-2">
        槽点 issues
      </div>
      <ul class="text-sm list-disc pl-5">{is_items or '<li class="text-slate-500">(无)</li>'}</ul>
    </div>
  </div>
</section>
"""


# 渲染入口

def render_tree_html(tree_json: dict, title: str = "代码树报告",
                     resolver=None) -> str:
    """渲染完整 HTML 文档。"""
    meta = tree_json.get("meta", {})
    verdict = tree_json.get("verdict", {}) or {}
    verdict_html = _render_verdict(verdict, resolver)
    similarity_html = _render_similarity(verdict.get("similarity") or {}, resolver)

    # 为顶层子系统及其直接子节点（模块）分配稳定锚点 id（供目录跳转 + 滚动高亮）。
    # 目录做两层：子系统（level 1）+ 模块（level 2），更深的节点不进目录以免过长。
    tree_root = tree_json.get("tree", {}) or {}
    subsystems = (tree_root.get("children") or []) \
        if tree_root.get("type") == "root" else [tree_root]
    anchor_ids: dict[int, str] = {}
    toc_subs: list[tuple[str, str, int]] = []  # (anchor_id, label, level)
    for i, node in enumerate(subsystems):
        if not isinstance(node, dict):
            continue
        sid = f"subsys-{i}"
        anchor_ids[id(node)] = sid
        label = node.get("name") or node.get("path") or f"模块 {i + 1}"
        toc_subs.append((sid, str(label), 1))
        for j, child in enumerate(node.get("children") or []):
            if not isinstance(child, dict):
                continue
            cid = f"subsys-{i}-{j}"
            anchor_ids[id(child)] = cid
            clabel = child.get("name") or child.get("path") or f"模块 {j + 1}"
            toc_subs.append((cid, str(clabel), 2))

    # 直接生成静态 HTML 树（折叠状态由 Alpine 的 x-data open 控制），
    # 节点内部 HTML 片段在服务端预渲染好，file:line 链接已解析。
    tree_static_html = _render_tree_node_static(
        tree_root, depth=0, resolver=resolver, anchor_ids=anchor_ids,
    )

    toc_html = _render_toc(verdict_html, similarity_html, toc_subs)

    title_safe = _esc(title)
    language_warning = ""
    if meta.get("language_incomplete"):
        language_warning = (
            '<div role="alert" class="mb-5 rounded-lg border border-amber-300 '
            'bg-amber-50 px-4 py-3 text-sm text-amber-900">'
            '<b>中文化未完成：</b>部分内容未能完成中文化，可能仍含英文正文或标题。'
            '请配置可用的 LLM API 后重跑，并在人工评审前复核。</div>'
        )
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title_safe}</title>
{_TREE_HEAD}
<style>{_TREE_CSS}</style>
</head>
<body class="bg-slate-50 dark:bg-slate-900 text-slate-900 dark:text-slate-100">
<div class="max-w-7xl mx-auto flex gap-6 px-4 md:px-6">
  {toc_html}
  <main class="flex-1 min-w-0 py-6 md:py-10">
    <header class="mb-6">
      <h1 class="text-2xl font-bold">{_esc(meta.get('repo','?'))}</h1>
      <div class="text-sm text-slate-500">
        评估时间：{_esc(meta.get('ts',''))} · 源文件：{_esc(meta.get('indexed_files',0))} 个
      </div>
    </header>
    {language_warning}
    {verdict_html}
    {similarity_html}
    <section id="tree" data-section-id="tree">
      <h2 class="text-xl font-semibold mb-3">代码树（下层 = 中性描述）</h2>
      <div class="tree-root rounded-lg border border-slate-200 dark:border-slate-700
                  bg-white dark:bg-slate-800 p-4">
        {tree_static_html}
      </div>
    </section>
  </main>
</div>
{_INIT_SCRIPT}
{_TREE_RESIZE_ON_OPEN}
</body>
</html>
"""
    # 产出前硬校验：每个目录项都必须精确定位到唯一锚点，否则抛错而非静默产出
    assert_toc_resolves(doc)
    return doc


def _render_toc(verdict_html: str, similarity_html: str,
                toc_subs: list[tuple[str, str, int]]) -> str:
    """左侧粘性目录：评判 / 相似度 / 代码树 + 子系统（含模块），点击平滑定位。

    toc_subs 每项为 (anchor_id, label, level)：level 1 = 子系统，2 = 模块（缩进更深）。
    """
    items: list[str] = []
    if verdict_html.strip():
        items.append('<a class="toc-link" href="#verdict">综合评判</a>')
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
    summary = _esc(node.get("summary", ""))
    open_default = "true" if depth <= 1 else "false"
    node_key = path or "__root__"

    # 节点标题字号随树深度递减：root > 子系统 > 模块 > 更深
    title_size = {0: "text-xl", 1: "text-lg", 2: "text-base"}.get(depth, "text-sm")

    # 聚合失败留痕：标题旁红色徽章 + 展开后醒目提示，避免空白被当成「正常但没内容」
    has_error = bool(node.get("_error"))
    err_badge = (
        '<span class="ml-1 px-1.5 py-0.5 rounded text-xs font-semibold '
        'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300" '
        'title="该子系统 LLM 聚合失败，内容缺失——重跑可恢复">⚠ 聚合失败</span>'
        if has_error else ""
    )

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
        f'{err_badge}'
        f'</div>'
    )

    body_parts: list[str] = []
    if has_error:
        body_parts.append(
            '<div class="mt-1 mb-2 p-2 rounded border border-red-300 dark:border-red-800 '
            'bg-red-50 dark:bg-red-900/20 text-sm text-red-700 dark:text-red-300">'
            '本子系统 LLM 聚合失败，正文与模块缺失（当前为规则兜底）。'
            '重跑即可恢复；失败结果不再写入缓存。</div>'
        )
    if summary:
        body_parts.append(
            f'<div class="text-sm text-slate-700 dark:text-slate-300 mt-1 mb-2">'
            f'{summary}</div>'
        )

    # 详细叙述正文（subsystem / module 的 agent HTML 输出）——原样嵌入并链接化
    content = _strip_html_ids(_strip_diagrams(node.get("content") or ""))
    if content.strip():
        body_parts.append(
            f'<div class="node-content prose prose-sm dark:prose-invert max-w-none mt-1 mb-2">'
            f'{linkify_html(content, resolver)}</div>'
        )

    # 本节点的 highlights / issues（subsystem 可能携带）——逐条列表，结构清晰
    body_parts.append(_render_findings(
        node.get("highlights") or [], resolver,
        title="本节点亮点", color_cls="text-green-700 dark:text-green-400",
        is_issue=False))
    body_parts.append(_render_findings(
        node.get("issues") or [], resolver,
        title="本节点槽点", color_cls="text-red-700 dark:text-red-400",
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
    # 优先生成指向仓库网页的链接（从 git remote+HEAD 推 blob 基址）；
    # 无 git 信息的本地仓库回退到本地编辑器 scheme。
    web_bases = ([derive_repo_web_base(Path(r)) for r in repo_roots]
                 if repo_roots else None)
    resolver = make_file_link_resolver(
        repo_roots, scheme="vscode", broken_paths=broken,
        repo_web_bases=web_bases,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_tree_html(
        tree_json,
        title=title or f"{tree_json.get('meta',{}).get('repo','?')} 代码树报告",
        resolver=resolver,
    )
    out_path.write_text(html_text, encoding="utf-8")
    return out_path, broken
