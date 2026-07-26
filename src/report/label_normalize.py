"""对比报告档位标签统一（单一真相源）。

历史原因导致同一个「同源代码」置信档位在报告各处叫法不一：
  confirmed 档：高置信同源代码（并兼容旧称：已确认借鉴 / 疑似借鉴等）
  review   档：needReview / needReview（疑似借鉴）/ 疑似借鉴 / 待复核 / 待人工判定
  weak         → 并入 review
再加上 confirmed 的「疑似借鉴」和 review 的「疑似借鉴」在不同图里撞名，老师读起来很乱。

本模块把最终 HTML 里的这些叫法统一为**两类结论 + 独立流程状态 + 未检出**：
  confirmed → 高置信同源代码   （红 #ef4444）
  review    → 模型复核后仍存疑   （琥珀 #f59e0b）
  failure   → 复核失败/未完成     （灰；不计为存疑）
  original  → 暂未检出相似     （绿；不等于原创认定）

图表里靠**颜色**判档（名字可能是「借鉴」也可能是「疑似借鉴」，但颜色恒定），故用颜色锚定改名。
所有规则**幂等**：对已统一的 HTML 再跑一遍不会二次改写。

由 semantic_compare 在写盘前调用，保证新报告即时统一。旧报告不再做结果性修补，缺少完整
召回契约时统一视为失效并要求重跑。
"""
from __future__ import annotations

import re

CONFIRMED = "高置信同源代码"
REVIEW = "模型复核后仍存疑"
ORIGINAL = "暂未检出相似"

STALE_REPORT_BANNER = (
    '<section id="retrieval-stale" data-retrieval-contract-version="missing" '
    'data-retrieval-complete="false" style="margin:12px;padding:14px;border:2px solid #dc2626;'
    'border-radius:8px;background:#fef2f2;color:#991b1b">'
    '<b>⚠ 旧版查重报告已失效，必须按完整召回链重跑</b><br>'
    '<span style="font-size:13px">本报告生成时未记录历史库完整覆盖、索引代际和无静默截断契约；'
    '仅可用于定位历史问题，不得据此认定任何函数原创或未借鉴。</span></section>'
)

