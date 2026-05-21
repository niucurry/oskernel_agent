"""
报告 HTML 渲染。

把 write_report 收到的 Markdown 文本转成一个独立的、带样式的 HTML 文件。

新增能力（全部通过 CDN 单标签引入，无构建流程）：
  - Tailwind CSS（含 typography 插件）作为整体样式系统
  - Mermaid 用于语义图示（状态机/时序图/类图/时间线）
  - ECharts 用于数据可视化（仪表盘/环形/雷达/旭日/桑基/堆叠柱状等）
  - Alpine.js 提供交互（折叠、TOC 联动、搜索过滤）

约定（供 prompts 端指导 agent）：
  ```mermaid     ... ```   → 渲染为 Mermaid 图
  ```echarts     {...} ``` → 渲染为 ECharts（内容为 option 的 JSON）
  ```summary     ...   ``` → 渲染为章节摘要卡片（折叠态展示）
  ```html        ...   ``` → 原样输出 HTML（用于 Tailwind 卡片等）
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Callable, Optional

# 解析器签名：(file_path_str, line_str_or_None) -> URL（可点击跳转）或 None
LinkResolver = Callable[[str, Optional[str]], Optional[str]]


# CDN 资源

_CDN_HEAD = """
<script src="https://cdn.tailwindcss.com?plugins=typography"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  const dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  mermaid.initialize({
    startOnLoad: false,
    theme: dark ? 'dark' : 'default',
    securityLevel: 'loose',
    flowchart: { useMaxWidth: true },
    sequence:  { useMaxWidth: true },
  });
  window.__renderMermaid = () => {
    const nodes = document.querySelectorAll('.mermaid:not([data-processed])');
    if (nodes.length) mermaid.run({ nodes });
  };
  document.addEventListener('DOMContentLoaded', () => window.__renderMermaid());
  document.addEventListener('section:opened', () => setTimeout(window.__renderMermaid, 30));
