"""对比报告档位标签统一（单一真相源）。

历史原因导致同一个「借鉴」置信档位在报告各处叫法不一：
  confirmed 档：已确认借鉴 / 借鉴 / 疑似借鉴 / 疑似借鉴·待人工判定
  review   档：needReview / needReview（疑似借鉴）/ 疑似借鉴 / 待复核 / 待人工判定
  weak         → 并入 review
再加上 confirmed 的「疑似借鉴」和 review 的「疑似借鉴」在不同图里撞名，老师读起来很乱。

本模块把最终 HTML 里的这些叫法统一为**两档 + 原创**：
  confirmed → 高度疑似借鉴   （红 #ef4444）
  review    → 疑似借鉴（待复核）（琥珀 #f59e0b）
  original  → 自研/原创        （绿）

图表里靠**颜色**判档（名字可能是「借鉴」也可能是「疑似借鉴」，但颜色恒定），故用颜色锚定改名。
所有规则**幂等**：对已统一的 HTML 再跑一遍不会二次改写。

两处调用：
  1. semantic_compare 写盘前（新报告即时统一）；
  2. fix_report_labels.py（批量修已生成的旧报告）。
"""
from __future__ import annotations

import re

CONFIRMED = "高度疑似借鉴"
REVIEW = "疑似借鉴（待复核）"
ORIGINAL = "自研/原创"

# (pattern, replacement, is_regex)
_RULES: list[tuple[str, str, bool]] = [
    # ---- A. 明确旧标签整体替换（顺序在前）----
    ("疑似借鉴（与历史代码逐行高度相似，待人工判定）", f"{CONFIRMED}（与历史代码逐行高度相似）", False),
    ("疑似借鉴·待人工判定", CONFIRMED, False),
    ("已确认借鉴", CONFIRMED, False),
    ("needReview（疑似借鉴）", REVIEW, False),
    ("needReview", REVIEW, False),
    ("弱相似", REVIEW, False),
    # review 档 KPI（此时 confirmed KPI 已是「高度疑似借鉴（函数）」；负向后顾 (?<!度) 避免
    # 命中 confirmed 的「…度疑似借鉴（函数）」，只改独立的 review KPI）
    (r"(?<!度)疑似借鉴（函数）", f"{REVIEW}（函数）", True),
    # ---- B. 悬挂限定词统一 ----
    ("待人工判定", "待复核", False),
    # ---- C. 进度条分段（&nbsp;…</div> 锚定，幂等）----
    ("&nbsp;借鉴</div>", f"&nbsp;{CONFIRMED}</div>", False),
    ("&nbsp;疑似借鉴</div>", f"&nbsp;{REVIEW}</div>", False),
    ("&nbsp;原创</div>", f"&nbsp;{ORIGINAL}</div>", False),
    # ---- D. ECharts series/data 的 name（颜色锚定；(?!"name") 防止跨对象）----
    (r'("name":\s*")(?:借鉴|疑似借鉴)("(?:(?!"name").)*?#ef4444)', r"\1" + CONFIRMED + r"\2", True),
    (r'("name":\s*")(?:疑似借鉴|待复核)("(?:(?!"name").)*?#f59e0b)', r"\1" + REVIEW + r"\2", True),
    (r'("name":\s*")(?:原创|自研/原创)("(?:(?!"name").)*?#(?:22c55e|16a34a))', r"\1" + ORIGINAL + r"\2", True),
    # ---- E. ECharts legend 的 data 数组（无颜色，按已知组合精确匹配）----
    ('"data": ["借鉴", "疑似借鉴", "原创"]', f'"data": ["{CONFIRMED}", "{REVIEW}", "{ORIGINAL}"]', False),
    ('"data": ["借鉴", "原创"]', f'"data": ["{CONFIRMED}", "{ORIGINAL}"]', False),
    ('"data": ["疑似借鉴", "待复核", "自研/原创"]', f'"data": ["{CONFIRMED}", "{REVIEW}", "{ORIGINAL}"]', False),
    ('"data": ["疑似借鉴", "自研/原创"]', f'"data": ["{CONFIRMED}", "{ORIGINAL}"]', False),
    ('"data": ["疑似借鉴"]', f'"data": ["{CONFIRMED}"]', False),
    # ---- F. 进度条 title 提示（幂等：锚定 title=" 起始）----
    (r'(title=")借鉴 ', r"\1" + CONFIRMED + " ", True),
    (r" / 疑似借鉴 (\d)", " / " + REVIEW + r" \1", True),
]


def normalize_labels(html: str) -> str:
    """把对比报告 HTML 里的档位标签统一为两档 + 原创。幂等。"""
    if not html:
        return html
    for pat, repl, is_re in _RULES:
        if is_re:
            html = re.sub(pat, repl, html, flags=re.DOTALL)
        else:
            html = html.replace(pat, repl)
    return html


# 用于校验：统一后不应再出现的旧叫法
LEGACY_TERMS = [
    "已确认借鉴", "needReview", "疑似借鉴·待人工判定", "待人工判定", "弱相似",
    "&nbsp;借鉴</div>", "&nbsp;疑似借鉴</div>",
]


def residual_legacy(html: str) -> list[str]:
    return [t for t in LEGACY_TERMS if t in html]
