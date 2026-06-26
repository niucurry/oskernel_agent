"""语义级对比报告：suspects.json → opencode 语义分析 → 直接产出 HTML。

取代旧流程：
  review (逐对 LLM) → reviewed.json → report (Markdown→HTML)
新流程：
  suspects.json → collect pairs → opencode (一次调用) → 直接 HTML

用法：
  python -m src.report compare \\
      --suspects data/output/xxx_suspects.json \\
      --query-repo data/historical_repos/team-xyz \\
      [--recall data/output/xxx_recall.json] \\
      [--output-dir data/output]
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import subprocess
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from loguru import logger

DEFAULT_OUTPUT_DIR = "data/output"

# 子模块列表及其显示名称
MODULES = ["sched", "mm", "fs", "trap", "driver", "arch", "other"]


def _find_opencode() -> str:
    """从 PATH 中找 opencode 可执行文件（兼容 Linux ~/.local/bin 与 Windows npm）。"""
    import shutil
    found = shutil.which("opencode")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "opencode",
        Path.home() / "AppData" / "Roaming" / "npm" / "opencode",
        Path.home() / "AppData" / "Roaming" / "npm" / "opencode.cmd",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "opencode"  # 最终兜底，报 FileNotFoundError 时捕获
_MODULE_DISPLAY = {
    "sched":  "进程调度",
    "mm":     "内存管理",
    "fs":     "文件系统",
    "trap":   "异常/系统调用",
    "driver": "设备驱动",
    "arch":   "硬件抽象",
    "other":  "其他",
}

_OPENCODE = _find_opencode()


# ─── 1. 统计：各子模块复制/原创百分比 ─────────────────────────────────────────

def compute_submodule_stats(suspects: list[dict], recall: dict | None = None) -> dict:
    """计算各子模块的复制/原创比例。

    Returns: dict[module_tag] → {confirmed, review, weak, total,
                                  copy_pct, original_pct, top_source}
    """
    # recall.json 提供每个模块的函数总数（更准确）
    module_totals: Counter = Counter()
    if recall:
        for item in recall.get("results", []):
            q = item.get("query", {})
            module_totals[q.get("module_tag", "other")] += 1

    agg: dict[str, dict] = {
        mod: {"confirmed": 0, "review": 0, "weak": 0, "sources": Counter()}
        for mod in MODULES
    }

    # 按 (file_path, func_name) 去重，避免同函数命中多个候选重复计数
    seen_query: set[tuple[str, str]] = set()
    for s in suspects:
        tier = s.get("tier", "")
        if tier in ("dismissed", "baseline_derived", "common_code"):
            continue
        q = s.get("query_func", {})
        key = (q.get("file_path", ""), q.get("func_name", ""))
        mod = q.get("module_tag", "other")
        if mod not in agg:
            mod = "other"
        c_repo = s.get("candidate_func", {}).get("repo_id", "?")
        agg[mod]["sources"][c_repo] += 1
        if key not in seen_query:
            seen_query.add(key)
            if tier in ("confirmed", "review", "weak"):
                agg[mod][tier] += 1

    result = {}
    for mod, data in agg.items():
        # total 优先用 recall 统计；无 recall 时用嫌疑对数量兜底
        total = (module_totals.get(mod, 0)
                 or (data["confirmed"] + data["review"] + data["weak"])
                 or 1)
        # 加权：confirmed=1.0, review=0.5, weak=0.2
        copy_score = data["confirmed"] * 1.0 + data["review"] * 0.5 + data["weak"] * 0.2
        copy_pct = min(1.0, copy_score / total)
        top_src = data["sources"].most_common(1)
        result[mod] = {
            "confirmed": data["confirmed"],
            "review":    data["review"],
            "weak":      data["weak"],
            "total":     total,
            "copy_pct":     round(copy_pct, 3),
            "original_pct": round(max(0.0, 1.0 - copy_pct), 3),
            "top_source":   top_src[0][0] if top_src else "—",
        }
    return result


def _original_functions(recall: dict, suspects: list[dict], top_n: int = 12) -> list[dict]:
    """从 recall.json 中找出与历史库最高相似度 < 0.5 且行数 > 20 的函数（原创候选）。"""
    suspected_keys = {
        (s.get("query_func", {}).get("file_path", ""),
         s.get("query_func", {}).get("func_name", ""))
        for s in suspects
        if s.get("tier") not in ("dismissed", "baseline_derived", "common_code")
    }
    out = []
    for item in recall.get("results", []):
        q = item.get("query", {})
        key = (q.get("file_path", ""), q.get("func_name", ""))
        if key in suspected_keys:
            continue
        cands = item.get("candidates", [])
        max_sim = max((c.get("score", 0.0) for c in cands), default=0.0)
        lines = (q.get("end_line", 0) or 0) - (q.get("start_line", 0) or 0) + 1
        if max_sim < 0.5 and lines > 20:
            out.append({
                "func":    q.get("func_name", ""),
                "file":    q.get("file_path", ""),
                "start":   q.get("start_line", 0),
                "end":     q.get("end_line", 0),
                "module":  q.get("module_tag", "other"),
                "max_sim": round(max_sim, 3),
                "lines":   lines,
            })
    out.sort(key=lambda x: -x["lines"])
    return out[:top_n]


# ─── 2. 收集代码对（各模块取 top_per_module 对） ──────────────────────────────

def collect_file_pairs(suspects: list[dict], top_per_module: int = 5) -> list[dict]:
    """从 suspects 按子模块提取代表性相似代码对（含代码片段）。"""
    by_module: dict[str, list[dict]] = defaultdict(list)
    for s in suspects:
        tier = s.get("tier", "")
        if tier in ("dismissed", "baseline_derived", "common_code"):
            continue
        q = s.get("query_func", {})
        c = s.get("candidate_func", {})
        mod = q.get("module_tag", "other")
        by_module[mod].append({
            "module":      mod,
            "tier":        tier,
            "sim":         round(float(s.get("final_score", 0.0)), 3),
            "query_func":  q.get("func_name", ""),
            "query_file":  q.get("file_path", ""),
            "query_start": q.get("start_line", 0),
            "query_end":   q.get("end_line", 0),
            "query_code":  (q.get("raw_code") or "")[:800],
            "ref_func":    c.get("func_name", ""),
            "ref_file":    c.get("file_path", ""),
            "ref_repo":    c.get("repo_id", ""),
            "ref_start":   c.get("start_line", 0),
            "ref_end":     c.get("end_line", 0),
            "ref_code":    (c.get("raw_code") or "")[:800],
        })
    result = []
    for mod in MODULES:
        pairs = sorted(by_module.get(mod, []), key=lambda x: -x["sim"])
        result.extend(pairs[:top_per_module])
    return result


# ─── 3. 上下文文件 + opencode 短消息 ─────────────────────────────────────────
# Windows 命令行限制 ~32K 字符，不能把完整代码对塞进 CLI 参数。
# 解决方案：把所有数据写到仓库里的临时 JSON 文件，用 read_file MCP 工具读取。

_CONTEXT_FILENAME = "_plagiarism_context.json"


def _write_context_file(
    query_repo_path: str,
    query_repo_id: str,
    file_pairs: list[dict],
    submodule_stats: dict,
    output_path: str,
) -> Path:
    """把分析上下文序列化到 {query_repo_path}/_plagiarism_context.json。"""
    ctx = {
        "query_repo_id":   query_repo_id,
        "output_path":     str(output_path),
        "submodule_stats": {
            mod: {
                "display":      _MODULE_DISPLAY.get(mod, mod),
                "confirmed":    s["confirmed"],
                "review":       s["review"],
                "weak":         s["weak"],
                "total":        s["total"],
                "copy_pct_pct": round(s["copy_pct"] * 100, 1),
                "top_source":   s["top_source"],
            }
            for mod, s in submodule_stats.items()
            if s["confirmed"] + s["review"] + s["weak"] > 0
        },
        "file_pairs": file_pairs,
    }
    ctx_path = Path(query_repo_path) / _CONTEXT_FILENAME
    ctx_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return ctx_path


def _build_short_request(query_repo_path: str, output_path: str,
                          ctx_path: str) -> str:
    """生成传给 opencode 的精简消息（直接指向绝对路径，用 bash 读写文件）。"""
    ctx_posix = Path(ctx_path).as_posix()
    out_posix  = Path(output_path).as_posix()
    return (
        f"请对 OS 内核新作品进行语义级功能借鉴分析，生成 HTML 对比报告。\n\n"
        f"**不要调用 initialize_analysis**（已知会超时）。直接用 bash 读写文件。\n\n"
        f"步骤：\n"
        f"1. bash: 读取分析上下文（JSON）\n"
        f"   命令示例：python -c \"import json; d=json.load(open(r'{ctx_posix}', encoding='utf-8')); "
        f"print(json.dumps(d['submodule_stats'], ensure_ascii=False, indent=2))\"\n"
        f"2. 对 file_pairs 中每个子模块，分析功能借鉴（语义层面，不只是文本相似）：\n"
        f"   - 借鉴了哪些功能/算法/机制\n"
        f"   - 借鉴程度：直接复制/变量改名/结构保留/受启发重实现\n"
        f"   - 代码证据：引用 文件:行号 格式\n"
        f"3. **必须**调用 write 工具将完整 HTML 写入（不要用 bash echo）：\n"
        f"   output_path: {out_posix}\n\n"
        f"HTML 格式：\n"
        f"- 每个模块一个 <section data-module=\"模块tag\">…</section>\n"
        f"- 用 <h3>模块名</h3><p>分析</p><ul><li>证据</li></ul>\n"
        f"- 文件引用写 path:line 纯文本（如 os/src/task/mod.rs:125）\n"
        f"- 不要写 Markdown，只写 HTML 标签\n\n"
        f"Context 文件：{ctx_posix}"
    )


# ─── 4. 调用 opencode ────────────────────────────────────────────────────────

def _opencode_env() -> dict:
    env = os.environ.copy()
    env["OPENCODE_SESSION_ID"] = uuid.uuid4().hex
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    # 确保 npm 全局 bin 在 PATH 中（Windows）
    npm_bin = str(Path.home() / "AppData" / "Roaming" / "npm")
    if npm_bin not in env.get("PATH", ""):
        sep = ";" if os.name == "nt" else ":"
        env["PATH"] = npm_bin + sep + env.get("PATH", "")
    return env


def _cache_key(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x1f")
    return h.hexdigest()


_ANALYSIS_SYSTEM = """\
你是代码原创性分析助手，专注于语义级（功能层面）的对比分析。

