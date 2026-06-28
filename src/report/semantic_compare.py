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

from src.fastpath.scan import aggregate_file_similarity

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

# 展示层 tier 命名映射（U5）：内部 tier 值保持 confirmed/review/weak 不变（不动 JSON/测试/
# 多模块逻辑），仅在报告里把 review 显示为 needReview，更贴合「需人工复核」语义。
_TIER_DISPLAY = {
    "confirmed": "已确认借鉴",
    "review":    "needReview（待复核）",
    "weak":      "弱相似",
}
# clone_type 展示名：按「逐字相同行占比」描述相似程度，中性命名，不臆断「改名」动机。
_CLONE_TYPE_DISPLAY = {
    "exact":    "完全相同",
    "near_dup": "近乎相同",
    "similar":  "高度相似",
    # 兼容旧缓存/旧数据里的取值
    "renamed":  "近乎相同",
    "near":     "高度相似",
    "—":        "—",
}


def _tier_disp(tier: str) -> str:
    return _TIER_DISPLAY.get(tier, tier)


def _clone_kind(pair_or_suspect: dict) -> str:
    """按「逐字相同行占已匹配行的比例」判克隆程度（中性命名，不臆断改名动机）：

      完全相同(exact)：全部匹配行逐字一致；
      近乎相同(near_dup)：仅零星行需归一化才匹配（≤15%，如个别变量/常量/注释不同，
                          也覆盖「只差一两行」的情形——不再误标为「改名复制」）；
      高度相似(similar)：较多行需归一化才匹配，或仅结构相似。

    旧逻辑「只要有一行 renamed 就整函数标改名复制」非黑即白，导致 99% 逐字相同、
    仅改一行也被标改名复制；改为按比例判定后更贴合实际。
    """
    ev = pair_or_suspect.get("evidence") or {}
    e = ev.get("exact_match_lines") or 0
    r = ev.get("renamed_match_lines") or 0
    total = e + r
    if total == 0:  # 无行级证据，退回 span 类型
        types = pair_or_suspect.get("match_type_per_span") or []
        if "exact" in types and "renamed" not in types:
            return "exact"
        if "renamed" in types:
            return "near_dup"
        return "similar"
    ratio = r / total
    if ratio == 0:
        return "exact"
    if ratio <= 0.15:
        return "near_dup"
    return "similar"


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
        # total 优先用 recall 统计；无 recall 时用嫌疑对数量兜底。真正无任何函数的模块 total=0
        # （不再虚构为 1），使其从概览图中自然排除——否则空模块会虚显为「100% 原创」的假条目。
        total = (module_totals.get(mod, 0)
                 or (data["confirmed"] + data["review"] + data["weak"]))
        # 借鉴比例只按「已确认借鉴」(confirmed) 计：review(待复核)/weak(弱相似) 属不确定项，
        # 报告已不展示，也不计入借鉴估算，避免不确定信号拉高比例。
        copy_score = data["confirmed"]
        copy_pct = min(1.0, copy_score / total) if total else 0.0
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


# ─── 2. 收集代码对（按 query 函数聚合全部候选） ──────────────────────────────

_TIER_RANK = {"confirmed": 3, "review": 2, "weak": 1}


def _pair_sim(s: dict) -> float:
    """单对相似度，带 D1 兜底：final_score==0 但有精确/改名匹配行时，
    按 (匹配行数 / query 函数行数) 估算，杜绝「行级相同却显示 0%」。
    """
    sim = float(s.get("final_score") or 0.0)
    if sim <= 0.0:
        ev = s.get("evidence") or {}
        matched = (ev.get("exact_match_lines") or 0) + (ev.get("renamed_match_lines") or 0)
        if matched > 0:
            q = s.get("query_func", {})
            qlines = max(1, (q.get("end_line", 0) or 0) - (q.get("start_line", 0) or 0) + 1)
            sim = min(1.0, matched / qlines)
    return round(sim, 3)


def collect_file_pairs(
    suspects: list[dict], top_per_module: int = 5, max_candidates: int = 4,
) -> list[dict]:
    """按 query 函数聚合候选（U6）：每个 query 函数列出其全部候选 + 各自相似度，并给
    「整体相似度」(= 最强候选)。每模块取整体相似度最高的 top_per_module 个函数组。

    返回 [{module, query_func, query_file, query_start/end, query_code, overall_sim,
           overall_tier, clone_type, candidates:[{tier,sim,clone_type,ref_*}, ...]}, ...]
    """
    groups: dict[tuple, dict] = {}
    for s in suspects:
        tier = s.get("tier", "")
        if tier in ("dismissed", "baseline_derived", "common_code"):
            continue
        q = s.get("query_func", {})
        c = s.get("candidate_func", {})
        mod = q.get("module_tag", "other")
        if mod not in _MODULE_DISPLAY:
            mod = "other"
        key = (mod, q.get("file_path", ""), q.get("func_name", ""), q.get("start_line", 0))
        g = groups.get(key)
        if g is None:
            g = {
                "module":      mod,
                "query_func":  q.get("func_name", ""),
                "query_file":  q.get("file_path", ""),
                "query_start": q.get("start_line", 0),
                "query_end":   q.get("end_line", 0),
                "query_code":  (q.get("raw_code") or "")[:800],
                "candidates":  [],
            }
            groups[key] = g
        g["candidates"].append({
            "tier":       tier,
            "sim":        _pair_sim(s),
            "clone_type": _clone_kind(s),
            "ref_func":   c.get("func_name", ""),
            "ref_file":   c.get("file_path", ""),
            "ref_repo":   c.get("repo_id", ""),
            "ref_start":  c.get("start_line", 0),
            "ref_end":    c.get("end_line", 0),
            "ref_code":   (c.get("raw_code") or "")[:800],
        })

    for g in groups.values():
        cands = g["candidates"]
        g["overall_tier"] = max((c["tier"] for c in cands),
                                key=lambda t: _TIER_RANK.get(t, 0), default="weak")
        cands.sort(key=lambda x: -x["sim"])
        g["overall_sim"] = cands[0]["sim"] if cands else 0.0
        g["clone_type"] = cands[0]["clone_type"] if cands else "—"
        g["candidate_count"] = len(cands)
        g["candidates"] = cands[:max_candidates]   # 限制展示候选数，避免报告过长

    by_module: dict[str, list[dict]] = defaultdict(list)
    for g in groups.values():
        by_module[g["module"]].append(g)
    result = []
    for mod in MODULES:
        gs = sorted(by_module.get(mod, []), key=lambda x: -x["overall_sim"])
        # 只保留「已确认借鉴」(confirmed) 函数组，全部展示；review(待复核)/weak(弱相似)
        # 属不确定项，按需求不纳入报告。
        result.extend(g for g in gs if g["overall_tier"] == "confirmed")
    return result