# (pattern, replacement, is_regex)
_RULES: list[tuple[str, str, bool]] = [
    # ---- 0. 旧报告的“原创”过度结论迁移为“暂未检出” ----
    ("自研/原创（函数）", f"{ORIGINAL}（函数）", False),
    ("自研/原创", ORIGINAL, False),
    ("原创代码清单", "暂未检出相似清单", False),
    (r'(<h2[^>]*>)原创代码(</h2>)',
     r"\1暂未检出历史相似（不等于原创）\2", True),
    (">原创代码</", ">暂未检出相似</", False),
    (r'共 <b>(\d+)</b> 个函数未与历史代码库构成借鉴.*?（按规模降序，全部列出）：',
     r'共 <b>\1</b> 个函数在当前历史库中暂未形成有效相似命中。'
     r'<b>这只表示系统暂未检出，不等于原创认定</b>；可能仍受历史库覆盖、召回与阈值影响。'
     r'以下按规模降序全部列出，供继续人工核验：', True),
    ("未发现原创函数", "没有暂未命中的函数", False),
    ("原创度高", "当前库内相似命中较少", False),
    ("原创性较高", "当前库内相似命中较少", False),
    ("疑似借鉴占自研代码", "疑似借鉴占纳入统计函数", False),
    ("疑似借鉴占自研函数", "疑似借鉴占纳入统计函数", False),
    ("作品自研部分", "排除机械重复后的待评估部分", False),
    ("个自研函数中标记出", "个纳入统计函数中标记出", False),
    ("按自研函数加权", "按纳入统计函数加权", False),
    ("下列数字均为<b>自研代码</b>口径", "下列数字为<b>待评估代码</b>口径", False),
    (r"绝大多数函数为自研实现，仅少量与历史作品高度相似，原创性良好。",
     "当前历史库中仅检出少量高度相似函数；未命中不等于已证明原创。", True),
    (r'原创 (\d+(?:\.\d+)?%)', r'暂未检出相似 \1', True),
    (r'/ 原创 (\d+)', r'/ 暂未检出 \1', True),
    ("原创 占比", "暂未检出相似 占比", False),
    # ---- A. 明确旧标签整体替换（顺序在前）----
    ("疑似借鉴（与历史代码逐行高度相似，待人工判定）", f"{CONFIRMED}（与历史代码逐行高度相似）", False),
    ("疑似借鉴·待人工判定", CONFIRMED, False),
    ("已确认借鉴", CONFIRMED, False),
    ("高度疑似借鉴", CONFIRMED, False),
    ("高度疑似", "高置信同源", False),
    ("needReview（疑似借鉴）", REVIEW, False),
    ("needReview", REVIEW, False),
    ("弱相似", REVIEW, False),
    ("疑似借鉴（待复核）", REVIEW, False),
    # review 档 KPI（此时 confirmed KPI 已统一；负向后顾 (?<!度) 避免
    # 命中 confirmed 的「…度疑似借鉴（函数）」，只改独立的 review KPI）
    (r"(?<!度)疑似借鉴（函数）", f"{REVIEW}（函数）", True),
    # ---- B. 悬挂限定词统一 ----
    ("待复核", REVIEW, False),
    ("待人工判定", "需人工确认", False),
    # ---- C. 进度条分段（&nbsp;…</div> 锚定，幂等）----
    ("&nbsp;借鉴</div>", f"&nbsp;{CONFIRMED}</div>", False),
    ("&nbsp;疑似借鉴</div>", f"&nbsp;{REVIEW}</div>", False),
    ("&nbsp;原创</div>", f"&nbsp;{ORIGINAL}</div>", False),
    ("&nbsp;自研/原创</div>", f"&nbsp;{ORIGINAL}</div>", False),
    # ---- D. ECharts series/data 的 name（颜色锚定；(?!"name") 防止跨对象）----
    (r'("name":\s*")(?:借鉴|疑似借鉴)("(?:(?!"name").)*?#ef4444)', r"\1" + CONFIRMED + r"\2", True),
    (r'("name":\s*")(?:疑似借鉴|待复核|疑似借鉴（待复核）)("(?:(?!"name").)*?#f59e0b)', r"\1" + REVIEW + r"\2", True),
    (r'("name":\s*")(?:原创|自研/原创|暂未检出相似)("(?:(?!"name").)*?#(?:22c55e|16a34a))', r"\1" + ORIGINAL + r"\2", True),
    # ---- E. ECharts legend 的 data 数组（无颜色，按已知组合精确匹配）----
    ('"data": ["借鉴", "疑似借鉴", "原创"]', f'"data": ["{CONFIRMED}", "{REVIEW}", "{ORIGINAL}"]', False),
    ('"data": ["借鉴", "原创"]', f'"data": ["{CONFIRMED}", "{ORIGINAL}"]', False),
    ('"data": ["疑似借鉴", "待复核", "自研/原创"]', f'"data": ["{CONFIRMED}", "{REVIEW}", "{ORIGINAL}"]', False),
    ('"data": ["高度疑似借鉴", "疑似借鉴（待复核）", "暂未检出相似"]', f'"data": ["{CONFIRMED}", "{REVIEW}", "{ORIGINAL}"]', False),
    ('"data": ["疑似借鉴", "自研/原创"]', f'"data": ["{CONFIRMED}", "{ORIGINAL}"]', False),
    ('"data": ["疑似借鉴"]', f'"data": ["{CONFIRMED}"]', False),
    # ---- F. 进度条 title 提示（幂等：锚定 title=" 起始）----
    (r'(title=")借鉴 ', r"\1" + CONFIRMED + " ", True),
    (r" / 疑似借鉴 (\d)", " / " + REVIEW + r" \1", True),
]


def normalize_labels(html: str) -> str:
    """统一标签；对没有召回完整性证明的旧完整 HTML 加失效标记。幂等。"""
    if not html:
        return html
    for pat, repl, is_re in _RULES:
        if is_re:
            html = re.sub(pat, repl, html, flags=re.DOTALL)
        else:
            html = html.replace(pat, repl)
    if ("<body" in html and "data-retrieval-contract-version=" not in html
            and 'id="retrieval-stale"' not in html):
        html = re.sub(r"(<body\b[^>]*>)", r"\1" + STALE_REPORT_BANNER,
                      html, count=1, flags=re.IGNORECASE)
    return html


# 用于校验：统一后不应再出现的旧叫法
LEGACY_TERMS = [
    "已确认借鉴", "高度疑似借鉴", "高度疑似", "needReview", "疑似借鉴·待人工判定", "待人工判定", "待复核", "弱相似",
    "&nbsp;借鉴</div>", "&nbsp;疑似借鉴</div>",
]


def residual_legacy(html: str) -> list[str]:
    return [t for t in LEGACY_TERMS if t in html]