分析 OS 内核新作品的功能借鉴情况，对每个子模块输出 HTML 片段。

分析维度：
1. 功能借鉴：借鉴了哪些算法/机制/数据结构（语义层面，不只是文本相似）
2. 借鉴程度：直接复制 / 变量改名 / 结构保留逻辑改写 / 受启发重新实现
3. 代码证据：引用 文件:行号 格式（如 os/src/task/mod.rs:125）

输出格式（严格遵守）：
- 只输出 HTML 标签，不要输出 Markdown
- 每个子模块用 <section data-module="模块tag">...</section> 包裹
- 用 <h3>/<p>/<ul>/<li> 语义标签
- 文件引用写纯文本 path:line，不要手写 <a> 标签
"""


def _build_analysis_message(
    query_repo_id: str,
    file_pairs: list[dict],
    submodule_stats: dict,
) -> str:
    """构造给 DeepSeek API 的完整分析消息（含代码片段）。"""
    lines = [
        f"请对新作品（{query_repo_id}）进行语义级功能借鉴分析。",
        "",
        "## 子模块统计",
        "",
    ]
    for mod, stats in submodule_stats.items():
        if stats["confirmed"] + stats["review"] + stats["weak"] == 0:
            continue
        disp = _MODULE_DISPLAY.get(mod, mod)
        lines.append(
            f"- **{disp}**（{mod}）：confirmed={stats['confirmed']}，"
            f"review={stats['review']}，weak={stats['weak']}，"
            f"主要来源：{stats['top_source']}"
        )
    lines += ["", "## 相似代码对（按子模块）", ""]

    current_mod = None
    for p in file_pairs:
        mod = p["module"]
        if mod != current_mod:
            lines.append(f"### {_MODULE_DISPLAY.get(mod, mod)} ({mod})")
            current_mod = mod
        tier_label = {"confirmed": "确认借鉴", "review": "疑似借鉴", "weak": "弱相似"}.get(p["tier"], p["tier"])
        lines += [
            f"**新作品** `{p['query_file']}:{p['query_start']}` 函数 `{p['query_func']}` "
            f"← {tier_label}（相似度 {p['sim']}）",
            f"**来源** `{p['ref_repo']}/{p['ref_file']}:{p['ref_start']}` 函数 `{p['ref_func']}`",
            "```",
            p["query_code"],
            "```",
            "参考：",
            "```",
            p["ref_code"],
            "```",
            "",
        ]

    lines += [
        "## 任务",
        "对每个**有相似代码对**的子模块，输出一段语义分析 HTML 片段。",
        "直接输出 HTML，不要输出 Markdown，不要有任何额外说明文字。",
    ]
    return "\n".join(lines)


def run_semantic_analysis(
    query_repo_id: str,
    query_repo_path: str,
    file_pairs: list[dict],
    submodule_stats: dict,
    work_dir: Path,
    timeout: int = 120,
) -> str:
    """直接调用 DeepSeek API 进行语义分析，返回 HTML 片段。

    使用 config.toml 中的 api.key 和 api.base_url，一次 API 调用完成全部子模块分析，
    替代 opencode CLI（opencode MCP 超时不稳定）。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir.resolve() / "semantic_analysis.html"
    cache_dir   = work_dir.resolve() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 缓存
    pair_sig = json.dumps(
        [(p["module"], p["query_func"], p["ref_func"], p["sim"]) for p in file_pairs],
        ensure_ascii=False, sort_keys=True,
    )
    ck = _cache_key(query_repo_id, pair_sig)
    html_cache = cache_dir / f"{ck}.html"

    if html_cache.exists():
        logger.info("[semantic] 缓存命中 → {}", html_cache)
        return html_cache.read_text(encoding="utf-8")

    # 读取 API 配置
    try:
        from oskernel_agent import config as _cfg
        api_key  = _cfg.api.get("key", "").strip()
        base_url = _cfg.api.get("base_url", "https://api.deepseek.com/v1").strip()
    except Exception:
        api_key = base_url = ""

    if not api_key:
        logger.warning("[semantic] 未找到 API key（config.toml），使用规则兜底")
        return _fallback_analysis(file_pairs, submodule_stats)

    user_msg = _build_analysis_message(query_repo_id, file_pairs, submodule_stats)
    logger.info("[semantic] 调用 DeepSeek API 进行语义分析（消息 {} 字符）", len(user_msg))

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": _ANALYSIS_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.3,
            max_tokens=8000,
        )
        html_text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("[semantic] API 调用失败：{}，使用规则兜底", e)
        return _fallback_analysis(file_pairs, submodule_stats)

    # 提取 HTML 片段（模型可能在 markdown 代码块里）
    html_content = _extract_html_from_text(html_text) or html_text

    output_path.write_text(html_content, encoding="utf-8")
    html_cache.write_text(html_content, encoding="utf-8")
    logger.info("[semantic] 分析完成（{} 字符）", len(html_content))
    return html_content


