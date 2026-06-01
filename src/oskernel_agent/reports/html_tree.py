"""
HTML 渲染：tree.json → 多层 Alpine.js 折叠树报告。

顶部：固定 verdict 卡片（多维度评分 + 亮点 / 槽点 + 一句话总评）
下方：递归折叠树，每个节点可点击展开 / 折叠，状态存 localStorage
"""

from __future__ import annotations

import html
from pathlib import Path

from .html import (
    _BROKEN_PREFIX,
    _CDN_HEAD,
    _INIT_SCRIPT,
    linkify_html,
    make_file_link_resolver,
)


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
html, body {
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
               Helvetica, Arial, sans-serif;
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
"""


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _score_pill(score) -> str:
    s = int(score or 0)
    cls = "score-green" if s >= 85 else ("score-yellow" if s >= 70 else "score-red")
    return f'<span class="score-pill {cls}">{s}</span>'


def _resolve_path_anchor(path_with_line: str, resolver) -> str:
    """形如 'kernel/trap.c:42' 的字符串解析为链接。"""
    if not path_with_line:
        return ""
    parts = path_with_line.rsplit(":", 1)
    if len(parts) == 2 and parts[1].isdigit():
        f, line = parts
    else:
        f, line = path_with_line, None
    url = resolver(f, line) if resolver else None
    if url:
        broken = url.startswith(_BROKEN_PREFIX)
        href = url[len(_BROKEN_PREFIX):] if broken else url
        cls = "file-jump file-broken" if broken else "file-jump"
        return f'<a class="{cls}" href="{_esc(href)}">{_esc(path_with_line)}</a>'
    return f'<span class="file-jump">{_esc(path_with_line)}</span>'


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

    # verdict 详细正文（agent 直出的 HTML，含强制图表）——原样嵌入并链接化 file:line
    content = verdict.get("content") or ""
    content_html = ""
    if content.strip():
        content_html = (
            f'<div class="prose prose-sm dark:prose-invert max-w-none mt-2 mb-4">'
            f'{linkify_html(content, resolver)}</div>'
        )

    return f"""
<section id="verdict" class="mb-8 p-6 rounded-lg border border-slate-300 dark:border-slate-700
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
    verdict_html = _render_verdict(tree_json.get("verdict", {}) or {}, resolver)

    # 直接生成静态 HTML 树（折叠状态由 Alpine 的 x-data open 控制），
    # 节点内部 HTML 片段在服务端预渲染好，file:line 链接已解析。
    tree_static_html = _render_tree_node_static(
        tree_json.get("tree", {}) or {}, depth=0, resolver=resolver,
    )

    title_safe = _esc(title)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title_safe}</title>
{_TREE_HEAD}
<style>{_TREE_CSS}</style>
</head>
<body class="bg-slate-50 dark:bg-slate-900 text-slate-900 dark:text-slate-100">
<main class="max-w-6xl mx-auto p-6 md:p-10">
  <header class="mb-6">
    <h1 class="text-2xl font-bold">{_esc(meta.get('repo','?'))}</h1>
    <div class="text-sm text-slate-500">
      评估时间：{_esc(meta.get('ts',''))} · 源文件：{_esc(meta.get('indexed_files',0))} 个
      · 规模：{_esc(meta.get('scale',''))}
    </div>
  </header>
  {verdict_html}
  <section>
    <h2 class="text-xl font-semibold mb-3">代码树（下层 = 中性描述）</h2>
    <div class="tree-root rounded-lg border border-slate-200 dark:border-slate-700
                bg-white dark:bg-slate-800 p-4">
      {tree_static_html}
    </div>
  </section>
