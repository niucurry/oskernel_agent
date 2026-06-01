"""
HTML 报告的公共构件（供 html_tree.py 使用）。

agent 现在**直接产出 HTML**（不再写 Markdown），所以本模块不再做 markdown→HTML
转换。它只保留三类被最终渲染器复用的能力：

  - CDN 头与初始化脚本：Tailwind CSS + Mermaid + ECharts + Alpine.js，
    全部通过 CDN 单标签引入，无构建流程。
  - 文件跳转链接解析：把仓库内的 `path:line` 解析成可点击的 vscode:// 等 URL，
    解析不到时标记断链。
  - linkify_html：在 agent 直出的 HTML 上，仅对“可见文本”里的 `path:line`
    做链接化，不触碰标签 / 脚本 / 代码块。

图表容器约定（供 prompts 端指导 agent 直接写 HTML）：
  <pre class="mermaid">...</pre>                                   → Mermaid 图
  <div class="echarts-chart"><script type="application/json">{...}</script></div>
                                                                   → ECharts 图
"""

from __future__ import annotations

import html
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


# 文件引用识别

_FILEREF_EXTS = (
    "c|h|cc|cpp|cxx|hpp|hh|hxx|rs|S|s|asm|ld|lds|"
    "toml|md|py|sh|mk|cfg|conf|ini|"
    "go|java|js|ts|json|yaml|yml|txt|rst"
)
_FILEREF_RE = re.compile(
    rf"([A-Za-z0-9_./\-]+\.(?:{_FILEREF_EXTS}))"
    rf"(?:(?::|#L)(\d+)(?:-L?(\d+))?)?"
)


def _wrap_anchor(inner_html: str, url: str) -> str:
    if url.startswith(_BROKEN_PREFIX):
        real_url = url[len(_BROKEN_PREFIX):]
        return (f'<a href="{html.escape(real_url, quote=True)}"'
                f' class="file-jump file-broken" title="路径在仓库中未找到，点击为猜测位置">'
                f'{inner_html}</a>')
    return f'<a href="{html.escape(url, quote=True)}" class="file-jump">{inner_html}</a>'


# 原始 HTML 的文件引用链接化（agent 直出 HTML 时使用，不做 markdown 解析）

# 受保护块：脚本 / 样式 / 代码 / 已有链接，内部文本不参与链接化
_PROTECT_BLOCKS_RE = re.compile(
    r"<(script|style|pre|code|a)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def linkify_html(fragment: str, resolver: LinkResolver | None = None) -> str:
    """把一段**已经是 HTML** 的文本中裸露的 `path:line` 引用变成可点击跳转链接。

    本函数不解析 markdown，只在“可见文本”节点上做 file:line → <a> 替换，
    绝不触碰标签属性、<script>/<style>/<pre>/<code> 以及已有 <a> 内部的内容。
    用于 agent 直接产出 HTML 的渲染路径。
    """
    if not resolver or not fragment:
        return fragment

    blocks: list[str] = []

    def _stash_block(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"\x00B{len(blocks) - 1}\x00"

    text = _PROTECT_BLOCKS_RE.sub(_stash_block, fragment)

    tags: list[str] = []

    def _stash_tag(m: re.Match) -> str:
        tags.append(m.group(0))
        return f"\x00T{len(tags) - 1}\x00"

    # 把所有剩余标签抽走，留下纯可见文本 + 占位符
    text = _ANY_TAG_RE.sub(_stash_tag, text)

    def _bare_sub(m: re.Match) -> str:
        url = resolver(m.group(1), m.group(2))
        if not url:
            return m.group(0)
        return _wrap_anchor(m.group(0), url)

    text = _FILEREF_RE.sub(_bare_sub, text)

    text = re.sub(r"\x00T(\d+)\x00", lambda m: tags[int(m.group(1))], text)
    text = re.sub(r"\x00B(\d+)\x00", lambda m: blocks[int(m.group(1))], text)
    return text


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