def _extract_html_from_text(text: str) -> str:
    """从 opencode stdout 中提取 HTML 片段（agent 有时直接输出而非写文件）。"""
    import re
    # 尝试匹配 ```html ... ``` 代码块
    m = re.search(r'```html\s*(.*?)```', text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # 尝试匹配直接包含 <section data-module 的 HTML 内容
    m2 = re.search(r'(<section\s+data-module=.*?</section>\s*)+', text, re.DOTALL | re.IGNORECASE)
    if m2:
        return m2.group(0).strip()
    return ""


def _fallback_analysis(file_pairs: list[dict], submodule_stats: dict) -> str:
    """opencode 不可用时的规则兜底分析 HTML。"""
    by_mod: dict[str, list[dict]] = defaultdict(list)
    for p in file_pairs:
        by_mod[p["module"]].append(p)

    parts = ['<div class="fallback-analysis">']
    for mod in MODULES:
        pairs = by_mod.get(mod, [])
        if not pairs:
            continue
        disp = _MODULE_DISPLAY.get(mod, mod)
        stats = submodule_stats.get(mod, {})
        items = "".join(
            f'<li><code>{p["query_file"]}:{p["query_start"]}</code> ↔ '
            f'<code>{p["ref_repo"]}/{p["ref_file"]}:{p["ref_start"]}</code> '
            f'（{p["tier"]}，相似度 {p["sim"]}）</li>'
            for p in pairs
        )
        parts.append(
            f'<section data-module="{html.escape(mod)}">'
            f'<h3>{html.escape(disp)}（{html.escape(mod)}）</h3>'
            f'<p>检测到 {stats.get("confirmed",0)} 个 confirmed 对、'
            f'{stats.get("review",0)} 个 review 对，'
            f'主要来源：{html.escape(stats.get("top_source","—"))}。'
            f'（opencode 未运行，语义分析不可用）</p>'
            f'<ul>{items}</ul>'
            f'</section>'
        )
    parts.append('</div>')
    return "\n".join(parts)


# ─── 5. 生成完整 HTML 报告 ────────────────────────────────────────────────────

def _pct_bar(copy_pct: float) -> str:
    """复制/原创双色进度条（copy 红 + original 绿）。"""
    c = round(copy_pct * 100)
    o = 100 - c
    return (
        f'<div class="pct-bar" title="借鉴 {c}% / 原创 {o}%">'
        f'<div class="pct-copy" style="width:{c}%">{c}%&nbsp;借鉴</div>'
        f'<div class="pct-orig" style="width:{o}%">{o}%&nbsp;原创</div>'
        f'</div>'
    )


def _echarts_overview(submodule_stats: dict) -> str:
    """ECharts 堆叠横向柱图：每个模块的借鉴/原创比例。"""
    mods = [m for m in MODULES if submodule_stats.get(m, {}).get("total", 0) > 0]
    if not mods:
        return ""
    labels = [_MODULE_DISPLAY.get(m, m) for m in mods]
    copy_vals  = [round(submodule_stats[m]["copy_pct"] * 100, 1) for m in mods]
    orig_vals  = [round(submodule_stats[m]["original_pct"] * 100, 1) for m in mods]
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["借鉴", "原创"]},
        "grid": {"left": "25%", "right": "12%", "top": "8%", "bottom": "6%"},
        "xAxis": {"type": "value", "max": 100,
                  "axisLabel": {"formatter": "{value}%"}},
        "yAxis": {"type": "category", "data": labels[::-1]},
        "series": [
            {
                "name": "借鉴",
                "type": "bar", "stack": "pct",
                "data": copy_vals[::-1],
                "itemStyle": {"color": "#ef4444"},
                "label": {"show": True, "formatter": "{c}%"},
            },
            {
                "name": "原创",
                "type": "bar", "stack": "pct",
                "data": orig_vals[::-1],
                "itemStyle": {"color": "#22c55e"},
                "label": {"show": True, "formatter": "{c}%"},
            },
        ],
    }
    height = max(180, len(mods) * 40)
    return (
        f'<div class="echarts-chart mt-4" style="height:{height}px">'
        f'<script type="application/json">{json.dumps(option, ensure_ascii=False)}</script>'
        f'</div>'
    )