def _limit_per_module(groups: list[dict], n: int) -> list[dict]:
    """每模块取整体相似度最高的 n 个 group（送 LLM 控 token，不影响表格全量展示）。"""
    by_mod: dict[str, list[dict]] = defaultdict(list)
    for g in groups:
        by_mod[g["module"]].append(g)
    out: list[dict] = []
    for mod in MODULES:
        gs = sorted(by_mod.get(mod, []), key=lambda x: -x["overall_sim"])
        out.extend(gs[:n])
    return out


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
你是 OS 内核代码原创性分析助手，专注于语义级（功能层面）的对比分析，面向评审人员。

对每个子模块输出一段信息充分、可追溯的 HTML 分析片段。

分析维度（每条结论尽量覆盖）：
1. 借鉴对象：借鉴了哪些**算法**（如调度策略、分配器、置换算法）、**数据结构**
   （如页表、inode、就绪队列）、**机制**（如 trap 上下文保存/恢复、锁、缓存）。
2. 借鉴程度（四档，必须明确给出其一）：
   - 完全相同（逐行逐字一致，仅空格/注释差异）
   - 近乎相同（仅个别变量/常量/少量行不同）
   - 结构相似（控制流一致、表达式改写）
   - 受启发重新实现（思路相近、实现独立）
3. 设计差异：新作品相对来源做了哪些改动/取舍（如换数据结构、改并发策略、增删功能）。

写作要求：
- 每个子模块用 2~4 句概述 + 一个 <ul> 列举具体借鉴点。
- 用函数名、算法名、数据结构名指代具体对象（如「run_tasks 的任务切换」「buddy 分配器」）。
- **不要写文件路径和行号**：报告表格已逐函数给出 文件:行 与可点击链接，分析正文只讲
  「借鉴了什么功能、借鉴到什么程度、做了哪些改动」，专注语义，不重复罗列地址。
- 用词中性专业：用「借鉴/复制/相似」，不要用「抄袭」等定性指控词。

输出格式（严格遵守）：
- 只输出 HTML 标签，不要输出 Markdown
- 每个子模块用 <section data-module="模块tag">...</section> 包裹
- 用 <h3>/<p>/<ul>/<li> 语义标签
- 不要写 path:line，不要手写 <a> 标签
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
        if stats["confirmed"] == 0:
            continue
        disp = _MODULE_DISPLAY.get(mod, mod)
        lines.append(
            f"- **{disp}**（{mod}）：已确认借鉴 {stats['confirmed']} 个函数，"
            f"主要来源：{stats['top_source']}"
        )
    lines += ["", "## 相似代码对（按子模块、按 query 函数聚合全部候选）", ""]

    current_mod = None
    for g in file_pairs:
        mod = g["module"]
        if mod != current_mod:
            lines.append(f"### {_MODULE_DISPLAY.get(mod, mod)} ({mod})")
            current_mod = mod
        tier_label = {"confirmed": "已确认借鉴", "review": "needReview", "weak": "弱相似"}.get(
            g["overall_tier"], g["overall_tier"])
        lines.append(
            f"**新作品** `{g['query_file']}:{g['query_start']}` 函数 `{g['query_func']}` "
            f"← {tier_label}（整体相似度 {g['overall_sim']}，{g['candidate_count']} 个候选来源）"
        )
        for c in g["candidates"]:
            ck = _CLONE_TYPE_DISPLAY.get(c["clone_type"], c["clone_type"])
            lines.append(
                f"  - 来源 `{c['ref_repo']}/{c['ref_file']}:{c['ref_start']}` 函数 "
                f"`{c['ref_func']}`（相似度 {c['sim']}，{ck}）"
            )
        best = g["candidates"][0] if g["candidates"] else {}
        lines += [
            "新作品代码：", "```", g["query_code"], "```",
            "最强候选来源代码：", "```", best.get("ref_code", ""), "```", "",
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
        [(g["module"], g["query_func"], g["overall_sim"],
          [c["ref_func"] for c in g["candidates"]]) for g in file_pairs],
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
            model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
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
    """LLM 不可用时的规则兜底分析 HTML（按 query 函数聚合，列出全部候选）。"""
    by_mod: dict[str, list[dict]] = defaultdict(list)
    for g in file_pairs:
        by_mod[g["module"]].append(g)

    parts = ['<div class="fallback-analysis">']
    for mod in MODULES:
        groups = by_mod.get(mod, [])
        if not groups:
            continue
        disp = _MODULE_DISPLAY.get(mod, mod)
        stats = submodule_stats.get(mod, {})
        items = []
        for g in groups:
            cands = "；".join(
                f'{c["ref_repo"]}/{c["ref_file"]}:{c["ref_start"]}'
                f'（相似度 {c["sim"]}，{_CLONE_TYPE_DISPLAY.get(c["clone_type"], c["clone_type"])}）'
                for c in g["candidates"]
            )
            items.append(
                f'<li><code>{html.escape(g["query_file"])}:{g["query_start"]}</code> '
                f'函数 <code>{html.escape(g["query_func"])}</code>'
                f'（{_tier_disp(g["overall_tier"])}，整体相似度 {g["overall_sim"]}）'
                f'<br><span class="text-slate-500">候选来源：{html.escape(cands)}</span></li>'
            )
        parts.append(
            f'<section data-module="{html.escape(mod)}">'
            f'<h3>{html.escape(disp)}（{html.escape(mod)}）</h3>'
            f'<p>检测到已确认借鉴 {stats.get("confirmed",0)} 个函数，'
            f'主要来源：{html.escape(stats.get("top_source","—"))}。'
            f'（未启用 LLM 语义分析，以下为规则汇总）</p>'
            f'<ul>{"".join(items)}</ul>'
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


def _echarts_tier_distribution(submodule_stats: dict) -> str:
    """ECharts 横向柱图：各模块「已确认借鉴」函数数（review/weak 不确定项已剔除）。"""
    mods = [m for m in MODULES
            if submodule_stats.get(m, {}).get("confirmed", 0) > 0]
    if not mods:
        return ""
    labels = [_MODULE_DISPLAY.get(m, m) for m in mods]
    conf = [submodule_stats[m]["confirmed"] for m in mods]
    series = [
        ("已确认借鉴", conf, "#ef4444"),
    ]
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": [s[0] for s in series]},
        "grid": {"left": "25%", "right": "8%", "top": "12%", "bottom": "6%"},
        "xAxis": {"type": "value", "name": "函数数", "minInterval": 1},
        "yAxis": {"type": "category", "data": labels[::-1]},
        "series": [
            {"name": name, "type": "bar", "stack": "tier", "data": vals[::-1],
             "itemStyle": {"color": color},
             "label": {"show": True, "formatter": "{c}"}}
            for name, vals, color in series
        ],
    }
    height = max(180, len(mods) * 44)
    return (
        f'<div class="echarts-chart mt-4" style="height:{height}px">'
        f'<script type="application/json">{json.dumps(option, ensure_ascii=False)}</script>'
        f'</div>'
    )


def _echarts_overall_donut(copy_pct: float) -> str:
    """整体借鉴 vs 原创环形图（按函数加权），中心标注借鉴百分比。"""
    c = round(copy_pct, 1)
    o = round(max(0.0, 100 - c), 1)
    option = {
        "title": {
            "text": f"{c}%", "subtext": "整体借鉴(按函数加权)",
            "left": "center", "top": "38%",
            "textAlign": "center",
            "textStyle": {"fontSize": 26, "fontWeight": "bold", "color": "#ef4444"},
            "subtextStyle": {"fontSize": 11, "color": "#64748b"},
        },
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}%"},
        "legend": {"bottom": 0, "data": ["借鉴", "原创"]},
        "series": [{
            "name": "占比", "type": "pie", "radius": ["54%", "78%"],
            "center": ["50%", "44%"], "avoidLabelOverlap": False,
            "label": {"show": False}, "labelLine": {"show": False},
            "data": [
                {"value": c, "name": "借鉴", "itemStyle": {"color": "#ef4444"}},
                {"value": o, "name": "原创", "itemStyle": {"color": "#22c55e"}},
            ],
        }],
    }
    return (
        '<div class="echarts-chart" style="height:230px">'
        f'<script type="application/json">{json.dumps(option, ensure_ascii=False)}</script>'
        '</div>'
    )


