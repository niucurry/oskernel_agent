"""比较报告的富交互 HTML 渲染。

复用 oskernel_agent.reports.html 的基础设施（CDN: Tailwind + Alpine + ECharts，
滚动联动 / TOC 点击初始化脚本，TOC 完整性校验），
把 src.report 生成的查重评审报告渲染成与「描述报告」同款的单文件交互 HTML：

  - 顶部摘要卡片：漏斗数字 + Top-N 命中历史作品 ECharts 柱图
  - 左侧 TOC 侧栏（滚动联动高亮、点击平滑跳转）
  - 每个 `## ` 章节为可折叠 <section>（Alpine，状态存 localStorage）
  - 表格里的 文件:行 引用已由 sections 阶段注入为 GitLab blob Markdown 链接（见 gitlab_links.py）
"""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

from markdown_it import MarkdownIt

from oskernel_agent.reports.html import (
    _CDN_HEAD,
    _INIT_SCRIPT,
    assert_toc_resolves,
)

_MD = MarkdownIt("commonmark", {"html": True}).enable("table").enable("strikethrough")


def _split_sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    """把报告 MD 拆成 (页标题, [(章标题, 章正文MD), ...])。

    约定：首个 `# ` 是报告标题；每个 `## ` 起一个章节，到下一个 `## `/文末为本章正文。
    """
    lines = md.splitlines()
    title = "作品查重评审报告"
    i = 0
    while i < len(lines) and not lines[i].startswith("# "):
        i += 1
    if i < len(lines):
        title = lines[i][2:].strip()
        i += 1
    sections: list[tuple[str, str]] = []
    cur_head: str | None = None
    cur_body: list[str] = []
    for ln in lines[i:]:
        if ln.startswith("## "):
            if cur_head is not None:
                sections.append((cur_head, "\n".join(cur_body).strip()))
            cur_head = ln[3:].strip()
            cur_body = []
        else:
            cur_body.append(ln)
    if cur_head is not None:
        sections.append((cur_head, "\n".join(cur_body).strip()))
    return title, sections


def _summary_card(reviewed: dict, recall: dict) -> str:
    """顶部摘要卡：漏斗数字 + Top-N 命中历史作品 ECharts 横向柱图。"""
    suspects = reviewed.get("suspects", [])
    query_repo = recall.get("query_repo_id") or "新作品"

    def _verdict(s):
        rv = s.get("review_verdict") or s.get("verdict")
        return rv.get("verdict") if isinstance(rv, dict) else rv

    total = len(suspects)
    confirmed = sum(
        1 for s in suspects
        if s.get("tier") == "confirmed" or _verdict(s) == "likely_clone"
    )
    reviewed_n = sum(1 for s in suspects if s.get("tier") == "review")

    repo_counts: Counter = Counter()
    for s in suspects:
        c = s.get("candidate_func") or {}
        rid = c.get("repo_id") or c.get("repo") or "?"
        repo_counts[rid] += 1
    top = repo_counts.most_common(6)

    pills = (
        '<div class="flex flex-wrap gap-2 text-sm">'
        f'<span class="px-3 py-1 rounded-full bg-slate-100 dark:bg-slate-800">嫌疑对 {total}</span>'
        f'<span class="px-3 py-1 rounded-full bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300">confirmed {confirmed}</span>'
        f'<span class="px-3 py-1 rounded-full bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">review {reviewed_n}</span>'
        '</div>'
    )

    chart_html = ""
    if top:
        labels = [t[0] for t in top]
        values = [t[1] for t in top]
        option = {
            "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
            "grid": {"left": "32%", "right": "10%", "top": "6%", "bottom": "6%"},
            "xAxis": {"type": "value", "minInterval": 1},
            "yAxis": {"type": "category", "data": labels[::-1],
                      "axisLabel": {"fontSize": 11}},
            "series": [{"type": "bar", "data": values[::-1],
                        "itemStyle": {"color": "#4a90d9"},
                        "label": {"show": True, "position": "right"}}],
        }
        chart_html = (
            f'<div class="echarts-chart mt-4" style="height:{max(180, len(top) * 36)}px">'
            f'<script type="application/json">{json.dumps(option, ensure_ascii=False)}</script>'
            '</div>'
        )

    return (
        f'<section id="summary" data-section-id="summary" '
        'class="mb-8 p-6 rounded-lg border border-slate-300 dark:border-slate-700 '
        'bg-white dark:bg-slate-900 shadow-sm">'
        '<div class="flex items-center justify-between flex-wrap gap-2">'
        '<h2 class="text-lg font-bold text-slate-800 dark:text-slate-100 m-0">'
        f'{html.escape(query_repo)} <span class="text-slate-400 font-normal text-base">查重摘要</span></h2>'
        f'{pills}</div>{chart_html}</section>'
    )