def _summary_card(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
) -> str:
    total = len([s for s in suspects if s.get("tier") not in ("dismissed",)])
    confirmed = len([s for s in suspects if s.get("tier") == "confirmed"])
    review    = len([s for s in suspects if s.get("tier") == "review"])
    weak      = len([s for s in suspects if s.get("tier") == "weak"])

    # 加权总借鉴比例
    all_copy = sum(st["copy_pct"] * st["total"] for st in submodule_stats.values())
    all_total = sum(st["total"] for st in submodule_stats.values()) or 1
    overall_copy_pct = round(all_copy / all_total * 100, 1)

    pills = (
        '<div class="flex flex-wrap gap-2 text-sm mt-2">'
        f'<span class="px-3 py-1 rounded-full bg-slate-100">嫌疑对共 {total}</span>'
        f'<span class="px-3 py-1 rounded-full bg-red-100 text-red-700">confirmed {confirmed}</span>'
        f'<span class="px-3 py-1 rounded-full bg-amber-100 text-amber-700">review {review}</span>'
        f'<span class="px-3 py-1 rounded-full bg-slate-200 text-slate-600">weak {weak}</span>'
        f'<span class="px-3 py-1 rounded-full bg-violet-100 text-violet-700">'
        f'整体借鉴估算 {overall_copy_pct}%</span>'
        '</div>'
    )
    chart = _echarts_overview(submodule_stats)

    return (
        '<section id="summary" data-section-id="summary" '
        'class="mb-8 p-6 rounded-lg border border-slate-300 bg-white shadow-sm">'
        '<h2 class="text-lg font-bold text-slate-800 m-0">'
        f'{html.escape(query_repo_id)} '
        '<span class="text-slate-400 font-normal text-base">查重对比分析报告</span>'
        '</h2>'
        f'{pills}{chart}'
        '</section>'
    )