def _echarts_top_sources(suspects: list[dict], top: int = 8) -> str:
    """Top 借鉴来源仓库柱图：各历史仓库被「已确认借鉴」命中的对数（review/weak 已剔除）。"""
    agg: dict[str, dict] = defaultdict(lambda: {"confirmed": 0})
    for s in suspects:
        if s.get("tier") != "confirmed":
            continue
        repo = s.get("candidate_func", {}).get("repo_id", "?")
        agg[repo]["confirmed"] += 1
    if not agg:
        return ""
    order = sorted(agg.items(), key=lambda kv: -kv[1]["confirmed"])[:top]
    labels = [k for k, _ in order][::-1]
    conf = [v["confirmed"] for _, v in order][::-1]
    series = [
        ("已确认借鉴", conf, "#ef4444"),
    ]
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": [s[0] for s in series]},
        "grid": {"left": "33%", "right": "8%", "top": "14%", "bottom": "6%"},
        "xAxis": {"type": "value", "name": "命中对数", "minInterval": 1},
        "yAxis": {"type": "category", "data": labels,
                  "axisLabel": {"fontSize": 10, "width": 150, "overflow": "truncate"}},
        "series": [
            {"name": name, "type": "bar", "stack": "src", "data": vals,
             "itemStyle": {"color": color},
             "label": {"show": True, "formatter": "{c}"}}
            for name, vals, color in series
        ],
    }
    height = max(180, len(order) * 38)
    return (
        f'<div class="echarts-chart mt-4" style="height:{height}px">'
        f'<script type="application/json">{json.dumps(option, ensure_ascii=False)}</script>'
        '</div>'
    )


_LEGEND_HTML = (
    '<div class="legend">'
    '<span><b>档位：</b></span>'
    '<span><span class="dot" style="background:#ef4444"></span>已确认借鉴（confirmed，证据充分）</span>'
    '<span style="margin-left:.6rem"><b>复制类型：</b></span>'
    '<span>完全相同（逐行逐字一致）</span>'
    '<span>近乎相同（仅零星行不同，≤15%）</span>'
    '<span>高度相似（较多行需归一化才匹配）</span>'
    '</div>'
)


def _kpi(value, label: str, color: str = "#0f172a") -> str:
    return (f'<div class="kpi"><span class="v" style="color:{color}">{value}</span>'
            f'<span class="l">{html.escape(label)}</span></div>')


def _summary_card(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_match_count: int = 0,
    file_similar_count: int = 0,
) -> str:
    confirmed = len([s for s in suspects if s.get("tier") == "confirmed"])

    # 加权总借鉴比例（只按已确认借鉴）
    all_copy = sum(st["copy_pct"] * st["total"] for st in submodule_stats.values())
    all_total = sum(st["total"] for st in submodule_stats.values()) or 1
    overall_copy_pct = round(all_copy / all_total * 100, 1)

    # KPI 卡片（只统计已确认借鉴；review/weak 不确定项已剔除）
    kpis = (
        '<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3">'
        + _kpi(f"{confirmed}", "已确认借鉴（对）", "#ef4444")
        + _kpi(f"{file_match_count}", "整文件相同（文件）", "#e11d48")
        + _kpi(f"{file_similar_count}", "整体相似文件（个）", "#d97706")
        + '</div>'
    )

    # 头部：环形图（整体借鉴%）+ Top 借鉴来源仓库，左右并排
    donut = _echarts_overall_donut(overall_copy_pct)
    top_src = _echarts_top_sources(suspects)
    head_charts = (
        '<div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4 items-start">'
        '<div><div class="text-sm font-semibold text-slate-700">整体借鉴估算（按函数加权）</div>'
        + donut + '</div>'
        + ('<div><div class="text-sm font-semibold text-slate-700">主要借鉴来源仓库（Top 8，按命中对数）</div>'
           + top_src + '</div>' if top_src else '<div></div>')
        + '</div>'
    )

    tier_chart = (
        '<div class="mt-4 text-sm font-semibold text-slate-700">各模块已确认借鉴函数数</div>'
        + _echarts_tier_distribution(submodule_stats)
    )
    pct_chart = (
        '<div class="mt-4 text-sm font-semibold text-slate-700">各模块借鉴 vs 原创占比</div>'
        + _echarts_overview(submodule_stats)
    )

    return (
        '<section id="summary" data-section-id="summary" '
        'class="mb-8 p-6 rounded-lg border border-slate-300 bg-white shadow-sm">'
        '<h2 class="text-lg font-bold text-slate-800 m-0">'
        f'{html.escape(query_repo_id)} '
        '<span class="text-slate-400 font-normal text-base">查重对比分析报告</span>'
        '</h2>'
        f'{_LEGEND_HTML}{kpis}{head_charts}{tier_chart}{pct_chart}'
        '</section>'
    )


