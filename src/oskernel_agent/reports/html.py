"""
HTML 报告的公共构件（供 html_tree.py 使用）。

agent 现在**直接产出 HTML**（不再写 Markdown），所以本模块不再做 markdown→HTML
转换。它只保留三类被最终渲染器复用的能力：

  - CDN 头与初始化脚本：Tailwind CSS + ECharts + Alpine.js，
    全部通过 CDN 单标签引入，无构建流程。
  - 文件跳转链接解析：把仓库内的 `path:line` 解析成可点击的 vscode:// 等 URL，
    解析不到时标记断链。
  - linkify_html：在 agent 直出的 HTML 上，仅对“可见文本”里的 `path:line`
    做链接化，不触碰标签 / 脚本 / 代码块。

数据图容器约定（仅 ECharts；架构图/流程图已弃用）：
  <div class="echarts-chart"><script type="application/json">{...}</script></div>
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

# 解析器签名：(file_path_str, line_str_or_None) -> URL（可点击跳转）或 None
LinkResolver = Callable[[str, Optional[str]], Optional[str]]


class TocIntegrityError(ValueError):
    """目录定位校验失败：存在点击后无法准确定位的目录项。"""


_TOC_HREF_RE = re.compile(r'class="toc-link[^"]*"[^>]*?href="#([^"]+)"')
_ID_ATTR_RE = re.compile(r'\sid="([^"]+)"')


def find_toc_locate_problems(html_text: str) -> list[str]:
    """检查每个目录项(.toc-link)是否都能**精确定位到唯一目标**。

    定位准确 ⇔ 目录项的 href="#X" 在文档里对应**恰好一个** id="X"：
      - 0 个 → 点击无处可跳；
      - ≥2 个 → 浏览器跳到第一个，可能不是目标，定位错乱。
    返回问题描述列表，空列表表示全部目录项定位准确。供渲染器产出前自检调用。
    """
    hrefs = _TOC_HREF_RE.findall(html_text)
    id_counts = Counter(_ID_ATTR_RE.findall(html_text))
    problems: list[str] = []
    for h in hrefs:
        n = id_counts.get(h, 0)
        if n == 0:
            problems.append(f'目录项 #{h} 找不到对应锚点 —— 点击无法定位')
        elif n > 1:
            problems.append(f'锚点 id="{h}" 重复 {n} 次 —— 会定位到错误位置')
    return problems


def assert_toc_resolves(html_text: str) -> None:
    """目录项若不能精确定位则抛 TocIntegrityError（渲染器产出前的硬校验）。"""
    problems = find_toc_locate_problems(html_text)
    if problems:
        raise TocIntegrityError("；".join(problems))


# CDN 资源

_CDN_HEAD = """
<script src="https://cdn.tailwindcss.com?plugins=typography"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
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
    var sections = document.querySelectorAll('[data-section-id]');
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
    }, { rootMargin: '-20% 0px -70% 0px', threshold: 0 });
    sections.forEach(function(s) { observer.observe(s); });
  }

  // 点击目录项：若目标在折叠的树节点内，先展开沿途节点再平滑滚动
  function initTocClick() {
    document.querySelectorAll('.toc-link').forEach(function(a) {
      a.addEventListener('click', function(ev) {
        var id = (a.getAttribute('href') || '').replace(/^#/, '');
        if (!id) return;
        var target = document.getElementById(id);
        if (!target) return;
        ev.preventDefault();
        var node = target;
        while (node) {
          if (node.classList && node.classList.contains('tree-node') &&
              window.Alpine && typeof Alpine.$data === 'function') {
            try {
              var data = Alpine.$data(node);
              if (data && data.open === false) data.open = true;
            } catch (e) {}
          }
          node = node.parentElement;
        }
        setTimeout(function() {
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 20);
        if (history.replaceState) history.replaceState(null, '', '#' + id);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function() {
    initECharts();
    initScrollSpy();
    initTocClick();
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
    rf"(?![A-Za-z0-9_])(?:(?::|#L)(\d+)(?:-L?(\d+))?)?"
)
# 整段（允许前后空白）恰好是一个 file:line 引用 —— 用于判定 <code>path:line</code>
_FILEREF_FULL = re.compile(rf"^\s*{_FILEREF_RE.pattern}\s*$")


def _wrap_anchor(inner_html: str, url: str) -> str:
    if url.startswith(_BROKEN_PREFIX):
        real_url = url[len(_BROKEN_PREFIX):]
        tgt = ' target="_blank" rel="noopener"' if real_url.startswith("http") else ""
        return (f'<a href="{html.escape(real_url, quote=True)}"{tgt}'
                f' class="file-jump file-broken" title="路径在仓库中未找到，点击为猜测位置">'
                f'{inner_html}</a>')
    tgt = ' target="_blank" rel="noopener"' if url.startswith("http") else ""
    return f'<a href="{html.escape(url, quote=True)}"{tgt} class="file-jump">{inner_html}</a>'


# 原始 HTML 的文件引用链接化（agent 直出 HTML 时使用，不做 markdown 解析）

# 受保护块：脚本 / 样式 / 预格式 / 已有链接，内部文本不参与链接化
# 注意：不含 <code> —— <code> 由 _CODE_RE 单独处理（整段是 file:line 的会被链接化）
_PROTECT_BLOCKS_RE = re.compile(
    r"<(script|style|pre|a)\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
_CODE_RE = re.compile(r"<code\b[^>]*>(.*?)</code>", re.DOTALL | re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def linkify_html(fragment: str, resolver: LinkResolver | None = None) -> str:
    """把一段**已经是 HTML** 的文本里的 `path:line` 引用变成可点击跳转链接。

    覆盖两类标注：
      1. 可见正文里裸写的 file:line（如“见 kernel/proc.c:120”）；
      2. 整段就是一个 file:line 的内联代码（如 <code>kernel/proc.c:120</code>）。
    不触碰标签属性、<script>/<style>/<pre>/已有 <a>，以及非文件引用的普通代码片段。
    用于 agent 直接产出 HTML 的渲染路径。
    """
    if not resolver or not fragment:
        return fragment

    blocks: list[str] = []

    def _stash(s: str) -> str:
        blocks.append(s)
        return f"\x00B{len(blocks) - 1}\x00"

    text = _PROTECT_BLOCKS_RE.sub(lambda m: _stash(m.group(0)), fragment)

    # <code>：整段恰好是 file:line 的 → 整块包成跳转链接；否则原样保护
    def _code_sub(m: re.Match) -> str:
        fm = _FILEREF_FULL.match(m.group(1))
        if fm:
            # 无目录且无行号的裸文件名通常只是正文术语；报告契约要求证据写完整
            # path:line，不应为这种文字猜测链接并制造断链。
            if "/" not in fm.group(1) and "\\" not in fm.group(1) and not fm.group(2):
                return _stash(m.group(0))
            url = resolver(fm.group(1), fm.group(2))
            if url:
                return _stash(_wrap_anchor(m.group(0), url))
        return _stash(m.group(0))

    text = _CODE_RE.sub(_code_sub, text)

    tags: list[str] = []

    def _stash_tag(m: re.Match) -> str:
        tags.append(m.group(0))
        return f"\x00T{len(tags) - 1}\x00"

    # 把所有剩余标签抽走，留下纯可见文本 + 占位符
    text = _ANY_TAG_RE.sub(_stash_tag, text)

    def _bare_sub(m: re.Match) -> str:
        if "/" not in m.group(1) and "\\" not in m.group(1) and not m.group(2):
            return m.group(0)
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

# 建索引时跳过的目录（与 tool_handlers._SKIP_DIRS 同口径，外加 VCS/依赖目录）
_INDEX_SKIP_DIRS = frozenset({
    ".git", "vendor", "third_party", "target",
    ".venv", "__pycache__", "node_modules",
})


def _build_repo_index(roots: list[Path]) -> dict[str, list[str]]:
    """遍历仓库根，建 basename → [posix 相对路径, ...] 索引，供后缀匹配兜底。

    相对路径前缀加根序号（root#i/...），多根时也能拼回正确的绝对路径。
    """
    import os
    index: dict[str, list[str]] = {}
    for ri, root in enumerate(roots):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _INDEX_SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                rel = (Path(dirpath) / name).relative_to(root).as_posix()
                index.setdefault(name, []).append(f"{ri}\x00{rel}")
    return index


def make_file_link_resolver(
    repo_roots: list[Path] | None,
    scheme: str = "vscode",
    broken_paths: set[str] | None = None,
    repo_web_bases: list[str | None] | None = None,
) -> LinkResolver | None:
    """构造一个把 file:line 解析为可点击跳转 URL 的解析器。

    repo_web_bases 与 repo_roots 一一对应；若某根提供了远程 blob 基址
    （形如 https://host/group/repo/-/blob/<ref>），命中该根的文件生成指向
    仓库网页的链接，否则回退到本地编辑器 scheme。不传则完全等同旧本地行为。
    """
    if not repo_roots:
        return None
    template = _JUMP_SCHEMES.get(scheme, _JUMP_SCHEMES["vscode"])
    roots = [Path(r).resolve() for r in repo_roots]
    web_bases = list(repo_web_bases) if repo_web_bases else []
    web_bases += [None] * (len(roots) - len(web_bases))
    path_maps: list[dict[str, str]] = []
    reverse_path_maps: list[dict[str, str]] = []
    for root in roots:
        mapping: dict[str, str] = {}
        mapping_file = root / ".codex_windows_path_map.json"
        if mapping_file.is_file():
            try:
                raw_mapping = json.loads(mapping_file.read_text(encoding="utf-8"))
                mapping = {
                    str(safe).replace("\\", "/"): str(original).replace("\\", "/")
                    for safe, original in raw_mapping.items()
                }
            except (OSError, json.JSONDecodeError, AttributeError):
                mapping = {}
        path_maps.append(mapping)
        reverse_path_maps.append({original: safe for safe, original in mapping.items()})

    # 后缀匹配索引按需懒建（仅在精确/剥前缀都失败时才付出遍历成本）
    index_cache: dict[str, dict[str, list[str]]] = {}
    line_count_cache: dict[Path, int] = {}

    def _suffix_unique_match(filepath: str) -> tuple[Path, int] | None:
        """在仓库里找路径以 filepath 结尾（按段对齐）的文件；仅唯一命中时返回。

        修复「agent 少写前缀」的常见情形，如 `axhal/src/cpu.rs` →
        `arceos/modules/axhal/src/cpu.rs`。重名歧义（多命中）保持不解析。
        """
        base = Path(filepath).name
        if "index" not in index_cache:
            index_cache["index"] = _build_repo_index(roots)
        want = Path(filepath).as_posix()
        matches: list[tuple[Path, int]] = []
        for tagged in index_cache["index"].get(base, []):
            ri_str, rel = tagged.split("\x00", 1)
            if rel == want or rel.endswith("/" + want):
                ri = int(ri_str)
                matches.append((roots[ri] / rel, ri))
        return matches[0] if len(matches) == 1 else None

    def _build_local_url(abs_path: str, line: str | None) -> str:
        anchor = f":{line}:1" if (line and scheme != "file") else ""
        from urllib.parse import quote
        encoded = quote(abs_path.lstrip("/"), safe="/:")
        return template.format(path="/" + encoded, anchor=anchor)

    def _build_remote_url(web_base: str, rel_posix: str, line: str | None) -> str:
        from urllib.parse import quote
        anchor = f"#L{line}" if line else ""
        return f"{web_base}/{quote(rel_posix, safe='/')}{anchor}"

    def _build_url(candidate: Path, ri: int, line: str | None) -> str:
        base = web_bases[ri] if 0 <= ri < len(web_bases) else None
        if base:
            try:
                rel = candidate.resolve().relative_to(roots[ri]).as_posix()
                rel = path_maps[ri].get(rel, rel)
                return _build_remote_url(base, rel, line)
            except (OSError, ValueError):
                pass
        try:
            abs_path = str(candidate.resolve())
        except OSError:
            abs_path = str(candidate)
        return _build_local_url(abs_path, line)

    def _line_is_valid(candidate: Path, line: str | None) -> bool:
        if not line:
            return True
        numbers = [int(value) for value in re.findall(r"\d+", str(line))]
        if not numbers or any(value < 1 for value in numbers):
            return False
        try:
            resolved = candidate.resolve()
            if resolved not in line_count_cache:
                with resolved.open("rb") as handle:
                    count = sum(chunk.count(b"\n") for chunk in iter(
                        lambda: handle.read(1024 * 1024), b""
                    ))
                # 非空且无末尾换行的单行文件也有第 1 行。
                if resolved.stat().st_size:
                    with resolved.open("rb") as handle:
                        handle.seek(-1, 2)
                        count += int(handle.read(1) != b"\n")
                line_count_cache[resolved] = count
            count = line_count_cache[resolved]
        except OSError:
            return False
        return numbers == sorted(numbers) and numbers[-1] <= count

    def _find(filepath: str) -> tuple[Path, int] | None:
        p = Path(filepath)
        if p.is_absolute():
            if not p.exists():
                return None
            rp = p.resolve()
            for ri, root in enumerate(roots):
                try:
                    rp.relative_to(root)
                    return rp, ri
                except ValueError:
                    continue
            return rp, 0
        for ri, root in enumerate(roots):
            cand = root / filepath
            if cand.exists():
                return cand, ri
            mapped = reverse_path_maps[ri].get(Path(filepath).as_posix())
            if mapped:
                cand = root / mapped
                if cand.exists():
                    return cand, ri
        parts = p.parts
        for strip in range(1, len(parts)):
            suffix = Path(*parts[strip:])
            for ri, root in enumerate(roots):
                cand = root / suffix
                if cand.exists():
                    return cand, ri
        # 仍未命中：尝试「后缀唯一匹配」补回缺失的中间前缀
        return _suffix_unique_match(filepath)

    def _resolve(filepath: str, line: str | None) -> str | None:
        found = _find(filepath)
        if found is not None:
            candidate, ri = found
            url = _build_url(candidate, ri, line)
            if _line_is_valid(candidate, line):
                return url
            if broken_paths is not None:
                broken_paths.add(f"{filepath}:{line}")
            return _BROKEN_PREFIX + url

        if broken_paths is not None:
            broken_paths.add(filepath)
        base = web_bases[0] if web_bases else None
        if base:
            return _BROKEN_PREFIX + _build_remote_url(
                base, Path(filepath).as_posix(), line)
        best_guess = str((roots[0] / filepath).resolve())
        return _BROKEN_PREFIX + _build_local_url(best_guess, line)

    return _resolve


def derive_repo_web_base(root: Path) -> str | None:
    """从仓库的 git remote + HEAD 推出网页版 blob 基址；无 git 信息返回 None。

    返回形如 https://host/group/repo/-/blob/<commit>（GitLab）或
    https://github.com/owner/repo/blob/<commit>（GitHub）。文件相对路径与
    #L<line> 由调用方拼接。
    """
    import subprocess

    def _git(*args: str) -> str:
        try:
            r = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    # ``git -C`` 默认会向父目录寻找 .git。分析的是下载归档时，这会错误继承
    # 报告生成器自身仓库的 remote，进而让全部证据链接指向另一个项目。
    top_level = _git("rev-parse", "--show-toplevel")
    if not top_level:
        return None
    try:
        if Path(top_level).resolve() != Path(root).resolve():
            return None
    except OSError:
        return None

    remote = _git("remote", "get-url", "origin")
    if not remote:
        return None
    ref = _git("rev-parse", "HEAD") or _git("rev-parse", "--abbrev-ref", "HEAD") or "HEAD"

    url = remote.strip()
    if url.startswith("git@"):                       # git@host:group/repo(.git)
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    elif url.startswith("ssh://"):                   # ssh://git@host[:port]/group/repo(.git)
        from urllib.parse import urlparse
        u = urlparse(url)
        url = f"https://{u.hostname}{u.path}"
    if url.endswith(".git"):
        url = url[:-4]
    url = url.rstrip("/")
    if "github.com" in url:
        return f"{url}/blob/{ref}"
    return f"{url}/-/blob/{ref}"


def repository_url_to_web_base(repository_url: str, ref: str = "main") -> str | None:
    """用报告元数据中的目标仓库 URL 构造稳定源码链接基址。"""
    url = str(repository_url or "").strip()
    if not url:
        return None
    if url.startswith("git@"):
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    elif url.startswith("ssh://"):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        url = f"https://{parsed.hostname}{parsed.path}"
    if url.endswith(".git"):
        url = url[:-4]
    url = url.rstrip("/")
    if not re.match(r"^https?://", url, re.I):
        return None
    from urllib.parse import quote
    safe_ref = quote(str(ref or "main"), safe="/")
    if "github.com" in url.casefold():
        return f"{url}/blob/{safe_ref}"
    return f"{url}/-/blob/{safe_ref}"
