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
# 标识符：snake_case / camelCase / 带 :: 或紧跟 ( 的 token
_IDENT = re.compile(
    r"[A-Za-z_][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+"          # snake_case
    r"|[a-z]+[A-Z][A-Za-z0-9]*"                          # camelCase
    r"|[A-Za-z_][A-Za-z0-9_]*(?=\s*(?:\(|::|<[A-Za-z]))"  # foo(  foo::  foo<T
)
_WORD = re.compile(r"[A-Za-z]{2,}")
# 代码味标点（出现多处 → 判为源码片段，不译）
_CODE_PUNCT = re.compile(r"->|::|[{};]|\)\s*\{|\)\s*->|==|!=|&&|\|\||#include|\basm\b")
# 标题 / 表头：即便正文已是中文，英文标题也要单独抓出来
_HEADING = re.compile(r"<(?:h[1-6]|th|strong)\b[^>]*>(.*?)</(?:h[1-6]|th|strong)>", re.I | re.S)


def _strip_noise(s: str) -> str:
    t = _CODE_SPAN.sub(" ", s)
    t = _TAG.sub(" ", t)
    t = _URL.sub(" ", t)
    t = _FILELINE.sub(" ", t)
    t = _IDENT.sub(" ", t)
    return t


def _looks_like_code(prose: str) -> bool:
    """去掉标识符后仍有多处代码标点 → 认为是粘贴的源码片段。"""
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


def needs_translation(s: str) -> bool:
    if not isinstance(s, str) or len(s) < 12:
        return False
    if _english_heading(s):
        return True
    prose = _strip_noise(s)
    if _looks_like_code(prose):
        return False
    words = [w for w in _WORD.findall(prose)
             if not (w.isupper() and len(w) <= 5)]  # 排除 VFS/ABI 之类缩写
    if len(words) < 5:
        return False
    cjk = len(_CJK.findall(prose))
    # 剩余仍以英文为主（中文字符数不足英文单词数的一半）→ 判为英文正文
    return cjk < len(words) * 0.5


_SYS_PROMPT = (
    "你是操作系统技术报告的中文化器。输入是报告中的一个字段（可能含 HTML 片段）。"
    "把其中的**英文自然语言句子/正文，以及英文标题**（<h1>–<h6> / <th> / <strong> 里的英文短语，"
    "如 'Task Core & Process Control Block'、'Program Loading & Execution'）全部改写成简洁准确的简体中文；"
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


def _translate(s: str, model: str, retries: int = 3) -> str:
    import time
    h = hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()
    if h in _cache:
        return _cache[h]
    cli = _get_client()
    if cli is None:
        return s
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
            if out:
                _cache[h] = out
                return out
            return s
        except Exception as e:  # noqa: BLE001（限流/超时 → 退避重试）
            last_err = e
            time.sleep(min(3 * i, 15))
    print(f"[lang_guard] 翻译失败（{retries} 次后保留原文）：{last_err}", file=sys.stderr)
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
        return {"enabled": False}
    model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    stats = {"checked": 0, "translated": 0}

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

    print(f"[lang_guard] 英文正文改中文：{stats['translated']}/{stats['checked']} 个字段"
          f"（去重 {len(uniq)} 次翻译）")
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