def _ref_repo_anchor(linker, ref_repo: str) -> str:
    """来源仓库列：生成指向该仓库 GitLab 首页的链接。"""
    if linker is None or not ref_repo:
        return html.escape(ref_repo)
    try:
        url_map = getattr(linker, "url_map", {})
        repo_url = url_map.get(ref_repo)
        if repo_url:
            return (f'<a class="file-jump" href="{html.escape(repo_url)}" '
                    f'target="_blank">{html.escape(ref_repo)}</a>')
    except Exception:
        pass
    return html.escape(ref_repo)


def _sim_class(sim: float) -> str:
    return "text-red-600" if sim > 0.9 else "text-amber-600" if sim > 0.7 else "text-slate-500"


def _candidates_cell(group: dict, linker) -> str:
    """单个 query 函数的全部候选来源（U6）：每个候选一行，含来源链接/相似度/复制类型。"""
    extra = group.get("candidate_count", len(group["candidates"])) - len(group["candidates"])
    items = []
    for c in group["candidates"]:
        ck = _CLONE_TYPE_DISPLAY.get(c["clone_type"], c["clone_type"])
        items.append(
            '<li class="leading-5">'
            + _ref_repo_anchor(linker, c["ref_repo"]) + ' '
            + _make_gitlab_anchor(linker, c["ref_repo"], c["ref_file"], c["ref_start"])
            + f' <span class="{_sim_class(c["sim"])} font-semibold">相似度 {c["sim"]}</span>'
            + f' <span class="text-slate-400">{html.escape(ck)}</span>'
            '</li>'
        )
    more = f'<li class="text-slate-400">…另有 {extra} 个候选</li>' if extra > 0 else ""
    return f'<ul class="text-xs list-disc pl-4 space-y-0.5">{"".join(items)}{more}</ul>'