</main>
{_INIT_SCRIPT}
{_TREE_RESIZE_ON_OPEN}
</body>
</html>
"""


def _render_tree_node_static(node: dict, depth: int, resolver) -> str:
    """静态渲染单个节点，Alpine 控制折叠状态。

    节点类型由 pipeline 产出：root / subsystem / module（历史上还有 dir / file）。
    渲染策略与 type 解耦——按节点实际携带的字段渲染，任何带 children 的节点都递归，
    避免 type 取值与渲染分支不一致导致整棵子树被丢弃。
    """
    typ = node.get("type", "")
    is_container = typ in ("dir", "root", "subsystem")
    path = node.get("path", "")
    name = _esc(node.get("name") or path or "/")
    role = _esc(node.get("role", ""))
    summary = _esc(node.get("summary", ""))
    open_default = "true" if depth <= 1 else "false"
    node_key = path or "__root__"

    # 子系统/模块不打分，评分只在顶层 verdict 卡片展示
    head = (
        f'<div class="tree-toggle flex items-baseline gap-2 py-1" @click="open = !open; '
        f'localStorage.setItem(\'tree:{_esc(node_key)}\', open ? \'1\' : \'0\'); '
        f'if (open) $dispatch(\'section:opened\')">'
        f'<span class="text-slate-400 w-4 text-center" x-text="open ? \'▾\' : \'▸\'"></span>'
        f'<span class="font-semibold {("text-cyan-700 dark:text-cyan-400" if is_container else "")}">'
        f'{name}{"/" if typ == "dir" else ""}</span>'
        f'{("<span class=\"text-xs text-slate-500\">("+role+")</span>") if role else ""}'
        f'</div>'
    )

    body_parts: list[str] = []
    if summary:
        body_parts.append(
            f'<div class="text-sm text-slate-700 dark:text-slate-300 mt-1 mb-2">'
            f'{summary}</div>'
        )

    # 详细叙述正文（subsystem / module 的 agent HTML 输出）——原样嵌入并链接化
    content = node.get("content") or ""
    if content.strip():
        body_parts.append(
            f'<div class="prose prose-sm dark:prose-invert max-w-none mt-1 mb-2">'
            f'{linkify_html(content, resolver)}</div>'
        )

    # 本节点的 highlights / issues（subsystem 可能携带）
    hi = node.get("highlights") or []
    ii = node.get("issues") or []
    if hi:
        body_parts.append(
            '<div class="text-xs mt-2"><span class="text-green-700 font-semibold">本节点亮点：</span> '
            + " · ".join(
                _resolve_path_anchor(h.get("path",""), resolver)
                + f' <span class="text-slate-600">— {_esc(h.get("quote",""))}</span>'
                for h in hi
            )
            + '</div>'
        )
    if ii:
        body_parts.append(
            '<div class="text-xs mt-1"><span class="text-red-700 font-semibold">本节点槽点：</span> '
            + " · ".join(
                _resolve_path_anchor(i.get("path",""), resolver)
                + f' <span class="severity-{_esc(i.get("severity",""))}">'
                + f'[{_esc(i.get("severity",""))}]</span>'
                + f' <span class="text-slate-600">— {_esc(i.get("quote",""))}</span>'
                for i in ii
            )
            + '</div>'
        )

    # 任何带 children 的节点都递归渲染子树
    children = node.get("children") or []
    if children:
        child_html = "".join(
            _render_tree_node_static(c, depth + 1, resolver)
            for c in children
        )
        body_parts.append(f'<div class="ml-4 mt-2">{child_html}</div>')

    body = "".join(body_parts)
    body_div = (
        f'<div class="tree-body" x-show="open" x-cloak>{body}</div>'
        if body else ""
    )
    return (
        f'<div class="tree-node mb-1" x-data="{{ open: {open_default} }}" '
        f'x-init="(function(){{ const s = localStorage.getItem(\'tree:{_esc(node_key)}\'); '
        f'if (s !== null) open = (s === \'1\'); }})()">'
        f'{head}{body_div}</div>'
    )


def write_tree_html(out_path: Path, tree_json: dict,
                    repo_roots: list[Path] | None = None,
                    title: str | None = None) -> tuple[Path, set[str]]:
    """把 tree.json 渲染为 HTML 并写入 out_path。返回 (path, 断链路径集合)。"""
    broken: set[str] = set()
    resolver = make_file_link_resolver(
        repo_roots, scheme="vscode", broken_paths=broken,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_tree_html(
        tree_json,
        title=title or f"{tree_json.get('meta',{}).get('repo','?')} 代码树报告",
        resolver=resolver,
    )
    out_path.write_text(html_text, encoding="utf-8")
    return out_path, broken
