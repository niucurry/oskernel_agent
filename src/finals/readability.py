"""确定性的中文精炼与可读性检查。

这里不替模型编造内容，只负责删模板腔、控制篇幅和补充常见术语的首次解释。
"""

from __future__ import annotations

import html
import re

MODULE_SUMMARY_LIMIT = 300

_EMPTY_PHRASES = (
    "综上所述，",
    "综上所述",
    "总的来说，",
    "总而言之，",
    "值得注意的是，",
    "需要指出的是，",
    "从上述分析可以看出，",
    "毋庸置疑，",
)

_TERMS = {
    "LOC": "代码变更行数（LOC）",
    "COW": "写时复制（Copy-on-Write，COW）",
    "VFS": "虚拟文件系统（VFS）",
    "ELF": "可执行与可链接格式（ELF）",
    "IPC": "进程间通信（IPC）",
    "ABI": "应用二进制接口（ABI）",
    "SMP": "对称多处理（SMP）",
    "TLB": "地址转换后备缓冲器（TLB）",
    "IRQ": "中断请求（IRQ）",
    "LTP": "Linux 测试项目（LTP）",
    "FFI": "外部函数接口（FFI）",
    "MMIO": "内存映射输入输出（MMIO）",
    "PCI": "外设组件互连（PCI）",
    "CMA": "连续内存分配器（CMA）",
    "UML": "统一建模语言（UML）",
    "futex": "快速用户态互斥量（futex）",
    "syscall": "系统调用（syscall）",
    "vendor": "仓库内置第三方（vendor）",
}


def _tidy_term_expansions(text: str) -> str:
    for expansion in _TERMS.values():
        escaped = re.escape(expansion)
        text = re.sub(
            rf"(?<=[\u3400-\u9fff，。；：、])\s+{escaped}", expansion, text
        )
        text = re.sub(
            rf"{escaped}\s+(?=[\u3400-\u9fff，。；：、])", expansion, text
        )
    return text.replace("（IRQ）中断", "（IRQ）").replace("（LTP）测试", "（LTP）")


def html_to_text(value: str) -> str:
    """把报告片段转成适合摘要的单行文字。"""
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", value or "",
                  flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def remove_ai_filler(value: str) -> str:
    """删除不承载事实的常见模板连接语。"""
    text = " ".join(str(value or "").split())
    for phrase in _EMPTY_PHRASES:
        text = text.replace(phrase, "")
    return re.sub(r"\s+", " ", text).strip(" ，。；")


def clip_at_sentence(value: str, limit: int) -> str:
    """优先在中文句末截断，避免输出半句话。"""
    text = remove_ai_filler(html_to_text(value))
    if len(text) <= limit:
        return text
    prefix = text[: max(1, limit - 1)]
    cuts = [prefix.rfind(mark) for mark in "。！？；"]
    cut = max(cuts)
    if cut >= max(20, limit // 2):
        return prefix[: cut + 1]
    comma = max(prefix.rfind("，"), prefix.rfind(","))
    if comma >= max(20, limit * 2 // 3):
        return prefix[:comma] + "。"
    return prefix.rstrip("，,；;：:") + "…"


def explain_terms_on_first_use(value: str) -> str:
    """给常见缩写补一次中文解释；已经解释过的文本保持不变。"""
    text = str(value or "")
    for term, expansion in _TERMS.items():
        if term not in text or expansion in text:
            continue
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                      expansion, text, count=1)
    return _tidy_term_expansions(text)


def explain_terms_in_html(value: str) -> str:
    """只处理 HTML 可见正文，并在整份文档中仅定义术语一次。

    ``head/script/style/code/pre/a`` 内的文本不参与首次出现判断，避免把样式、
    代码标识符或文件路径误当作面向读者的术语定义。
    """
    parts = re.split(r"(<[^>]+>)", str(value or ""))
    skip_tags = {"head", "script", "style", "code", "pre", "a", "nav"}
    skipped: list[str] = []
    defined: set[str] = set()
    output: list[str] = []

    for part in parts:
        if part.startswith("<"):
            match = re.match(r"<\s*(/?)\s*([A-Za-z0-9]+)", part)
            if match:
                closing, tag = match.group(1), match.group(2).lower()
                if closing:
                    if tag in skipped:
                        reverse_index = len(skipped) - 1 - skipped[::-1].index(tag)
                        skipped.pop(reverse_index)
                elif tag in skip_tags and not part.rstrip().endswith("/>"):
                    skipped.append(tag)
            output.append(part)
            continue

        if skipped or not part:
            output.append(part)
            continue

        text = part
        for term, expansion in _TERMS.items():
            if term in defined:
                continue
            term_match = re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text
            )
            if not term_match:
                continue
            expansion_at = text.find(expansion)
            if 0 <= expansion_at <= term_match.start():
                defined.add(term)
                continue
            text = text[:term_match.start()] + expansion + text[term_match.end():]
            defined.add(term)
        output.append(_tidy_term_expansions(text))
    return "".join(output)


def concise_module_summary(value: str) -> str:
    return explain_terms_on_first_use(clip_at_sentence(value, MODULE_SUMMARY_LIMIT))


def readability_errors(value: str, *, max_chars: int | None = None) -> list[str]:
    """返回可测试的可读性问题；不对技术内容作主观评分。"""
    text = html_to_text(value)
    errors: list[str] = []
    if max_chars is not None and len(text) > max_chars:
        errors.append(f"正文 {len(text)} 字，超过 {max_chars} 字上限")
    for phrase in _EMPTY_PHRASES:
        if phrase in text:
            errors.append(f"包含空泛模板语：{phrase.rstrip('，')}")
    for term, expansion in _TERMS.items():
        first = text.find(term)
        if first >= 0 and expansion not in text[: first + len(expansion) + 8]:
            errors.append(f"术语 {term} 首次出现时未解释")
    if re.search(r"[。！？][。！？]+", text):
        errors.append("存在重复句末标点")
    return errors
