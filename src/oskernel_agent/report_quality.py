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


def find_system_placeholders(rendered: str) -> list[str]:
    """返回最终 HTML 可见内容中的系统占位标记；不检查用户源码中的普通 TODO。"""
    if not rendered:
        return []
    visible = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", "", rendered,
                     flags=re.IGNORECASE | re.DOTALL)
    visible = html.unescape(re.sub(r"<[^>]+>", " ", visible))
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
    if errors or markers:
        details = errors + [f"占位文本：{marker}" for marker in markers]
        raise IncompleteReportError("报告分析未完整完成：" + "；".join(details))
