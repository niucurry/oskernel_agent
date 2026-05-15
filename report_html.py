"""
报告 HTML 渲染。

把 write_report 收到的 Markdown 文本转成一个独立的、带样式的 HTML 文件
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Callable, Optional

# 解析器签名：(file_path_str, line_str_or_None) -> URL（可点击跳转）或 None
LinkResolver = Callable[[str, Optional[str]], Optional[str]]


_CSS = """
:root { color-scheme: light dark; }
body {
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
                 Helvetica, Arial, sans-serif;
    max-width: 920px;
    margin: 2rem auto;
    padding: 0 1.2rem 4rem;
    line-height: 1.65;
    color: #1f2328;
    background: #ffffff;
}
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin-top: 1.8em; margin-bottom: 0.6em; }
h1 { font-size: 2em; border-bottom: 1px solid #d0d7de; padding-bottom: 0.3em; }
h2 { font-size: 1.5em; border-bottom: 1px solid #d0d7de; padding-bottom: 0.3em; }
h3 { font-size: 1.25em; }
p { margin: 0.8em 0; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
code {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    background: rgba(175, 184, 193, 0.2);
    padding: 0.15em 0.35em;
    border-radius: 4px;
    font-size: 0.92em;
}
pre {
    background: #f6f8fa;
    border: 1px solid #d0d7de;
    border-radius: 6px;
    padding: 12px 14px;
    overflow-x: auto;
}
pre code { background: transparent; padding: 0; border-radius: 0; font-size: 0.9em; }
blockquote {
    margin: 0.8em 0;
    padding: 0.2em 1em;
    color: #59636e;
    border-left: 4px solid #d0d7de;
    background: #f6f8fa;
}
ul, ol { padding-left: 1.6em; margin: 0.6em 0; }
li { margin: 0.25em 0; }
table { border-collapse: collapse; margin: 1em 0; display: block; overflow-x: auto; }
th, td { border: 1px solid #d0d7de; padding: 6px 12px; }
th { background: #f6f8fa; font-weight: 600; }
hr { border: none; border-top: 1px solid #d0d7de; margin: 2em 0; }
@media (prefers-color-scheme: dark) {
    body { background: #0d1117; color: #e6edf3; }
    h1, h2 { border-bottom-color: #30363d; }
    a { color: #4493f8; }
    code { background: rgba(110, 118, 129, 0.4); }
    pre { background: #161b22; border-color: #30363d; }
    blockquote { background: #161b22; border-left-color: #30363d; color: #9198a1; }
    th, td { border-color: #30363d; }
    th { background: #161b22; }
    hr { border-top-color: #30363d; }
}
"""


# 行内元素

_INLINE_CODE = re.compile(r"`([^`\n]+?)`")
_BOLD        = re.compile(r"\*\*([^*\n]+?)\*\*")
_ITALIC      = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_LINK        = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# 报告中常见的"文件引用"形式：可带相对路径、可附 :LINE 或 :LINE-LINE 或 #LLINE
_FILEREF_EXTS = (
    "c|h|cc|cpp|cxx|hpp|hh|hxx|rs|S|s|asm|ld|lds|"
    "toml|md|py|sh|mk|cfg|conf|ini|"
    "go|java|js|ts|json|yaml|yml|txt|rst"
)
_FILEREF_RE = re.compile(
    rf"([A-Za-z0-9_./\-]+\.(?:{_FILEREF_EXTS}))"      # 路径
    rf"(?:(?::|#L)(\d+)(?:-L?(\d+))?)?"                # 起始行  结束行
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
    """若 URL 看起来像本地文件相对路径（可能带 #Lnn 或 :nn），尝试用 resolver 重写。"""
    if not resolver: return None
    if _EXTERNAL_URL.match(url): return None
    m = _FILEREF_FULL.match(url)
    if not m: return None
    return resolver(m.group(1), m.group(2))


def _render_inline(text: str, resolver: LinkResolver | None = None) -> str:
    # 先抠出行内代码段（按占位符替换），避免其中的 * 被误识别
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

    # 在普通文本（非代码、非已成型 <a>）中，把裸露的 path:line 包成跳转链接
    if resolver:
        # 已渲染的 <a>...</a> 用占位符暂存，避免重复包裹
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


def markdown_to_html_body(md: str, resolver: LinkResolver | None = None) -> str:
    lines = md.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # 代码围栏
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            lang  = m.group(2)
            i += 1
            buf: list[str] = []
            while i < n and not (lines[i].startswith(fence) and lines[i].strip() == lines[i].rstrip()):
                buf.append(lines[i])
                i += 1
            if i < n: i += 1  # 跳过收尾围栏
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(buf))}</code></pre>")
            continue

        # 空行
        if not line.strip():
            i += 1
            continue

        # 水平线
        if _HR_RE.match(line):
            out.append("<hr/>")
            i += 1
            continue

        # 标题
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_render_inline(m.group(2), resolver)}</h{level}>")
            i += 1
            continue

        # 表格
        if _looks_like_table(lines, i):
            header = _split_row(lines[i])
            i += 2  # 跳过表头与分隔
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

        # 引用块
        if _BQ_RE.match(line):
            buf = []
            while i < n and _BQ_RE.match(lines[i]):
                buf.append(_BQ_RE.match(lines[i]).group(1))
                i += 1
            inner = markdown_to_html_body("\n".join(buf), resolver)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # 列表（有序 / 无序，支持嵌套按缩进对齐）
        if _OL_RE.match(line) or _UL_RE.match(line):
            i = _consume_list(lines, i, out, base_indent=-1, resolver=resolver)
            continue

        # 段落：把后续未中断的行合并
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
    """从 lines[i] 开始消费同级或更深缩进的列表项，写入 out。返回下一行索引。"""
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
            # 列表内允许空行，但若空行后不再是同级列表项就结束
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
            # 嵌套：交给递归（把上一项的 </li> 暂缓）
            # 简化：把嵌套块作为独立子列表追加在最近一项内
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


# 文件跳转链接

_JUMP_SCHEMES = {
    "vscode":  "vscode://file/{path}{anchor}",
    "cursor":  "cursor://file/{path}{anchor}",
    "vscode-insiders": "vscode-insiders://file/{path}{anchor}",
    "file":    "file://{path}",
}

# 断链标记：resolver 在 URL 前加此前缀表示文件不存在，_wrap_anchor 据此选择样式
_BROKEN_PREFIX = "\x00broken\x00"


def make_file_link_resolver(
    repo_roots: list[Path] | None,
    scheme: str = "vscode",
    broken_paths: set[str] | None = None,
) -> LinkResolver | None:
    """构造一个把 file:line 解析为可点击跳转 URL 的解析器。

    repo_roots：用来把相对路径还原为绝对路径的根目录列表（按顺序尝试）。
    scheme：跳转协议。默认 vscode（VS Code / Cursor 都注册了此 handler）。
    broken_paths：若提供，解析失败的路径会被收集进此 set（供调用方统计）。

    文件存在 → 返回正常 URL；文件不存在 → 返回带 _BROKEN_PREFIX 前缀的"猜测 URL"，
    使引用仍可点击，但会以红色虚线样式显示，提示路径有误。
    """
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

        # 文件在磁盘上找不到：生成"猜测路径"链接，加断链前缀以触发红色样式
        if broken_paths is not None:
            broken_paths.add(filepath)
        best_guess = str((roots[0] / filepath).resolve())
        return _BROKEN_PREFIX + _build_url(best_guess, line)

    return _resolve


# 对外入口

_EXTRA_CSS = """
a.file-jump { border-bottom: 1px dashed currentColor; }
a.file-jump:hover { background: rgba(9, 105, 218, 0.08); }
a.file-broken { color: #cf222e; border-bottom: 1px dashed #cf222e; }
a.file-broken:hover { background: rgba(207, 34, 46, 0.08); }
@media (prefers-color-scheme: dark) {
    a.file-jump:hover { background: rgba(68, 147, 248, 0.12); }
    a.file-broken { color: #ff7b72; border-bottom-color: #ff7b72; }
    a.file-broken:hover { background: rgba(255, 123, 114, 0.12); }
}
"""


def render_html(
    markdown_text: str,
    title: str = "评估报告",
    repo_roots: list[Path] | None = None,
    link_scheme: str = "vscode",
    broken_paths: set[str] | None = None,
) -> str:
    resolver = make_file_link_resolver(repo_roots, scheme=link_scheme,
                                       broken_paths=broken_paths)
    body = markdown_to_html_body(markdown_text, resolver=resolver)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8"/>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_CSS}{_EXTRA_CSS}</style>\n"
        "</head>\n<body>\n"
        f"{body}\n"
        "</body>\n</html>\n"
    )


def write_html_sibling(
    md_path: Path,
    markdown_text: str,
    repo_roots: list[Path] | None = None,
    link_scheme: str = "vscode",
) -> tuple[Path, set[str]]:
    """在 md_path 同目录写一份同名 .html，返回 (html路径, 无法解析的路径集合)。

    repo_roots 用来把报告中的相对路径解析为绝对路径，从而生成
    可点击跳转的 vscode:// 链接。无法解析的路径在 HTML 中以红色断链样式显示，
    同时通过第二个返回值告知调用方，以便在 write_report 响应里反馈给 LLM。
    """
    broken: set[str] = set()
    html_path = md_path.with_suffix(".html")
    title = md_path.stem
    html_path.write_text(
        render_html(
            markdown_text, title=title,
            repo_roots=repo_roots, link_scheme=link_scheme,
            broken_paths=broken,
        ),
        encoding="utf-8",
    )
    return html_path, broken
