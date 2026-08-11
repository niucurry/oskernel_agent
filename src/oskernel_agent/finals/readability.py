"""确定性的中文精炼与可读性检查。

这里不替模型编造内容，只负责删模板腔、控制篇幅和补充常见术语的首次解释。
"""

from __future__ import annotations

import html
import re

MODULE_SUMMARY_LIMIT = 300

_DEPENDENCY_SCOPE_ONLY_RE = re.compile(
    r"(?:smoltcp|lwip|littlefs|fatfs|virtio|第三方|上游|依赖).{0,60}"
    r"(?:不完整|非完整|不是完整|未实现全部|不支持全部)"
    r"|(?:不完整|非完整|不是完整|未实现全部|不支持全部).{0,60}"
    r"(?:Linux\s*)?(?:TCP(?:/IP)?|网络|协议|文件系统).{0,12}(?:栈|实现)?",
    re.IGNORECASE,
)
_SYSTEM_VISIBLE_EFFECT_RE = re.compile(
    r"(?:系统调用|syscall|ABI|错误码|errno|ENOSYS|EINTR|EAGAIN|"
    r"阻塞|非阻塞|超时|信号|poll|epoll|select|backlog|路由|eth0|网卡|"
    r"外部网络|收包|发包|连接失败|无法连接|崩溃|panic|"
    r"socket|connect|listen|accept|send|recv|shutdown|sockopt)",
    re.IGNORECASE,
)


def is_dependency_scope_only_issue(value: str) -> bool:
    """Reject upstream-completeness criticism without an OS-visible consequence."""
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return bool(_DEPENDENCY_SCOPE_ONLY_RE.search(text)) and not bool(
        _SYSTEM_VISIBLE_EFFECT_RE.search(text)
    )

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
    "AI": "人工智能（AI）",
    "OS": "操作系统（Operating System，OS）",
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
    "MMU": "内存管理单元（MMU）",
    "DMA": "直接内存访问（DMA）",
    "TTY": "终端设备（TTY）",
    "PID": "进程标识符（PID）",
    "ASID": "地址空间标识符（ASID）",
    "SBI": "监管者二进制接口（SBI）",
    "CSR": "控制与状态寄存器（CSR）",
    "QEMU": "开源硬件模拟器（QEMU）",
    "RV64": "64 位 RISC-V（RV64）",
    "SV39": "三级虚拟内存分页方案（SV39）",
    "VirtIO": "虚拟输入输出设备规范（VirtIO）",
    "FDT": "扁平设备树（Flattened Device Tree，FDT）",
    "CFS": "完全公平调度器（Completely Fair Scheduler，CFS）",
    "PLIC": "平台级中断控制器（PLIC）",
    "HAL": "硬件抽象层（HAL）",
    "POSIX": "可移植操作系统接口（POSIX）",
    "bootstrap hart": "引导硬件线程（bootstrap hart）",
    "secondary hart": "次级硬件线程（secondary hart）",
    "shootdown": "跨核失效同步（shootdown）",
    "PCI": "外设组件互连（PCI）",
    "CMA": "连续内存分配器（CMA）",
    "UML": "统一建模语言（UML）",
    "futex": "快速用户态互斥量（futex）",
    "syscall": "系统调用（syscall）",
    "page table": "页表（page table）",
    "page fault": "缺页异常（page fault）",
    "context switch": "上下文切换（context switch）",
    "scheduler": "调度器（scheduler）",
    "cache": "缓存（cache）",
    "trap": "陷阱与异常入口（trap）",
    "inode": "索引节点（inode）",
    "mutex": "互斥锁（mutex）",
    "semaphore": "信号量（semaphore）",
    "heap": "堆（heap）",
    "stack": "栈（stack）",
    "vendor": "仓库内置第三方（vendor）",
}


def _inside_source_path(text: str, start: int, end: int) -> bool:
    """术语若位于 ``path/to/file.rs:42`` 中，不得改写源码路径。"""
    separators = set(" \t\r\n<>\"'，。；：、！？()（）[]【】{}")
    left = start
    while left > 0 and text[left - 1] not in separators:
        left -= 1
    right = end
    while right < len(text) and text[right] not in separators:
        right += 1
    token = text[left:right]
    separators_in_token = token.count("/") + token.count("\\")
    return separators_in_token >= 2 or bool(
        separators_in_token
        and re.search(r"\.[A-Za-z0-9_+-]+(?:(?::|#L)\d+(?:-L?\d+)?)?$", token)
    )