def render_comparison_html(
    md: str,
    reviewed: dict,
    recall: dict,
) -> str:
    """把报告 MD + 结构化数据渲染为富交互单文件 HTML。

    文件:行 链接已在 sections 数据组装阶段以 Markdown 链接形式注入（指向 GitLab blob），
    本函数不再做 vscode 本地跳转链接化。
    """
    title, sections = _split_sections(md)

    summary_html = _summary_card(reviewed, recall)

    toc_items: list[str] = ['<a class="toc-link" href="#summary">查重摘要</a>']
    body_parts: list[str] = [summary_html]
    for idx, (head, body_md) in enumerate(sections):
        sid = f"sec-{idx}"
        toc_items.append(f'<a class="toc-link" href="#{sid}">{html.escape(head)}</a>')
        body_html = _MD.render(body_md) if body_md else ""
        # 三引号 f-string：可自由含 ' 与 "，{{ / }} 转义字面花括号，无需反斜杠转义
        section = f"""<section id="{sid}" data-section-id="{sid}" class="mb-6 rounded-lg border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 shadow-sm overflow-hidden" x-data="{{ open: true }}" x-init="(function(){{const s=localStorage.getItem('cmp:{sid}');if(s!==null)open=(s==='1');}})()">
<div class="px-6 py-3 flex items-center gap-2 cursor-pointer select-none border-b border-slate-200 dark:border-slate-800 bg-slate-50 dark:bg-slate-800/50" @click="open=!open; localStorage.setItem('cmp:{sid}', open?'1':'0')">
<span class="text-slate-400 w-4 text-center" x-text="open?'▾':'▸'"></span>
<h2 class="text-base font-semibold text-slate-800 dark:text-slate-100 m-0">{html.escape(head)}</h2>
</div>
<div class="prose prose-sm dark:prose-invert max-w-none px-6 py-4" x-show="open" x-cloak>{body_html}</div>
</section>"""
        body_parts.append(section)

    toc_html = "\n".join(toc_items)
    main_html = "\n".join(body_parts)

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{_CDN_HEAD}
<style>
  [x-cloak] {{ display: none !important; }}
  body {{ background: #f6f8fa; }}
  @media (prefers-color-scheme: dark) {{ body {{ background: #0d1117; }} }}
  .layout {{ display: flex; gap: 1.5rem; max-width: 1280px; margin: 0 auto; padding: 1.5rem; }}
  .toc {{ position: sticky; top: 1.5rem; align-self: flex-start; width: 220px; flex-shrink: 0; font-size: .85rem; }}
  .toc .toc-link {{ display: block; padding: .3rem .5rem; border-left: 2px solid transparent; color: #475569; text-decoration: none; border-radius: 0 .3rem .3rem 0; }}
  .toc .toc-link:hover {{ background: #eef2f7; }}
  .toc .toc-active {{ border-left-color: #4a90d9; color: #1a66d4; font-weight: 600; background: #eef2fb; }}
  @media (prefers-color-scheme: dark) {{
    .toc .toc-link {{ color: #94a3b8; }}
    .toc .toc-link:hover {{ background: #161b22; }}
    .toc .toc-active {{ border-left-color: #6cb6ff; color: #6cb6ff; background: #11161d; }}
  }}
  .main {{ flex: 1; min-width: 0; }}
  .file-jump {{ color: #1a66d4; text-decoration: underline dotted; }}
  .file-jump.file-broken {{ color: #b45309; text-decoration: underline dashed; }}
  @media (max-width: 860px) {{ .layout {{ flex-direction: column; }} .toc {{ position: static; width: auto; }} }}
</style>
</head>
<body>
<div class="layout">
  <nav class="toc">{toc_html}</nav>
  <main class="main">
    <h1 class="text-2xl font-bold text-slate-800 dark:text-slate-100 mb-6 border-b-2 border-blue-400 pb-2">{html.escape(title)}</h1>
    {main_html}
  </main>
</div>
{_INIT_SCRIPT}
</body>
</html>
"""
    assert_toc_resolves(doc)
    return doc