def _diff_cols(left_code: str, right_code: str) -> tuple[str, str]:
    """逐行对齐高亮：返回 (新作品列 HTML, 来源列 HTML)，差异行加底色。

    用 difflib 对齐两段代码，相同行普通显示，差异行高亮——直接回应「$f0 vs $f3
    要看得见差异」：寄存器不同的那一行会被标黄/标红，评审一眼可辨「改名复制」。
    """
    import difflib
    ql = left_code.split("\n")
    rl = right_code.split("\n")
    sm = difflib.SequenceMatcher(None, ql, rl, autojunk=False)
    left: list[tuple[str, bool]] = []
    right: list[tuple[str, bool]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        same = tag == "equal"
        lseg = [(ql[k], not same) for k in range(i1, i2)]
        rseg = [(rl[k], not same) for k in range(j1, j2)]
        # 补齐两列行数，保持视觉对齐
        while len(lseg) < len(rseg):
            lseg.append(("", True))
        while len(rseg) < len(lseg):
            rseg.append(("", True))
        left.extend(lseg)
        right.extend(rseg)

    def fmt(rows: list[tuple[str, bool]]) -> str:
        return "".join(
            f'<div class="cl{" df" if ch else ""}">{html.escape(t) if t else "&nbsp;"}</div>'
            for t, ch in rows
        )
    return fmt(left), fmt(right)


def _code_evidence(group: dict) -> tuple[str, str]:
    """返回 (toggle_html, panel_row_html)：可折叠的「新作品 vs 最强候选来源」并排代码。

    数据已在 collect_file_pairs 收集（query_code/各候选 ref_code），此前只送给 LLM、
    从不渲染——评审看不到证据。这里把最强候选的代码并排展示、差异行高亮。
    """
    q = group.get("query_code") or ""
    best = group["candidates"][0] if group.get("candidates") else {}
    r = best.get("ref_code") or ""
    if not q.strip() and not r.strip():
        return "", ""
    left_html, right_html = _diff_cols(q, r)
    toggle = (
        '<button type="button" class="code-toggle text-xs text-blue-600 hover:underline" '
        '@click="o=!o" x-text="o ? \'收起代码 ▴\' : \'查看代码 ▾\'">查看代码 ▾</button>'
    )
    src_label = html.escape(f'{best.get("ref_repo","")}/{best.get("ref_file","")}:{best.get("ref_start","")}')
    panel = (
        '<tr x-show="o" x-cloak><td colspan="6" class="p-0">'
        '<div class="code-pair">'
        '<div class="code-col"><div class="code-h">新作品（差异行标黄）</div>'
        f'<div class="code-body">{left_html}</div></div>'
        f'<div class="code-col"><div class="code-h">最强候选来源 · {src_label}（差异行标红）</div>'
        f'<div class="code-body">{right_html}</div></div>'
        '</div></td></tr>'
    )
    return toggle, panel


def _groups_table(title: str, groups: list[dict], linker, query_repo_id: str, accent: str) -> str:
    """渲染一张「按 query 函数聚合候选」的清单表（U3 分类清单 + U6 全候选 + 代码证据）。"""
    if not groups:
        return ""
    bodies = []
    for g in groups:
        toggle, panel = _code_evidence(g)
        main = (
            '<tr>'
            '<td class="font-mono text-xs align-top">'
            + _make_gitlab_anchor(linker, query_repo_id, g["query_file"], g["query_start"])
            + '</td>'
            f'<td class="text-xs align-top">{html.escape(g["query_func"])}</td>'
            f'<td class="text-xs align-top font-semibold {_sim_class(g["overall_sim"])}">{g["overall_sim"]}</td>'
            f'<td class="text-xs align-top">{html.escape(_CLONE_TYPE_DISPLAY.get(g["clone_type"], g["clone_type"]))}</td>'
            '<td class="align-top">' + _candidates_cell(g, linker) + '</td>'
            f'<td class="text-xs align-top whitespace-nowrap">{toggle}</td>'
            '</tr>'
        )
        bodies.append(
            f'<tbody x-data="{{o:false}}" class="border-b border-slate-100">{main}{panel}</tbody>'
        )
    return (
        f'<div class="mt-3"><div class="text-sm font-semibold {accent} mb-1">{html.escape(title)}'
        f'（{len(groups)} 个函数）</div>'
        '<div class="overflow-x-auto">'
        '<table class="w-full text-sm border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">新作品 文件:行</th>'
        '<th class="text-left p-2 border-b">函数</th>'
        '<th class="text-left p-2 border-b">整体相似度</th>'
        '<th class="text-left p-2 border-b">复制类型</th>'
        '<th class="text-left p-2 border-b">候选来源（全部）</th>'
        '<th class="text-left p-2 border-b">代码证据</th>'
        '</tr></thead>'
        f'{"".join(bodies)}'
        '</table></div></div>'
    )


def _module_section(
    mod: str,
    submodule_stats: dict,
    file_pairs: list[dict],
    analysis_html: str,
    linker,
    query_repo_id: str,
    idx: int,
) -> tuple[str, str]:
    """返回 (toc_entry_html, section_html)。"""
    stats = submodule_stats.get(mod, {})
    pairs = [p for p in file_pairs if p["module"] == mod]

    if not pairs and stats.get("confirmed", 0) == 0:
        return "", ""

    disp  = _MODULE_DISPLAY.get(mod, mod)
    sid   = f"sec-mod-{mod}"
    label = f"{disp} ({mod})"
    copy_pct = stats.get("copy_pct", 0.0)

    # 子模块统计概要行（只统计已确认借鉴；review/weak 不确定项已剔除）
    stat_row = (
        f'<div class="flex flex-wrap gap-3 text-sm mb-3">'
        f'<span class="px-2 py-0.5 rounded bg-red-50 text-red-700">'
        f'借鉴估算 {copy_pct*100:.0f}%</span>'
        f'<span class="px-2 py-0.5 rounded bg-green-50 text-green-700">'
        f'原创估算 {(1-copy_pct)*100:.0f}%</span>'
        f'<span class="text-slate-500">函数总数 {stats.get("total","—")} 个 | '
        f'已确认借鉴 {stats.get("confirmed",0)} 个</span>'
        f'<span class="text-slate-400">主要来源：{html.escape(stats.get("top_source","—"))}</span>'
        f'</div>'
        + _pct_bar(copy_pct)
    )

    # 只展示「已确认借鉴」清单（U6：每函数列出全部候选）
    confirmed = [g for g in pairs if g["overall_tier"] == "confirmed"]
    table = _groups_table("已确认借鉴清单", confirmed, linker, query_repo_id, "text-red-700")

    # 语义分析片段（从 LLM 输出中抠取当前模块的部分）。
    # 表格已逐函数给出 文件:行 与链接，分析正文只讲语义、不再列地址，故不做链接化；
    # 兜底清掉模型偶尔仍写出的裸 path:line 文本，避免与表格重复。
    mod_analysis = _extract_module_analysis(analysis_html, mod)
    if mod_analysis:
        mod_analysis = _strip_addr_refs(mod_analysis)

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
    n_conf = stats.get("confirmed", 0)
    badge_cls = "toc-badge" if n_conf else "toc-badge zero"
    badge = f'<span class="{badge_cls}" title="已确认借鉴 {n_conf} 个函数">{n_conf}</span>'
    toc = f'<a class="toc-link" href="#{sid}"><span>{html.escape(label)}</span>{badge}</a>'
    return toc, section


def _make_link(resolver, file_path: str, line: int) -> str:
    if resolver is None:
        return "#"
    url = resolver(file_path, str(line))
    return url or "#"


# ─── GitLab 链接辅助 ──────────────────────────────────────────────────────────

def _build_gitlab_linker(
    query_repo_path: str | Path | None,
    query_repo_id: str,
    ref_repos: set[str],
) -> "GitLabLinker | None":
    """构建 GitLabLinker：新作品用本地克隆精确 sha，参考仓库用字面量 HEAD。"""
    try:
        from .gitlab_links import (
            GitLabLinker, build_repo_url_map, query_repo_info,
        )
        url_map = build_repo_url_map()
        if not url_map and not query_repo_path:
            return None

        # 参考仓库（历史库）不绑定具体 sha：functions.db 建库时未记录 commit sha，
        # data/repos 历史克隆工作树已清理（无法 rev-parse），远程最新 HEAD 又可能因仓库
        # 更新导致文件移动/删除。heads 留空 → gitlab_blob_url 兜底用字面量 HEAD
        # （/-/blob/HEAD/<path>），文件仍在即可打开；也省掉 ls-remote 的逐仓超时。
        heads: dict[str, str] = {}

        # 新作品（query）仓库：本地克隆 = 分析版本，sha 精确，行号一一对应
        q_url = q_sha = None
        if query_repo_path:
            q_url, q_sha = query_repo_info(query_repo_path)

        linker = GitLabLinker(url_map, heads, query_repo_url=q_url, query_sha=q_sha)
        linker.mark_query_repo(query_repo_id)
        return linker
    except Exception as e:
        logger.warning("[compare] 构建 GitLabLinker 失败：{}，链接降级为纯文本", e)
        return None


def _gitlab_url(linker, repo_id: str, file_path: str, start: int, end: int = 0) -> str | None:
    """从 linker 取 GitLab blob URL，失败返回 None。"""
    if linker is None:
        return None
    try:
        from .gitlab_links import gitlab_blob_url, _canonical_repo_url
        url = linker.url_map.get(repo_id) if hasattr(linker, "url_map") else None
        # query 仓库
        if repo_id == linker._query_key() and hasattr(linker, "query_repo_url"):
            url = linker.query_repo_url
            sha = linker.query_sha
        elif url:
            sha = linker.heads.get(_canonical_repo_url(url))
        else:
            return None
        return gitlab_blob_url(url, sha, file_path, start or 0, end or 0)
    except Exception:
        return None


def _make_gitlab_anchor(linker, repo_id: str, file_path: str, start: int,
                         end: int = 0, css_class: str = "file-jump") -> str:
    """生成 <a href="gitlab_url">file:line</a>，无 URL 时退化为纯文本。"""
    url = _gitlab_url(linker, repo_id, file_path, start, end)
    label = f"{html.escape(file_path)}:{start}" if start else html.escape(file_path)
    if url:
        return f'<a class="{css_class}" href="{html.escape(url)}" target="_blank">{label}</a>'
    return label


def _strip_addr_refs(fragment: str) -> str:
    """删除语义分析正文里残留的裸 path:line 引用（表格已给地址，正文不重复）。

    只清「带行号」的文件引用（如 os/src/task/mod.rs:125 或 a\\b\\c.c:120-130），保留
    <code>函数名</code>、纯文件名等不带行号的指代。顺带清理因删除产生的空括号/孤立标点。
    """
    import re
    if not fragment:
        return fragment
    ref = re.compile(
        r"[A-Za-z0-9_./\\\-]+\.(?:rs|c|h|cc|cpp|hpp|S|s|py|sh|toml|md):\d+(?:-\d+)?"
    )
    text = ref.sub("", fragment)
    # 清掉因删除留下的空 <code></code>、空括号「（）()」、连续标点
    text = re.sub(r"<code>\s*</code>", "", text)
    text = re.sub(r"[（(]\s*[)）]", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text


def _linkify_with_gitlab(fragment: str, linker, query_repo_id: str,
                          file_pairs: list[dict] | None = None) -> str:
    """把 HTML 片段（LLM 语义分析）里的 path:line 引用转为 GitLab 在线链接。

    LLM 写的路径不可信（会简写成纯文件名、带仓库名前缀、残缺），所以不直接用它的路径，
    而是用「文件名 + 行号」去 file_pairs（按 query 函数聚合的 group）里精确反查出
    **完整路径 + 正确仓库归属**：
      - loc_map (basename, line) → (repo_id, full_path)  精确匹配优先
      - base_map basename → [(repo_id, full_path, line), ...]  行号不精确时取最近
    命中新作品→用精确 sha；命中历史仓库→用 HEAD；file_pairs 无此文件→保留纯文本（不造坏链）。
    正则字符类含反斜杠以完整匹配 a\\b\\c.c:120；<code> 内也链接化（证据常写在 <code> 里）。
    """
    import re
    if linker is None:
        return fragment

    loc_map: dict[tuple[str, int], tuple[str, str]] = {}
    base_map: dict[str, list[tuple[str, str, int]]] = defaultdict(list)

    def _reg(repo_id: str, raw_path: str, line: int) -> None:
        fp = (raw_path or "").replace("\\", "/")
        if not fp:
            return
        base = fp.rsplit("/", 1)[-1]
        loc_map[(base, line)] = (repo_id, fp)
        base_map[base].append((repo_id, fp, line))

    for g in (file_pairs or []):
        _reg(query_repo_id, g.get("query_file", ""), g.get("query_start", 0))
        for c in g.get("candidates", []):
            _reg(c.get("ref_repo", ""), c.get("ref_file", ""), c.get("ref_start", 0))

    _FILEREF_RE = re.compile(
        r"([A-Za-z0-9_./\\\-]+\.(?:rs|c|h|cc|cpp|hpp|S|s|py|sh|toml|md))"
        r"(?::(\d+)(?:-(\d+))?)?"
    )
    _PROTECT_RE = re.compile(r"<(script|style|pre|a)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)

    blocks: list[str] = []

    def _stash(m: re.Match) -> str:
        blocks.append(m.group(0))
        return f"\x00B{len(blocks)-1}\x00"

    text = _PROTECT_RE.sub(_stash, fragment)

    def _sub(m: re.Match) -> str:
        fp, start, end = m.group(1), m.group(2), m.group(3)
        line = int(start) if start else 0
        end_line = int(end) if end else line
        base = fp.replace("\\", "/").rsplit("/", 1)[-1]
        hit = loc_map.get((base, line))
        if hit is None and base in base_map:
            repo_id, full_path, _ = min(base_map[base], key=lambda c: abs(c[2] - line))
            hit = (repo_id, full_path)
        if hit is None:
            return m.group(0)
        repo_id, full_path = hit
        url = _gitlab_url(linker, repo_id, full_path, line, end_line)
        if not url:
            return m.group(0)
        return f'<a class="file-jump" href="{html.escape(url)}" target="_blank">{m.group(0)}</a>'

    text = _FILEREF_RE.sub(_sub, text)
    text = re.sub(r"\x00B(\d+)\x00", lambda m: blocks[int(m.group(1))], text)
    return text


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


def _original_section(original_funcs: list[dict], linker, query_repo_id: str,
                      analysis_html: str = "") -> tuple[str, str]:
    """创新点分析章节（U8）：以设计维度（所属子系统 + 规模 + 定位）为主描述原创实现，
    相似度仅作辅助标注。若 LLM 产出了创新点片段（data-module="innovation"）则优先展示。
    """
    sid = "sec-original"
    llm_innov = _extract_module_analysis(analysis_html, "innovation") if analysis_html else ""
    if llm_innov:
        llm_innov = _linkify_with_gitlab(llm_innov, linker, query_repo_id)
    if not original_funcs:
        body = ("<p class='text-slate-500 text-sm'>未发现明显原创函数"
                "（与历史库最高相似度 &lt; 0.5 且规模 &gt; 20 行）。</p>")
    else:
        rows = "".join(
            f'<tr>'
            f'<td class="text-xs">{html.escape(_MODULE_DISPLAY.get(f["module"], f["module"]))}</td>'
            f'<td class="text-xs">{html.escape(f["func"])}</td>'
            f'<td class="font-mono text-xs">'
            + _make_gitlab_anchor(linker, query_repo_id, f["file"], f["start"], f.get("end", 0))
            + f'</td>'
            f'<td class="text-xs">{f["lines"]} 行</td>'
            f'<td class="text-xs text-slate-400">最高相似度 {f["max_sim"]}</td>'
            f'</tr>'
            for f in original_funcs
        )
        body = (
            '<p class="text-sm text-slate-600 mb-3">'
            '以下函数规模较大（&gt; 20 行）且与历史代码库最高相似度低（&lt; 0.5），'
            '从设计维度看属于该作品的原创/自研实现（按所属子系统与规模排列；相似度仅作辅助参考）：'
            '</p>'
            '<div class="overflow-x-auto">'
            '<table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">子系统</th>'
            '<th class="text-left p-2 border-b">函数</th>'
            '<th class="text-left p-2 border-b">文件:行</th>'
            '<th class="text-left p-2 border-b">规模</th>'
            '<th class="text-left p-2 border-b">辅助：相似度</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody>'
            '</table></div>'
        )
    if llm_innov:
        body += (
            '<div class="mt-4 p-4 bg-emerald-50 rounded-lg border border-emerald-100">'
            '<h4 class="text-sm font-semibold text-emerald-800 mb-2">设计层面创新点分析</h4>'
            + llm_innov +
            '</div>'
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
.toc{position:sticky;top:1.5rem;align-self:flex-start;width:230px;flex-shrink:0;font-size:.85rem}
.toc .toc-link{display:flex;align-items:center;justify-content:space-between;gap:.4rem;
  padding:.3rem .5rem;border-left:2px solid transparent;
  color:#475569;text-decoration:none;border-radius:0 .3rem .3rem 0}
.toc .toc-link:hover{background:#eef2f7}
.toc .toc-active{border-left-color:#4a90d9;color:#1a66d4;font-weight:600;background:#eef2fb}
.toc-badge{font-size:.65rem;line-height:1;padding:.15rem .4rem;border-radius:999px;
  background:#fee2e2;color:#b91c1c;font-weight:600;flex-shrink:0}
.toc-badge.zero{background:#e2e8f0;color:#64748b}
.main{flex:1;min-width:0}
.file-jump,.repo-link{color:#1a66d4;text-decoration:underline dotted}
.file-jump:hover,.repo-link:hover{text-decoration:underline solid}
.pct-bar{display:flex;height:20px;border-radius:4px;overflow:hidden;margin:6px 0}
.pct-copy{background:#ef4444;color:#fff;font-size:11px;
  display:flex;align-items:center;padding:0 6px;min-width:0;white-space:nowrap}
.pct-orig{background:#22c55e;color:#fff;font-size:11px;
  display:flex;align-items:center;padding:0 6px;min-width:0;white-space:nowrap}
.pct-bar div:only-child{border-radius:4px}
table{border-collapse:collapse}
td,th{padding:.4rem .6rem;border-bottom:1px solid #e2e8f0;vertical-align:top}
/* KPI 卡片 */
.kpi{display:flex;flex-direction:column;gap:.15rem;padding:.7rem .9rem;border-radius:.6rem;
  background:#fff;border:1px solid #e2e8f0}
.kpi .v{font-size:1.6rem;font-weight:700;line-height:1.1}
.kpi .l{font-size:.72rem;color:#64748b}
/* 档位/类型图例 */
.legend{display:flex;flex-wrap:wrap;gap:.4rem .9rem;font-size:.72rem;color:#475569;margin-top:.5rem}
.legend .dot{display:inline-block;width:.7rem;height:.7rem;border-radius:2px;margin-right:.3rem;vertical-align:-1px}
/* 代码证据并排 */
.code-toggle{cursor:pointer;background:none;border:none;padding:0}
.code-pair{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid #e2e8f0}
.code-col{min-width:0;border-left:1px solid #e2e8f0}
.code-col:first-child{border-left:none}
.code-h{font-size:.7rem;font-weight:600;color:#475569;padding:.35rem .6rem;
  background:#f1f5f9;border-bottom:1px solid #e2e8f0;position:sticky;top:0}
.code-body{margin:0;max-height:360px;overflow:auto;font-size:.72rem;line-height:1.45;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.cl{white-space:pre;padding:0 .6rem}
.cl.df{background:#fff1f2}
.code-col:first-child .cl.df{background:#fef9c3}
@media(max-width:720px){.code-pair{grid-template-columns:1fr}.code-col{border-left:none;border-top:1px solid #e2e8f0}}
/* 回到顶部 */
.to-top{position:fixed;right:1.1rem;bottom:1.1rem;width:2.4rem;height:2.4rem;border-radius:999px;
  background:#1a66d4;color:#fff;display:flex;align-items:center;justify-content:center;
  text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.2);font-size:1.1rem;opacity:.85}
.to-top:hover{opacity:1}
@media(max-width:860px){.layout{flex-direction:column}.toc{position:static;width:auto}}
@media print{
  .toc,.to-top{display:none!important}
  body{background:#fff}
  .layout{display:block;max-width:none;padding:0}
  section{break-inside:avoid;box-shadow:none!important}
  [x-cloak]{display:revert!important}
  .code-body{max-height:none}
}
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


def _file_level_section(file_matches: list[dict], file_similar: list[dict],
                        linker, query_repo_id: str) -> tuple[str, str]:
    """文件级整体相同/相似清单（U3 的文件维度 + L0 结果）。"""
    sid = "sec-files"
    if not file_matches and not file_similar:
        return "", ""
    parts: list[str] = []
    if file_matches:
        rows = "".join(
            '<tr>'
            '<td class="font-mono text-xs align-top">'
            + _make_gitlab_anchor(linker, query_repo_id, m["query_file"], 0)
            + '</td>'
            f'<td class="text-xs align-top">{m.get("line_count","—")} 行</td>'
            '<td class="align-top"><ul class="text-xs list-disc pl-4">'
            + "".join(
                '<li>' + _ref_repo_anchor(linker, c["repo_id"]) + ' '
                + _make_gitlab_anchor(linker, c["repo_id"], c["file_path"], 0) + '</li>'
                for c in m.get("matches", [])
            )
            + '</ul></td></tr>'
            for m in file_matches
        )
        parts.append(
            '<div class="text-sm font-semibold text-rose-700 mb-1">'
            f'整文件相同（规范化哈希一致，仅空格/注释差异）（{len(file_matches)} 个文件）</div>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">新作品文件</th>'
            '<th class="text-left p-2 border-b">规模</th>'
            '<th class="text-left p-2 border-b">相同来源文件</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )
    if file_similar:
        rows = "".join(
            '<tr>'
            '<td class="font-mono text-xs">'
            + _make_gitlab_anchor(linker, query_repo_id, f["file_path"], 0)
            + '</td>'
            f'<td class="text-xs">{html.escape(_MODULE_DISPLAY.get(f["module"], f["module"]))}</td>'
            f'<td class="text-xs">{f["hit"]}/{f["total"]} 个函数</td>'
            f'<td class="text-xs font-semibold text-amber-700">{round(f["ratio"]*100)}%</td>'
            '<td class="text-xs text-slate-500">'
            + _ref_repo_anchor(linker, f["top_source"])
            + (' ' + _make_gitlab_anchor(linker, f["top_source"], f["top_source_file"], 0)
               if f.get("top_source_file") else "")
            + '</td>'
            '</tr>'
            for f in file_similar
        )
        parts.append(
            '<div class="text-sm font-semibold text-amber-700 mt-3 mb-1">'
            f'文件整体相似（≥95% 函数命中借鉴）（{len(file_similar)} 个文件）</div>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">新作品文件</th>'
            '<th class="text-left p-2 border-b">子系统</th>'
            '<th class="text-left p-2 border-b">命中函数</th>'
            '<th class="text-left p-2 border-b">整体相似</th>'
            '<th class="text-left p-2 border-b">主要来源</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
        )
    body = "".join(parts)
    section = (
        f'<section id="{sid}" data-section-id="{sid}" '
        'class="mb-6 rounded-lg border border-slate-200 bg-white shadow-sm overflow-hidden" '
        f'x-data="{{open: true}}" '
        f"x-init=\"(function(){{const s=localStorage.getItem('cmp:{sid}');if(s!==null)open=(s==='1');}})()\">"
        '<div class="px-6 py-3 flex items-center gap-2 cursor-pointer select-none '
        'border-b border-slate-200 bg-slate-50" '
        f"@click=\"open=!open;localStorage.setItem('cmp:{sid}',open?'1':'0')\">"
        '<span class="text-slate-400 w-4 text-center" x-text="open?\'▾\':\'▸\'"></span>'
        '<h2 class="text-base font-semibold text-slate-800 m-0">文件级整体相同 / 相似</h2>'
        '</div>'
        '<div class="px-6 py-4" x-show="open" x-cloak>' + body + '</div>'
        '</section>'
    )
    toc = f'<a class="toc-link" href="#{sid}">文件级整体相同/相似</a>'
    return toc, section


def generate_comparison_html(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_pairs: list[dict],
    analysis_html: str,
    original_funcs: list[dict],
    query_repo_path: Path | None = None,
    linker=None,
    file_matches: list[dict] | None = None,
    file_similar: list[dict] | None = None,
) -> str:
    """组装完整的查重对比 HTML 报告（直接产出，不经 Markdown 转换）。"""
    file_matches = file_matches or []
    file_similar = file_similar or []
    # 摘要卡
    summary_html = _summary_card(query_repo_id, suspects, submodule_stats,
                                 file_match_count=len(file_matches),
                                 file_similar_count=len(file_similar))

    toc_items  = ['<a class="toc-link" href="#summary">总览</a>']
    body_parts = [summary_html]

    # 文件级整体相同/相似清单（L0 结果，紧随总览）
    toc_files, sec_files = _file_level_section(file_matches, file_similar, linker, query_repo_id)
    if toc_files:
        toc_items.append(toc_files)
        body_parts.append(sec_files)

    # 各子模块章节
    for idx, mod in enumerate(MODULES):
        toc_entry, section = _module_section(
            mod, submodule_stats, file_pairs, analysis_html,
            linker, query_repo_id, idx)
        if toc_entry:
            toc_items.append(toc_entry)
            body_parts.append(section)

    # 创新点分析章节
    toc_orig, sec_orig = _original_section(original_funcs, linker, query_repo_id, analysis_html)
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
<a href="#summary" class="to-top" title="回到顶部">↑</a>
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
    filematch_path: str | Path | None = None,
) -> dict:
    """主入口：suspects.json → LLM 语义分析 → 直接 HTML 报告。

    Args:
        suspects_path:   exact 阶段产出的 *_suspects.json 路径
        query_repo_path: 新作品本地克隆路径（用于文件链接 + 语义分析）
        recall_path:     embed 阶段产出的 *_recall.json（用于计算函数总数 / 原创函数）
        output_dir:      HTML 输出目录
        top_per_module:  每个子模块送入 LLM 的最大代码对数
        skip_opencode:   True 时跳过 LLM，仅用规则生成报告（调试用）
        filematch_path:  fastpath（L0 文件指纹层）产出的 *_filematch.json（整文件复制清单）
    """
    suspects_path = Path(suspects_path)
    data          = json.loads(suspects_path.read_text(encoding="utf-8"))
    suspects      = data.get("suspects", [])
    query_repo_id = data.get("query_repo_id") or suspects_path.stem.split("_suspects")[0]

    recall: dict | None = None
    if recall_path and Path(recall_path).exists():
        recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))

    # L0 文件指纹结果（整文件相同）+ 后聚合（文件整体相似）
    file_matches: list[dict] = []
    if filematch_path and Path(filematch_path).exists():
        file_matches = json.loads(Path(filematch_path).read_text(encoding="utf-8")).get("matched_files", [])
    file_similar = aggregate_file_similarity(suspects, recall)

    logger.info("[compare] 新作品 {}：{} 个嫌疑对，整文件相同 {} 个，整体相似 {} 个",
                query_repo_id, len(suspects), len(file_matches), len(file_similar))

    # 统计
    submodule_stats = compute_submodule_stats(suspects, recall)
    # 表格展示：confirmed 全 + review/weak 限量；送 LLM：每模块再取 sim 最高若干个，控 token
    file_pairs = collect_file_pairs(suspects, top_per_module=top_per_module)
    llm_pairs  = _limit_per_module(file_pairs, 5)

    # 原创候选函数
    original_funcs  = _original_functions(recall, suspects) if recall else []

    # opencode 语义分析
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / f"{query_repo_id}_semantic_work"

    if skip_opencode or not llm_pairs:
        analysis_html = _fallback_analysis(llm_pairs, submodule_stats)
    else:
        qpath = str(Path(query_repo_path).resolve()) if query_repo_path else ""
        analysis_html = run_semantic_analysis(
            query_repo_id, qpath, llm_pairs, submodule_stats, work_dir
        )

    # 构建 GitLab linker（取各参考仓库的在线 URL + HEAD sha）
    ref_repos = {c["ref_repo"] for g in file_pairs for c in g["candidates"]}
    ref_repos |= {m["repo_id"] for fm in file_matches for m in fm.get("matches", [])}
    linker = _build_gitlab_linker(
        Path(query_repo_path).resolve() if query_repo_path else None,
        query_repo_id,
        ref_repos,
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
        linker          = linker,
        file_matches    = file_matches,
        file_similar    = file_similar,
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
        "file_matches":     len(file_matches),
        "file_similar":     len(file_similar),
    }