def _module_section(
    mod: str,
    submodule_stats: dict,
    file_pairs: list[dict],
    analysis_html: str,
    resolver,
    idx: int,
) -> tuple[str, str]:
    """返回 (toc_entry_html, section_html)。"""
    from oskernel_agent.reports.html import linkify_html
    stats = submodule_stats.get(mod, {})
    pairs = [p for p in file_pairs if p["module"] == mod]

    if not pairs and stats.get("confirmed", 0) + stats.get("review", 0) == 0:
        return "", ""

    disp  = _MODULE_DISPLAY.get(mod, mod)
    sid   = f"sec-mod-{mod}"
    label = f"{disp} ({mod})"
    copy_pct = stats.get("copy_pct", 0.0)

    # 子模块统计概要行
    stat_row = (
        f'<div class="flex flex-wrap gap-3 text-sm mb-3">'
        f'<span class="px-2 py-0.5 rounded bg-red-50 text-red-700">'
        f'借鉴 {copy_pct*100:.0f}%</span>'
        f'<span class="px-2 py-0.5 rounded bg-green-50 text-green-700">'
        f'原创 {(1-copy_pct)*100:.0f}%</span>'
        f'<span class="text-slate-500">函数总数 {stats.get("total","—")} | '
        f'confirmed {stats.get("confirmed",0)} | review {stats.get("review",0)}</span>'
        f'<span class="text-slate-400">主要来源：{html.escape(stats.get("top_source","—"))}</span>'
        f'</div>'
        + _pct_bar(copy_pct)
    )

    # 相似代码对表格
    if pairs:
        rows = "".join(
            f'<tr>'
            f'<td class="font-mono text-xs">'
            + (
                f'<a class="file-jump" href="{html.escape(_make_link(resolver, p["query_file"], p["query_start"]))}">'
                f'{html.escape(p["query_file"])}:{p["query_start"]}</a>'
                if resolver else
                f'{html.escape(p["query_file"])}:{p["query_start"]}'
            )
            + f'</td>'
            f'<td class="text-xs">{html.escape(p["query_func"])}</td>'
            f'<td class="text-xs text-slate-500">{html.escape(p["ref_repo"])}</td>'
            f'<td class="font-mono text-xs">{html.escape(p["ref_file"])}:{p["ref_start"]}</td>'
            f'<td class="text-xs">{html.escape(p["ref_func"])}</td>'
            f'<td class="text-xs font-semibold '
            + ("text-red-600" if p["sim"] > 0.9 else "text-amber-600" if p["sim"] > 0.7 else "text-slate-500")
            + f'">{p["sim"]}</td>'
            f'<td class="text-xs">'
            + {"confirmed": '<span class="px-1 rounded bg-red-100 text-red-700">confirmed</span>',
               "review":    '<span class="px-1 rounded bg-amber-100 text-amber-700">review</span>',
               "weak":      '<span class="px-1 rounded bg-slate-100 text-slate-600">weak</span>',
               }.get(p["tier"], p["tier"])
            + f'</td>'
            f'</tr>'
            for p in pairs
        )
        table = (
            '<div class="overflow-x-auto mt-3">'
            '<table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">新作品 文件:行</th>'
            '<th class="text-left p-2 border-b">函数</th>'
            '<th class="text-left p-2 border-b">来源仓库</th>'
            '<th class="text-left p-2 border-b">来源 文件:行</th>'
            '<th class="text-left p-2 border-b">来源函数</th>'
            '<th class="text-left p-2 border-b">相似度</th>'
            '<th class="text-left p-2 border-b">档位</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody>'
            '</table></div>'
        )
    else:
        table = ""

    # 语义分析片段（从 opencode 输出中抠取当前模块的部分）
    mod_analysis = _extract_module_analysis(analysis_html, mod)
    if mod_analysis:
        mod_analysis = linkify_html(mod_analysis, resolver)

    body = stat_row + table
    if mod_analysis:
        body += (
            '<div class="mt-4 p-4 bg-blue-50 rounded-lg border border-blue-100">'
            '<h4 class="text-sm font-semibold text-blue-800 mb-2">语义级功能借鉴分析</h4>'
            + mod_analysis +
            '</div>'
        )

    section = (
        f'<section id="{sid}" data-section-id="{sid}" '
        'class="mb-6 rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden" '
        f'x-data="{{open: true}}" '
        f"x-init=\"(function(){{const s=localStorage.getItem('cmp:{sid}');if(s!==null)open=(s==='1');}})()\">"
        f'<div class="px-6 py-3 flex items-center gap-2 cursor-pointer select-none '
        'border-b border-slate-200 bg-slate-50" '
        f"@click=\"open=!open;localStorage.setItem('cmp:{sid}',open?'1':'0')\">"
        '<span class="text-slate-400 w-4 text-center" x-text="open?\'▾\':\'▸\'"></span>'
        f'<h2 class="text-base font-semibold text-slate-800 m-0">{html.escape(label)}</h2>'
        '</div>'
        '<div class="px-6 py-4" x-show="open" x-cloak>'
        + body +
        '</div>'
        '</section>'
    )
    toc = f'<a class="toc-link" href="#{sid}">{html.escape(label)}</a>'
    return toc, section