def _tidy_term_expansions(text: str) -> str:
    for expansion in _TERMS.values():
        escaped = re.escape(expansion)
        text = re.sub(
            rf"(?<=[\u3400-\u9fff，。；：、])\s+{escaped}", expansion, text
        )
        text = re.sub(
            rf"{escaped}\s+(?=[\u3400-\u9fff，。；：、])", expansion, text
        )
    return (
        text.replace("（IRQ）中断", "（IRQ）")
        .replace("（LTP）测试", "（LTP）")
        .replace("虚拟输入输出设备规范（VirtIO）规范", "虚拟输入输出设备规范（VirtIO）")
        .replace(
            "写时复制（写时复制（Copy-on-Write，COW））",
            "写时复制（Copy-on-Write，COW）",
        )
        .replace("多核（对称多处理（SMP））", "对称多处理（SMP）多核")
        .replace("平台级中断控制器（PLIC）中断控制器", "平台级中断控制器（PLIC）")
        .replace("三级虚拟内存分页方案（SV39）分页", "三级虚拟内存分页方案（SV39）")
    )


def _term_replacement(text: str, match: re.Match, expansion: str) -> str:
    """Avoid nesting a full expansion inside an already localized label."""
    if "（" not in expansion or not expansion.endswith("）"):
        return expansion
    label, inner = expansion[:-1].split("（", 1)
    if text[:match.start()].rstrip().endswith(label + "（"):
        return inner
    return expansion


def sanitize_html_controls(value: str) -> str:
    """Render forbidden C0 bytes visibly instead of emitting invalid HTML."""
    return re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f]",
        lambda match: f"\\x{ord(match.group(0)):02x}",
        str(value or ""),
    )


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
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])"
        )
        for match in pattern.finditer(text):
            if _inside_source_path(text, match.start(), match.end()):
                continue
            replacement = _term_replacement(text, match, expansion)
            text = text[:match.start()] + replacement + text[match.end():]
            break
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
            if _inside_source_path(text, term_match.start(), term_match.end()):
                # 同一文本节点里可能稍后还有正文用法；寻找第一个非路径命中。
                term_match = next(
                    (
                        match for match in re.finditer(
                            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])",
                            text,
                        )
                        if not _inside_source_path(text, match.start(), match.end())
                    ),
                    None,
                )
                if term_match is None:
                    continue
            expansion_at = text.find(expansion)
            if 0 <= expansion_at <= term_match.start():
                defined.add(term)
                continue
            replacement = _term_replacement(text, term_match, expansion)
            text = text[:term_match.start()] + replacement + text[term_match.end():]
            defined.add(term)
        output.append(_tidy_term_expansions(text))
    return "".join(output)


def ai_disclaimer_html(kind: str) -> str:
    """统一的 AI 生成声明，三份报告复用。"""
    sources = {
        "description": "源码结构分析与编译运行日志",
        "development": "Git 提交历史与代码变更记录",
        "comparison": "历史作品向量检索与代码相似度比对",
    }
    source_text = sources.get(kind, "程序自动分析")
    return (
        '<div class="ai-disclaimer">'
        "本报告由人工智能（AI）分析工具自动生成，参赛队伍不得修改。"
        f"分析依据：{source_text}。"
        "AI 判断仅供评委参考，不构成违规认定。"
        "</div>"
    )


def concise_module_summary(value: str) -> str:
    # 先解释术语再限长；反过来会让补入的中文全称把已截到 300 字的摘要再次撑长。
    plain = remove_ai_filler(html_to_text(value))
    return clip_at_sentence(
        explain_terms_on_first_use(plain), MODULE_SUMMARY_LIMIT,
    )


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
        match = re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text,
        )
        if match and expansion not in text[: match.start() + len(expansion) + 8]:
            errors.append(f"术语 {term} 首次出现时未解释")
    if re.search(r"[。！？][。！？]+", text):
        errors.append("存在重复句末标点")
    return errors
