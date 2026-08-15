"""描述报告语言护栏：确定性地把 LLM 漏出的**英文正文**改写成中文。

背景：subsys/verdict 的提示词（约束2a）已要求「描述性字段一律中文」，但 LLM 仍会
偶发整句英文（如 "The trap entry point ...", "Module 1: Trap Entry & Syscall Dispatch",
"ArceOS task management module. Provides primitives ..."）。光靠提示词约束不可靠，
故在 build_tree 产出 tree 后、渲染前，遍历 tree 的描述性字段，检测到**英文自然语言正文**
就用 LLM 翻成中文（保留 HTML 标签 / <code> 标识符 / file:line 引用）。

只针对**英文散文**。以下一律不动：
  - 源码片段（quote 里粘贴的代码）—— 属另一类缺陷，翻译反而会毁掉；
  - 夹带 snake_case / camelCase 标识符的中文句子（如 `sys_x 为 stub`）；
  - 全大写缩写（VFS/ABI/PCB…）。

开关：环境变量 AGENT_LANG_GUARD=0 关闭。无 API key 时静默跳过（返回原文）。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys

# 只对这些承载散文的字段生效
PROSE_KEYS = {
    "summary", "role", "one_line", "quote", "comment", "reason",
    "content", "note", "desc", "text", "overview",
}

_CJK = re.compile(r"[一-鿿]")
_CODE_SPAN = re.compile(r"<code\b[^>]*>.*?</code>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(r"https?://\S+")
_FILELINE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+:\d+(?:-\d+)?")
_BUILD_FILELINE = re.compile(
    r"\b(?:GNUmakefile|Makefile|makefile)(?::\d+(?:-\d+)?)?\b"
)
_FILEPATH = re.compile(r"(?:[A-Za-z0-9_.-]+[/\\])+(?:[A-Za-z0-9_.-]+)?")
_FILENAME = re.compile(r"\b[A-Za-z0-9_][A-Za-z0-9_.-]*\.(?:rs|c|cc|cpp|h|hpp|s|asm|py|sh|ld|toml|yaml|yml|json|md)\b", re.I)
_PAREN_IDENT_LIST = re.compile(
    r"\([A-Za-z_][A-Za-z0-9_]*(?:\s*[/,]\s*[A-Za-z_][A-Za-z0-9_]*)*\)"
)
# 标识符：snake_case / camelCase / 带 :: 或紧跟 ( 的 token
_IDENT = re.compile(
    r"[A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"          # snake_case
    r"|[a-z]+[A-Z][A-Za-z0-9]*"                          # camelCase
    r"|[A-Za-z_][A-Za-z0-9_]*(?=\s*(?:\(|::|<[A-Za-z]))"  # foo(  foo::  foo<T
)
_WORD = re.compile(r"[A-Za-z]{2,}")
# 代码味标点（出现多处 → 判为源码片段，不译）
_CODE_PUNCT = re.compile(r"->|::|[{};]|\)\s*\{|\)\s*->|==|!=|&&|\|\||#include|\basm\b")
# 代码行：行首出现 Rust/C 语句关键词或注释/宏标记。报告 quote 字段常粘贴
# 源码摘录（如 `if let Some(..) = ..`、`let .. = if cfg!(..)`），模型按
# 提示词正确保留原文，检测器必须同样识别为代码而非英文散文。
_CODE_LINE = re.compile(
    r"(?m)^\s*(?:#\s*\[|//|/\*|\*\s)?\s*(?:"
    r"(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?(?:fn|let|use|static|const|mod|struct|enum|impl|trait|match|loop|while|for)\b"
    r"|if\s+let\b|macro_rules!|cfg!\(|extern\s+(?:fn|crate)\b"
    r")"
)
# 标题 / 表头：即便正文已是中文，英文标题也要单独抓出来
_HEADING = re.compile(r"<(?:h[1-6]|th|strong)\b[^>]*>(.*?)</(?:h[1-6]|th|strong)>", re.I | re.S)
# HTML 正文块逐块检查，防止一大段中文掩盖其中某一个全英文章节。
_TEXT_BLOCK = re.compile(
    r"<(?:h[1-6]|p|li|th|td|caption|figcaption|blockquote)\b[^>]*>"
    r"(.*?)</(?:h[1-6]|p|li|th|td|caption|figcaption|blockquote)>",
    re.I | re.S,
)


def _strip_noise(s: str) -> str:
    t = _CODE_SPAN.sub(" ", s)
    t = _TAG.sub(" ", t)
    t = _URL.sub(" ", t)
    t = _FILELINE.sub(" ", t)
    t = _BUILD_FILELINE.sub(" ", t)
    t = _FILEPATH.sub(" ", t)
    t = _FILENAME.sub(" ", t)
    t = _PAREN_IDENT_LIST.sub(" ", t)
    t = _IDENT.sub(" ", t)
    return t


def _looks_like_code(prose: str) -> bool:
    """去掉标识符后仍有多处代码标点，或含代码行起始关键词 → 认为是粘贴的源码片段。"""
    if _CODE_LINE.search(prose):
        return True
    return len(_CODE_PUNCT.findall(prose)) >= 3


def _english_heading(s: str) -> bool:
    """正文可能已中文，但仍有英文标题/表头（<h1-6>/<th>/<strong>）——单独判定。"""
    for raw in _HEADING.findall(s):
        prose = _strip_noise(raw)
        if _looks_like_code(prose):
            continue
        words = [w for w in _WORD.findall(prose)
                 if not (w.isupper() and len(w) <= 5)]
        cjk = len(_CJK.findall(prose))
        if len(words) >= 2 and cjk == 0:   # 纯英文短语标题
            return True
    return False


def _plain_needs_translation(s: str) -> bool:
    prose = _strip_noise(s)
    if _looks_like_code(prose):
        return False
    words = [w for w in _WORD.findall(prose)
             if not (w.isupper() and len(w) <= 5)]  # 排除 VFS/ABI 之类缩写
    cjk = len(_CJK.findall(prose))
    # 很短的完整英文句子也要拦截；技术词列表（无句末标点）仍允许保留英文。
    if cjk == 0 and len(words) >= 3 and re.search(r"[.!?。！？]", prose):
        return True
    if len(words) < 5:
        return False
    # 剩余仍以英文为主（中文字符数不足英文单词数的一半）→ 判为英文正文
    return cjk < len(words) * 0.5


def needs_translation(s: str) -> bool:
    if not isinstance(s, str) or len(s) < 12:
        return False
    if _english_heading(s):
        return True
    # 对 HTML 逐正文块检查，不能只用整篇中英文比例判断。
    if any(_plain_needs_translation(block) for block in _TEXT_BLOCK.findall(s)):
        return True
    return _plain_needs_translation(s)


_SYS_PROMPT = (
    "你是操作系统技术报告的中文化器。输入是报告中的一个字段（可能含 HTML 片段）。"
    "把其中的**英文自然语言句子/正文，以及英文标题**（<h1>–<h6> / <th> / <strong> 里的英文短语，"
    "如 'Task Core & Process Control Block'、'Program Loading & Execution'、"
    "'per-hart PLIC context'）全部改写成简洁准确的简体中文；技术短语也要翻译，例如将 "
    "'per-hart PLIC context' 写成“每硬件线程 PLIC 上下文”；"
    "严格原样保留：所有 HTML 标签本身、<code>…</code> 里的标识符、形如 path/to/file.c:120 "
    "的文件行号引用、函数名/类型名等代码标识符。不要新增解释、不要加 Markdown 代码围栏、"
    "不要改变 HTML 结构。只输出改写后的内容本身。"
)

_cache: dict[str, str] = {}
_client = None
_client_ready = False


def _get_client():
    global _client, _client_ready
    if _client_ready:
        return _client
    _client_ready = True
    try:
        try:
            # 源码流水线入口（python -m oskernel_agent.comparison.pipeline）。
            from oskernel_agent import config as _cfg
        except ModuleNotFoundError:
            # 安装后的 ``oskernel-agent`` 控制台入口。
            from oskernel_agent import config as _cfg
        from openai import OpenAI
        key = (_cfg.api.get("key", "") or "").strip()
        base = (_cfg.api.get("base_url", "") or "").strip() or "https://api.deepseek.com/v1"
        if not key:
            _client = None
        else:
            _client = OpenAI(api_key=key, base_url=base, timeout=120)
    except Exception as e:  # noqa: BLE001
        print(f"[lang_guard] 初始化 LLM 客户端失败：{e}", file=sys.stderr)
        _client = None
    return _client


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")

# 单次翻译请求可安全处理的最大字符数；超过则按 HTML 文本块拆分。
_TRANSLATE_CHUNK_LIMIT = 4000


def _translate_oversized_block(block: str, model: str) -> str:
    """超长英文块（>4000 字符）按句界切成小段逐段翻译，重组回原标签。

    整块直译会被 max_tokens 截断；小段翻译可靠，且保留原开闭标签属性。
    """
    m_open = re.match(r"<[^>]+>", block)
    m_close = re.search(r"</[^>]+>\s*$", block)
    if not (m_open and m_close):
        return block
    open_tag, close_tag = m_open.group(0), m_close.group(0)
    inner = block[m_open.end():m_close.start()]
    pieces = re.findall(r".*?[。！？；.!?;](?:\s+|$)", inner)
    if not pieces or sum(len(p) for p in pieces) < len(inner) * 0.5:
        # 没有可靠句界：按固定窗口硬切
        pieces = [inner[i:i + 2000] for i in range(0, len(inner), 2000)]
    parts: list[str] = []
    for piece in pieces:
        translated = _translate(open_tag + piece + close_tag, model)
        m = re.search(r"<[^>]+>(.*)</[^>]+>\s*$", translated, re.S)
        parts.append(m.group(1) if m else piece)
    return open_tag + "".join(parts) + close_tag


def _translate_large(s: str, model: str) -> str:
    """大字段（如整份 verdict 正文）按 HTML 文本块拆分翻译后原位回填。

    整段直译会被 max_tokens 截断、校验失败；文本块单块很小，翻译可靠。
    超长块进一步按句界切分逐段翻译，避免漏译触发中文交付门禁。
    """
    if _TEXT_BLOCK.search(s) is None:
        return s
    out: list[str] = []
    last = 0
    for match in _TEXT_BLOCK.finditer(s):
        block = match.group(0)
        inner = match.group(1)
        out.append(s[last:match.start()])
        if _plain_needs_translation(inner) or _english_heading(block):
            if len(block) <= _TRANSLATE_CHUNK_LIMIT:
                out.append(_translate(block, model))
            else:
                out.append(_translate_oversized_block(block, model))
        else:
            out.append(block)
        last = match.end()
    out.append(s[last:])
    return "".join(out)


def _translate(s: str, model: str, retries: int = 3) -> str:
    import time
    h = hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()
    if h in _cache:
        return _cache[h]
    cli = _get_client()
    if cli is None:
        return s
    if len(s) > _TRANSLATE_CHUNK_LIMIT:
        chunked = _translate_large(s, model)
        if not needs_translation(chunked):
            _cache[h] = chunked
            return chunked
        # 分块后仍残留英文：不再对整段做必然被截断的大请求，保留最佳结果。
        print(f"[lang_guard] 分块翻译后仍残留英文（{len(s)} 字符字段）", file=sys.stderr)
        return chunked
    last_err = None
    for i in range(1, retries + 1):
        try:
            r = cli.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _SYS_PROMPT},
                          {"role": "user", "content": s}],
                temperature=0.2,
                max_tokens=4000,
            )
            out = (r.choices[0].message.content or "").strip()
            out = _FENCE.sub("", out).strip()
            # 翻译输出仍含英文正文时不缓存，也不把它当作成功结果。
            if out and not needs_translation(out):
                _cache[h] = out
                return out
            last_err = "模型返回内容仍未通过中文校验"
        except Exception as e:  # 限流或超时时退避重试
            last_err = e
            time.sleep(min(3 * i, 15))
    print(f"[lang_guard] 翻译失败（{retries} 次后保留原文）：{last_err}", file=sys.stderr)
    if len(s) > 40:
        print(f"[lang_guard] 失败字段样例：{s[:200]!r}", file=sys.stderr)
    return s


def _workers() -> int:
    raw = os.environ.get("AGENT_LANG_GUARD_WORKERS", "").strip()
    try:
        return max(1, min(int(raw), 12)) if raw else 6
    except ValueError:
        return 6


def normalize_tree_language(tree: dict) -> dict:
    """就地遍历 tree，把英文正文字段翻成中文（并发翻译，去重）。返回统计。"""
    if os.environ.get("AGENT_LANG_GUARD", "").strip().lower() in ("0", "false", "no", "off"):
        return {"enabled": False, "complete": True, "remaining": 0}
    # 翻译是机械改写任务；模型跟随主链路 LLM_MODEL（生产为 deepseek-v4-pro），
    # 可另用 AGENT_LANG_GUARD_MODEL 覆盖。大字段由 _translate 分块，
    # 避免推理模型在整段输入上耗尽输出预算导致 content 为空。
    model = (
        os.getenv("AGENT_LANG_GUARD_MODEL", "").strip()
        or os.getenv("LLM_MODEL", "").strip()
        or "deepseek-v4-flash"
    )
    stats = {"enabled": True, "checked": 0, "translated": 0,
             "remaining": 0, "complete": True}

    # 1. 收集所有需要翻译的字段引用
    targets: list[tuple[dict, str, str]] = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and k in PROSE_KEYS:
                    stats["checked"] += 1
                    if needs_translation(v):
                        targets.append((obj, k, v))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    walk(tree)
    if not targets:
        return stats

    # 2. 去重后并发翻译（I/O 密集，线程池即可）
    uniq = list({v for _, _, v in targets})
    if _get_client() is None:
        print("[lang_guard] 无 API key，跳过中文化（保留英文）", file=sys.stderr)
        stats["remaining"] = len(targets)
        stats["complete"] = False
        return stats

    from concurrent.futures import ThreadPoolExecutor
    trans: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=_workers()) as ex:
        for src, dst in zip(uniq, ex.map(lambda s: _translate(s, model), uniq)):
            trans[src] = dst

    # 3. 回填
    for obj, k, v in targets:
        nv = trans.get(v, v)
        if nv != v:
            obj[k] = nv
            stats["translated"] += 1

    stats["remaining"] = sum(1 for obj, k, _ in targets if needs_translation(obj[k]))
    stats["complete"] = stats["remaining"] == 0

    print(f"[lang_guard] 英文正文改中文：{stats['translated']}/{stats['checked']} 个字段"
          f"（去重 {len(uniq)} 次翻译）")
    return stats


def normalize_html_language(fragment: str) -> tuple[str, dict]:
    """校验单段模型 HTML；仅在发现英文正文时调用现有翻译模型兜底。"""
    stats = {"enabled": True, "checked": 1, "translated": 0,
             "remaining": 0, "complete": True}
    if not needs_translation(fragment):
        return fragment, stats
    if os.environ.get("AGENT_LANG_GUARD", "").strip().lower() in ("0", "false", "no", "off"):
        stats.update(enabled=False, remaining=1, complete=False)
        return fragment, stats
    if _get_client() is None:
        stats.update(remaining=1, complete=False)
        return fragment, stats
    normalized = _translate(fragment, os.getenv("LLM_MODEL", "deepseek-v4-flash"))
    if normalized != fragment:
        stats["translated"] = 1
    stats["remaining"] = int(needs_translation(normalized))
    stats["complete"] = stats["remaining"] == 0
    return normalized, stats


def _looks_like_natural_english(s: str) -> bool:
    """英文自然语言句子判定：≥3 个非缩写英文词且带句末标点。"""
    prose = _strip_noise(s)
    words = [w for w in _WORD.findall(prose) if not (w.isupper() and len(w) <= 5)]
    return len(words) >= 3 and bool(re.search(r"[.!?。！？]", prose))


def residual_english_acceptable(s: str) -> bool:
    """残留英文是否仅限标识符/函数名/字段名/术语（无英文自然语言句子）。

    正文块、标题、整字段逐层检查：只要某处残留构成英文自然句即不可接受；
    术语短语（如 System V IPC、per-hart PLIC context）不构成句子，放行。
    """
    for block in _TEXT_BLOCK.findall(s):
        if _plain_needs_translation(block) and _looks_like_natural_english(block):
            return False
    for heading in _HEADING.findall(s):
        if _looks_like_natural_english(heading):
            return False
    if _plain_needs_translation(s) and _looks_like_natural_english(s):
        return False
    return True


def language_output_complete(data: dict) -> bool:
    """判断生成结果是否满足中文交付要求，供缓存准入使用。"""
    complete = True

    def walk(obj, module_record: bool = False):
        nonlocal complete
        if not complete:
            return
        if isinstance(obj, dict):
            if (module_record or obj.get("type") == "module") and title_needs_translation(
                obj.get("name", "")
            ):
                complete = False
                return
            for key, value in obj.items():
                if isinstance(value, str) and key in PROSE_KEYS and needs_translation(value):
                    complete = False
                    return
                if key == "modules" and isinstance(value, list):
                    for item in value:
                        walk(item, module_record=True)
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, module_record=module_record)

    walk(data)
    return complete


# ======================================================================
# 模块标题护栏：把 LLM 漏出的英文模块名（tree 节点 type=="module" 的 name）翻成中文。
# 该字段渲染在树节点头与目录（TOC）里，不在 PROSE_KEYS 覆盖的正文范围内——
# 漏翻会造成「正文已中文、目录还是英文」的割裂。
# 仅处理 module 节点：subsystem 名来自固定中文清单，root 名是仓库目录名，均不可译。
# ======================================================================

_TITLE_SYS = (
    "你是操作系统技术报告的中文化器。输入是报告目录里的一个**模块标题**（英文短语）。"
    "把它翻译成简洁准确的简体中文标题；保留缩写（VFS/ABI/COW…）、函数名/类型名等代码标识符。"
    "不要加解释、引号或标点结尾。只输出翻译后的标题本身。"
)


def title_needs_translation(s: str) -> bool:
    """英文短语标题 → True。比 needs_translation 宽（标题常只有 2-4 个词）。"""
    if not isinstance(s, str) or len(s) < 6:
        return False
    if _CJK.search(s):
        return False
    prose = _strip_noise(s)
    if _looks_like_code(prose):
        return False
    words = [w for w in _WORD.findall(prose)
             if not (w.isupper() and len(w) <= 5)]   # 排除 VFS/ABI 之类缩写
    return len(words) >= 2


def _translate_title(s: str, model: str) -> str:
    h = hashlib.sha1(("T|" + s).encode("utf-8", "replace")).hexdigest()
    if h in _cache:
        return _cache[h]
    cli = _get_client()
    if cli is None:
        return s
    import time
    for i in range(1, 4):
        try:
            r = cli.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _TITLE_SYS},
                          {"role": "user", "content": s}],
                temperature=0.2, max_tokens=200,
            )
            out = _FENCE.sub("", (r.choices[0].message.content or "").strip()).strip()
            if out and _CJK.search(out):
                _cache[h] = out
                return out
            return s
        except Exception:  # noqa: BLE001
            time.sleep(min(3 * i, 15))
    return s


def normalize_tree_titles(tree: dict) -> dict:
    """就地把 module 节点或生成阶段 modules[] 的英文 name 翻成中文。"""
    if os.environ.get("AGENT_LANG_GUARD", "").strip().lower() in ("0", "false", "no", "off"):
        return {"enabled": False, "complete": True, "remaining": 0}
    model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    targets: list[dict] = []

    def walk(obj, module_record: bool = False):
        if isinstance(obj, dict):
            if (module_record or obj.get("type") == "module") and title_needs_translation(
                obj.get("name", "")
            ):
                targets.append(obj)
            for key, value in obj.items():
                if key == "modules" and isinstance(value, list):
                    for item in value:
                        walk(item, module_record=True)
                else:
                    walk(value)
        elif isinstance(obj, list):
            for x in obj:
                walk(x, module_record=module_record)

    walk(tree)
    stats = {"enabled": True, "checked": len(targets), "translated": 0,
             "remaining": 0, "complete": True}
    if not targets:
        return stats
    if _get_client() is None:
        stats["remaining"] = len(targets)
        stats["complete"] = False
        return stats

    from concurrent.futures import ThreadPoolExecutor
    uniq = list({n["name"] for n in targets})
    with ThreadPoolExecutor(max_workers=_workers()) as ex:
        trans = dict(zip(uniq, ex.map(lambda s: _translate_title(s, model), uniq)))
    for n in targets:
        nv = trans.get(n["name"], n["name"])
        if nv != n["name"]:
            n["name"] = nv
            stats["translated"] += 1
    stats["remaining"] = sum(1 for n in targets if title_needs_translation(n.get("name", "")))
    stats["complete"] = stats["remaining"] == 0
    if stats["translated"]:
        print(f"[lang_guard] 英文模块标题改中文：{stats['translated']}/{stats['checked']} 个")
    return stats


# ======================================================================
# 槽点/亮点 quote 护栏：把粘贴的源码/TODO 改写成中文一句话点评
# （subsys.md 要求 quote 是「中文一句话点评，不是粘贴源码原文」，但 LLM 常违反）
# ======================================================================

# quote 里的代码味标记（命中 ≥2 判为源码摘录而非点评）
_QUOTE_CODE = re.compile(
    r"->|::|[{}]|\bfn\b|\bpub\b|\bstruct\b|\bimpl\b|\benum\b|\bstatic\b|\breturn\b"
    r"|#\[|cfg_if!|unimplemented!\(|;\s|\)\s*\{|==|!=|&&|\|\||\[[A-Z_]{3,}\]"
)


def is_code_quote(q: str) -> bool:
    """quote 是「粘贴的源码/英文原文」而非中文点评 → True。"""
    if not isinstance(q, str) or len(q) < 10:
        return False
    s = _FILELINE.sub("", q)
    s = _CODE_SPAN.sub("", s)
    cjk = len(_CJK.findall(s))
    if len(_QUOTE_CODE.findall(s)) >= 2:
        return True
    # 几乎无中文、又不短 → 英文/代码堆砌
    if cjk < 3 and len(s.strip()) > 15:
        return True
    if re.match(r"\s*(TODO|FIXME|XXX|HACK|NOTE)\b", s):
        return True
    return False


_QUOTE_SYS = (
    "你是操作系统代码评审助手。用户给你报告里的一个「亮点」或「槽点」条目：含代码位置与一段"
    "**源码摘录**。请把它改写成**一句简洁准确的中文点评**——亮点说清这里实现了什么/好在哪，"
    "槽点说清这里存在什么问题。要求："
    "①直接陈述，不要出现「该亮点/该槽点/本条/这里的代码」之类的自我指代；"
    "②关键标识符（函数名/类型名/常量）用 <code>…</code> 包裹，不要用反引号 `；"
    "③不要粘贴原始代码、不要输出位置路径、不要加解释或代码围栏。只输出这一句中文点评。"
)


def rewrite_code_quote(quote: str, path: str, kind: str, model: str) -> str:
    h = hashlib.sha1(("Q|" + kind + "|" + path + "|" + quote).encode("utf-8", "replace")).hexdigest()
    if h in _cache:
        return _cache[h]
    cli = _get_client()
    if cli is None:
        return quote
    import time
    msg = f"类型：{kind}\n位置：{path}\n源码摘录：{quote}"
    for i in range(1, 4):
        try:
            r = cli.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _QUOTE_SYS},
                          {"role": "user", "content": msg}],
                temperature=0.2, max_tokens=400,
            )
            out = _FENCE.sub("", (r.choices[0].message.content or "").strip()).strip()
            if out:
                _cache[h] = out
                return out
            return quote
        except Exception:  # noqa: BLE001
            time.sleep(min(3 * i, 15))
    return quote


def remaining_english_count(tree: dict) -> int:
    """最终交付复核：统计仍需要中文化的正文字段与模块标题数。

    正文/标题护栏各自返回运行时快照统计；quote 护栏可能在之后把残留英文 quote
    改写为中文，因此交付门禁必须按最终树重算，避免陈旧 complete=False 误拒交付。
    """
    count = 0

    def walk(obj, module_record: bool = False):
        nonlocal count
        if isinstance(obj, dict):
            if (module_record or obj.get("type") == "module") and title_needs_translation(
                obj.get("name", "")
            ):
                count += 1
            for key, value in obj.items():
                if key == "modules" and isinstance(value, list):
                    for item in value:
                        walk(item, module_record=True)
                elif isinstance(value, str) and key in PROSE_KEYS and needs_translation(value):
                    count += 1
                else:
                    walk(value)
        elif isinstance(obj, list):
            for x in obj:
                walk(x, module_record=module_record)

    walk(tree)
    return count


def normalize_tree_quotes(tree: dict) -> dict:
    """遍历 tree 的 highlights/issues，把「代码摘录型」quote 改写成中文点评。就地修改。"""
    if os.environ.get("AGENT_QUOTE_GUARD", "").strip().lower() in ("0", "false", "no", "off"):
        return {"enabled": False}
    model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    targets: list[tuple[dict, str, str]] = []  # (item_dict, kind, quote)

    def walk(o):
        if isinstance(o, dict):
            for key, kind in (("highlights", "亮点"), ("issues", "槽点")):
                for it in (o.get(key) or []):
                    if isinstance(it, dict) and is_code_quote(it.get("quote", "")):
                        targets.append((it, kind, it.get("quote", "")))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(tree)
    stats = {"checked": len(targets), "rewritten": 0}
    if not targets or _get_client() is None:
        return stats

    from concurrent.futures import ThreadPoolExecutor
    def _do(t):
        it, kind, q = t
        return rewrite_code_quote(q, it.get("path", ""), kind, model)
    with ThreadPoolExecutor(max_workers=_workers()) as ex:
        results = list(ex.map(_do, targets))
    for (it, _kind, q), nv in zip(targets, results):
        if nv and nv != q:
            it["quote"] = nv
            stats["rewritten"] += 1
    if stats["rewritten"]:
        print(f"[quote_guard] 代码摘录改中文点评：{stats['rewritten']}/{stats['checked']} 条")
    return stats


if __name__ == "__main__":  # 冒烟测试
    samples = [
        ("summary", "ArceOS task management module. Provides primitives for scheduling."),
        ("quote", "sys_sched_getaffinity 为 stub 实现，直接返回全 1 掩码"),
        ("quote", "pub fn fork(self: &Arc<ProcessControlBlock>, sp: usize) -> isize { ... }"),
        ("content", "<h3>Module 1: Trap Entry &amp; Syscall Dispatch</h3><p>The trap entry point saves registers.</p>"),
        ("role", "VFS 抽象 + FAT32 实现"),
        ("quote", "WAIT/WAKE/REQUEUE/CMP_REQUEUE 均属 Futex 操作"),
    ]
    for k, v in samples:
        print(f"{needs_translation(v)!s:5} [{k}] {v[:60]}")
    print("--- is_code_quote ---")
    qs = [
        "pub struct TaskInner { id, name, state, cpumask, ... }",
        "static uint64 (*syscalls[])(void) = { [SYS_fork] sys_fork, ... };",
        "TODO: Implement better load balancing across CPUs",
        "if (strncmp((char const*)(b->data + 82), \"FAT32\", 5)) { brelse(b); }",
        "支持 20+ CloneFlags 的细粒度 clone 实现，区分线程与进程创建",
        "fork 为 stub（unimplemented()），用户态不可创建子进程",
        "AxRunQueue — 每 CPU 运行队列，封装可插拔 Scheduler，提供 yield/exit 调度操作",
    ]
    for q in qs:
        print(f"{is_code_quote(q)!s:5} {q[:64]}")