def _make_link(resolver, file_path: str, line: int) -> str:
    if resolver is None:
        return "#"
    url = resolver(file_path, str(line))
    return url or "#"


def _extract_module_analysis(analysis_html: str, mod: str) -> str:
    """从 opencode 产出的完整 HTML 中，尝试提取该模块的 <section> 片段。"""
    import re
    # 匹配 <section data-module="mod">...</section>
    pattern = re.compile(
        rf'<section[^>]*data-module=["\']?{re.escape(mod)}["\']?[^>]*>.*?</section>',
        re.DOTALL | re.IGNORECASE,
    )
    m = pattern.search(analysis_html)
    if m:
        return m.group(0)
    # 兜底：按 <h3> 标题分割
    disp = _MODULE_DISPLAY.get(mod, mod)
    pattern2 = re.compile(
        rf'(<h3[^>]*>[^<]*{re.escape(mod)}[^<]*</h3>.*?)(?=<h3|$)',
        re.DOTALL | re.IGNORECASE,
    )
    m2 = pattern2.search(analysis_html)
    if m2:
        return m2.group(1)
    disp_pat = re.compile(
        rf'(<h3[^>]*>[^<]*{re.escape(disp)}[^<]*</h3>.*?)(?=<h3|$)',
        re.DOTALL | re.IGNORECASE,
    )
    m3 = disp_pat.search(analysis_html)
    if m3:
        return m3.group(1)
    return ""


