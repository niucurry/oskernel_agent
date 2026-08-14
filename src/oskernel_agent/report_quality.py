"""报告完整性硬门禁：禁止把模型失败或未启用状态渲染成评委报告模块。"""

from __future__ import annotations

import html
import re


class IncompleteReportError(RuntimeError):
    """报告包含未完成分析模块，必须重跑或修复后才能交付。"""


SYSTEM_PLACEHOLDER_MARKERS = (
    "未启用 LLM 语义分析",
    "当前未启用语义模型",
    "LLM 聚合失败",
    "LLM 评判失败",
    "LLM 顶层评判失败",
    "当前为规则兜底",
    "使用规则兜底",
    "正文与模块缺失",
    "⚠ 聚合失败",
    "AI 模型检测未完成",
    "AI 检测产物中没有整体统计",
)

_ELLIPSIS_RE = re.compile(r"…|(?<!\.)\.{3}(?!\.)")

# 正文里含省略号的代码式片段（函数调用 / 结构体字面量 / 尖括号泛型等），
# 如 bail!(EPERM, ...)、Self { this: this.clone(), ... }、Vec<Option<...>>。
# 模型描述代码差异时习惯用 ... 缩写参数列表，这是代码引述而非正文截断；
# 省略号门禁只看正文（剥离 <code> 内容），因此确定性包成 <code> 即可通过门禁，
# 且不改变可见内容。尖括号分支要求标识符与 < 之间无空格，避免误吞 HTML 标签。
_CODE_ELLIPSIS_RE = re.compile(
    r"`?(?:[A-Za-z_][A-Za-z0-9_:]*!?\s*[\(\[{]"
    r"|[A-Za-z_][A-Za-z0-9_:]*<)"
    r"[^>]{0,120}?(?:\.\.\.|…)[^<]{0,120}?[\)\]}>]"
)


def sanitize_code_ellipses(html_text: str) -> str:
    """把正文里含省略号的代码式片段包进 <code>，已有 <code> 段落先占位保护。"""
    code_spans = re.findall(
        r"<code\b[^>]*>.*?</code>", html_text or "", re.IGNORECASE | re.DOTALL)
    protected = html_text or ""
    placeholders: dict[str, str] = {}
    for index, span in enumerate(code_spans):
        key = f"\x00CODE{index}\x00"
        placeholders[key] = span
        protected = protected.replace(span, key, 1)

    def _wrap(match: re.Match) -> str:
        token = match.group(0).strip("`")
        return f"<code>{html.escape(token)}</code>"

    wrapped = _CODE_ELLIPSIS_RE.sub(_wrap, protected)
    for key, span in placeholders.items():
        wrapped = wrapped.replace(key, span)
    return wrapped

_VISUAL_TRUNCATION_PATTERNS = (
    (re.compile(r"text-overflow\s*:\s*ellipsis", re.I), "CSS text-overflow: ellipsis"),
    (re.compile(r"-webkit-line-clamp\s*:", re.I), "CSS line-clamp"),
    (
        re.compile(r'["\']overflow["\']\s*:\s*["\']truncate["\']', re.I),
        "图表文字 truncate",
    ),
)


def visible_report_text(rendered: str) -> str:
    """提取报告自然语言正文；源码和脚本中的语言语法不参与省略号门禁。"""
    visible = re.sub(
        r"<(?:script|style|code|pre)\b[^>]*>.*?</(?:script|style|code|pre)>",
        " ",
        rendered or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    # 对比报告把逐行源码证据渲染成 <div class="cl">/`<div class="cl df">`（非 <pre>/<code>）。
    # 这些行是真实源码而非正文，其中字符串字面量里的省略号（如 debug!("...重试...")）不是
    # 报告截断，必须按代码剥离，否则会被误判为正文省略号。每个 cl 行经 html.escape 转义、
    # 不含嵌套 div，故可安全整行匹配。
    visible = re.sub(
        r'<div class="cl[^"]*">.*?</div>',
        " ",
        visible,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(re.sub(r"<[^>]+>", " ", visible))


def find_ellipsis_omissions(rendered: str) -> list[str]:
    """定位正文中用省略号代替未说完内容的位置。"""
    visible = re.sub(r"\s+", " ", visible_report_text(rendered)).strip()
    contexts: list[str] = []
    for match in _ELLIPSIS_RE.finditer(visible):
        start = max(0, match.start() - 28)
        end = min(len(visible), match.end() + 28)
        contexts.append(visible[start:end].strip())
    return contexts


def find_visual_truncation_styles(rendered: str) -> list[str]:
    """拒绝浏览器或图表组件以省略号、行数钳制隐藏报告文字。"""
    return [label for pattern, label in _VISUAL_TRUNCATION_PATTERNS if pattern.search(rendered or "")]


def find_system_placeholders(rendered: str) -> list[str]:
    """返回最终 HTML 可见内容中的系统占位标记；不检查用户源码中的普通 TODO。"""
    if not rendered:
        return []
    visible = visible_report_text(rendered)
    return [marker for marker in SYSTEM_PLACEHOLDER_MARKERS if marker in visible]


def collect_error_markers(value: object, path: str = "report") -> list[str]:
    """递归查找结构化报告中的 ``_error``，覆盖总评、子系统和模块。"""
    errors: list[str] = []
    if isinstance(value, dict):
        if value.get("_error"):
            errors.append(f"{path}: {value['_error']}")
        for key, child in value.items():
            if key != "_error":
                errors.extend(collect_error_markers(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(collect_error_markers(child, f"{path}[{index}]"))
    return errors


def assert_report_complete(rendered: str, *, structured: object | None = None) -> None:
    """发现结构化失败或占位正文时拒绝生成报告。"""
    errors = collect_error_markers(structured) if structured is not None else []
    markers = find_system_placeholders(rendered)
    ellipses = find_ellipsis_omissions(rendered)
    truncation_styles = find_visual_truncation_styles(rendered)
    if errors or markers or ellipses or truncation_styles:
        details = (
            errors
            + [f"占位文本：{marker}" for marker in markers]
            + [f"省略号截断：{context}" for context in ellipses]
            + [f"隐藏文字样式：{label}" for label in truncation_styles]
        )
        raise IncompleteReportError("报告分析未完整完成：" + "；".join(details))