</script>
"""

_INIT_SCRIPT = """
<script>
(function() {
  function initECharts() {
    if (typeof echarts === 'undefined') { setTimeout(initECharts, 100); return; }
    document.querySelectorAll('.echarts-chart:not([data-rendered])').forEach(function(el) {
      var dataEl = el.querySelector('script[type="application/json"]');
      if (!dataEl) return;
      try {
        var option = JSON.parse(dataEl.textContent);
        if (!el.style.height) el.style.height = '360px';
        var chart = echarts.init(el, null, { renderer: 'canvas' });
        chart.setOption(option);
        el.setAttribute('data-rendered', '1');
        el.__chart = chart;
        window.addEventListener('resize', function () { chart.resize(); });
      } catch (e) {
        el.innerHTML = '<div class="text-red-600 text-sm p-2 border border-red-300 rounded">ECharts 配置 JSON 解析失败: ' + e.message + '</div>';
      }
    });
  }

  function initScrollSpy() {
    var links = document.querySelectorAll('.toc-link');
    var sections = document.querySelectorAll('section[data-section-id]');
    if (!('IntersectionObserver' in window) || !links.length || !sections.length) return;
    var byId = {};
    links.forEach(function(a) {
      var id = a.getAttribute('href');
      if (id) byId[id.replace(/^#/, '')] = a;
    });
    var observer = new IntersectionObserver(function(entries) {
      entries.forEach(function(e) {
        if (e.isIntersecting) {
          var id = e.target.getAttribute('data-section-id');
          Object.keys(byId).forEach(function(k) { byId[k].classList.remove('toc-active'); });
          if (byId[id]) byId[id].classList.add('toc-active');
        }
      });
    }, { rootMargin: '-30% 0px -60% 0px', threshold: 0 });
    sections.forEach(function(s) { observer.observe(s); });
  }

  document.addEventListener('DOMContentLoaded', function() {
    initECharts();
    initScrollSpy();
  });

  document.addEventListener('section:opened', function() {
    setTimeout(initECharts, 30);
  });
})();
</script>
"""


# 自定义 CSS

_CUSTOM_CSS = """
:root { color-scheme: light dark; }
html, body {
  font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
               Helvetica, Arial, sans-serif;
}
.toc-link.toc-active {
  background: rgb(59 130 246 / 0.12);
  color: rgb(37 99 235);
  border-left-color: rgb(37 99 235);
}
@media (prefers-color-scheme: dark) {
  .toc-link.toc-active {
    background: rgb(96 165 250 / 0.18);
    color: rgb(147 197 253);
    border-left-color: rgb(147 197 253);
  }
}
a.file-jump { border-bottom: 1px dashed currentColor; text-decoration: none; }
a.file-jump:hover { background: rgba(9, 105, 218, 0.08); }
a.file-broken { color: #cf222e; border-bottom: 1px dashed #cf222e; }
a.file-broken:hover { background: rgba(207, 34, 46, 0.08); }
@media (prefers-color-scheme: dark) {
  a.file-jump:hover { background: rgba(68, 147, 248, 0.12); }
  a.file-broken { color: #ff7b72; border-bottom-color: #ff7b72; }
  a.file-broken:hover { background: rgba(255, 123, 114, 0.12); }
}
.section-header { cursor: pointer; user-select: none; }
.section-header:hover .toggle-icon { opacity: 1; }
.toggle-icon { opacity: 0.55; transition: opacity 0.15s, transform 0.15s; display: inline-block; width: 1em; }
.section-header[data-open="false"] .toggle-icon { transform: rotate(-90deg); }
.mermaid { background: transparent; text-align: center; margin: 1rem 0; }
.echarts-chart { width: 100%; min-height: 320px; margin: 1rem 0; }
.section-summary-card { margin: 0.5rem 0 1rem; }
.section-summary-card p:first-child { margin-top: 0; }
.section-summary-card p:last-child { margin-bottom: 0; }
@media print {
  .toc-sidebar, .toc-controls { display: none !important; }
  main { margin-left: 0 !important; }
  section [x-show] { display: block !important; }
}
"""


# 行内元素

_INLINE_CODE = re.compile(r"`([^`\n]+?)`")
_BOLD        = re.compile(r"\*\*([^*\n]+?)\*\*")
_ITALIC      = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_LINK        = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

_FILEREF_EXTS = (
    "c|h|cc|cpp|cxx|hpp|hh|hxx|rs|S|s|asm|ld|lds|"
    "toml|md|py|sh|mk|cfg|conf|ini|"
    "go|java|js|ts|json|yaml|yml|txt|rst"
)
_FILEREF_RE = re.compile(
    rf"([A-Za-z0-9_./\-]+\.(?:{_FILEREF_EXTS}))"
    rf"(?:(?::|#L)(\d+)(?:-L?(\d+))?)?"
)
_FILEREF_FULL = re.compile(rf"^{_FILEREF_RE.pattern}$")
_EXTERNAL_URL = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//|#|mailto:)", re.IGNORECASE)


def _wrap_anchor(inner_html: str, url: str) -> str:
    if url.startswith(_BROKEN_PREFIX):
        real_url = url[len(_BROKEN_PREFIX):]
        return (f'<a href="{html.escape(real_url, quote=True)}"'
                f' class="file-jump file-broken" title="路径在仓库中未找到，点击为猜测位置">'
                f'{inner_html}</a>')
    return f'<a href="{html.escape(url, quote=True)}" class="file-jump">{inner_html}</a>'


def _rewrite_link_url(url: str, resolver: LinkResolver | None) -> str | None:
    if not resolver: return None
    if _EXTERNAL_URL.match(url): return None
    m = _FILEREF_FULL.match(url)
    if not m: return None
    return resolver(m.group(1), m.group(2))


def _render_inline(text: str, resolver: LinkResolver | None = None) -> str:
    code_slots: list[str] = []

    def _stash_code(m: re.Match) -> str:
        raw = m.group(1)
        code_html = f"<code>{html.escape(raw)}</code>"
        if resolver:
            fm = _FILEREF_FULL.match(raw)
            if fm:
                url = resolver(fm.group(1), fm.group(2))
                if url:
                    code_html = _wrap_anchor(code_html, url)
        code_slots.append(code_html)
        return f"\x00C{len(code_slots) - 1}\x00"

    text = _INLINE_CODE.sub(_stash_code, text)
    text = html.escape(text, quote=False)

    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)

    def _link_sub(m: re.Match) -> str:
        label = m.group(1)
        url   = m.group(2)
        rewritten = _rewrite_link_url(url, resolver)
        final_url = rewritten or url
        cls = ' class="file-jump"' if rewritten else ""
        return f'<a href="{html.escape(final_url, quote=True)}"{cls}>{label}</a>'

    text = _LINK.sub(_link_sub, text)

    if resolver:
        anchor_slots: list[str] = []

        def _stash_anchor(m: re.Match) -> str:
            anchor_slots.append(m.group(0))
            return f"\x00A{len(anchor_slots) - 1}\x00"

        text = re.sub(r"<a\b[^>]*>.*?</a>", _stash_anchor, text, flags=re.DOTALL)

        def _bare_sub(m: re.Match) -> str:
            url = resolver(m.group(1), m.group(2))
            if not url:
                return m.group(0)
            return _wrap_anchor(m.group(0), url)

        text = _FILEREF_RE.sub(_bare_sub, text)
        text = re.sub(r"\x00A(\d+)\x00", lambda m: anchor_slots[int(m.group(1))], text)

    def _restore_code(m: re.Match) -> str:
        return code_slots[int(m.group(1))]

    text = re.sub(r"\x00C(\d+)\x00", _restore_code, text)
    return text


# 块级解析

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE   = re.compile(r"^(```+|~~~+)\s*([\w+\-.]*)\s*$")
_HR_RE      = re.compile(r"^\s{0,3}(-{3,}|_{3,}|\*{3,})\s*$")
_OL_RE      = re.compile(r"^(\s*)(\d+)\.\s+(.*)$")
_UL_RE      = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_BQ_RE      = re.compile(r"^>\s?(.*)$")
_TABLE_SEP  = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"): s = s[1:]
    if s.endswith("|"):   s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _looks_like_table(lines: list[str], i: int) -> bool:
    if i + 1 >= len(lines): return False
    if "|" not in lines[i]: return False
    return bool(_TABLE_SEP.match(lines[i + 1]))


def _render_fence(lang: str, content: str, resolver: LinkResolver | None) -> str:
    """根据语言标签渲染围栏代码块。"""
    lang_l = (lang or "").lower()

    if lang_l == "mermaid":
        # mermaid 自己解析 textContent，HTML 转义不会破坏其语法
        return f'<pre class="mermaid">{html.escape(content)}</pre>'

    if lang_l == "echarts":
        # 校验 JSON；失败时仍然嵌入原文以便页面侧报错可读
        try:
            json.loads(content)
            payload = content
        except Exception:
            payload = content  # 让前端给出可读错误
        safe = payload.replace("</", "<\\/")
        return (
            '<div class="echarts-chart">'
            f'<script type="application/json">{safe}</script>'
            '</div>'
        )

    if lang_l == "summary":
        inner = markdown_to_html_body(content, resolver)
        return f'<aside class="section-summary-card not-prose">{inner}</aside>'

    if lang_l == "html":
        # 信任 agent：原样输出，用于 Tailwind 卡片等
        return f'<div class="not-prose">{content}</div>'

    cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
    return f"<pre><code{cls}>{html.escape(content)}</code></pre>"


def markdown_to_html_body(md: str, resolver: LinkResolver | None = None) -> str:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            lang  = m.group(2)
            i += 1
            buf: list[str] = []
            while i < n and not (lines[i].startswith(fence) and lines[i].strip() == lines[i].rstrip()):
                buf.append(lines[i])
                i += 1
            if i < n: i += 1
            content = "\n".join(buf)
            out.append(_render_fence(lang, content, resolver))
            continue

        if not line.strip():
            i += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr/>")
            i += 1
            continue

        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_render_inline(m.group(2), resolver)}</h{level}>")
            i += 1
            continue

        if _looks_like_table(lines, i):
            header = _split_row(lines[i])
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_split_row(lines[i]))
                i += 1
            out.append("<table>")
            out.append("<thead><tr>" + "".join(
                f"<th>{_render_inline(c, resolver)}</th>" for c in header) + "</tr></thead>")
            out.append("<tbody>")
            for r in rows:
                cells = r + [""] * (len(header) - len(r))
                out.append("<tr>" + "".join(
                    f"<td>{_render_inline(c, resolver)}</td>" for c in cells[:len(header)]) + "</tr>")
            out.append("</tbody></table>")
            continue

        if _BQ_RE.match(line):
            buf = []
            while i < n and _BQ_RE.match(lines[i]):
                buf.append(_BQ_RE.match(lines[i]).group(1))
                i += 1
            inner = markdown_to_html_body("\n".join(buf), resolver)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        if _OL_RE.match(line) or _UL_RE.match(line):
            i = _consume_list(lines, i, out, base_indent=-1, resolver=resolver)
            continue

        buf = [line]
        i += 1
        while i < n and lines[i].strip() and not (
            _HEADING_RE.match(lines[i])
            or _FENCE_RE.match(lines[i])
            or _HR_RE.match(lines[i])
            or _OL_RE.match(lines[i])
            or _UL_RE.match(lines[i])
            or _BQ_RE.match(lines[i])
            or _looks_like_table(lines, i)
        ):
            buf.append(lines[i])
            i += 1
        out.append("<p>" + _render_inline(" ".join(s.strip() for s in buf), resolver) + "</p>")

    return "\n".join(out)


def _consume_list(
    lines: list[str],
    i: int,
    out: list[str],
    base_indent: int,
    resolver: LinkResolver | None = None,
) -> int:
    first = lines[i]
    m_ol = _OL_RE.match(first)
    m_ul = _UL_RE.match(first)
    indent = len((m_ol or m_ul).group(1))
    if indent <= base_indent:
        return i
    ordered = m_ol is not None
    tag = "ol" if ordered else "ul"
    out.append(f"<{tag}>")

    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j >= n: break
            nxt = lines[j]
            m2 = _OL_RE.match(nxt) or _UL_RE.match(nxt)
            if not m2 or len(m2.group(1)) != indent:
                break
            i = j
            continue

        m_ol = _OL_RE.match(line)
        m_ul = _UL_RE.match(line)
        if not (m_ol or m_ul):
            break
        cur_indent = len((m_ol or m_ul).group(1))
        if cur_indent < indent:
            break
        if cur_indent > indent:
            sub_buf: list[str] = []
            i = _consume_list(lines, i, sub_buf, base_indent=indent, resolver=resolver)
            if out and out[-1].endswith("</li>"):
                out[-1] = out[-1][:-len("</li>")] + "\n".join(sub_buf) + "</li>"
            else:
                out.extend(sub_buf)
            continue

        content = (m_ol or m_ul).group(3 if ordered else 2)
        out.append(f"<li>{_render_inline(content, resolver)}</li>")
        i += 1

    out.append(f"</{tag}>")
    return i


# 章节分段与 TOC

_H2_BLOCK = re.compile(r"<h2>(.*?)</h2>", re.DOTALL)
_H3_BLOCK = re.compile(r"<h3>(.*?)</h3>", re.DOTALL)
_SUMMARY_BLOCK = re.compile(
    r'<aside class="section-summary-card not-prose">(.*?)</aside>',
    re.DOTALL,
)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _slugify(title: str, idx: int) -> str:
    """优先用 '1.2' 这种章节号生成稳定 id；否则退化为 sec-N。"""
    m = re.match(r"^\s*(\d+(?:\.\d+)*)", title)
    if m:
        return "sec-" + m.group(1).replace(".", "-")
    return f"sec-{idx}"


def _split_h2_sections(body: str) -> list[tuple[str | None, str]]:
    """把 body 按 <h2> 切分为 [(h2_html or None, content), ...]。"""
    positions: list[tuple[int, int, str]] = []
    for m in _H2_BLOCK.finditer(body):
        positions.append((m.start(), m.end(), m.group(0)))
    if not positions:
        return [(None, body)]
    chunks: list[tuple[str | None, str]] = []
    prefix = body[: positions[0][0]]
    if prefix.strip():
        chunks.append((None, prefix))
    for i, (start, end, h2_html) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(body)
        content = body[end:next_start]
        chunks.append((h2_html, content))
    return chunks


def _wrap_h3_subsections(content: str) -> str:
    """把 content 中每个 <h3> 段落包成可折叠子区块。"""
    positions: list[tuple[int, int, str]] = []
    for m in _H3_BLOCK.finditer(content):
        positions.append((m.start(), m.end(), m.group(0)))
    if not positions:
        return content

    out: list[str] = []
    prefix = content[: positions[0][0]]
    if prefix.strip():
        out.append(prefix)

    for i, (start, end, h3_html) in enumerate(positions):
        next_start = positions[i + 1][0] if i + 1 < len(positions) else len(content)
        sub_body = content[end:next_start]
        title_text = _strip_tags(h3_html)
        slug = _slugify(title_text, i)
        key = f"sub-{slug}"
        h3_with_id = h3_html.replace("<h3>", f'<h3 id="{slug}" class="!mt-0">', 1)
        out.append(
            f'<div class="subsection border-l-2 border-slate-200 dark:border-slate-700 pl-3 my-3" '
            f'x-data="{{open: (localStorage.getItem({key!r})!==\'0\')}}" '
            f'x-init="$watch(\'open\', v => localStorage.setItem({key!r}, v ? \'1\' : \'0\'))" '
            f'@expand-all.window="open=true" '
            f'@collapse-all.window="open=false">'
            f'<div class="section-header flex items-baseline gap-1" '
            f':data-open="open" @click="open=!open; if(open) $dispatch(\'section:opened\')">'
            f'<span class="toggle-icon">▾</span>'
            f'<div class="flex-1">{h3_with_id}</div>'
            f'</div>'
            f'<div x-show="open" x-transition.duration.150ms class="subsection-body">{sub_body}</div>'
            f'</div>'
        )
    return "".join(out)


def _build_sections_and_toc(body: str) -> tuple[str, list[dict]]:
    """返回 (重新组装后的 body_html, toc 条目列表)。"""
    chunks = _split_h2_sections(body)
    out: list[str] = []
    toc: list[dict] = []

    for idx, (h2_html, content) in enumerate(chunks):
        if h2_html is None:
            out.append(content)
            continue
        title = _strip_tags(h2_html)
        slug = _slugify(title, idx)
        toc.append({"id": slug, "title": title})

        # 抽出可选的摘要卡片
        summary_html = ""
        m = _SUMMARY_BLOCK.search(content)
        if m:
            summary_html = m.group(0)
            content = content[: m.start()] + content[m.end():]

        content = _wrap_h3_subsections(content)
        h2_with_id = h2_html.replace("<h2>", f'<h2 id="{slug}" class="!mt-0">', 1)
        store_key = f"sec-{slug}"

        summary_block = (
            f'<div x-show="!open" class="opacity-90">{summary_html}</div>'
            if summary_html else ""
        )

        out.append(
            f'<section data-section-id="{slug}" class="report-section my-6 rounded-lg '
            f'border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800/40 '
            f'shadow-sm p-4 md:p-5" '
            f'x-data="{{open: (localStorage.getItem({store_key!r})!==\'0\')}}" '
            f'x-init="$watch(\'open\', v => localStorage.setItem({store_key!r}, v ? \'1\' : \'0\'))" '
            f'@expand-all.window="open=true" '
            f'@collapse-all.window="open=false">'
            f'<div class="section-header flex items-baseline gap-2" '
            f':data-open="open" @click="open=!open; if(open) $dispatch(\'section:opened\')">'
            f'<span class="toggle-icon text-slate-500">▾</span>'
            f'<div class="flex-1">{h2_with_id}</div>'
            f'</div>'
            f'{summary_block}'
            f'<div x-show="open" x-transition.duration.150ms class="section-body">{content}</div>'
            f'</section>'
        )

    return "\n".join(out), toc


def _render_toc(toc: list[dict]) -> str:
    if not toc:
        return ""
    items = []
    for entry in toc:
        title_safe = html.escape(entry["title"])
        items.append(
            f'<a href="#{entry["id"]}" class="toc-link block py-1.5 px-2 text-sm '
            f'border-l-2 border-transparent hover:bg-slate-100 dark:hover:bg-slate-700/60 '
            f'text-slate-700 dark:text-slate-300" '
            f'x-show="!search || {json.dumps(entry["title"].lower())}.includes(search.toLowerCase())">'
            f'{title_safe}</a>'
        )
    return "\n".join(items)


# 文件跳转链接

_JUMP_SCHEMES = {
    "vscode":  "vscode://file/{path}{anchor}",
    "cursor":  "cursor://file/{path}{anchor}",
    "vscode-insiders": "vscode-insiders://file/{path}{anchor}",
    "file":    "file://{path}",
}

_BROKEN_PREFIX = "\x00broken\x00"


def make_file_link_resolver(
    repo_roots: list[Path] | None,
    scheme: str = "vscode",
    broken_paths: set[str] | None = None,
) -> LinkResolver | None:
    """构造一个把 file:line 解析为可点击跳转 URL 的解析器。"""
    if not repo_roots:
        return None
    template = _JUMP_SCHEMES.get(scheme, _JUMP_SCHEMES["vscode"])
    roots = [Path(r).resolve() for r in repo_roots]

    def _build_url(abs_path: str, line: str | None) -> str:
        anchor = f":{line}:1" if (line and scheme != "file") else ""
        from urllib.parse import quote
        encoded = quote(abs_path.lstrip("/"), safe="/:")
        return template.format(path="/" + encoded, anchor=anchor)

    def _resolve(filepath: str, line: str | None) -> str | None:
        candidate: Path | None = None
        p = Path(filepath)
        if p.is_absolute():
            if p.exists():
                candidate = p
        else:
            for root in roots:
                cand = root / filepath
                if cand.exists():
                    candidate = cand
                    break
            if candidate is None:
                parts = p.parts
                for strip in range(1, len(parts)):
                    suffix = Path(*parts[strip:])
                    for root in roots:
                        cand = root / suffix
                        if cand.exists():
                            candidate = cand
                            break
                    if candidate is not None:
                        break

        if candidate is not None:
            try:
                abs_path = str(candidate.resolve())
            except OSError:
                abs_path = str(candidate)
            return _build_url(abs_path, line)

        if broken_paths is not None:
            broken_paths.add(filepath)
        best_guess = str((roots[0] / filepath).resolve())
        return _BROKEN_PREFIX + _build_url(best_guess, line)

    return _resolve


# 对外入口

def render_html(
    markdown_text: str,
    title: str = "评估报告",
    repo_roots: list[Path] | None = None,
    link_scheme: str = "vscode",
    broken_paths: set[str] | None = None,
) -> str:
    resolver = make_file_link_resolver(repo_roots, scheme=link_scheme,
                                       broken_paths=broken_paths)
    raw_body = markdown_to_html_body(markdown_text, resolver=resolver)
    body_html, toc = _build_sections_and_toc(raw_body)
    toc_items = _render_toc(toc)

    title_safe = html.escape(title)
    has_toc = bool(toc)

    toc_aside = ""
    main_class = "max-w-5xl mx-auto p-6 md:p-10"
    if has_toc:
        main_class = "ml-0 md:ml-64 max-w-5xl px-4 md:px-10 py-6 md:py-10"
        toc_aside = f"""
<aside class="toc-sidebar hidden md:flex md:flex-col fixed top-0 left-0 h-screen w-64
              border-r border-slate-200 dark:border-slate-700
              bg-slate-50 dark:bg-slate-900/70 p-4 overflow-y-auto z-10">
  <div class="text-xs uppercase tracking-wider text-slate-500 mb-2">目录</div>
  <div class="toc-controls flex gap-2 mb-2">
    <button class="text-xs px-2 py-1 rounded bg-blue-600 text-white hover:bg-blue-700"
            @click="$dispatch('expand-all')">全部展开</button>
    <button class="text-xs px-2 py-1 rounded bg-slate-300 dark:bg-slate-600 text-slate-800 dark:text-slate-100
                   hover:bg-slate-400 dark:hover:bg-slate-500"
            @click="$dispatch('collapse-all')">全部收起</button>
  </div>
  <input type="text" placeholder="过滤章节..." x-model="search"
         class="w-full mb-2 px-2 py-1 text-sm border rounded
                bg-white dark:bg-slate-800 border-slate-300 dark:border-slate-600
                focus:outline-none focus:ring-2 focus:ring-blue-500"/>
  <nav class="toc-nav flex-1 overflow-y-auto">
    {toc_items}
  </nav>
</aside>"""

    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f"<title>{title_safe}</title>\n"
        f"{_CDN_HEAD}\n"
        f"<style>{_CUSTOM_CSS}</style>\n"
        "</head>\n"
        '<body class="bg-slate-50 dark:bg-slate-900 text-slate-900 dark:text-slate-100" '
        'x-data="{ search: \'\' }">\n'
        f"{toc_aside}\n"
        f'<main class="{main_class}">\n'
        '<article class="prose dark:prose-invert prose-slate max-w-none '
        'prose-headings:scroll-mt-6 prose-pre:bg-slate-100 dark:prose-pre:bg-slate-800 '
        'prose-a:text-blue-600 dark:prose-a:text-blue-400">\n'
        f"{body_html}\n"
        "</article>\n"
        "</main>\n"
        f"{_INIT_SCRIPT}\n"
        "</body>\n</html>\n"
    )


def write_html(
    html_path: Path,
    markdown_text: str,
    repo_roots: list[Path] | None = None,
    link_scheme: str = "vscode",
) -> tuple[Path, set[str]]:
    """把 Markdown 渲染为 HTML 并写入 html_path，返回 (html路径, 无法解析的路径集合)。"""
    broken: set[str] = set()
    title = html_path.stem
    html_path.write_text(
        render_html(
            markdown_text, title=title,
            repo_roots=repo_roots, link_scheme=link_scheme,
            broken_paths=broken,
        ),
        encoding="utf-8",
    )
    return html_path, broken


def write_html_sibling(
    md_path: Path,
    markdown_text: str,
    repo_roots: list[Path] | None = None,
    link_scheme: str = "vscode",
) -> tuple[Path, set[str]]:
    """在 md_path 同目录写一份同名 .html，返回 (html路径, 无法解析的路径集合)。"""
    html_path = md_path.with_suffix(".html")
    return write_html(html_path, markdown_text, repo_roots=repo_roots, link_scheme=link_scheme)