def _original_section(original_funcs: list[dict], resolver) -> tuple[str, str]:
    """原创代码章节。"""
    from oskernel_agent.reports.html import linkify_html
    sid = "sec-original"
    if not original_funcs:
        body = "<p class='text-slate-500 text-sm'>未发现明显原创函数（与历史库相似度 &lt; 0.5 且行数 &gt; 20）。</p>"
    else:
        rows = "".join(
            f'<tr>'
            f'<td class="text-xs">{html.escape(f["func"])}</td>'
            f'<td class="font-mono text-xs">'
            + (
                f'<a class="file-jump" href="{html.escape(_make_link(resolver, f["file"], f["start"]))}">'
                f'{html.escape(f["file"])}:{f["start"]}</a>'
                if resolver else
                f'{html.escape(f["file"])}:{f["start"]}'
            )
            + f'</td>'
            f'<td class="text-xs text-slate-500">{html.escape(_MODULE_DISPLAY.get(f["module"], f["module"]))}</td>'
            f'<td class="text-xs">{f["max_sim"]}</td>'
            f'<td class="text-xs">{f["lines"]}</td>'
            f'</tr>'
            for f in original_funcs
        )
        body = (
            '<p class="text-sm text-slate-600 mb-3">'
            '以下函数与历史代码库相似度低（&lt; 0.5）且规模较大（&gt; 20 行），为疑似原创实现：'
            '</p>'
            '<div class="overflow-x-auto">'
            '<table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">函数</th>'
            '<th class="text-left p-2 border-b">文件:行</th>'
            '<th class="text-left p-2 border-b">模块</th>'
            '<th class="text-left p-2 border-b">最高相似度</th>'
            '<th class="text-left p-2 border-b">行数</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody>'
            '</table></div>'
        )
    section = (
        f'<section id="{sid}" data-section-id="{sid}" '
        'class="mb-6 rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden" '
        f'x-data="{{open: true}}" '
        f"x-init=\"(function(){{const s=localStorage.getItem('cmp:{sid}');if(s!==null)open=(s==='1');}})()\">"
        '<div class="px-6 py-3 flex items-center gap-2 cursor-pointer select-none '
        'border-b border-slate-200 bg-slate-50" '
        f"@click=\"open=!open;localStorage.setItem('cmp:{sid}',open?'1':'0')\">"
        '<span class="text-slate-400 w-4 text-center" x-text="open?\'▾\':\'▸\'"></span>'
        '<h2 class="text-base font-semibold text-slate-800 m-0">原创代码</h2>'
        '</div>'
        '<div class="px-6 py-4" x-show="open" x-cloak>'
        + body +
        '</div>'
        '</section>'
    )
    toc = f'<a class="toc-link" href="#{sid}">原创代码</a>'
    return toc, section


_CDN_HEAD = """
<script src="https://cdn.tailwindcss.com?plugins=typography"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
"""

_STYLES = """
<style>
[x-cloak]{display:none!important}
body{background:#f6f8fa}
.layout{display:flex;gap:1.5rem;max-width:1360px;margin:0 auto;padding:1.5rem}
.toc{position:sticky;top:1.5rem;align-self:flex-start;width:220px;flex-shrink:0;font-size:.85rem}
.toc .toc-link{display:block;padding:.3rem .5rem;border-left:2px solid transparent;
  color:#475569;text-decoration:none;border-radius:0 .3rem .3rem 0}
.toc .toc-link:hover{background:#eef2f7}
.toc .toc-active{border-left-color:#4a90d9;color:#1a66d4;font-weight:600;background:#eef2fb}
.main{flex:1;min-width:0}
.file-jump{color:#1a66d4;text-decoration:underline dotted}
.pct-bar{display:flex;height:20px;border-radius:4px;overflow:hidden;margin:6px 0}
.pct-copy{background:#ef4444;color:#fff;font-size:11px;
  display:flex;align-items:center;padding:0 6px;min-width:0;white-space:nowrap}
.pct-orig{background:#22c55e;color:#fff;font-size:11px;
  display:flex;align-items:center;padding:0 6px;min-width:0;white-space:nowrap}
.pct-bar div:only-child{border-radius:4px}
table{border-collapse:collapse}
td,th{padding:.4rem .6rem;border-bottom:1px solid #e2e8f0;vertical-align:top}
@media(max-width:860px){.layout{flex-direction:column}.toc{position:static;width:auto}}
</style>
"""

_INIT_SCRIPT = """
<script>
(function(){
  function initECharts(){
    if(typeof echarts==='undefined'){setTimeout(initECharts,100);return}
    document.querySelectorAll('.echarts-chart:not([data-rendered])').forEach(function(el){
      var d=el.querySelector('script[type="application/json"]');
      if(!d)return;
      try{
        var opt=JSON.parse(d.textContent);
        if(!el.style.height)el.style.height='320px';
        var c=echarts.init(el,null,{renderer:'canvas'});
        c.setOption(opt);el.setAttribute('data-rendered','1');el.__chart=c;
        window.addEventListener('resize',function(){c.resize()});
      }catch(e){el.innerHTML='<p class="text-red-500 text-sm p-2">ECharts 配置解析失败: '+e.message+'</p>'}
    });
  }
  function initScrollSpy(){
    var links=document.querySelectorAll('.toc-link');
    var secs=document.querySelectorAll('[data-section-id]');
    if(!('IntersectionObserver' in window)||!links.length||!secs.length)return;
    var byId={};
    links.forEach(function(a){var id=a.getAttribute('href');if(id)byId[id.replace(/^#/,'')]=a});
    var obs=new IntersectionObserver(function(entries){
      entries.forEach(function(e){
        if(e.isIntersecting){
          var id=e.target.getAttribute('data-section-id');
          Object.keys(byId).forEach(function(k){byId[k].classList.remove('toc-active')});
          if(byId[id])byId[id].classList.add('toc-active');
        }
      });
    },{rootMargin:'-20% 0px -70% 0px',threshold:0});
    secs.forEach(function(s){obs.observe(s)});
  }
  document.addEventListener('DOMContentLoaded',function(){initECharts();initScrollSpy()});
  document.addEventListener('section:opened',function(){setTimeout(initECharts,30)});
})();
</script>
"""


