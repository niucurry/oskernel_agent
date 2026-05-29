"""
HTML 渲染：tree.json → 多层 Alpine.js 折叠树报告。

顶部：固定 verdict 卡片（多维度评分 + 亮点 / 槽点 + 一句话总评）
下方：递归折叠树，每个节点可点击展开 / 折叠，状态存 localStorage
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .html import (
    _BROKEN_PREFIX,
    make_file_link_resolver,
    markdown_to_html_body,
)


_TREE_HEAD = """
<script src="https://cdn.tailwindcss.com?plugins=typography"></script>
<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
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


_TREE_INIT = """
<script>
function scoreClass(s) {
  s = Number(s || 0);
  if (s >= 85) return 'score-green';
  if (s >= 70) return 'score-yellow';
  return 'score-red';
}

function treeRoot() {
  return {
    data: null,
    init() {
      const el = document.getElementById('tree-data');
      this.data = JSON.parse(el.textContent);
    },
  };
}

function treeNode(node, depth) {
  return {
    node: node,
    depth: depth,
    open: (function(){
      const key = 'tree:' + (node.path || '__root__');
      const saved = localStorage.getItem(key);
      if (saved !== null) return saved === '1';
      return depth <= 1;
    })(),
    toggle() {
      this.open = !this.open;
      localStorage.setItem('tree:' + (this.node.path || '__root__'),
                           this.open ? '1' : '0');
    },
    scoreClass(s) { return scoreClass(s); },
  };
}
</script>
"""


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


def _score_pill(score) -> str:
    s = int(score or 0)
    cls = "score-green" if s >= 85 else ("score-yellow" if s >= 70 else "score-red")
    return f'<span class="score-pill {cls}">{s}</span>'


def _resolve_evidence(evidence: list, resolver) -> str:
    """把 evidence 列表渲染成一组可点击链接。"""
    if not evidence:
        return ""
    items: list[str] = []
    for e in evidence:
        f = e.get("file", "")
        line = e.get("line")
        if not f:
            continue
        url = resolver(f, str(line) if line is not None else None) if resolver else None
        label = f"{_esc(f)}:{_esc(line)}" if line is not None else _esc(f)
        if url:
            broken = url.startswith(_BROKEN_PREFIX)
            href = url[len(_BROKEN_PREFIX):] if broken else url
            cls = "file-jump file-broken" if broken else "file-jump"
            items.append(f'<a class="{cls}" href="{_esc(href)}">{label}</a>')
        else:
            items.append(f'<span class="file-jump">{label}</span>')
    return " · ".join(items)


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


def _render_key_apis(key_apis: list, resolver) -> str:
    if not key_apis:
        return ""
    rows = ""
    for api in key_apis:
        name = _esc(api.get("name", ""))
        line = api.get("line")
        kind = _esc(api.get("kind", ""))
        note = _esc(api.get("note", ""))
        rows += (
            f'<tr class="border-b border-slate-200 dark:border-slate-700">'
            f'<td class="py-1 px-2 font-mono text-sm">{name}'
            f'<span class="text-slate-500">:{_esc(line)}</span></td>'
            f'<td class="py-1 px-2 text-xs text-slate-500">{kind}</td>'
            f'<td class="py-1 px-2 text-sm text-slate-700 dark:text-slate-300">{note}</td>'
            f'</tr>'
        )
    return (
        f'<table class="w-full text-sm mt-2">'
        f'<thead><tr class="text-xs text-slate-500 border-b">'
        f'<th class="py-1 px-2 text-left">符号</th>'
        f'<th class="py-1 px-2 text-left">类型</th>'
        f'<th class="py-1 px-2 text-left">说明</th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )


# 渲染入口

def render_tree_html(tree_json: dict, title: str = "代码树报告",
                     resolver=None) -> str:
    """渲染完整 HTML 文档。"""
    meta = tree_json.get("meta", {})
    verdict_html = _render_verdict(tree_json.get("verdict", {}) or {}, resolver)

    # tree 数据作为 JSON 内嵌；Alpine 递归 component 通过 x-data 拿
    tree_data_json = json.dumps(tree_json, ensure_ascii=False)

    # 把 tree 节点改造成"路径锚点 HTML 预渲染"，使 evidence / highlights /
    # issues / key_apis 的链接由服务端解析好，避免在前端再算 file 路径。
    # 我们用 Alpine 递归渲染骨架（折叠状态），节点内部的 HTML 片段服务端预算好。
    # 实现简化：直接生成静态 HTML（折叠用 Alpine 控制 open）。
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
{_TREE_INIT}
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
<script id="tree-data" type="application/json">{_esc(tree_data_json)}</script>
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
    score = int(node.get("score") or 0)
    role = _esc(node.get("role", ""))
    summary = _esc(node.get("summary", ""))
    open_default = "true" if depth <= 1 else "false"
    node_key = path or "__root__"

    head = (
        f'<div class="tree-toggle flex items-baseline gap-2 py-1" @click="open = !open; '
        f'localStorage.setItem(\'tree:{_esc(node_key)}\', open ? \'1\' : \'0\')">'
        f'<span class="text-slate-400 w-4 text-center" x-text="open ? \'▾\' : \'▸\'"></span>'
        f'{_score_pill(score)}'
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

    # 详细叙述正文（subsystem / module 的 LLM markdown 输出）
    content = node.get("content") or ""
    if content.strip():
        body_parts.append(
            f'<div class="prose prose-sm dark:prose-invert max-w-none mt-1 mb-2">'
            f'{markdown_to_html_body(content, resolver)}</div>'
        )

    # 文件特有：key_apis + evidence
    key_apis = node.get("key_apis") or []
    if key_apis:
        body_parts.append(_render_key_apis(key_apis, resolver))
    evidence = node.get("evidence") or []
    if evidence:
        body_parts.append(
            f'<div class="text-xs text-slate-500 mt-2">证据：'
            f'{_resolve_evidence(evidence, resolver)}</div>'
        )

    # 本节点的 highlights / issues（subsystem / dir 均可能携带）
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