def generate_comparison_html(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_pairs: list[dict],
    analysis_html: str,
    original_funcs: list[dict],
    query_repo_path: Path | None = None,
) -> str:
    """组装完整的查重对比 HTML 报告（直接产出，不经 Markdown 转换）。"""
    from oskernel_agent.reports.html import make_file_link_resolver
    resolver = make_file_link_resolver(
        [query_repo_path] if query_repo_path else None,
        scheme="vscode",
    )

    # 摘要卡
    summary_html = _summary_card(query_repo_id, suspects, submodule_stats)

    toc_items  = ['<a class="toc-link" href="#summary">总览</a>']
    body_parts = [summary_html]

    # 各子模块章节
    for idx, mod in enumerate(MODULES):
        toc_entry, section = _module_section(
            mod, submodule_stats, file_pairs, analysis_html, resolver, idx)
        if toc_entry:
            toc_items.append(toc_entry)
            body_parts.append(section)

    # 原创代码章节
    toc_orig, sec_orig = _original_section(original_funcs, resolver)
    toc_items.append(toc_orig)
    body_parts.append(sec_orig)

    toc_html  = "\n".join(toc_items)
    main_html = "\n".join(body_parts)
    title     = f"{html.escape(query_repo_id)} 查重对比分析报告"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{_CDN_HEAD}
{_STYLES}
</head>
<body>
<div class="layout">
  <nav class="toc">{toc_html}</nav>
  <main class="main">
    <h1 class="text-2xl font-bold text-slate-800 mb-6 border-b-2 border-blue-400 pb-2">
      {title}
    </h1>
    {main_html}
  </main>
</div>
{_INIT_SCRIPT}
</body>
</html>
"""


# ─── 6. 主入口 ────────────────────────────────────────────────────────────────

def run_semantic_compare(
    suspects_path: str | Path,
    query_repo_path: str | Path | None = None,
    recall_path: str | Path | None = None,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    top_per_module: int = 5,
    skip_opencode: bool = False,
) -> dict:
    """主入口：suspects.json → opencode 分析 → 直接 HTML 报告。

    Args:
        suspects_path:   exact 阶段产出的 *_suspects.json 路径
        query_repo_path: 新作品本地克隆路径（用于文件链接 + opencode 初始化）
        recall_path:     embed 阶段产出的 *_recall.json（用于计算函数总数 / 原创函数）
        output_dir:      HTML 输出目录
        top_per_module:  每个子模块送入 opencode 的最大代码对数
        skip_opencode:   True 时跳过 opencode，仅用规则生成报告（调试用）
    """
    suspects_path = Path(suspects_path)
    data          = json.loads(suspects_path.read_text(encoding="utf-8"))
    suspects      = data.get("suspects", [])
    query_repo_id = data.get("query_repo_id") or suspects_path.stem.split("_suspects")[0]

    recall: dict | None = None
    if recall_path and Path(recall_path).exists():
        recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))

    logger.info("[compare] 新作品 {}：{} 个嫌疑对", query_repo_id, len(suspects))

    # 统计
    submodule_stats = compute_submodule_stats(suspects, recall)
    file_pairs      = collect_file_pairs(suspects, top_per_module=top_per_module)

    # 原创候选函数
    original_funcs  = _original_functions(recall, suspects) if recall else []

    # opencode 语义分析
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / f"{query_repo_id}_semantic_work"

    if skip_opencode or not file_pairs:
        analysis_html = _fallback_analysis(file_pairs, submodule_stats)
    else:
        qpath = str(Path(query_repo_path).resolve()) if query_repo_path else ""
        analysis_html = run_semantic_analysis(
            query_repo_id, qpath, file_pairs, submodule_stats, work_dir
        )

    # 生成 HTML
    html_text = generate_comparison_html(
        query_repo_id   = query_repo_id,
        suspects        = suspects,
        submodule_stats = submodule_stats,
        file_pairs      = file_pairs,
        analysis_html   = analysis_html,
        original_funcs  = original_funcs,
        query_repo_path = Path(query_repo_path).resolve() if query_repo_path else None,
    )

    safe_id  = query_repo_id.replace("/", "_")
    out_path = out_dir / f"{safe_id}_comparison.html"
    out_path.write_text(html_text, encoding="utf-8")
    logger.info("[compare] HTML 报告 → {}", out_path)

    return {
        "html_path":        str(out_path),
        "query_repo_id":    query_repo_id,
        "total_suspects":   len(suspects),
        "submodule_stats":  submodule_stats,
        "original_funcs":   len(original_funcs),
    }
