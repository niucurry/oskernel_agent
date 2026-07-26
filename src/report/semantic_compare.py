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
import re
import sqlite3
import subprocess
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.fastpath.scan import (WHOLE_FILE_LINE_RATIO, WHOLE_FILE_SIM_RATIO,
                               aggregate_file_similarity)
from src.retrieval_contract import (CONTRACT_VERSION, contract_errors,
                                    require_complete_contract)

from .false_positives import (FP_REASON_DISP, false_positive_stats,
                              tag_false_positives, tag_internal_arch_dups)
from .libraries import match_library, reused_library_stats, tag_library_reuse
from .upstream_baselines import (is_excluded_file_path, tag_upstream_baselines,
                                 upstream_baseline_stats)

DEFAULT_OUTPUT_DIR = "data/output"
_SEMANTIC_PROMPT_VERSION = "semantic-cn-v2"
_INNOVATION_PROMPT_VERSION = "innovation-map-cn-v2"
DEFAULT_FUNCTIONS_DB = Path(__file__).resolve().parents[2] / "data" / "db" / "functions.db"

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
        try:
            if c.exists():
                return str(c)
        except OSError:
            # 受限运行环境可能允许读取项目目录，却拒绝探测用户目录。
            # opencode 只是可选依赖，路径不可访问不应阻断 audit 等无关子命令。
            continue
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

# 展示层 tier 命名映射：内部 tier 值保持 confirmed/review/weak 不变；模型失败状态另行统计，
# 不通过 tier 名称伪装成“仍存疑”。
_TIER_DISPLAY = {
    "confirmed": "高置信同源代码",
    "review":    "模型复核后仍存疑",
    "weak":      "低强度相似信号",
}
# clone_type 展示名：按「逐字相同行占比」描述相似程度，中性命名，不臆断「改名」动机。
_CLONE_TYPE_DISPLAY = {
    "exact":    "匹配片段完全相同",
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

      匹配片段完全相同(exact)：全部已匹配行逐字一致；
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


def _match_coverage(pair_or_suspect: dict) -> dict:
    """返回行级匹配片段占目标函数的比例；无行级证据时不拿向量分数冒充覆盖率。"""
    ev = pair_or_suspect.get("evidence") or {}
    matched = int(ev.get("exact_match_lines") or 0) + int(ev.get("renamed_match_lines") or 0)
    q = pair_or_suspect.get("query_func") or {}
    start, end = int(q.get("start_line") or 0), int(q.get("end_line") or 0)
    total = max(0, end - start + 1) if start and end >= start else 0
    if total <= 0:
        total = sum(1 for line in (q.get("raw_code") or "").splitlines() if line.strip())
    if matched <= 0 or total <= 0:
        return {"matched_lines": matched, "query_lines": total, "match_coverage": None}
    matched = min(matched, total)
    return {
        "matched_lines": matched,
        "query_lines": total,
        "match_coverage": round(matched / total, 3),
    }


def _clone_summary(candidate: dict) -> str:
    """面向报告/提示词的局部匹配性质与目标函数覆盖说明。"""
    kind = _CLONE_TYPE_DISPLAY.get(candidate.get("clone_type", "—"),
                                   candidate.get("clone_type", "—"))
    coverage = candidate.get("match_coverage")
    if coverage is None:
        return f"{kind} · 暂无行级覆盖证据"
    matched = int(candidate.get("matched_lines") or 0)
    total = int(candidate.get("query_lines") or 0)
    return f"{kind} · 覆盖目标函数 {coverage * 100:.1f}%（{matched}/{total} 行）"


_OPENCODE = _find_opencode()


# ─── 1. 统计：各子模块复制/原创百分比 ─────────────────────────────────────────

# 一个 confirmed query 函数命中 >= 此数的不同历史仓库 → 公共/样板代码（如 print/println 宏），
# 不计入「值得关注的借鉴」。复用上游 metadata 通道4 的判定（与 config common_code_repo_threshold 一致）。
COMMON_CODE_REPO_THRESHOLD = 5


def _is_common_code(s: dict) -> bool:
    """confirmed 但命中多个历史仓库的公共/样板代码（多队都有的同款 print 宏 / 样板函数）。

    直接复用上游 metadata 通道4 的标注：confirmed 高广度命中只标注、不降级（见 metadata/runner.py），
    这里据此把它从借鉴图/清单剔除，归入「公共/样板代码」小节。
    """
    if s.get("common_code_note"):
        return True
    return (s.get("evidence") or {}).get("common_code_repos", 0) >= COMMON_CODE_REPO_THRESHOLD


def _is_excluded_pair(s: dict) -> bool:
    """不计入「值得关注的借鉴」的对：① vendored 库复用；② 公共/样板代码（命中多仓库）；
    ③ 疑似误报（跨架构/跨语言/样板汇编）；④ 作品内部跨架构硬拷贝复用；
    ⑤ 上游基线 vendored（双方同上游根下同相对路径）/ ABI 受限代码。

    后三类由报告层 tag_* 标注，仅「降级 / 归类」——从 KPI/借鉴清单剔除并在各自小节单列，
    人工仍可核（见 [[false_positives]] [[upstream_baselines]]）。"""
    return (bool(s.get("reuse_library")) or _is_common_code(s)
            or bool(s.get("false_positive")) or bool(s.get("internal_arch_dup"))
            or bool(s.get("upstream_vendored")) or bool(s.get("abi_constrained")))


def common_code_stats(suspects: list[dict]) -> list[dict]:
    """公共/样板代码统计：命中多个历史仓库的公共/框架/样板函数（如 print/println 宏）。

    含两类：① confirmed 但高广度命中（上游只标注未降级）；② 已降级为 common_code 档的。
    按 query 函数去重、按命中仓库数降序。返回 [{name, file, start, module, repos}, ...]。
    """
    funcs: dict[tuple, dict] = {}
    for s in suspects:
        # 库复用单列；其余「命中多仓库」的公共/样板（confirmed-标注 或 已降级 common_code）都收进来
        if s.get("reuse_library"):
            continue
        if not (_is_common_code(s) or s.get("tier") == "common_code"):
            continue
        q = s.get("query_func", {})
        key = (q.get("file_path", ""), q.get("func_name", ""))
        repos = (s.get("evidence") or {}).get("common_code_repos", 0)
        f = funcs.setdefault(key, {
            "name": q.get("func_name", ""), "file": q.get("file_path", ""),
            "start": q.get("start_line", 0), "module": q.get("module_tag", "other"), "repos": 0})
        f["repos"] = max(f["repos"], repos)
    out = list(funcs.values())
    out.sort(key=lambda x: -x["repos"])
    return out


def baseline_stats(suspects: list[dict]) -> list[dict]:
    """基线衍生统计：双侧均与同一基线库（教学OS/模板/官方第三方库）相似的函数。

    按 query 函数去重。返回 [{name, file, start, module, source, note}, ...]。
    """
    funcs: dict[tuple, dict] = {}
    for s in suspects:
        if s.get("tier") != "baseline_derived":
            continue
        q = s.get("query_func", {})
        key = (q.get("file_path", ""), q.get("func_name", ""))
        funcs.setdefault(key, {
            "name": q.get("func_name", ""), "file": q.get("file_path", ""),
            "start": q.get("start_line", 0), "module": q.get("module_tag", "other"),
            "source": s.get("candidate_func", {}).get("repo_id", ""),
            "note": s.get("baseline_note", "")})
    return sorted(funcs.values(), key=lambda x: (x["module"], x["name"]))


def compute_submodule_stats(suspects: list[dict], recall: dict | None = None) -> dict:
    """各子模块同源、明确存疑、复核未完成、暂未检出四类**函数数**统计。

    按 query 函数去重、取最高档归类：
      借鉴(confirmed)：confirmed 且非库复用、非公共/样板；
      模型仍存疑(review)：review/weak 且模型返回有效“疑似”；
      复核未完成：模型调用/格式校验失败，或未配置模型；绝不计入“模型仍存疑”；
      原创(original)：recall 中（非库）完全未进入嫌疑清单的函数。
    库复用 / 公共样板 / baseline 既不算借鉴也不算原创，**不计入 total**（在各自小节单列），
    所以 total = 同源 + 存疑 + 复核未完成 + 暂未检出，占比相加为 100%。

    Returns: dict[module] → {confirmed, review, review_failed, review_pending,
                             review_incomplete, original, total, ...}
    """
    category_rank = {"confirmed": 4, "review": 3, "review_failed": 2,
                     "review_pending": 1}
    best: dict[tuple, list] = {}                 # key -> [rank, module, category]
    sources: dict[str, Counter] = defaultdict(Counter)
    matched_keys: set[tuple] = set()             # 任何非 dismissed 命中（含库/公共）→ 不算原创
    for s in suspects:
        tier = s.get("tier", "")
        q = s.get("query_func", {})
        key = (q.get("file_path", ""), q.get("func_name", ""))
        if tier != "dismissed":
            matched_keys.add(key)
        if _is_excluded_pair(s) or tier in ("dismissed", "baseline_derived", "common_code"):
            continue
        if tier not in ("confirmed", "review", "weak"):
            continue
        mod = q.get("module_tag", "other")
        if mod not in MODULES:
            mod = "other"
        if tier == "confirmed":
            category = "confirmed"
        elif s.get("review_verdict") == "疑似":
            category = "review"
        elif s.get("review_verdict") == "复核失败":
            category = "review_failed"
        else:
            category = "review_pending"
        r = category_rank[category]
        cur = best.get(key)
        if cur is None or r > cur[0]:
            best[key] = [r, mod, category]
        if tier == "confirmed":
            sources[mod][s.get("candidate_func", {}).get("repo_id", "?")] += 1

    agg = {
        mod: {"confirmed": 0, "review": 0, "review_failed": 0,
              "review_pending": 0, "original": 0}
        for mod in MODULES
    }
    for _key, (_r, mod, category) in best.items():
        agg[mod][category] += 1

    # 原创 = recall 中（非库）完全未进入嫌疑清单的函数
    if recall:
        for item in recall.get("results", []):
            q = item.get("query", {})
            if match_library(q.get("file_path")):
                continue
            key = (q.get("file_path", ""), q.get("func_name", ""))
            if key in matched_keys:
                continue
            mod = q.get("module_tag", "other")
            if mod not in MODULES:
                mod = "other"
            agg[mod]["original"] += 1

    result = {}
    for mod, d in agg.items():
        incomplete = d["review_failed"] + d["review_pending"]
        total = d["confirmed"] + d["review"] + incomplete + d["original"]
        top = sources[mod].most_common(1)
        result[mod] = {
            "confirmed":    d["confirmed"],
            "review":       d["review"],
            "review_failed": d["review_failed"],
            "review_pending": d["review_pending"],
            "review_incomplete": incomplete,
            "weak":         0,            # weak 已并入「疑似借鉴」(review)，保留键以兼容
            "original":     d["original"],
            "total":        total,
            "copy_pct":     round(d["confirmed"] / total, 3) if total else 0.0,
            "review_pct":   round(d["review"] / total, 3) if total else 0.0,
            "review_incomplete_pct": round(incomplete / total, 3) if total else 0.0,
            "original_pct": round(d["original"] / total, 3) if total else 0.0,
            "top_source":   top[0][0] if top else "—",
        }
    return result


def _original_functions(recall: dict, suspects: list[dict], top_n: int | None = None) -> list[dict]:
    """recall 中「完全未进入嫌疑清单（非库复用/非公共样板/非任何命中）」的函数 = 原创/自研。

    与 compute_submodule_stats 的「原创」同口径（matched = 任何非 dismissed 命中），所以二者
    数量一致。判据用「是否进入命中清单」而非相似度阈值：代码嵌入余弦相似度有很高地板（OS
    内核链表/调度循环等结构高度雷同，无关函数 max_sim 也普遍 0.6+），用阈值会把几乎所有函数
    误判为非原创。返回**全部**原创函数（按行数降序）；展示层自行截断并显示总数。
    """
    matched_keys = {
        (s.get("query_func", {}).get("file_path", ""),
         s.get("query_func", {}).get("func_name", ""))
        for s in suspects
        if s.get("tier") != "dismissed"   # 任何命中（含库/公共/baseline）都不算原创
    }
    out = []
    for item in recall.get("results", []):
        q = item.get("query", {})
        if match_library(q.get("file_path")):
            continue  # vendored 第三方库代码不算原创/自研
        key = (q.get("file_path", ""), q.get("func_name", ""))
        if key in matched_keys:
            continue
        cands = item.get("candidates", [])
        max_sim = max((c.get("score", 0.0) for c in cands), default=0.0)
        lines = (q.get("end_line", 0) or 0) - (q.get("start_line", 0) or 0) + 1
        if True:
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
    return out[:top_n] if top_n else out


# ─── 1b. 相对参考 repo 的创新实现候选 ──────────────────────────────────────

_BRANCH_RE = re.compile(r"\b(?:if|else\s+if|for|while|loop|match|switch|case|catch)\b|&&|\|\|")


def _reference_repo_by_module(suspects: list[dict], recall: dict | None) -> dict[str, str]:
    """按有效相似命中推断每个模块最主要的参考 repo；无命中时再用召回分数兜底。"""
    counts: dict[str, Counter] = defaultdict(Counter)
    global_counts: Counter = Counter()
    for s in suspects:
        if s.get("tier") not in ("confirmed", "review", "weak") or _is_excluded_pair(s):
            continue
        q = s.get("query_func") or {}
        c = s.get("candidate_func") or {}
        repo = str(c.get("repo_id") or "")
        if not repo:
            continue
        mod = q.get("module_tag", "other")
        mod = mod if mod in MODULES else "other"
        weight = 3 if s.get("tier") == "confirmed" else 1
        counts[mod][repo] += weight
        global_counts[repo] += weight

    # 没有进入嫌疑清单的模块仍可能有低于阈值的最近参考；只给低权重，避免压过有效命中。
    if recall:
        for item in recall.get("results", []):
            q = item.get("query") or {}
            mod = q.get("module_tag", "other")
            mod = mod if mod in MODULES else "other"
            for candidate in (item.get("candidates") or [])[:3]:
                payload = candidate.get("payload") or candidate
                repo = str(payload.get("repo_id") or "")
                if not repo or payload.get("is_baseline") or match_library(payload.get("file_path")):
                    continue
                score = float(candidate.get("score") or 0.0)
                if score > 0:
                    counts[mod][repo] += score * 0.1
                    global_counts[repo] += score * 0.02

    global_repo = global_counts.most_common(1)[0][0] if global_counts else ""
    return {
        mod: (counts[mod].most_common(1)[0][0] if counts[mod] else global_repo)
        for mod in MODULES
    }


def _load_functions_by_id(db_path: str | Path | None, ids: set[int]) -> dict[int, dict]:
    """从 functions.db 补齐召回 payload 未携带的参考源码；数据库不可用时安静降级。"""
    path = Path(db_path or DEFAULT_FUNCTIONS_DB)
    if not ids or not path.is_file():
        return {}
    rows: dict[int, dict] = {}
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            ordered = sorted(ids)
            for offset in range(0, len(ordered), 500):
                chunk = ordered[offset:offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                sql = (
                    "SELECT id, repo_id, file_path, start_line, end_line, func_name, "
                    "module_tag, lang, raw_code FROM functions WHERE id IN (" + placeholders + ")"
                )
                for row in conn.execute(sql, chunk):
                    rows[int(row["id"])] = dict(row)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("[innovation] 参考函数源码读取失败：{}", exc)
    return rows


def build_innovation_candidates(
    recall: dict | None,
    suspects: list[dict],
    functions_db_path: str | Path | None = None,
    max_candidates: int = 18,
) -> list[dict]:
    """构造“目标未命中函数 ↔ 主要参考 repo 最近实现”的代码比较输入。

    这里不做创新认定，只形成有代码、有来源、有稳定 key 的候选；后续 LLM 归纳和确定性
    校验都只能引用这些 key，因此项目文档或模型臆造路径无法进入最终报告。
    """
    if not recall:
        return []
    refs_by_mod = _reference_repo_by_module(suspects, recall)
    originals = _original_functions(recall, suspects)
    original_keys = {(f["file"], f["func"]) for f in originals}
    original_meta = {(f["file"], f["func"]): f for f in originals}

    recall_items = []
    for item in recall.get("results", []):
        q = item.get("query") or {}
        key = (q.get("file_path", ""), q.get("func_name", ""))
        if key not in original_keys:
            continue
        lines = max(0, int(q.get("end_line") or 0) - int(q.get("start_line") or 0) + 1)
        if lines < 5 or not (q.get("raw_code") or "").strip():
            continue
        mod = q.get("module_tag", "other")
        mod = mod if mod in MODULES else "other"
        recall_items.append((lines, item, original_meta[key], mod))
    recall_items.sort(key=lambda x: -x[0])
    balanced: list[tuple] = []
    per_module: Counter = Counter()
    for record in recall_items:
        mod = record[3]
        if per_module[mod] >= 4:
            continue
        per_module[mod] += 1
        balanced.append(record)
        if len(balanced) >= max_candidates:
            break
    recall_items = balanced

    all_ref_ids: set[int] = set()
    for _, item, _, _ in recall_items:
        for candidate in item.get("candidates") or []:
            payload = candidate.get("payload") or candidate
            try:
                all_ref_ids.add(int(candidate.get("id") or payload.get("id")))
            except (TypeError, ValueError):
                continue
    hydrated = _load_functions_by_id(functions_db_path, all_ref_ids)

    def same_language(query: dict, payload: dict, func_id: int) -> bool:
        query_lang = str(query.get("lang") or "").lower()
        ref_lang = str((hydrated.get(func_id) or {}).get("lang")
                       or payload.get("lang") or "").lower()
        return bool(query_lang and ref_lang and query_lang == ref_lang)

    selected_refs: dict[int, tuple[dict, dict, str]] = {}
    for _, item, _, mod in recall_items:
        q = item.get("query") or {}
        preferred_repo = refs_by_mod.get(mod, "")
        candidates = []
        for candidate in item.get("candidates") or []:
            payload = candidate.get("payload") or candidate
            try:
                func_id = int(candidate.get("id") or payload.get("id"))
            except (TypeError, ValueError):
                continue
            if payload.get("is_baseline") or match_library(payload.get("file_path")):
                continue
            if not same_language(q, payload, func_id):
                continue
            repo = str(payload.get("repo_id") or "")
            priority = 1 if preferred_repo and repo == preferred_repo else 0
            candidates.append((priority, float(candidate.get("score") or 0.0), func_id, candidate, payload))
        candidates.sort(key=lambda x: (-x[0], -x[1]))
        for _, _, func_id, candidate, payload in candidates[:1]:
            selected_refs[func_id] = (candidate, payload, preferred_repo)

    result: list[dict] = []
    reference_seq = 0
    for index, (_, item, meta, mod) in enumerate(recall_items, start=1):
        q = item.get("query") or {}
        preferred_repo = refs_by_mod.get(mod, "")
        refs: list[dict] = []
        candidates = []
        for candidate in item.get("candidates") or []:
            payload = candidate.get("payload") or candidate
            try:
                func_id = int(candidate.get("id") or payload.get("id"))
            except (TypeError, ValueError):
                continue
            if func_id not in selected_refs:
                continue
            if not same_language(q, payload, func_id):
                continue
            repo = str(payload.get("repo_id") or "")
            priority = 1 if preferred_repo and repo == preferred_repo else 0
            candidates.append((priority, float(candidate.get("score") or 0.0), func_id, candidate, payload))
        candidates.sort(key=lambda x: (-x[0], -x[1]))
        for _, score, func_id, _, payload in candidates[:1]:
            row = hydrated.get(func_id, {})
            reference_seq += 1
            refs.append({
                "key": f"r{reference_seq:04d}",
                "repo": row.get("repo_id") or payload.get("repo_id", ""),
                "file": row.get("file_path") or payload.get("file_path", ""),
                "start": int(row.get("start_line") or payload.get("start_line") or 0),
                "end": int(row.get("end_line") or payload.get("end_line") or 0),
                "func": row.get("func_name") or payload.get("func_name", ""),
                "lang": row.get("lang") or payload.get("lang", ""),
                "score": round(score, 3),
                "raw_code": (row.get("raw_code") or "")[:1400],
            })
        result.append({
            "key": f"t{index:04d}",
            "module": mod,
            "module_display": _MODULE_DISPLAY.get(mod, mod),
            "reference_repo": preferred_repo or (refs[0]["repo"] if refs else ""),
            "file": q.get("file_path", ""),
            "start": int(q.get("start_line") or 0),
            "end": int(q.get("end_line") or 0),
            "func": q.get("func_name", ""),
            "lang": q.get("lang", ""),
            "lines": int(meta.get("lines") or 0),
            "raw_code": (q.get("raw_code") or "")[:1800],
            "references": refs,
        })
    return result


def _innovation_complexity(targets: list[dict]) -> dict:
    files = {t["file"] for t in targets if t.get("file")}
    code_lines = sum(max(0, int(t.get("lines") or 0)) for t in targets)
    branches = sum(len(_BRANCH_RE.findall(t.get("raw_code") or "")) for t in targets)
    symbol_count = len({(t.get("file"), t.get("func")) for t in targets})
    score = round(
        min(code_lines, 400) * 0.14
        + min(branches, 40) * 0.9
        + min(symbol_count, 10) * 3
        + min(len(files), 6) * 4
    )
    score = max(0, min(100, score))
    return {
        "level": "高" if score >= 60 else ("中" if score >= 30 else "低"),
        "score": score,
        "code_lines": code_lines,
        "file_count": len(files),
        "symbol_count": symbol_count,
        "branch_points": branches,
    }


def _fallback_innovation_points(candidates: list[dict]) -> list[dict]:
    """LLM 不可用时保守展示代码差异候选，不把“未命中”冒充已证实创新。"""
    by_module: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        by_module[candidate["module"]].append(candidate)
    points: list[dict] = []
    for mod in MODULES:
        targets = by_module.get(mod, [])[:3]
        if not targets:
            continue
        refs = [ref for target in targets for ref in target.get("references", [])[:1]]
        reference_repo = targets[0].get("reference_repo") or (refs[0]["repo"] if refs else "")
        funcs = "、".join(t["func"] for t in targets)
        points.append({
            "title": f'{_MODULE_DISPLAY.get(mod, mod)}代码差异候选：{funcs}',
            "kind": "待核验候选",
            "confidence": "low",
            "reference_repo": reference_repo,
            "baseline": (
                f"以 {reference_repo} 中的最近邻函数为比较基线；当前未启用语义模型，"
                "无法自动确认二者是否承担同一机制。"
            ),
            "delta": "这些目标函数未形成有效历史相似命中，只能说明实现存在代码差异，不能据此直接认定创新。",
            "why_it_matters": "建议点击目标与参考实现，人工核对数据结构、控制流和跨函数协作。",
            "impact_scope": "影响范围尚未由模型确认，以所列文件、函数及自动发现的调用位置为边界。",
            "validation_hint": "需要补充与该机制直接关联的内核测试或 benchmark。",
            "counterevidence": "未形成历史相似命中只能证明当前检索未命中，不能单独证明机制创新。",
            "targets": targets,
            "references": refs,
            "complexity": _innovation_complexity(targets),
        })
        if len(points) >= 5:
            break
    return points


def _normalize_innovation_points(raw: object, candidates: list[dict]) -> list[dict]:
    target_map = {c["key"]: c for c in candidates}
    ref_map = {r["key"]: r for c in candidates for r in c.get("references", [])}
    items = raw.get("innovations", []) if isinstance(raw, dict) else []
    normalized: list[dict] = []
    used_target_keys: set[str] = set()
    for item in items[:8] if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        raw_target_keys = item.get("target_keys", [])
        raw_target_keys = raw_target_keys if isinstance(raw_target_keys, list) else []
        target_keys = list(dict.fromkeys(raw_target_keys))
        target_keys = [k for k in target_keys if k in target_map and k not in used_target_keys]
        targets = [target_map[k] for k in target_keys]
        raw_reference_keys = item.get("reference_keys", [])
        raw_reference_keys = raw_reference_keys if isinstance(raw_reference_keys, list) else []
        refs = [ref_map[k] for k in dict.fromkeys(raw_reference_keys) if k in ref_map]
        title = str(item.get("title") or "").strip()
        baseline = str(item.get("baseline") or "").strip()
        delta = str(item.get("delta") or "").strip()
        if not targets or not title or not baseline or not delta:
            continue
        # 只能引用与这些目标函数真实关联的 reference key，防止模型跨卡拼错来源。
        allowed_refs = {r["key"] for target in targets for r in target.get("references", [])}
        refs = [r for r in refs if r["key"] in allowed_refs]
        repo_counts = Counter(r["repo"] for r in refs if r.get("repo"))
        if not repo_counts:
            repo_counts.update(t.get("reference_repo", "") for t in targets if t.get("reference_repo"))
        confidence = str(item.get("confidence") or "medium").lower()
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"
        if not refs and confidence == "high":
            confidence = "medium"
        if refs and not any((r.get("raw_code") or "").strip() for r in refs):
            confidence = "low"
            baseline = (
                f"已定位到 {repo_counts.most_common(1)[0][0] if repo_counts else '参考 repo'} 的最近函数，"
                "但 functions.db 未提供参考源码，无法自动核验其机制基线。"
            )
            delta = "目标代码未形成有效相似命中；在缺少参考源码时只能列为待人工核验的差异候选。"
        normalized.append({
            "title": title[:120],
            "kind": str(item.get("kind") or "工程改良")[:40],
            "confidence": confidence,
            "reference_repo": repo_counts.most_common(1)[0][0] if repo_counts else "未识别",
            "baseline": baseline[:400],
            "delta": delta[:400],
            "why_it_matters": str(item.get("why_it_matters") or "")[:400],
            "impact_scope": str(item.get("impact_scope") or "")[:400],
            "validation_hint": str(item.get("validation_hint") or "")[:400],
            "counterevidence": str(item.get("counterevidence") or "")[:400],
            "targets": targets,
            "references": refs,
            "complexity": _innovation_complexity(targets),
        })
        used_target_keys.update(target_keys)
    return normalized or _fallback_innovation_points(candidates)



# ─── 2. 收集代码对（按 query 函数聚合全部候选） ──────────────────────────────

_TIER_RANK = {"confirmed": 3, "review": 2, "weak": 1}

# 候选来源展示下限：相似度低于此值的候选属召回巧合命中（非真实借鉴来源），不展示。
# 取「弱相似」档下限 0.5（见 models.py tier 口径），避免 0.02/0.2 这类被误列为来源。
MIN_CANDIDATE_SIM = 0.5


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
    keep_tiers: tuple[str, ...] = ("confirmed",),
) -> list[dict]:
    """按 query 函数聚合候选（U6）：每个 query 函数列出其全部候选 + 各自相似度，并给
    「整体相似度」(= 最强候选)。每模块取整体相似度最高的 top_per_module 个函数组。

    返回 [{module, query_func, query_file, query_start/end, query_code, overall_sim,
           overall_tier, clone_type, candidates:[{tier,sim,clone_type,ref_*}, ...]}, ...]
    """
    groups: dict[tuple, dict] = {}
    for s in suspects:
        tier = s.get("tier", "")
        # 库复用 / 公共样板代码不进相似清单，另在对应小节单列
        if tier in ("dismissed", "baseline_derived", "common_code") or _is_excluded_pair(s):
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
        coverage = _match_coverage(s)
        g["candidates"].append({
            "tier":       tier,
            "via_review": s.get("confirm_via") == "review_llm",  # 由低端模型复核升为借鉴
            "sim":        _pair_sim(s),
            "clone_type": _clone_kind(s),
            "ref_func":   c.get("func_name", ""),
            "ref_file":   c.get("file_path", ""),
            "ref_repo":   c.get("repo_id", ""),
            "ref_start":  c.get("start_line", 0),
            "ref_end":    c.get("end_line", 0),
            "ref_code":   (c.get("raw_code") or "")[:800],
            **coverage,
        })

    for g in groups.values():
        cands = g["candidates"]
        g["overall_tier"] = max((c["tier"] for c in cands),
                                key=lambda t: _TIER_RANK.get(t, 0), default="weak")
        # 该 confirmed 是否「仅由模型复核认定」（无逐行铁证候选）——供清单加标记区分
        conf_cands = [c for c in cands if c["tier"] == "confirmed"]
        g["via_review"] = bool(conf_cands) and all(c.get("via_review") for c in conf_cands)
        cands.sort(key=lambda x: -x["sim"])
        g["overall_sim"] = cands[0]["sim"] if cands else 0.0
        g["clone_type"] = cands[0]["clone_type"] if cands else "—"
        g["matched_lines"] = cands[0].get("matched_lines", 0) if cands else 0
        g["query_lines"] = cands[0].get("query_lines", 0) if cands else 0
        g["match_coverage"] = cands[0].get("match_coverage") if cands else None
        # 只展示「确有相似」的候选来源：相似度 >= 弱相似下限 或 confirmed。否则 recall 残留的
        # 0.02/0.2 这类巧合命中会被误列为「借鉴来源」。至少保留最强 1 个（必为 confirmed）。
        strong = [c for c in cands if c["sim"] >= MIN_CANDIDATE_SIM or c["tier"] == "confirmed"]
        if not strong and cands:
            strong = cands[:1]
        g["candidate_count"] = len(strong)
        g["candidates"] = strong[:max_candidates]   # 限制展示候选数，避免报告过长

    by_module: dict[str, list[dict]] = defaultdict(list)
    for g in groups.values():
        by_module[g["module"]].append(g)
    result = []
    for mod in MODULES:
        gs = sorted(by_module.get(mod, []), key=lambda x: -x["overall_sim"])
        # 默认只保留 confirmed（已确认借鉴）；keep_tiers 可放开到 review/weak，
        # 供「疑似借鉴清单」单独收集（不影响 confirmed 主表与送 LLM 的输入）。
        result.extend(g for g in gs if g["overall_tier"] in keep_tiers)
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


def _exclude_confirmed_review_groups(review_groups: list[dict],
                                     confirmed_groups: list[dict]) -> list[dict]:
    """按 (文件, 函数名) 去重；已进入 confirmed 时不再在模型存疑清单重复出现。"""
    confirmed_keys = {
        (g.get("query_file", ""), g.get("query_func", "")) for g in confirmed_groups
    }
    result = []
    seen = set()
    for g in review_groups:
        key = (g.get("query_file", ""), g.get("query_func", ""))
        if key in confirmed_keys or key in seen:
            continue
        seen.add(key)
        result.append(g)
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
你是 OS 内核代码原创性分析助手，专注于语义级（功能层面）的对比分析，面向评审人员。

对每个子模块输出一段信息充分、可追溯的 HTML 分析片段。

分析维度（每条结论尽量覆盖）：
1. 借鉴对象：借鉴了哪些**算法**（如调度策略、分配器、置换算法）、**数据结构**
   （如页表、inode、就绪队列）、**机制**（如 trap 上下文保存/恢复、锁、缓存）。
2. 借鉴程度（四档，必须明确给出其一）：
   - 匹配片段完全相同（已匹配片段逐行逐字一致；必须结合目标函数覆盖比例解读）
   - 近乎相同（仅个别变量/常量/少量行不同）
   - 结构相似（控制流一致、表达式改写）
   - 受启发重新实现（思路相近、实现独立）
3. 设计差异：新作品相对来源做了哪些改动/取舍（如换数据结构、改并发策略、增删功能）。

写作要求：
- **最终交付必须一次性使用简体中文**：所有标题、段落、列表项和自然语言说明均用中文；
  函数名、类型名、算法名、数据结构名、代码标识符和专有名词可保留英文原文。
- 禁止出现整句英文、整段英文或整节英文。即使输入代码和来源材料是英文，也必须用中文分析。
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
- 输出前逐个检查 <h3>/<p>/<li>/<th>/<td>：如仍有英文自然语言句子，先改写成中文再输出。
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
            f"- **{disp}**（{mod}）：高置信同源代码 {stats['confirmed']} 个函数，"
            f"主要来源：{stats['top_source']}"
        )
    lines += ["", "## 相似代码对（按子模块、按 query 函数聚合全部候选）", ""]

    current_mod = None
    for g in file_pairs:
        mod = g["module"]
        if mod != current_mod:
            lines.append(f"### {_MODULE_DISPLAY.get(mod, mod)} ({mod})")
            current_mod = mod
        tier_label = {"confirmed": "高置信同源代码", "review": "模型复核后仍存疑",
                      "weak": "低强度相似信号"}.get(
            g["overall_tier"], g["overall_tier"])
        lines.append(
            f"**新作品** `{g['query_file']}:{g['query_start']}` 函数 `{g['query_func']}` "
            f"← {tier_label}（整体相似度 {g['overall_sim']}，{g['candidate_count']} 个候选来源）"
        )
        for c in g["candidates"]:
            ck = _clone_summary(c)
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
        "报告必须一次性完整使用简体中文；仅代码标识符和技术专名保留英文，不要生成英文版等待翻译。",
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
    ck = _cache_key(_SEMANTIC_PROMPT_VERSION, query_repo_id, pair_sig)
    html_cache = cache_dir / f"{ck}.html"

    if html_cache.exists():
        cached_html = html_cache.read_text(encoding="utf-8")
        from src.oskernel_agent.pipeline.lang_guard import needs_translation
        if not needs_translation(cached_html):
            logger.info("[semantic] 中文缓存命中 → {}", html_cache)
            return cached_html
        logger.warning("[semantic] 缓存含英文正文，忽略并重新生成：{}", html_cache)

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
            temperature=0.2,
            max_tokens=8000,
        )
        html_text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("[semantic] API 调用失败：{}，使用规则兜底", e)
        return _fallback_analysis(file_pairs, submodule_stats)

    # 提取 HTML 片段（模型可能在 markdown 代码块里）
    html_content = _extract_html_from_text(html_text) or html_text

    # 首轮直接生成中文；只有确实检测到英文正文时才保留一次翻译兜底。
    from src.oskernel_agent.pipeline.lang_guard import normalize_html_language
    html_content, lang_stats = normalize_html_language(html_content)
    if not lang_stats["complete"]:
        logger.warning(
            "[semantic] 中文兜底仍未通过校验，改用确定性的中文规则报告（残留 {} 处）",
            lang_stats["remaining"],
        )
        html_content = _fallback_analysis(file_pairs, submodule_stats)
    elif lang_stats["translated"]:
        logger.warning("[semantic] 首轮残留英文，已启用保留的翻译兜底")

    output_path.write_text(html_content, encoding="utf-8")
    html_cache.write_text(html_content, encoding="utf-8")
    logger.info("[semantic] 分析完成（{} 字符）", len(html_content))
    return html_content


_INNOVATION_SYSTEM = """你是 OS 内核代码差异分析助手。你的任务不是复述 README，也不是把
“未检出相似”直接改名为“创新”，而是仅根据给出的目标函数源码与参考 repo 最近实现，归纳
相对参考实现有实质意义的机制变化。

判断顺序：
1. 先确认目标与参考是否属于可比较的同一职责；职责不同的最近邻不能作为创新基线。
2. 比较数据结构、控制流、同步/并发、状态机、错误处理和跨函数协作；变量改名、代码增量、
   多几个 wrapper、调用成熟第三方库不算创新。
3. 可以把同一机制的多个 target key 合成一个创新点，但不能把无关函数硬凑成“大创新”。
4. 每个结论必须引用输入中真实存在的 target_keys；reference_keys 也只能引用输入 key。
5. 输入不含项目文档，禁止根据项目自述下结论。证据不足时省略，或用 kind="存疑"、
   confidence="low" 明确标注。

只输出合法 JSON，不要 Markdown。格式：
{"innovations":[{
  "title":"简洁机制名",
  "kind":"架构扩展|机制改良|工程增强|存疑",
  "baseline":"参考 repo 的对应机制如何实现",
  "delta":"目标 repo 在代码层具体改变了什么",
  "why_it_matters":"带来的能力、性能、安全性或代价",
  "impact_scope":"可能影响的子系统、调用链或运行时行为",
  "validation_hint":"应通过什么测试或 benchmark 验证",
  "counterevidence":"削弱该创新判断的限制、替代解释或反证",
  "confidence":"high|medium|low",
  "target_keys":["t0001"],
  "reference_keys":["r0001"]
}]}

输出 2–6 个最有实质性的条目；确实不足 2 个时可以更少，不能凑数。所有自然语言字段使用简体中文。
"""


def _innovation_message(query_repo_id: str, candidates: list[dict]) -> str:
    compact = []
    for candidate in candidates:
        compact.append({
            "key": candidate["key"],
            "module": candidate["module_display"],
            "reference_repo": candidate.get("reference_repo", ""),
            "target": {
                "file": candidate["file"], "start": candidate["start"], "end": candidate["end"],
                "func": candidate["func"], "raw_code": candidate["raw_code"],
            },
            "references": candidate.get("references", []),
        })
    return (
        f"目标仓库：{query_repo_id}\n"
        "下面每个条目都是‘暂未形成有效历史相似命中’的代码差异候选，并非已认定创新。"
        "请严格比较代码后再归纳：\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
    )


def _parse_json_object(text: str) -> dict | None:
    cleaned = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start:end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
    return None


def run_innovation_analysis(
    query_repo_id: str,
    candidates: list[dict],
    work_dir: Path,
    skip_llm: bool = False,
    timeout: int = 120,
) -> list[dict]:
    """代码对比归纳创新点；返回值已绑定真实 key 并补充确定性复杂度。"""
    if not candidates:
        return []
    if skip_llm:
        return _fallback_innovation_points(candidates)

    signature = json.dumps(
        [(c["key"], c["file"], c["func"], c.get("reference_repo"),
          [(r["key"], r["repo"], r["func"], r["score"]) for r in c.get("references", [])])
         for c in candidates],
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_dir = work_dir.resolve() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{_cache_key(_INNOVATION_PROMPT_VERSION, query_repo_id, signature)}.innovation.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            return _normalize_innovation_points(cached, candidates)
        except (OSError, json.JSONDecodeError):
            pass

    try:
        from oskernel_agent import config as _cfg
        api_key = _cfg.api.get("key", "").strip()
        base_url = _cfg.api.get("base_url", "https://api.deepseek.com/v1").strip()
    except Exception:
        api_key = base_url = ""
    if not api_key:
        logger.warning("[innovation] 未找到 API key，使用保守代码差异候选")
        return _fallback_innovation_points(candidates)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        response = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
            messages=[
                {"role": "system", "content": _INNOVATION_SYSTEM},
                {"role": "user", "content": _innovation_message(query_repo_id, candidates)},
            ],
            temperature=0.15,
            max_tokens=5000,
        )
        parsed = _parse_json_object(response.choices[0].message.content or "")
    except Exception as exc:
        logger.warning("[innovation] 代码差异归纳失败：{}，使用保守候选", exc)
        parsed = None
    if parsed is None:
        return _fallback_innovation_points(candidates)
    try:
        cache_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    points = _normalize_innovation_points(parsed, candidates)
    logger.info("[innovation] 生成 {} 个创新实现映射", len(points))
    return points


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
                f'（相似度 {c["sim"]}，{_clone_summary(c)}）'
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
            f'<p>检测到高置信同源代码 {stats.get("confirmed",0)} 个函数，'
            f'主要来源：{html.escape(stats.get("top_source","—"))}。'
            f'（未启用 LLM 语义分析，以下为规则汇总）</p>'
            f'<ul>{"".join(items)}</ul>'
            f'</section>'
        )
    parts.append('</div>')
    return "\n".join(parts)


# ─── 5. 生成完整 HTML 报告 ────────────────────────────────────────────────────

def _pct_bar(copy_pct: float, review_pct: float = 0.0,
             review_incomplete_pct: float = 0.0,
             original_pct: float | None = None) -> str:
    """同源/有效存疑/复核未完成/暂未检出进度条。"""
    c = round(copy_pct * 100)
    rv = round(review_pct * 100)
    inc = round(review_incomplete_pct * 100)
    o = round(original_pct * 100) if original_pct is not None else max(0, 100 - c - rv - inc)
    seg = ""
    if c:
        seg += f'<div class="pct-copy" style="width:{c}%">{c}%&nbsp;高置信同源代码</div>'
    if rv:
        seg += f'<div class="pct-review" style="width:{rv}%">{rv}%&nbsp;模型复核后仍存疑</div>'
    if inc:
        seg += f'<div class="pct-incomplete" style="width:{inc}%">{inc}%&nbsp;复核未完成</div>'
    if o:
        seg += f'<div class="pct-orig" style="width:{o}%">{o}%&nbsp;暂未检出相似</div>'
    title = (f"高置信同源代码 {c}% / 模型复核后仍存疑 {rv}% / "
             f"复核未完成 {inc}% / 暂未检出相似 {o}%")
    return f'<div class="pct-bar" title="{title}">{seg}</div>'


def _echarts_overview(submodule_stats: dict) -> str:
    """ECharts 堆叠横向柱图：同源/有效存疑/复核未完成/暂未检出比例。"""
    mods = [m for m in MODULES if submodule_stats.get(m, {}).get("total", 0) > 0]
    if not mods:
        return ""
    labels = [_MODULE_DISPLAY.get(m, m) for m in mods]
    copy_vals = [round(submodule_stats[m]["copy_pct"] * 100, 1) for m in mods]
    rev_vals  = [round(submodule_stats[m].get("review_pct", 0.0) * 100, 1) for m in mods]
    inc_vals = [round(submodule_stats[m].get("review_incomplete_pct", 0.0) * 100, 1)
                for m in mods]
    orig_vals = [round(submodule_stats[m]["original_pct"] * 100, 1) for m in mods]
    show_rev = any(v > 0 for v in rev_vals)   # 不确定的复核结果会保留 review 档
    show_inc = any(v > 0 for v in inc_vals)
    series = [{"name": "高置信同源代码", "type": "bar", "stack": "pct", "data": copy_vals[::-1],
               "itemStyle": {"color": "#ef4444"}, "label": {"show": True, "formatter": "{c}%"}}]
    if show_rev:
        series.append({"name": "模型复核后仍存疑", "type": "bar", "stack": "pct", "data": rev_vals[::-1],
                       "itemStyle": {"color": "#f59e0b"}, "label": {"show": True, "formatter": "{c}%"}})
    if show_inc:
        series.append({"name": "复核失败/未完成", "type": "bar", "stack": "pct",
                       "data": inc_vals[::-1], "itemStyle": {"color": "#94a3b8"},
                       "label": {"show": True, "formatter": "{c}%"}})
    series.append({"name": "暂未检出相似", "type": "bar", "stack": "pct", "data": orig_vals[::-1],
                   "itemStyle": {"color": "#22c55e"}, "label": {"show": True, "formatter": "{c}%"}})
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": ["高置信同源代码"]
                   + (["模型复核后仍存疑"] if show_rev else [])
                   + (["复核失败/未完成"] if show_inc else [])
                   + ["暂未检出相似"]},
        "grid": {"left": "25%", "right": "12%", "top": "8%", "bottom": "6%"},
        "xAxis": {"type": "value", "max": 100,
                  "axisLabel": {"formatter": "{value}%"}},
        "yAxis": {"type": "category", "data": labels[::-1]},
        "series": series,
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
        ("高置信同源代码", conf, "#ef4444"),
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


def _echarts_overall_donut(copy_pct: float, review_pct: float = 0.0,
                           review_incomplete_pct: float = 0.0,
                           original_pct: float | None = None) -> str:
    """整体同源/有效存疑/复核未完成/暂未检出环形图（按函数加权）。"""
    c = round(copy_pct, 1)
    rv = round(review_pct, 1)
    inc = round(review_incomplete_pct, 1)
    o = round(original_pct if original_pct is not None else max(0.0, 100 - c - rv - inc), 1)
    option = {
        "title": {
            "text": f"{c}%", "subtext": "高置信同源占纳入统计函数",
            "left": "center", "top": "38%",
            "textAlign": "center",
            "textStyle": {"fontSize": 26, "fontWeight": "bold", "color": "#ef4444"},
            "subtextStyle": {"fontSize": 11, "color": "#64748b"},
        },
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}%"},
        "legend": {"bottom": 0, "data": ["高置信同源代码"]
                   + (["模型复核后仍存疑"] if rv else [])
                   + (["复核失败/未完成"] if inc else [])
                   + ["暂未检出相似"]},
        "series": [{
            "name": "占比", "type": "pie", "radius": ["54%", "78%"],
            "center": ["50%", "44%"], "avoidLabelOverlap": False,
            "label": {"show": False}, "labelLine": {"show": False},
            "data": [{"value": c, "name": "高置信同源代码", "itemStyle": {"color": "#ef4444"}}]
                    + ([{"value": rv, "name": "模型复核后仍存疑", "itemStyle": {"color": "#f59e0b"}}] if rv else [])
                    + ([{"value": inc, "name": "复核失败/未完成", "itemStyle": {"color": "#94a3b8"}}] if inc else [])
                    + [{"value": o, "name": "暂未检出相似", "itemStyle": {"color": "#22c55e"}}],
        }],
    }
    return (
        '<div class="echarts-chart" style="height:230px">'
        f'<script type="application/json">{json.dumps(option, ensure_ascii=False)}</script>'
        '</div>'
    )


def _query_loc(q: dict) -> int:
    """函数有效规模：优先非空源码行，缺源码时回退位置信息。"""
    code = q.get("raw_code") or ""
    nonblank = sum(1 for line in code.splitlines() if line.strip())
    if nonblank:
        return nonblank
    start, end = int(q.get("start_line") or 0), int(q.get("end_line") or 0)
    return max(1, end - start + 1) if start or end else 1


def _effective_similar_loc(s: dict) -> int:
    """估算有效相似代码行；行级证据优先，缺失时按函数规模×最终相似度回退。"""
    q = s.get("query_func") or {}
    q_loc = _query_loc(q)
    ev = s.get("evidence") or {}
    matched = int(ev.get("exact_match_lines") or 0) + int(ev.get("renamed_match_lines") or 0)
    if matched <= 0:
        matched = round(q_loc * max(0.0, min(1.0, _pair_sim(s))))
    return max(1, min(q_loc, matched))


def _source_metrics(suspects: list[dict]) -> list[dict]:
    """按来源仓库聚合唯一目标函数、有效相似行、文件和子系统，避免候选对重复放大。"""
    by_repo: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for s in suspects:
        if s.get("tier") != "confirmed" or _is_excluded_pair(s):
            continue
        q = s.get("query_func") or {}
        repo = str((s.get("candidate_func") or {}).get("repo_id") or "未知来源")
        key = (q.get("file_path", ""), q.get("func_name", ""))
        row = {
            "loc": _effective_similar_loc(s),
            "file": q.get("file_path", ""),
            "module": q.get("module_tag", "other"),
            "sim": _pair_sim(s),
        }
        current = by_repo[repo].get(key)
        if current is None or (row["loc"], row["sim"]) > (current["loc"], current["sim"]):
            by_repo[repo][key] = row
    metrics = []
    for repo, targets in by_repo.items():
        rows = list(targets.values())
        metrics.append({
            "repo": repo,
            "functions": len(rows),
            "effective_loc": sum(r["loc"] for r in rows),
            "files": len({r["file"] for r in rows}),
            "modules": len({r["module"] for r in rows}),
        })
    return sorted(metrics, key=lambda x: (-x["functions"], -x["effective_loc"], x["repo"]))


def _echarts_top_sources(metrics: list[dict], top: int = 8) -> str:
    """Top 同源来源柱图：按唯一目标函数计数，不再按候选 pair 计数。"""
    order = metrics[:top]
    if not order:
        return ""
    labels = [x["repo"] for x in order][::-1]
    conf = [x["functions"] for x in order][::-1]
    series = [
        ("高置信同源代码", conf, "#ef4444"),
    ]
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": [s[0] for s in series]},
        "grid": {"left": "33%", "right": "8%", "top": "14%", "bottom": "6%"},
        "xAxis": {"type": "value", "minInterval": 1},
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


def _source_metrics_table(metrics: list[dict], top: int = 8) -> str:
    if not metrics:
        return ""
    rows = "".join(
        '<tr>'
        f'<td>{html.escape(x["repo"])}</td>'
        f'<td>{x["functions"]}</td><td>{x["effective_loc"]}</td>'
        f'<td>{x["files"]}</td><td>{x["modules"]}</td>'
        '</tr>'
        for x in metrics[:top]
    )
    return (
        '<div class="source-metrics-table"><table>'
        '<thead><tr><th>历史作品</th><th>唯一目标函数</th><th>有效相似行</th>'
        '<th>涉及文件</th><th>涉及子系统</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


_LEGEND_HTML = (
    '<div class="legend">'
    '<span><b>档位：</b></span>'
    '<span><span class="dot" style="background:#ef4444"></span>高置信同源代码</span>'
    '<span><span class="dot" style="background:#f59e0b"></span>模型复核后仍存疑</span>'
    '<span><span class="dot" style="background:#94a3b8"></span>复核失败/未完成（不计为存疑）</span>'
    '<span><span class="dot" style="background:#22c55e"></span>暂未检出相似</span>'
    '<span style="margin-left:.6rem"><b>相似程度：</b></span>'
    '<span>匹配片段完全相同（仅指命中片段；同时查看函数覆盖比例）</span>'
    '<span>近乎相同（仅零星行不同，≤15%）</span>'
    '<span>高度相似（较多行需归一化才匹配）</span>'
    '</div>'
)


def _kpi(value, label: str, color: str = "#0f172a") -> str:
    return (f'<div class="kpi"><span class="v" style="color:{color}">{value}</span>'
            f'<span class="l">{html.escape(label)}</span></div>')


def _exclusion_totals(suspects: list[dict]) -> dict:
    """统计「已扣除的机械误报」各类去重函数数，**互斥归一**（每个函数按优先级只归一类），
    使各类之和 == 总数，供导读卡透明呈现，避免「分类相加远超总数」让评审困惑。

    优先级（从具体到泛化）：第三方库 > 上游框架/ABI > 上游基线衍生 > 跨架构误报 > 公共样板。
    """
    _PRIO = ("library", "upstream", "baseline", "false_positive", "common")

    def _cat_of(s: dict) -> str | None:
        if s.get("reuse_library"):                    return "library"
        if s.get("upstream_vendored") or s.get("abi_constrained"): return "upstream"
        if s.get("tier") == "baseline_derived":       return "baseline"
        if s.get("false_positive"):                   return "false_positive"
        if s.get("tier") == "common_code" or s.get("common_code_note"): return "common"
        return None

    # 每个 query 函数跨其全部嫌疑对取**最高优先级**类别（一函数只归一类）
    best: dict[tuple, str] = {}
    for s in suspects:
        cat = _cat_of(s)
        if cat is None:
            continue
        key = (s.get("query_func", {}).get("file_path", ""),
               s.get("query_func", {}).get("func_name", ""))
        cur = best.get(key)
        if cur is None or _PRIO.index(cat) < _PRIO.index(cur):
            best[key] = cat
    out = {k: 0 for k in _PRIO}
    for cat in best.values():
        out[cat] += 1
    out["total_excluded"] = len(best)
    return out


def _retrieval_status(contract: dict | None) -> str:
    """把召回边界直接写进交付报告，旧产物不得伪装成完整查全结果。"""
    errors = contract_errors(contract)
    if errors:
        detail = "；".join(html.escape(e) for e in errors)
        return (
            '<section id="retrieval-stale" data-retrieval-contract-version="missing" '
            'data-retrieval-complete="false" class="mb-5 p-4 rounded border-2 border-red-500 bg-red-50">'
            '<div class="font-bold text-red-700">⚠ 本报告缺少完整召回证明，已失效，必须重跑</div>'
            f'<p class="text-sm text-red-700 mt-1 mb-0">{detail}。旧报告只能用于定位历史问题，'
            '不得据此认定任何函数原创或未借鉴。</p></section>'
        )
    coverage = contract["history_coverage"]
    channels = "、".join(contract.get("channels") or [])
    return (
        f'<section id="retrieval-contract" data-retrieval-contract-version="{CONTRACT_VERSION}" '
        'data-retrieval-complete="true" class="mb-5 p-3 rounded border border-emerald-300 bg-emerald-50">'
        '<div class="text-sm font-semibold text-emerald-800">召回完整性已核验</div>'
        f'<p class="text-xs text-emerald-700 mt-1 mb-0">历史作品覆盖 '
        f'{coverage["covered"]}/{coverage["configured"]}；候选禁止静默截断；'
        f'仅比较同一编程语言；启用通道：{html.escape(channels)}。'
        '未命中仍只表示“当前系统暂未检出”，不等于原创认定。</p>'
        '</section>'
    )


def _reading_guide(query_repo_id: str, borrowed_n: int, review_n: int,
                   review_failed_n: int, review_pending_n: int, original_n: int,
                   overall_copy_pct: float, excl: dict,
                   retrieval_contract: dict | None = None) -> str:
    """报告顶部「导读 + 体检结论」卡：用大白话告诉第一次看报告的老师——这是什么、数字怎么读、
    系统做了哪些自动过滤、该如何使用。回应「辅助参考而非最终裁决」的项目定位。"""
    total_kept = borrowed_n + review_n + review_failed_n + review_pending_n + original_n
    # 体检结论（按疑似借鉴占比给一句话定性，中性、不替评审下结论）
    if overall_copy_pct <= 5:
        verdict, vcolor, vicon = "当前库内相似命中较少", "#16a34a", "✓"
        vtext = "当前历史库中仅检出少量高度相似函数；未命中不等于已证明原创。"
    elif overall_copy_pct <= 20:
        verdict, vcolor, vicon = "当前库内少量相似", "#16a34a", "✓"
        vtext = "有一部分函数与历史作品相似；其余仅表示当前未检出，建议结合人工核验。"
    elif overall_copy_pct <= 50:
        verdict, vcolor, vicon = "相似比例偏高，需重点核查", "#d97706", "!"
        vtext = "相当一部分函数与历史作品相似，建议逐一人工核对借鉴清单。"
    else:
        verdict, vcolor, vicon = "相似比例很高，重点核查", "#dc2626", "!"
        vtext = "多数函数与历史作品高度相似，建议优先人工复核。"

    excl_total = excl.get("total_excluded", 0)
    excl_parts = []
    if excl.get("upstream"):  excl_parts.append(f"上游框架/ABI 受限 {excl['upstream']}")
    if excl.get("baseline"):  excl_parts.append(f"上游基线衍生 {excl['baseline']}")
    if excl.get("library"):   excl_parts.append(f"第三方库 {excl['library']}")
    if excl.get("common"):    excl_parts.append(f"公共样板 {excl['common']}")
    if excl.get("false_positive"): excl_parts.append(f"跨架构/样板误报 {excl['false_positive']}")
    excl_detail = "、".join(excl_parts) if excl_parts else "无"

    return (
        _retrieval_status(retrieval_contract) +
        '<section id="guide" data-section-id="guide" '
        'class="mb-6 p-5 rounded-lg border-l-4 bg-blue-50/60" style="border-left-color:#3b82f6">'
        '<div class="flex items-start gap-3">'
        '<div class="text-2xl leading-none">📋</div>'
        '<div class="flex-1 min-w-0">'
        '<div class="text-base font-bold text-slate-800 mb-1">报告导读（请先阅读）</div>'
        '<p class="text-sm text-slate-700 leading-relaxed m-0">'
        '本报告由 AI 自动比对该作品与历年参赛作品，标记「与历史代码相似、可能存在借鉴」的函数，'
        '<b>仅作为人工评审的辅助参考，不构成抄袭的最终认定</b>。'
        '系统已自动过滤掉所有团队都会用的「上游框架代码、第三方库、ABI/规范受限写法」等机械重复，'
        '下方数字仅针对<b>排除机械重复后的待评估部分</b>。</p>'
        # 体检结论
        f'<div class="mt-3 inline-flex items-center gap-2 px-3 py-1.5 rounded-md font-semibold text-sm" '
        f'style="background:{vcolor}1a;color:{vcolor}">'
        f'<span class="inline-flex items-center justify-center w-5 h-5 rounded-full text-white text-xs" '
        f'style="background:{vcolor}">{vicon}</span>'
        f'初步体检：{verdict}（高置信同源代码占纳入统计函数 {overall_copy_pct}%）</div>'
        f'<p class="text-sm text-slate-600 mt-2 mb-0">{vtext}</p>'
        # 透明度：扣除了多少误报
        '<div class="mt-3 text-xs text-slate-500 bg-white/70 rounded px-3 py-2 border border-slate-200">'
        f'📊 <b>过滤透明度</b>：系统纳入统计 <b>{total_kept}</b> 个函数，其中 '
        f'<b>{borrowed_n}</b> 个高置信同源代码、<b>{review_n}</b> 个模型有效复核后仍存疑、'
        f'<b>{review_failed_n}</b> 个复核失败、<b>{review_pending_n}</b> 个复核未完成；'
        f'另已剔除 <b>{excl_total}</b> 个机械重复函数（{excl_detail}），这些不计入上方借鉴统计，'
        '在第 7 节「合法复用与许可证合规」分类列出，可点开核对。'
        '</div>'
        '<p class="text-xs text-slate-400 mt-2 mb-0">'
        '建议用法：①看总体结果 → ②先核对「高置信同源代码」的代码证据 → '
        '③再查看模型存疑与复核异常清单 → ④结合候选创新与合规复用证据完成判断。</p>'
        '</div></div></section>'
    )


def _summary_card(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_match_count: int = 0,
    file_similar_count: int = 0,
    retrieval_contract: dict | None = None,
) -> str:
    # 按**函数**计（与各清单一致）：借鉴/疑似借鉴/原创 来自三类口径的统计
    borrowed_n = sum(st["confirmed"] for st in submodule_stats.values())
    review_n   = sum(st["review"] for st in submodule_stats.values())
    review_failed_n = sum(st.get("review_failed", 0) for st in submodule_stats.values())
    review_pending_n = sum(st.get("review_pending", 0) for st in submodule_stats.values())
    review_incomplete_n = review_failed_n + review_pending_n
    original_n = sum(st["original"] for st in submodule_stats.values())

    # 加权总占比（按函数）
    all_total = sum(st["total"] for st in submodule_stats.values()) or 1
    overall_copy_pct = round(borrowed_n / all_total * 100, 1)
    overall_review_pct = round(review_n / all_total * 100, 1)
    overall_incomplete_pct = round(review_incomplete_n / all_total * 100, 1)
    overall_original_pct = round(original_n / all_total * 100, 1)

    # KPI 卡片（按函数；库复用 / 公共样板已剔除，单列各自小节）
    # 高置信同源与模型仍存疑明确分栏，避免把“已经过模型但模型拿不准”误读成尚未审核。
    kpis = (
        '<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3">'
        + _kpi(f"{borrowed_n}", "高置信同源代码（函数）", "#ef4444")
        + (_kpi(f"{review_n}", "模型复核后仍存疑（函数）", "#d97706") if review_n else "")
        + (_kpi(f"{review_failed_n}", "模型复核失败（函数）", "#64748b") if review_failed_n else "")
        + (_kpi(f"{review_pending_n}", "模型复核未完成（函数）", "#64748b") if review_pending_n else "")
        + _kpi(f"{original_n}", "暂未检出相似（函数）", "#16a34a")
        + _kpi(f"{file_match_count}", "整文件相同（文件）", "#e11d48")
        + _kpi(f"{file_similar_count}", "整体相似文件（个）", "#d97706")
        + '</div>'
    )

    # 头部：环形图（整体借鉴%）+ Top 借鉴来源仓库，左右并排
    donut = _echarts_overall_donut(
        overall_copy_pct, overall_review_pct, overall_incomplete_pct, overall_original_pct)
    source_metrics = _source_metrics(suspects)
    top_src = _echarts_top_sources(source_metrics)
    source_table = _source_metrics_table(source_metrics)
    head_charts = (
        '<div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4 items-start">'
        '<div><div class="chart-title">整体结果分布（按纳入统计函数）</div>'
        + donut + '</div>'
        + ('<div><div class="chart-title">高置信同源来源（按唯一目标函数，Top 8）</div>'
           + top_src + source_table + '</div>' if top_src else '<div></div>')
        + '</div>'
    )

    tier_chart = (
        '<div class="chart-title mt-4">各模块高置信同源函数数</div>'
        + _echarts_tier_distribution(submodule_stats)
    )
    pct_title = '各模块：高置信同源 / 模型仍存疑 / 复核未完成 / 暂未检出 占比'
    pct_chart = (
        f'<div class="chart-title mt-4">{pct_title}</div>'
        + _echarts_overview(submodule_stats)
    )

    # 顶部导读 + 体检结论卡（老师第一眼看到，建立正确语境）
    guide = _reading_guide(query_repo_id, borrowed_n, review_n,
                           review_failed_n, review_pending_n, original_n,
                           overall_copy_pct, _exclusion_totals(suspects),
                           retrieval_contract)

    return (
        guide +
        '<section id="summary" data-section-id="summary" '
        'class="summary-card">'
        '<div class="summary-heading"><div><span class="summary-eyebrow">REPORT OVERVIEW</span>'
        '<h2>总体结果</h2></div><span class="summary-repo">'
        f'{html.escape(query_repo_id)}</span></div>'
        '<p class="text-xs text-slate-500 mt-1 mb-0">下列数字为<b>扣除上游框架/库/规范受限代码后的待评估口径</b>；'
        '「高置信同源代码」和有效模型复核后的「仍存疑」均为辅助筛查结果；'
        '复核失败/未完成是流程状态，不是风险结论。'
        '<b>需结合代码证据人工判断</b>，不代表系统已经认定抄袭。</p>'
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
    """单个 query 函数的全部候选来源：含来源、相似度、片段性质与目标函数覆盖率。"""
    extra = group.get("candidate_count", len(group["candidates"])) - len(group["candidates"])
    items = []
    for c in group["candidates"]:
        ck = _clone_summary(c)
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


def _code_evidence(group: dict, colspan: int = 6) -> tuple[str, str]:
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
        f'<tr x-show="o" x-cloak><td colspan="{colspan}" class="p-0">'
        '<div class="code-pair">'
        '<div class="code-col"><div class="code-h">新作品（差异行标黄）</div>'
        f'<div class="code-body">{left_html}</div></div>'
        f'<div class="code-col"><div class="code-h">最强候选来源 · {src_label}（差异行标红）</div>'
        f'<div class="code-body">{right_html}</div></div>'
        '</div></td></tr>'
    )
    return toggle, panel


_REVIEW_VERDICT_STYLE = {
    "借鉴":   ("bg-red-50 text-red-700", "借鉴"),
    "疑似":   ("bg-amber-50 text-amber-700", "模型仍存疑"),
    "非借鉴": ("bg-green-50 text-green-700", "非借鉴"),
    "复核失败": ("bg-rose-50 text-rose-700", "复核失败"),
    "未复核": ("bg-slate-100 text-slate-500", "复核未完成"),
}


def _verdict_cell(g: dict) -> str:
    """展示职责门控、严格复核结论、可核验理由及代码锚点。"""
    v = g.get("review_verdict", "未复核")
    cls, lbl = _REVIEW_VERDICT_STYLE.get(v, _REVIEW_VERDICT_STYLE["未复核"])
    reason = html.escape(g.get("review_reason", "") or "")
    responsibility = html.escape(g.get("review_responsibility", "未判定") or "未判定")
    responsibility_reason = html.escape(
        g.get("review_responsibility_reason", "") or "")
    anchors = [str(x) for x in (g.get("review_evidence_anchors") or []) if str(x).strip()]
    role_html = (
        f'<div class="text-xs text-slate-500 mt-1 max-w-[18rem]">'
        f'<b>职责：{responsibility}</b>'
        + (f' · {responsibility_reason}' if responsibility_reason else "")
        + '</div>'
    ) if responsibility != "未判定" or responsibility_reason else ""
    anchors_html = (
        '<div class="review-anchors">证据锚点：'
        + " ".join(f'<code>{html.escape(a)}</code>' for a in anchors)
        + '</div>'
    ) if anchors else ""
    return (f'<td class="text-xs align-top">'
            f'<span class="px-2 py-0.5 rounded {cls} whitespace-nowrap font-semibold">{lbl}</span>'
            + role_html
            + (f'<div class="text-xs text-slate-400 mt-1 max-w-[16rem]">{reason}</div>' if reason else "")
            + anchors_html
            + '</td>')


def _review_priority(g: dict) -> dict:
    """为模型仍存疑函数生成可解释的人工复核优先级。"""
    sim = max(0.0, min(1.0, float(g.get("overall_sim") or 0.0)))
    lines = max(1, sum(1 for line in (g.get("query_code") or "").splitlines() if line.strip()))
    module = g.get("module", "other")
    clone_weight = {"exact": 1.0, "near_dup": .85, "similar": .65}.get(
        g.get("clone_type"), .6)
    score = round(100 * (
        .45 * sim + .25 * _MODULE_PRIORITY.get(module, .55)
        + .2 * min(1.0, lines / 80) + .1 * clone_weight
    ))
    if score >= 75:
        level, cls = "高", "priority-high"
    elif score >= 56:
        level, cls = "中", "priority-medium"
    else:
        level, cls = "低", "priority-low"
    reasons = []
    if sim >= .9: reasons.append("相似度很高")
    elif sim >= .7: reasons.append("相似度中高")
    if lines >= 60: reasons.append("代码规模较大")
    if _MODULE_PRIORITY.get(module, .55) >= .95: reasons.append("内核核心子系统")
    if int(g.get("candidate_count") or len(g.get("candidates") or [])) > 1:
        reasons.append("存在多个历史来源")
    if not reasons: reasons.append("模型证据不足")
    return {"score": score, "level": level, "class": cls, "reasons": reasons}


def _priority_cell(g: dict) -> str:
    p = g.get("review_priority") or _review_priority(g)
    reason = "、".join(p["reasons"])
    return (
        '<td class="review-priority-cell">'
        f'<span class="priority-badge {p["class"]}">{p["level"]} {p["score"]}</span>'
        f'<small>{html.escape(reason)}</small></td>'
    )


def _groups_table(title: str, groups: list[dict], linker, query_repo_id: str, accent: str,
                  show_verdict: bool = False, show_priority: bool = False) -> str:
    """渲染一张「按 query 函数聚合候选」的清单表（U3 分类清单 + U6 全候选 + 代码证据）。
    show_verdict=True 时（疑似借鉴清单）额外加一列「复核结论」展示低端模型的借鉴判定。"""
    if not groups:
        return ""
    bodies = []
    for g in groups:
        colspan = 6 + int(show_verdict) + int(show_priority)
        toggle, panel = _code_evidence(g, colspan)
        main = (
            '<tr>'
            '<td class="font-mono text-xs align-top">'
            + _make_gitlab_anchor(linker, query_repo_id, g["query_file"], g["query_start"])
            + '</td>'
            f'<td class="text-xs align-top">{html.escape(g["query_func"])}'
            + ('<span class="ml-1 px-1.5 py-0.5 rounded bg-purple-50 text-purple-700 '
               'whitespace-nowrap" title="相似度中等、经 AI 模型复核认定为借鉴（非逐行铁证）">'
               '模型复核认定</span>' if g.get("via_review") else "")
            + '</td>'
            + (_priority_cell(g) if show_priority else "")
            + (_verdict_cell(g) if show_verdict else "")
            + f'<td class="text-xs align-top font-semibold {_sim_class(g["overall_sim"])}">{g["overall_sim"]}</td>'
            f'<td class="text-xs align-top">{html.escape(_clone_summary(g))}</td>'
            '<td class="align-top">' + _candidates_cell(g, linker) + '</td>'
            f'<td class="text-xs align-top whitespace-nowrap">{toggle}</td>'
            '</tr>'
        )
        bodies.append(
            f'<tbody x-data="{{o:false}}" class="border-b border-slate-100">{main}{panel}</tbody>'
        )
    verdict_th = ('<th class="text-left p-2 border-b">复核结论</th>' if show_verdict else "")
    priority_th = ('<th class="text-left p-2 border-b">复核优先级</th>' if show_priority else "")
    # 计数按 query 函数 (文件,函数名) 去重，与导航栏 badge / 模块统计 compute_submodule_stats
    # 同口径（后者也按 (file_path,func_name) 去重）；len(groups) 会把同名不同起始行的函数
    # 算成多个，导致「badge 35 / 清单 37」之类前后矛盾。
    n_funcs = len({(g.get("query_file", ""), g.get("query_func", "")) for g in groups})
    return (
        f'<div class="mt-3"><div class="text-sm font-semibold {accent} mb-1">{html.escape(title)}'
        f'（{n_funcs} 个函数）</div>'
        '<div class="overflow-x-auto">'
        '<table class="w-full text-sm border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">新作品 文件:行</th>'
        '<th class="text-left p-2 border-b">函数</th>'
        + priority_th
        + verdict_th
        + '<th class="text-left p-2 border-b">整体相似度</th>'
        '<th class="text-left p-2 border-b">匹配片段性质与函数覆盖</th>'
        '<th class="text-left p-2 border-b">候选来源（全部）</th>'
        '<th class="text-left p-2 border-b">代码证据</th>'
        '</tr></thead>'
        f'{"".join(bodies)}'
        '</table></div></div>'
    )


def _toc_link(sid: str, label: str, badge: str = "", badge_tone: str = "default") -> str:
    """生成高度和对齐方式一致的目录项。长标题最多显示两行，数字徽标固定靠右。"""
    badge_html = ""
    if badge:
        badge_html = (
            f'<span class="toc-badge {html.escape(badge_tone)}">'
            f'{html.escape(str(badge))}</span>'
        )
    return (
        f'<a class="toc-link" href="#{html.escape(sid)}">'
        f'<span class="toc-link-label">{html.escape(label)}</span>{badge_html}</a>'
    )


def _toc_group(title: str, items: list[str]) -> str:
    if not items:
        return ""
    return (
        '<div class="toc-group">'
        f'<div class="toc-group-title">{html.escape(title)}</div>'
        + "".join(items) + '</div>'
    )


def _collapsible_html(sid: str, title: str, body: str, *, default_open: bool = True,
                      tone: str = "default", subtitle: str = "") -> str:
    """对比报告所有正文卡片共用的结构，避免各节边距、标题和折叠按钮各写一套。"""
    opened = "true" if default_open else "false"
    subtitle_html = (
        f'<span class="section-subtitle">{html.escape(subtitle)}</span>' if subtitle else ""
    )
    return (
        f'<section id="{html.escape(sid)}" data-section-id="{html.escape(sid)}" '
        f'class="report-section" data-tone="{html.escape(tone)}" '
        f'x-data="{{open: {opened}}}" '
        f'x-init="(function(){{const s=localStorage.getItem(\'cmp:{sid}\');'
        f'if(s!==null)open=(s===\'1\');}})()">'
        '<button type="button" class="section-toggle" '
        f'@click="open=!open;localStorage.setItem(\'cmp:{sid}\',open?\'1\':\'0\')" '
        ':aria-expanded="open.toString()">'
        '<span class="section-chevron" aria-hidden="true" x-text="open?\'▾\':\'▸\'"></span>'
        '<span class="section-heading">'
        f'<span class="section-title">{html.escape(title)}</span>{subtitle_html}</span>'
        '</button>'
        f'<div class="section-body" x-show="open" x-cloak>{body}</div>'
        '</section>'
    )


def _chapter_heading(index: str, title: str, description: str) -> str:
    """正文一级分组标题；不加入滚动监听，目录仍精确定位到实际内容卡片。"""
    return (
        '<div class="chapter-heading">'
        f'<span class="chapter-index">{html.escape(index)}</span>'
        '<div>'
        f'<h2>{html.escape(title)}</h2>'
        f'<p>{html.escape(description)}</p>'
        '</div></div>'
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
    review_pct = stats.get("review_pct", 0.0)
    incomplete_pct = stats.get("review_incomplete_pct", 0.0)
    original_pct = stats.get("original_pct", 0.0)

    # 子模块统计概要行（借鉴/原创，按函数；库复用/公共样板不计入）
    # 只有有效“疑似”算模型仍存疑；失败/未完成单列，不能混算。
    rev_n = stats.get("review", 0)
    incomplete_n = stats.get("review_incomplete", 0)
    review_chip = (f'<span class="status-chip status-review">'
                   f'模型复核后仍存疑 {review_pct*100:.0f}%</span>') if rev_n else ""
    review_cnt = f' / 模型复核后仍存疑 {rev_n}' if rev_n else ""
    incomplete_chip = (f'<span class="status-chip status-incomplete">'
                       f'复核失败/未完成 {incomplete_pct*100:.0f}%</span>') if incomplete_n else ""
    incomplete_cnt = f' / 复核失败或未完成 {incomplete_n}' if incomplete_n else ""
    stat_row = (
        f'<div class="module-summary">'
        f'<span class="status-chip status-confirmed">'
        f'高置信同源代码 {copy_pct*100:.0f}%</span>'
        + review_chip +
        incomplete_chip +
        f'<span class="status-chip status-original">'
        f'暂未检出相似 {original_pct*100:.0f}%</span>'
        f'<span class="module-summary-text">函数总数 {stats.get("total","—")} 个 · '
        f'高置信同源 {stats.get("confirmed",0)}{review_cnt}{incomplete_cnt} · 暂未检出 {stats.get("original",0)}</span>'
        f'<span class="module-summary-source">主要来源：{html.escape(stats.get("top_source","—"))}</span>'
        f'</div>'
        + _pct_bar(copy_pct, review_pct, incomplete_pct, original_pct)
    )

    # 只展示「已确认借鉴」清单（U6：每函数列出全部候选）
    confirmed = [g for g in pairs if g["overall_tier"] == "confirmed"]
    table = _groups_table("高置信同源代码清单", confirmed, linker, query_repo_id, "text-red-700")

    # 语义分析片段（从 LLM 输出中抠取当前模块的部分）。
    # 表格已逐函数给出 文件:行 与链接，分析正文只讲语义、不再列地址，故不做链接化；
    # 兜底清掉模型偶尔仍写出的裸 path:line 文本，避免与表格重复。
    mod_analysis = _extract_module_analysis(analysis_html, mod)
    if mod_analysis:
        mod_analysis = _strip_addr_refs(mod_analysis)

    body = stat_row + table
    if mod_analysis:
        body += (
            '<div class="analysis-panel">'
            '<h4>语义级功能相似分析</h4>'
            + mod_analysis +
            '</div>'
        )

    section = _collapsible_html(
        sid, label, body, tone="evidence",
        subtitle="按函数聚合全部同语言候选，可展开查看并排代码证据",
    )
    n_conf = stats.get("confirmed", 0)
    toc = _toc_link(sid, label, str(n_conf), "confirmed" if n_conf else "zero")
    return toc, section


_FEATURE_LABELS = {
    "ext4": "Ext4 文件系统", "fat32": "FAT32 文件系统", "vfs": "虚拟文件系统",
    "inode": "Inode 与目录项", "page": "页与页缓存", "frame": "物理页帧",
    "signal": "信号机制", "syscall": "系统调用", "trap": "异常与中断",
    "task": "任务管理", "sched": "调度器", "timer": "时钟与定时器",
    "virtio": "VirtIO 驱动", "uart": "串口驱动", "block": "块设备",
    "net": "网络栈", "fs": "文件系统", "mm": "内存管理",
}
_MODULE_PRIORITY = {"sched": 1.0, "mm": 1.0, "fs": 1.0, "trap": .95,
                    "driver": .8, "arch": .75, "other": .55}


def _feature_label(group: dict) -> tuple[str, str]:
    """从路径与函数名提取稳定的功能簇键；无法细分时回退子系统。"""
    path = str(group.get("query_file") or "").replace("\\", "/").lower()
    func = str(group.get("query_func") or "").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", path + "/" + func) if w]
    for token in _FEATURE_LABELS:
        if token in words or token in path:
            return token, _FEATURE_LABELS[token]
    mod = group.get("module", "other")
    return mod, _MODULE_DISPLAY.get(mod, mod)


def build_similarity_clusters(file_pairs: list[dict]) -> list[dict]:
    """把 confirmed 函数组合并为来源×子系统×功能的评审事件。"""
    grouped: dict[tuple, dict] = {}
    for g in file_pairs:
        if g.get("overall_tier") != "confirmed" or not g.get("candidates"):
            continue
        best = g["candidates"][0]
        feature_key, feature_name = _feature_label(g)
        repo = best.get("ref_repo", "未知来源")
        mod = g.get("module", "other")
        key = (repo, mod, feature_key)
        cluster = grouped.setdefault(key, {
            "source": repo, "module": mod, "feature": feature_name, "groups": [],
            "files": set(), "effective_loc": 0, "similarities": [],
        })
        cluster["groups"].append(g)
        cluster["files"].add(g.get("query_file", ""))
        lines = max(1, sum(1 for line in (g.get("query_code") or "").splitlines() if line.strip()))
        cluster["effective_loc"] += max(1, round(lines * float(g.get("overall_sim") or 0.0)))
        cluster["similarities"].append(float(g.get("overall_sim") or 0.0))

    clusters = []
    for c in grouped.values():
        c["groups"].sort(key=lambda g: -float(g.get("overall_sim") or 0.0))
        c["function_count"] = len({(g.get("query_file", ""), g.get("query_func", ""))
                                   for g in c["groups"]})
        c["file_count"] = len(c.pop("files"))
        mean_sim = sum(c.pop("similarities")) / max(1, len(c["groups"]))
        size_factor = min(1.0, c["effective_loc"] / 300)
        c["priority_score"] = round(100 * (
            .5 * mean_sim + .3 * _MODULE_PRIORITY.get(c["module"], .55) + .2 * size_factor
        ))
        c["priority"] = "重点核查" if c["priority_score"] >= 78 else (
            "优先核查" if c["priority_score"] >= 62 else "常规核查")
        clusters.append(c)
    return sorted(clusters, key=lambda c: (-c["priority_score"], -c["effective_loc"], c["feature"]))


def _cluster_section(file_pairs: list[dict], analysis_html: str, linker,
                     query_repo_id: str) -> tuple[str, str]:
    clusters = build_similarity_clusters(file_pairs)
    sid = "sec-clusters"
    if not clusters:
        body = '<p class="text-sm text-slate-500">没有形成高置信同源功能簇。</p>'
    else:
        cards = []
        analyzed_modules = set()
        for index, c in enumerate(clusters, 1):
            tone = "critical" if c["priority"] == "重点核查" else "normal"
            table = _groups_table(
                "簇内函数证据", c["groups"], linker, query_repo_id, "text-red-700")
            analysis = ""
            if c["module"] not in analyzed_modules:
                frag = _extract_module_analysis(analysis_html, c["module"])
                if frag:
                    analyzed_modules.add(c["module"])
                    analysis = '<div class="cluster-analysis"><b>模块语义说明</b>' + _strip_addr_refs(frag) + '</div>'
            cards.append(
                f'<article class="cluster-card" data-priority="{tone}" x-data="{{open:{str(index <= 3).lower()}}}">'
                '<button type="button" class="cluster-head" @click="open=!open">'
                f'<span class="cluster-index">C{index:02d}</span><span class="cluster-main">'
                f'<b>{html.escape(c["feature"])}</b><small>{html.escape(_MODULE_DISPLAY.get(c["module"], c["module"]))}'
                f' · 主要来源 {html.escape(c["source"])}</small></span>'
                f'<span class="cluster-metrics">{c["function_count"]} 函数 · {c["file_count"]} 文件 · '
                f'{c["effective_loc"]} 有效相似行</span>'
                f'<span class="cluster-priority">{c["priority"]} {c["priority_score"]}</span>'
                '<span class="cluster-chevron" x-text="open?\'▾\':\'▸\'"></span></button>'
                f'<div class="cluster-body" x-show="open" x-cloak>{table}{analysis}</div></article>'
            )
        intro = (
            '<div class="section-intro">系统将同一来源、同一子系统且属于同一功能域的函数合并为一个'
            '“同源事件”。优先级综合代码相似度、有效相似行和内核子系统重要度；函数级链接与并排代码'
            '仍保留在簇内。</div>'
        )
        body = intro + "".join(cards)
    section = _collapsible_html(
        sid, "高置信同源功能簇", body, tone="evidence",
        subtitle="将零散函数聚合成可评审的功能级同源事件",
    )
    return _toc_link(sid, "高置信同源功能簇", str(len(clusters)), "confirmed"), section


def _lineage_section(query_repo_id: str, suspects: list[dict], linker) -> tuple[str, str]:
    """展示排除共同上游后的来源统计，不输出时间或版本方向判断。"""
    sid = "sec-lineage"
    metrics = _source_metrics(suspects)
    rows = []
    for x in metrics[:12]:
        rows.append(
            '<tr>'
            f'<td>{_ref_repo_anchor(linker, x["repo"])}</td>'
            f'<td>{x["functions"]}</td><td>{x["effective_loc"]}</td></tr>'
        )
    exclusion = _exclusion_totals(suspects)
    categories = [
        ("共同上游 / ABI", exclusion.get("upstream", 0)),
        ("比赛基线衍生", exclusion.get("baseline", 0)),
        ("第三方库", exclusion.get("library", 0)),
        ("机械误报", exclusion.get("false_positive", 0)),
        ("公共 / 样板代码", exclusion.get("common", 0)),
    ]
    cards = "".join(
        f'<div><span>{html.escape(label)}</span><b>{int(count)}</b></div>'
        for label, count in categories
    )
    body = (
        '<div class="lineage-summary">'
        f'<div><span>唯一排除函数</span><b>{exclusion.get("total_excluded", 0)}</b></div>'
        f'{cards}'
        '</div>'
        '<div class="section-intro">系统约定比较运行时取得的最新代码，因此本节不展示版本或时间方向。'
        '<b>判断重点是：相似代码能否由共同上游、ABI 规范、第三方库、比赛基线或公共样板解释。</b>'
        '上方数量按目标函数互斥归一；详细排除证据在第 7 节展开。下表仅保留排除这些因素后的主要'
        '高置信同源来源。</div>'
        '<div class="overflow-x-auto"><table><thead><tr><th>主要同源来源</th>'
        '<th>唯一目标函数</th><th>有效相似行</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
    )
    section = _collapsible_html(
        sid, "共同上游判断", body, tone="evidence",
        subtitle="解释可归因的公共来源，并保留排除后的主要同源来源",
    )
    return _toc_link(sid, "共同上游判断"), section


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
    if start and end and end > start:
        label = f"{html.escape(file_path)}:{start}-{end}"
    else:
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


def _innovation_runtime_evidence(point: dict, query_repo_path: Path | None) -> dict:
    """从目标仓库补充候选创新的调用/引用位置与测试、benchmark 证据。"""
    evidence = {"call_sites": [], "validation_files": []}
    if not query_repo_path or not query_repo_path.is_dir():
        return evidence
    names = sorted({str(t.get("func") or "") for t in point.get("targets") or []
                    if len(str(t.get("func") or "")) >= 3})
    if not names:
        return evidence
    call_re = re.compile(r"\b(?:" + "|".join(re.escape(name) for name in names) + r")\s*\(")
    target_locs = {(str(t.get("file") or "").replace("\\", "/"), int(t.get("start") or 0))
                   for t in point.get("targets") or []}
    suffixes = {".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".s", ".S"}
    ignored = {".git", "target", "vendor", "third_party", "node_modules", "build"}
    for path in query_repo_path.rglob("*"):
        if len(evidence["call_sites"]) >= 8 and len(evidence["validation_files"]) >= 4:
            break
        if not path.is_file() or path.suffix not in suffixes or any(p in ignored for p in path.parts):
            continue
        try:
            if path.stat().st_size > 1_500_000:
                continue
            rel = path.relative_to(query_repo_path).as_posix()
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        is_validation = bool(re.search(r"(?:^|/)(?:tests?|benches?|benchmarks?|perf)(?:/|$)", rel,
                                       re.IGNORECASE))
        matched_validation = False
        for line_no, line in enumerate(lines, 1):
            if not call_re.search(line):
                continue
            if (rel, line_no) not in target_locs and len(evidence["call_sites"]) < 8:
                evidence["call_sites"].append({"file": rel, "line": line_no})
            matched_validation = matched_validation or is_validation
        if matched_validation and len(evidence["validation_files"]) < 4:
            evidence["validation_files"].append({"file": rel, "line": 1})
    return evidence


def _innovation_section(points: list[dict], linker, query_repo_id: str,
                        query_repo_path: Path | None = None) -> tuple[str, str]:
    """候选创新 → 参考基线 → 目标实现 → 调用/验证/反证的独立证据地图。"""
    if not points:
        body = (
            '<div class="p-3 rounded bg-slate-50 border border-slate-200 text-sm text-slate-600">'
            '当前数据中没有形成可核验的相对创新点。可能原因是没有暂未命中的函数、'
            '未识别到稳定参考 repo，或代码证据不足。<b>不输出不代表项目没有创新</b>。</div>'
        )
        section = _collapsible_html(
            "sec-innovation", "相对参考实现的候选创新", body, tone="innovation",
            subtitle="代码差异候选，不把未命中或项目自述直接当作创新结论",
        )
        return _toc_link("sec-innovation", "相对参考实现的候选创新", "0", "zero"), section

    cards: list[str] = []
    confidence_labels = {"high": "高", "medium": "中", "low": "低"}
    for index, point in enumerate(points, start=1):
        runtime = _innovation_runtime_evidence(point, query_repo_path)
        complexity = point.get("complexity") or {}
        level = str(complexity.get("level") or "未知")
        level_cls = {
            "高": "bg-red-50 text-red-700 border-red-200",
            "中": "bg-amber-50 text-amber-700 border-amber-200",
            "低": "bg-green-50 text-green-700 border-green-200",
        }.get(level, "bg-slate-50 text-slate-600 border-slate-200")
        confidence = confidence_labels.get(str(point.get("confidence") or "").lower(), "中")

        target_items = []
        for target in point.get("targets") or []:
            anchor = _make_gitlab_anchor(
                linker, query_repo_id, target.get("file", ""),
                int(target.get("start") or 0), int(target.get("end") or 0),
            )
            target_items.append(
                f'<li>{anchor} · <code>{html.escape(str(target.get("func") or ""))}</code>'
                f' · {int(target.get("lines") or 0)} 行</li>'
            )

        reference_items = []
        for reference in point.get("references") or []:
            anchor = _make_gitlab_anchor(
                linker, reference.get("repo", ""), reference.get("file", ""),
                int(reference.get("start") or 0), int(reference.get("end") or 0),
            )
            reference_items.append(
                f'<li>{anchor} · <code>{html.escape(str(reference.get("func") or ""))}</code>'
                f' · 最近邻相似度 {float(reference.get("score") or 0.0):.3f}</li>'
            )

        target_html = "".join(target_items) or '<li class="text-slate-400">无有效目标代码证据</li>'
        reference_html = "".join(reference_items) or (
            '<li class="text-slate-400">参考 repo 中未绑定到可比较的具体函数；该项把握度已降低</li>'
        )
        call_html = "".join(
            '<li>' + _make_gitlab_anchor(linker, query_repo_id, x["file"], x["line"]) + '</li>'
            for x in runtime["call_sites"]
        ) or '<li class="text-slate-400">未自动定位到目标函数之外的调用/引用位置</li>'
        validation_html = "".join(
            '<li>' + _make_gitlab_anchor(linker, query_repo_id, x["file"], x["line"]) + '</li>'
            for x in runtime["validation_files"]
        ) or '<li class="text-amber-700">未发现直接关联测试或 benchmark；能力与性能收益仍需验证</li>'
        why = html.escape(str(point.get("why_it_matters") or ""))
        why_html = f'<p class="text-sm mt-2"><b>作用与代价：</b>{why}</p>' if why else ""
        target_files = sorted({str(t.get("file") or "") for t in point.get("targets") or [] if t.get("file")})
        modules = sorted({_MODULE_DISPLAY.get(str(t.get("module") or "other"), str(t.get("module") or "other"))
                          for t in point.get("targets") or []})
        auto_scope = f'{"、".join(modules) or "未分类子系统"}；{len(target_files)} 个实现文件、{len(runtime["call_sites"])} 个外部调用/引用位置'
        impact = html.escape(str(point.get("impact_scope") or auto_scope))
        validation_hint = html.escape(str(point.get("validation_hint") or "需以针对性测试或 benchmark 验证。"))
        counter = html.escape(str(point.get("counterevidence") or
                                  "静态代码差异与未命中不能单独证明能力、性能或原创性。"))
        metric_text = (
            f'{int(complexity.get("file_count") or 0)} 个文件 · '
            f'{int(complexity.get("symbol_count") or 0)} 个函数 · '
            f'{int(complexity.get("code_lines") or 0)} 行实现 · '
            f'{int(complexity.get("branch_points") or 0)} 个控制流分支'
        )
        cards.append(f"""
<article class="mb-4 rounded-lg border border-emerald-200 bg-emerald-50/30 overflow-hidden">
  <div class="px-4 py-3 border-b border-emerald-100 bg-emerald-50 flex flex-wrap items-center gap-2">
    <span class="text-xs font-mono text-emerald-700">#{index:02d}</span>
    <h3 class="font-semibold text-slate-800 mr-auto">候选创新：{html.escape(str(point.get('title') or '代码机制差异'))}</h3>
    <span class="px-2 py-0.5 rounded bg-white border border-emerald-200 text-xs text-emerald-700">{html.escape(str(point.get('kind') or '工程改良'))}</span>
    <span class="text-xs text-slate-500">证据把握度：{confidence}</span>
    <span class="candidate-status">待人工确认</span>
  </div>
  <div class="p-4">
    <div class="grid grid-cols-1 md:grid-cols-[9rem_1fr] gap-x-3 gap-y-2 text-sm">
      <div class="font-semibold text-slate-500">参考 repo</div><div>{html.escape(str(point.get('reference_repo') or '未识别'))}</div>
      <div class="font-semibold text-slate-500">参考实现基线</div><div>{html.escape(str(point.get('baseline') or ''))}</div>
      <div class="font-semibold text-emerald-700">本作品代码变化</div><div>{html.escape(str(point.get('delta') or ''))}</div>
    </div>
    {why_html}
    <div class="innovation-evidence-grid">
      <div><b>影响范围</b><p>{impact}</p></div>
      <div><b>建议验证</b><p>{validation_hint}</p></div>
      <div class="innovation-counter"><b>限制与反证</b><p>{counter}</p></div>
    </div>
    <div class="mt-3 p-3 rounded bg-white border border-slate-200">
      <div class="flex flex-wrap items-center gap-2 mb-2 text-xs">
        <b>实现复杂度（静态估算）</b>
        <span class="px-2 py-0.5 rounded border {level_cls}">{html.escape(level)}</span>
        <span class="text-slate-500">{html.escape(metric_text)}</span>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
        <div><div class="font-semibold text-emerald-700 mb-1">本作品实现（点击查看代码）</div><ul class="list-disc pl-5 space-y-1">{target_html}</ul></div>
        <div><div class="font-semibold text-slate-600 mb-1">参考实现（点击对照）</div><ul class="list-disc pl-5 space-y-1">{reference_html}</ul></div>
        <div><div class="font-semibold text-blue-700 mb-1">调用/引用入口</div><ul class="list-disc pl-5 space-y-1">{call_html}</ul></div>
        <div><div class="font-semibold text-amber-700 mb-1">测试 / Benchmark 证据</div><ul class="list-disc pl-5 space-y-1">{validation_html}</ul></div>
      </div>
    </div>
  </div>
</article>
""")

    intro = (
        '<div class="mb-4 p-3 rounded bg-blue-50 border border-blue-200 text-xs text-blue-800">'
        '本节从“暂未形成有效相似命中”的函数出发，再与各模块主要参考 repo 的最近实现做代码级比较。'
        '<b>项目 README / 设计文档不作为独立创新证据</b>；“未命中”本身也不等于创新。'
        '所有条目均为待人工确认的候选；复杂度只反映代码阅读与实现规模，不是质量评分或算法时间复杂度。</div>'
    )
    section = _collapsible_html(
        "sec-innovation", "相对参考实现的候选创新", intro + "".join(cards),
        tone="innovation", subtitle="基线、实现、调用入口、验证证据和反证一一映射",
    )
    return _toc_link("sec-innovation", "相对参考实现的候选创新",
                     str(len(points)), "original"), section


def _original_section(original_funcs: list[dict], linker, query_repo_id: str) -> tuple[str, str]:
    """列出全部暂未命中函数；不把“未命中”直接表述为原创。"""
    sid = "sec-original"
    if not original_funcs:
        body = ("<p class='text-slate-500 text-sm'>没有暂未命中的函数"
                "（所有函数都与历史代码库有相似命中）。</p>")
    else:
        rows_parts = []
        for f in original_funcs:
            module = _MODULE_DISPLAY.get(f["module"], f["module"])
            search_text = html.escape(f'{module} {f["func"]} {f["file"]}'.lower(), quote=True)
            rows_parts.append(
                f'<tr data-search="{search_text}">'
                f'<td class="text-xs">{html.escape(module)}</td>'
                f'<td class="text-xs">{html.escape(f["func"])}</td>'
                f'<td class="font-mono text-xs">'
                + _make_gitlab_anchor(linker, query_repo_id, f["file"], f["start"], f.get("end", 0))
                + f'</td><td class="text-xs">{f["lines"]} 行</td></tr>'
            )
        rows = "".join(rows_parts)
        body = (
            '<p class="text-sm text-slate-600 mb-3">'
            f'共 <b>{len(original_funcs)}</b> 个函数在当前历史库中暂未形成有效相似命中。'
            '<b>这只表示系统暂未检出，不等于原创认定</b>；可能仍受历史库覆盖、召回与阈值影响。'
            '以下按规模降序全部列出，供继续人工核验：'
            '</p>'
            '<input class="appendix-search" type="search" placeholder="筛选子系统、函数或路径…" '
            'oninput="var q=this.value.toLowerCase();this.parentElement.querySelectorAll(\'tr[data-search]\').forEach(function(r){r.style.display=r.dataset.search.indexOf(q)>=0?\'\':\'none\'})">'
            '<div class="overflow-x-auto">'
            '<table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">子系统</th>'
            '<th class="text-left p-2 border-b">函数</th>'
            '<th class="text-left p-2 border-b">文件:行</th>'
            '<th class="text-left p-2 border-b">规模</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody>'
            '</table></div>'
        )
    section = _collapsible_html(
        sid, "附录：暂未检出相似函数（不等于原创）", body, default_open=False,
        tone="appendix", subtitle="完整清单默认折叠，可按子系统、函数或路径筛选",
    )
    toc = _toc_link(sid, "暂未检出相似函数", str(len(original_funcs)), "original")
    return toc, section


_CDN_HEAD = """
<script src="https://cdn.tailwindcss.com?plugins=typography"></script>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<script defer src="https://unpkg.com/alpinejs@3.x.x/dist/cdn.min.js"></script>
"""

_STYLES = """
<style>
:root{--bg:#f3f6fa;--card:#fff;--line:#dbe3ec;--line-soft:#e9eef4;--text:#172033;
  --muted:#64748b;--blue:#2563eb;--blue-soft:#eff6ff;--red:#dc2626;--amber:#d97706;
  --green:#16803c;--shadow:0 10px 30px rgba(15,23,42,.06)}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:1.25rem}
[x-cloak]{display:none!important}
body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,"PingFang SC","Microsoft YaHei",system-ui,sans-serif}
.layout{display:flex;align-items:flex-start;gap:1.5rem;max-width:1540px;margin:0 auto;padding:1.5rem 1.75rem 4rem}
.main{flex:1;min-width:0;max-width:1210px}
/* 页面标题 */
.report-header{margin:0 0 1rem;padding:.35rem .15rem 1.1rem;border-bottom:1px solid #cbd5e1}
.report-kicker,.toc-kicker,.summary-eyebrow{display:block;font-size:.65rem;line-height:1.2;letter-spacing:.14em;
  font-weight:800;color:#3b82f6;text-transform:uppercase}
.report-header h1{margin:.35rem 0 .2rem;font-size:1.72rem;line-height:1.25;font-weight:760;color:#0f172a}
.report-header p{margin:0;color:var(--muted);font-size:.82rem}
/* 左侧目录：固定宽度、分组、等高行和固定徽标列 */
.toc{position:sticky;top:1.25rem;align-self:flex-start;width:278px;flex:0 0 278px;font-size:.82rem}
.toc-card{overflow:hidden;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.96);
  box-shadow:0 8px 24px rgba(15,23,42,.06)}
.toc-header{padding:1rem 1rem .85rem;border-bottom:1px solid var(--line-soft);background:linear-gradient(145deg,#fff,#f8fbff)}
.toc-header strong{display:block;margin-top:.28rem;font-size:1rem;color:#0f172a}
.toc-repo{display:block;margin-top:.3rem;color:#64748b;font:500 .7rem/1.35 ui-monospace,SFMono-Regular,Consolas,monospace;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.toc-scroll{max-height:calc(100vh - 4rem);overflow-y:auto;padding:.55rem .55rem .8rem;scrollbar-width:thin}
.toc-group+.toc-group{margin-top:.5rem;padding-top:.45rem;border-top:1px solid #edf1f5}
.toc-group-title{padding:.35rem .55rem .28rem;color:#94a3b8;font-size:.64rem;font-weight:800;letter-spacing:.12em}
.toc .toc-link{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:.55rem;
  height:42px;margin:2px 0;padding:.4rem .55rem .4rem .7rem;border-radius:8px;color:#475569;text-decoration:none;overflow:hidden}
.toc .toc-link:before{content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;border-radius:3px;background:transparent}
.toc-link-label{min-width:0;line-height:1.25;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.toc .toc-link:hover{background:#f1f5f9;color:#1e3a5f}
.toc .toc-active{background:#eaf2ff;color:#1d4ed8;font-weight:700}
.toc .toc-active:before{background:#3b82f6}
.toc-badge{min-width:1.45rem;padding:.2rem .38rem;border-radius:999px;text-align:center;font-size:.64rem;line-height:1;
  font-weight:800;background:#e2e8f0;color:#64748b}
.toc-badge.confirmed{background:#fee2e2;color:#b91c1c}.toc-badge.review{background:#fef3c7;color:#a16207}
.toc-badge.original{background:#dcfce7;color:#16713a}.toc-badge.excluded{background:#e2e8f0;color:#475569}
.toc-badge.zero{background:#f1f5f9;color:#94a3b8}
/* 顶部状态与总览 */
#retrieval-contract{margin-bottom:1rem!important;padding:.8rem 1rem!important;border-radius:10px!important}
#guide{margin-bottom:1rem!important;padding:1.15rem 1.25rem!important;border:1px solid #bfdbfe!important;
  border-left:4px solid #3b82f6!important;border-radius:12px!important;background:#f5f9ff!important}
.summary-card{margin:0 0 1.75rem;padding:1.35rem;border:1px solid var(--line);border-radius:14px;background:var(--card);box-shadow:var(--shadow)}
.summary-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:1rem;padding-bottom:.85rem;border-bottom:1px solid var(--line-soft)}
.summary-heading h2{margin:.18rem 0 0;font-size:1.2rem;color:#0f172a}.summary-repo{max-width:48%;color:#64748b;
  font:500 .72rem/1.35 ui-monospace,SFMono-Regular,Consolas,monospace;text-align:right;overflow-wrap:anywhere}
.chart-title{font-size:.82rem;font-weight:700;color:#334155}
/* 正文层级 */
.chapter-heading,.excluded-heading{display:flex;align-items:flex-start;gap:.85rem;margin:2.25rem 0 .85rem;padding:.1rem .15rem .75rem;
  border-bottom:1px solid #cbd5e1}
.chapter-index{display:inline-flex;align-items:center;justify-content:center;width:2rem;height:2rem;flex:0 0 2rem;border-radius:8px;
  background:#e7effb;color:#2563eb;font-size:.7rem;font-weight:800}
.chapter-heading h2,.excluded-heading h2{margin:0;font-size:1.12rem;line-height:1.35;color:#172033;font-weight:760}
.chapter-heading p,.excluded-heading p{margin:.2rem 0 0;color:#64748b;font-size:.76rem;line-height:1.55}
.excluded-heading{margin-top:2.5rem}.excluded-heading .chapter-index{background:#e2e8f0;color:#475569}
.report-section{margin:0 0 1rem;overflow:hidden;border:1px solid var(--line);border-radius:12px;background:var(--card);box-shadow:0 4px 16px rgba(15,23,42,.035)}
.report-section[data-tone="review"]{border-color:#f2d38c}.report-section[data-tone="original"]{border-color:#b9dfc5}
.report-section[data-tone="excluded"]{border-color:#d8dee7}.report-section[data-tone="innovation"]{border-color:#b7dfcc}
.report-section[data-tone="compliance"]{border-color:#cbd5e1}.report-section[data-tone="appendix"]{border-color:#d8dee7;box-shadow:none}
.section-toggle{display:flex;align-items:flex-start;width:100%;gap:.65rem;padding:.9rem 1.05rem;border:0;border-bottom:1px solid var(--line-soft);
  background:#f8fafc;color:inherit;text-align:left;cursor:pointer}
.section-toggle:hover{background:#f3f7fb}.report-section[data-tone="review"] .section-toggle{background:#fffbeb}
.report-section[data-tone="original"] .section-toggle{background:#f3fbf5}.report-section[data-tone="excluded"] .section-toggle{background:#f8fafc}
.report-section[data-tone="innovation"] .section-toggle{background:#f0fdf6}.report-section[data-tone="compliance"] .section-toggle{background:#f8fafc}
.report-section[data-tone="appendix"] .section-toggle{background:#f8fafc}
.section-chevron{display:inline-flex;align-items:center;justify-content:center;width:1.2rem;height:1.3rem;color:#64748b;font-size:.82rem;flex:0 0 1.2rem}
.section-heading{display:block;min-width:0}.section-title{display:block;font-size:.95rem;line-height:1.35;font-weight:750;color:#1e293b}
.section-subtitle{display:block;margin:.16rem 0 0;color:#7b8ba1;font-size:.69rem;line-height:1.4;font-weight:400}
.section-body{padding:1rem 1.1rem 1.15rem}.section-body>p:first-child{margin-top:0}.section-body>p:last-child{margin-bottom:0}
.analysis-panel{margin-top:1rem;padding:1rem;border:1px solid #cfe0f6;border-radius:10px;background:#f6faff;color:#334155}
.analysis-panel h4{margin:0 0 .55rem;font-size:.82rem;font-weight:750;color:#1d4f91}
.review-note{margin-bottom:.85rem;padding:.75rem .85rem;border:1px solid #f1d28a;border-radius:9px;background:#fff9e8;color:#8a5a05;font-size:.75rem}
.section-intro{margin:0 0 .9rem;padding:.72rem .82rem;border:1px solid #dbe7f3;border-radius:9px;background:#f7faff;color:#4b5f77;font-size:.75rem;line-height:1.65}
.module-summary{display:flex;flex-wrap:wrap;align-items:center;gap:.45rem .55rem;margin-bottom:.75rem}
.status-chip{display:inline-flex;padding:.28rem .55rem;border-radius:999px;font-size:.7rem;font-weight:700}
.status-confirmed{background:#fee2e2;color:#b91c1c}.status-review{background:#fef3c7;color:#a16207}.status-incomplete{background:#e2e8f0;color:#475569}.status-original{background:#dcfce7;color:#16713a}
.module-summary-text,.module-summary-source{font-size:.72rem;color:#64748b}.module-summary-source{margin-left:auto;color:#94a3b8}
/* 链接、进度条和表格 */
.file-jump,.repo-link{color:#1d5fbf;text-decoration:underline;text-decoration-style:dotted;text-underline-offset:2px;overflow-wrap:anywhere}
.file-jump:hover,.repo-link:hover{color:#174a91;text-decoration-style:solid}
.pct-bar{display:flex;height:18px;border-radius:6px;overflow:hidden;margin:.4rem 0 .15rem;background:#eef2f7}
.pct-copy,.pct-review,.pct-incomplete,.pct-orig{display:flex;align-items:center;min-width:0;padding:0 6px;color:#fff;font-size:10px;white-space:nowrap;overflow:hidden}
.pct-copy{background:#ef4444}.pct-review{background:#f59e0b}.pct-incomplete{background:#94a3b8}.pct-orig{background:#22c55e}.pct-bar div:only-child{border-radius:6px}
.section-body .overflow-x-auto{border:1px solid var(--line-soft);border-radius:9px}
.main table{width:100%;border-collapse:separate;border-spacing:0;background:#fff}
.main th,.main td{padding:.58rem .68rem;border-bottom:1px solid var(--line-soft);vertical-align:top}
.main th{background:#f7f9fc!important;color:#536277!important;font-size:.7rem;font-weight:750;white-space:nowrap}
.main tbody:last-child tr:last-child td,.main tbody>tr:last-child td{border-bottom:0}
.main tbody>tr:hover>td{background:#fafcff}
.source-metrics-table{margin-top:.7rem;overflow-x:auto;border:1px solid var(--line-soft);border-radius:9px}
/* 时间线与功能簇 */
.lineage-summary,.compliance-grid,.technical-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.65rem;margin-bottom:.9rem}
.lineage-summary>div,.compliance-grid>div,.technical-grid>div{display:flex;min-width:0;flex-direction:column;gap:.2rem;padding:.68rem .75rem;border:1px solid var(--line-soft);border-radius:9px;background:#fafcff}
.lineage-summary span,.compliance-grid span,.technical-grid span{font-size:.66rem;color:#7b8ba1}.lineage-summary b,.compliance-grid b,.technical-grid b{font-size:.77rem;overflow-wrap:anywhere}
.cluster-card{margin:.65rem 0;border:1px solid var(--line);border-radius:10px;overflow:hidden}.cluster-card[data-priority="critical"]{border-color:#efb2b2}
.cluster-head{display:grid;width:100%;grid-template-columns:2.4rem minmax(11rem,1fr) auto auto 1rem;align-items:center;gap:.7rem;padding:.72rem .8rem;border:0;background:#f8fafc;text-align:left;cursor:pointer}
.cluster-card[data-priority="critical"] .cluster-head{background:#fff7f7}.cluster-index{font:.72rem ui-monospace,SFMono-Regular,Consolas;color:#64748b}.cluster-main{display:flex;min-width:0;flex-direction:column}.cluster-main b{font-size:.82rem}.cluster-main small{margin-top:.12rem;color:#718096;font-size:.66rem}
.cluster-metrics{font-size:.68rem;color:#64748b;white-space:nowrap}.cluster-priority{padding:.22rem .48rem;border-radius:999px;background:#fff;border:1px solid #e2e8f0;color:#9a5b06;font-size:.66rem;font-weight:700;white-space:nowrap}.cluster-body{padding:.2rem .8rem .85rem}.cluster-analysis{margin-top:.7rem;padding:.75rem;border:1px solid #dbe7f3;border-radius:8px;background:#f7faff;font-size:.75rem}
/* 复核优先级 */
.review-priority-cell{min-width:9rem}.review-priority-cell small{display:block;margin-top:.25rem;color:#718096;font-size:.64rem;line-height:1.35}.priority-badge{display:inline-flex;padding:.2rem .42rem;border-radius:999px;font-size:.66rem;font-weight:750}.priority-high{background:#fee2e2;color:#b91c1c}.priority-medium{background:#fef3c7;color:#a16207}.priority-low{background:#e2e8f0;color:#475569}
.review-anchors{margin-top:.3rem;color:#64748b;font-size:.66rem;line-height:1.5}.review-anchors code{display:inline-block;margin:.1rem .15rem .1rem 0;padding:.05rem .25rem;border-radius:.25rem;background:#f1f5f9;color:#334155;white-space:normal;word-break:break-all}
/* 候选创新与合规 */
.candidate-status{padding:.2rem .45rem;border-radius:999px;background:#fff7ed;color:#9a4d07;border:1px solid #fed7aa;font-size:.65rem;font-weight:700}.innovation-evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem;margin-top:.8rem}.innovation-evidence-grid>div{padding:.65rem .72rem;border:1px solid #dbe8e2;border-radius:8px;background:#fff;font-size:.72rem}.innovation-evidence-grid p{margin:.2rem 0 0;color:#526175;line-height:1.5}.innovation-evidence-grid .innovation-counter{grid-column:1/-1;border-color:#f0d3a2;background:#fffaf0}
.license-panel{padding:.75rem;border:1px solid var(--line-soft);border-radius:9px;background:#fafcff;font-size:.72rem}.license-panel ul{margin:.45rem 0 0;padding-left:1.1rem}.appendix-search{width:100%;margin:0 0 .7rem;padding:.58rem .72rem;border:1px solid #cbd5e1;border-radius:8px;background:#fff;font-size:.75rem}
/* KPI 卡片 */
.kpi{display:flex;flex-direction:column;gap:.2rem;min-height:82px;padding:.8rem .9rem;border-radius:10px;background:#f9fbfd;border:1px solid var(--line-soft)}
.kpi .v{font-size:1.55rem;font-weight:780;line-height:1.1}.kpi .l{font-size:.69rem;line-height:1.35;color:#64748b}
/* 档位/类型图例 */
.legend{display:flex;flex-wrap:wrap;gap:.4rem .8rem;margin-top:.7rem;padding:.6rem .7rem;border-radius:8px;background:#f8fafc;
  font-size:.69rem;color:#526175}.legend .dot{display:inline-block;width:.62rem;height:.62rem;border-radius:3px;margin-right:.3rem;vertical-align:-1px}
/* 代码证据并排 */
.code-toggle{cursor:pointer;background:none;border:none;padding:0}
.code-pair{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid var(--line)}
.code-col{min-width:0;border-left:1px solid var(--line)}
.code-col:first-child{border-left:none}
.code-h{font-size:.7rem;font-weight:600;color:#475569;padding:.35rem .6rem;
  background:#f1f5f9;border-bottom:1px solid var(--line);position:sticky;top:0}
.code-body{margin:0;max-height:360px;overflow:auto;font-size:.72rem;line-height:1.45;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.cl{white-space:pre;padding:0 .6rem}
.cl.df{background:#fff1f2}
.code-col:first-child .cl.df{background:#fef9c3}
@media(max-width:720px){.code-pair{grid-template-columns:1fr}.code-col{border-left:none;border-top:1px solid #e2e8f0}}
/* 回到顶部 */
.to-top{position:fixed;right:1.1rem;bottom:1.1rem;width:2.4rem;height:2.4rem;border-radius:999px;background:#2563eb;color:#fff;
  display:flex;align-items:center;justify-content:center;text-decoration:none;box-shadow:0 4px 14px rgba(37,99,235,.28);font-size:1.1rem;opacity:.9}
.to-top:hover{opacity:1}
@media(max-width:1060px){.layout{gap:1rem;padding:1rem}.toc{width:242px;flex-basis:242px}}
@media(max-width:860px){
  .layout{display:block;padding:.8rem}.toc{position:static;width:auto;margin-bottom:1rem}.toc-scroll{max-height:none}
  .toc-header{padding:.8rem 1rem}.toc-scroll{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.35rem .75rem}
  .toc-group+.toc-group{margin:0;padding:0;border:0}.main{max-width:none}.report-header h1{font-size:1.35rem}
  .module-summary-source{width:100%;margin-left:0}.lineage-summary,.compliance-grid,.technical-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .cluster-head{grid-template-columns:2.2rem minmax(8rem,1fr) auto 1rem}.cluster-metrics{display:none}
}
@media(max-width:620px){.toc-scroll{grid-template-columns:1fr}.summary-heading{display:block}.summary-repo{display:block;max-width:none;text-align:left;margin-top:.4rem}.lineage-summary,.compliance-grid,.technical-grid,.innovation-evidence-grid{grid-template-columns:1fr}.innovation-evidence-grid .innovation-counter{grid-column:auto}.cluster-head{grid-template-columns:2rem minmax(0,1fr) 1rem}.cluster-priority{display:none}}
@media print{
  .toc,.to-top{display:none!important}
  body{background:#fff}
  .layout{display:block;max-width:none;padding:0}
  .report-header{margin-bottom:.6rem}.report-section,.summary-card{break-inside:avoid;box-shadow:none!important}
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


def _file_similar_row(f: dict, linker, query_repo_id: str) -> str:
    """「文件整体相似」单行：借鉴函数数 + 非函数(结构体等)行 + 整体相似% + 主要来源。"""
    if f.get("method") == "line":
        nf = f.get("line_nonfunc", 0)
        tot = f.get("line_total", 0) or 1
        nonfunc = f'{nf} 行（{round(nf / tot * 100)}%）'
    else:
        nonfunc = "—"
    src = _ref_repo_anchor(linker, f["top_source"])
    if f.get("top_source_file"):
        src += ' ' + _make_gitlab_anchor(linker, f["top_source"], f["top_source_file"], 0)
    return (
        '<tr>'
        '<td class="font-mono text-xs">'
        + _make_gitlab_anchor(linker, query_repo_id, f["file_path"], 0) + '</td>'
        f'<td class="text-xs">{html.escape(_MODULE_DISPLAY.get(f["module"], f["module"]))}</td>'
        f'<td class="text-xs">{f["hit"]}/{f["total"]} 个函数</td>'
        f'<td class="text-xs text-slate-500">{nonfunc}</td>'
        f'<td class="text-xs font-semibold text-amber-700">{round(f["ratio"] * 100)}%</td>'
        '<td class="text-xs text-slate-500">' + src + '</td>'
        '</tr>'
    )


def _file_level_section(file_matches: list[dict], file_similar: list[dict],
                        linker, query_repo_id: str) -> tuple[str, str]:
    """文件级与非函数代码证据（整文件哈希、覆盖率、结构体/宏/汇编/配置）。"""
    sid = "sec-files"
    nonfunc_suffixes = {".s", ".asm", ".ld", ".lds", ".toml", ".yaml", ".yml", ".json"}
    nonfunc_files = sum(
        1 for m in file_matches
        if Path(str(m.get("query_file") or "")).suffix.lower() in nonfunc_suffixes
    )
    nonfunc_lines = sum(int(f.get("line_nonfunc") or 0) for f in file_similar)
    parts: list[str] = [
        '<div class="section-intro">函数清单之外，本节保留整文件规范化哈希与文件覆盖率证据；'
        '因此结构体、枚举、常量、宏、汇编、链接脚本和配置等非函数实现不会被函数级分析遮蔽。'
        f'当前直接涉及 <b>{nonfunc_files}</b> 个非函数型整文件，文件覆盖统计另包含 '
        f'<b>{nonfunc_lines}</b> 行非函数内容。</div>'
    ]
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
        rows = "".join(_file_similar_row(f, linker, query_repo_id) for f in file_similar)
        line_based = any(f.get("method") == "line" for f in file_similar)
        head = (f'文件整体相似（借鉴函数覆盖行 ≥{round(WHOLE_FILE_LINE_RATIO * 100)}% 文件总行，分母含结构体等非函数内容）'
                if line_based else f'文件整体相似（≥{round(WHOLE_FILE_SIM_RATIO * 100)}% 函数命中借鉴）')
        parts.append(
            '<div class="text-sm font-semibold text-amber-700 mt-3 mb-1">'
            f'{head}（{len(file_similar)} 个文件）</div>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">新作品文件</th>'
            '<th class="text-left p-2 border-b">子系统</th>'
            '<th class="text-left p-2 border-b">借鉴函数</th>'
            '<th class="text-left p-2 border-b">非函数内容(结构体等)</th>'
            '<th class="text-left p-2 border-b">整体相似</th>'
            '<th class="text-left p-2 border-b">主要来源</th>'
            '</tr></thead>'
            f'<tbody>{rows}</tbody></table></div>'
            + ('<p class="text-xs text-slate-400 mt-1">'
               '* 整体相似 = 借鉴(已确认)函数覆盖的非空行 ÷ 文件总非空行；分母含 struct/enum/'
               '常量/宏/use 等非函数内容，故结构体占比大的文件不会因「函数都被借鉴」而被判整体相似。'
               '</p>' if line_based else '')
        )
    if not file_matches and not file_similar:
        parts.append('<p class="text-sm text-slate-500">当前批次未形成文件级或非函数代码同源证据。</p>')
    body = "".join(parts)
    section = _collapsible_html(
        sid, "文件级和非函数代码证据", body, tone="evidence",
        subtitle="整文件哈希、函数覆盖率及结构体/宏/汇编/配置等证据",
    )
    toc = _toc_link(sid, "文件级和非函数代码证据",
                    str(len(file_matches) + len(file_similar)), "confirmed")
    return toc, section


def _reused_libraries_section(lib_stats: list[dict], query_repo_id: str) -> tuple[str, str]:
    """复用库统计：列出新作品 vendored 的公开第三方库及规模。

    这些库代码（lwext4 / smoltcp / fatfs …）为多队合法共用，已从借鉴图/清单中剔除，
    此处单列说明，避免「数字凭空消失」。无复用库时返回空（不渲染本节）。
    """
    if not lib_stats:
        return "", ""
    sid = "sec-reused-libs"
    total_funcs = sum(x["func_count"] for x in lib_stats)
    total_pairs = sum(x["pair_count"] for x in lib_stats)
    rows = "".join(
        '<tr>'
        f'<td class="text-xs font-semibold">{html.escape(x["name"])}</td>'
        f'<td class="text-xs">{x["func_count"]}</td>'
        f'<td class="text-xs">{x["pair_count"]}</td>'
        f'<td class="text-xs">{x["repo_count"]}</td>'
        '</tr>'
        for x in lib_stats
    )
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        '本作品 vendored（整库签入）了以下公开第三方库。这类库代码为多队合法共用，'
        '<b>不计入值得关注的借鉴/抄袭</b>，已从「主要借鉴来源」图、各模块借鉴占比/清单、'
        '原创代码清单与文件级清单中剔除，仅在此单列。识别规则见 <code>config/libraries.yaml</code>，'
        '如有遗漏可在该文件补充。'
        '</p>'
        '<div class="flex flex-wrap gap-3 text-sm mb-3">'
        f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">复用库 {len(lib_stats)} 个</span>'
        f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">库函数 {total_funcs} 个</span>'
        f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">已剔除嫌疑对 {total_pairs}</span>'
        '</div>'
        '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">复用库</th>'
        '<th class="text-left p-2 border-b">新作品中函数数</th>'
        '<th class="text-left p-2 border-b">已剔除嫌疑对</th>'
        '<th class="text-left p-2 border-b">命中历史仓库数</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )
    section = _collapsible_html(
        sid, "复用库统计（不计入借鉴）", body,
        default_open=False, tone="excluded",
    )
    toc = _toc_link(sid, "复用库统计", str(len(lib_stats)), "excluded")
    return toc, section


def _common_code_section(cc_funcs: list[dict], linker, query_repo_id: str) -> tuple[str, str]:
    """公共/样板代码小节：confirmed 但命中多个历史仓库的同款函数（不计入借鉴，已剔除）。"""
    if not cc_funcs:
        return "", ""
    sid = "sec-common-code"
    limit = 50
    shown = cc_funcs[:limit]
    rows = "".join(
        '<tr>'
        f'<td class="text-xs font-mono">{html.escape(f["name"])}</td>'
        '<td class="font-mono text-xs">'
        + _make_gitlab_anchor(linker, query_repo_id, f["file"], f.get("start", 0))
        + '</td>'
        f'<td class="text-xs">{f["repos"]} 个</td>'
        '</tr>'
        for f in shown
    )
    more = f'（按命中仓库数降序，列出前 {limit} 个）' if len(cc_funcs) > limit else ''
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        f'下列 <b>{len(cc_funcs)}</b> 个函数同时命中 ≥{COMMON_CODE_REPO_THRESHOLD} 个不同历史仓库，'
        '属多队通用的公共/框架/样板代码（如 console 的 print/println 宏、panic handler、lang_items 等），'
        f'<b>不计入值得关注的借鉴</b>，已从摘要、借鉴来源图与各模块清单中剔除{more}。'
        '</p>'
        '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">函数</th>'
        '<th class="text-left p-2 border-b">文件:行</th>'
        '<th class="text-left p-2 border-b">命中历史仓库数</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )
    section = _collapsible_html(
        sid, "公共 / 样板代码（不计入借鉴）", body,
        default_open=False, tone="excluded",
    )
    toc = _toc_link(sid, "公共 / 样板代码", str(len(cc_funcs)), "excluded")
    return toc, section


def _baseline_section(base_funcs: list[dict], linker, query_repo_id: str) -> tuple[str, str]:
    """基线衍生小节：双侧均与同一基线库（教学OS/模板/官方第三方库）相似的函数（不计入借鉴）。"""
    if not base_funcs:
        return "", ""
    sid = "sec-baseline"
    limit = 50
    shown = base_funcs[:limit]
    rows = "".join(
        '<tr>'
        f'<td class="text-xs font-mono">{html.escape(f["name"])}</td>'
        '<td class="font-mono text-xs">'
        + _make_gitlab_anchor(linker, query_repo_id, f["file"], f.get("start", 0))
        + '</td>'
        f'<td class="text-xs">{html.escape(f.get("source", "") or "—")}</td>'
        f'<td class="text-xs text-slate-500">{html.escape(f.get("note", "") or "—")}</td>'
        '</tr>'
        for f in shown
    )
    more = f'（列出前 {limit} 个）' if len(base_funcs) > limit else ''
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        f'下列 <b>{len(base_funcs)}</b> 个函数与新作品、历史库**同时**高度相似于某个基线库'
        '（教学 OS rCore/uCore/xv6、组委会模板、官方第三方库等），属公共模板代码，'
        f'<b>不计入值得关注的借鉴</b>{more}。'
        '</p>'
        '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">函数</th>'
        '<th class="text-left p-2 border-b">文件:行</th>'
        '<th class="text-left p-2 border-b">基线来源</th>'
        '<th class="text-left p-2 border-b">说明</th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )
    section = _collapsible_html(
        sid, "基线衍生（不计入借鉴）", body,
        default_open=False, tone="excluded",
    )
    toc = _toc_link(sid, "基线衍生", str(len(base_funcs)), "excluded")
    return toc, section


def _false_positive_section(fp_funcs: list[dict], linker, query_repo_id: str) -> tuple[str, str]:
    """疑似误报小节：跨架构 / 行业样板汇编 / 内部跨架构复用（已从借鉴剔除，需人工确认）。

    这些对在「掩码逐行匹配」口径下达到高相似，但属机械误报（详见 false_positives 模块），
    不计入值得关注的借鉴；此处按成因分组单列，保留来源链接，供人工核对（不直接丢弃）。
    """
    # 跨编程语言 pair 在召回/metadata 已被硬过滤，不属于报告展示范围；对旧数据也不展示。
    fp_funcs = [f for f in fp_funcs if f.get("reason") != "cross_lang"]
    if not fp_funcs:
        return "", ""
    sid = "sec-false-positive"
    # 按成因分组
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for f in fp_funcs:
        by_reason[f["reason"]].append(f)

    chips = "".join(
        f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">'
        f'{html.escape(FP_REASON_DISP.get(r, r).split("（")[0])} {len(items)}</span>'
        for r, items in by_reason.items()
    )
    blocks = []
    for reason in ("boilerplate_asm", "cross_arch", "internal_dup"):
        items = by_reason.get(reason)
        if not items:
            continue
        rows = []
        for f in items:
            src = f.get("source") or {}
            if reason == "internal_dup" and f.get("canonical"):
                src_cell = ('<span class="text-slate-500">内部复用自 </span>'
                            + _make_gitlab_anchor(linker, query_repo_id, f["canonical"], 0))
            else:
                src_cell = (_ref_repo_anchor(linker, src.get("repo", "")) + ' '
                            + _make_gitlab_anchor(linker, src.get("repo", ""),
                                                  src.get("file", ""), src.get("start", 0)))
            rows.append(
                '<tr>'
                f'<td class="text-xs font-mono">{html.escape(f["name"])}</td>'
                '<td class="font-mono text-xs">'
                + _make_gitlab_anchor(linker, query_repo_id, f["file"], f.get("start", 0))
                + '</td>'
                f'<td class="text-xs text-slate-500">{html.escape(f.get("lang", "") or "—")}</td>'
                f'<td class="text-xs">{src_cell}</td>'
                f'<td class="text-xs text-slate-400">{src.get("sim", "—")}</td>'
                '</tr>'
            )
        blocks.append(
            f'<div class="text-sm font-semibold text-slate-700 mt-3 mb-1">'
            f'{html.escape(FP_REASON_DISP.get(reason, reason))}（{len(items)} 个函数）</div>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">函数</th>'
            '<th class="text-left p-2 border-b">新作品 文件:行</th>'
            '<th class="text-left p-2 border-b">语言</th>'
            '<th class="text-left p-2 border-b">匹配来源 / 内部副本</th>'
            '<th class="text-left p-2 border-b">逐行匹配率</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        f'下列 <b>{len(fp_funcs)}</b> 个函数在「掩码后逐行匹配」口径下达到高相似，但经成因核查'
        '属<b>机械误报</b>，<b>不计入值得关注的借鉴</b>，已从摘要、借鉴来源图与各模块清单中剔除。'
        '保留来源以便人工复核：</p>'
        '<ul class="text-xs text-slate-500 list-disc pl-5 mb-3 space-y-0.5">'
        '<li><b>跨指令集架构</b>：如龙芯 <code>csrwr</code> 与 RISC-V <code>csrrw</code>，指令本体在 '
        '<code>asm!("…")</code> 字符串里、被归一化掩码成占位符，只剩内联汇编外壳相同——面向不同 CPU，'
        '不可能逐字借鉴。</li>'
        '<li><b>行业样板汇编</b>：<code>__switch</code> 等任务切换的寄存器存取序列，各队写法固定雷同。</li>'
        '<li><b>内部跨架构复用</b>：作品自身 <code>src/</code> 与 <code>src-la/</code> 硬拷贝同名函数，'
        '同一次外部借鉴只记一次、其余记为内部复用。</li>'
        '</ul>'
        f'<div class="flex flex-wrap gap-2 mb-1">{chips}</div>'
        + "".join(blocks)
    )
    section = _collapsible_html(
        sid, "疑似误报（不计入借鉴，需人工确认）", body,
        default_open=False, tone="excluded",
    )
    return _toc_link(sid, "疑似误报", str(len(fp_funcs)), "excluded"), section


def _upstream_baseline_section(ub_funcs: list[dict], linker, query_repo_id: str) -> tuple[str, str]:
    """上游基线 / ABI 受限代码 小节：vendored 上游 OS + Linux/POSIX ABI 受限实现（不计入借鉴）。

    ① 双方 file_path 在同一 upstream_root（arceos/rcore/…）下且相对路径相同 → 双方 vendored
    了同一份上游文件，非跨队抄袭；② 受 ABI 规范硬性限制的唯一性实现（stat 转换、syscall
    shim、build.rs 等），只有一种正确写法。两类已从借鉴 KPI/清单剔除，此处分组单列供核对。
    """
    if not ub_funcs:
        return "", ""
    sid = "sec-upstream-baseline"
    uv = [x for x in ub_funcs if x["reason"] == "upstream_vendored"]
    abi = [x for x in ub_funcs if x["reason"] == "abi_constrained"]
    chips = []
    if uv:
        chips.append(f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">'
                     f'vendored 上游基线 {len(uv)}</span>')
    if abi:
        chips.append(f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">'
                     f'ABI 受限实现 {len(abi)}</span>')

    def _rows(items: list[dict]) -> str:
        out = []
        for f in items:
            src = f.get("source") or {}
            if f["reason"] == "upstream_vendored":
                src_cell = (f'<span class="text-slate-500">双方均 vendored 上游 </span>'
                            f'<code>{html.escape(f.get("root",""))}</code> 同相对路径')
            else:
                src_cell = (_ref_repo_anchor(linker, src.get("repo", "")) + ' '
                            + _make_gitlab_anchor(linker, src.get("repo", ""),
                                                  src.get("file", ""), src.get("start", 0)))
            out.append(
                '<tr>'
                f'<td class="text-xs font-mono">{html.escape(f["name"])}</td>'
                '<td class="font-mono text-xs">'
                + _make_gitlab_anchor(linker, query_repo_id, f["file"], f.get("start", 0))
                + '</td>'
                f'<td class="text-xs text-slate-500">{html.escape(f.get("lang", "") or "—")}</td>'
                f'<td class="text-xs">{src_cell}</td>'
                f'<td class="text-xs text-slate-400">{src.get("sim", "—")}</td>'
                '</tr>'
            )
        return "".join(out)

    blocks = []
    if uv:
        blocks.append(
            '<div class="text-sm font-semibold text-slate-700 mt-3 mb-1">'
            f'vendored 上游基线（{len(uv)} 个函数）</div>'
            '<p class="text-xs text-slate-500 mb-1">双方文件路径位于同一上游根（如 '
            '<code>arceos/</code>）下、且相对路径相同——即双方都整库签入了同一份上游 OS/框架，'
            '逐字相同属必然，<b>非跨队抄袭</b>。队伍自研的新模块（其他队无同名相对路径）不受影响。</p>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">函数</th><th class="text-left p-2 border-b">新作品 文件:行</th>'
            '<th class="text-left p-2 border-b">语言</th><th class="text-left p-2 border-b">说明</th>'
            '<th class="text-left p-2 border-b">逐行匹配率</th>'
            '</tr></thead><tbody>' + _rows(uv) + '</tbody></table></div>'
        )
    if abi:
        blocks.append(
            '<div class="text-sm font-semibold text-slate-700 mt-3 mb-1">'
            f'ABI 受限的唯一性实现（{len(abi)} 个函数）</div>'
            '<p class="text-xs text-slate-500 mb-1">受 Linux/POSIX ABI 规范硬性限制的转换 / shim '
            '（<code>metadata_to_kstat</code>、<code>sys_*</code> 系统调用转换、<code>dummy_stat_*</code>、'
            'build.rs / scripts 等构建脚本、C 库 shim 层）——字段 / 签名由规范规定、只有一种正确写法，'
            '多队必然雷同，<b>不计为借鉴</b>。</p>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">函数</th><th class="text-left p-2 border-b">新作品 文件:行</th>'
            '<th class="text-left p-2 border-b">语言</th><th class="text-left p-2 border-b">匹配来源</th>'
            '<th class="text-left p-2 border-b">逐行匹配率</th>'
            '</tr></thead><tbody>' + _rows(abi) + '</tbody></table></div>'
        )
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        f'下列 <b>{len(ub_funcs)}</b> 个函数属上游基线 / ABI 受限代码，<b>不计入值得关注的借鉴</b>，'
        '已从摘要、借鉴来源图与各模块清单中剔除，单列供核对：</p>'
        f'<div class="flex flex-wrap gap-2 mb-1">{"".join(chips)}</div>'
        + "".join(blocks)
    )
    section = _collapsible_html(
        sid, "上游基线 / ABI 受限代码（不计入借鉴）", body,
        default_open=False, tone="excluded",
    )
    return _toc_link(sid, "上游基线 / ABI 受限代码", str(len(ub_funcs)), "excluded"), section


def _excluded_divider() -> tuple[str, str]:
    """「不计入借鉴的代码」分隔横幅 + TOC 锚（其后跟 库复用/公共样板/基线衍生 各小节）。"""
    sid = "sec-excluded"
    section = (
        f'<section id="{sid}" data-section-id="{sid}" class="excluded-heading">'
        '<span class="chapter-index">04</span><div>'
        '<h2>排除项与统计口径</h2>'
        '<p>以下代码已从上方统计中剔除，用于核对系统的排除是否合理。为避免误判，系统把'
        '「所有团队都会用、并非该团队原创」的代码自动剔除，不计入借鉴。'
        '主要包括：<b>①上游框架自带代码</b>（如 ArceOS/Starry 框架的 axfs/axhal 等模块）；'
        '<b>②第三方开源库</b>（lwext4、fatfs 等整库签入）；'
        '<b>③标准规范受限写法</b>（POSIX 系统调用、文件系统魔数、Linux ABI 转换等只有唯一正确写法的代码）；'
        '<b>④多团队通用样板</b>（print 宏、panic 处理等）；'
        '<b>⑤教学/模板衍生代码</b>。下方按类别列出，可逐项点开核对扣除是否合理。</p></div>'
        '</section>'
    )
    toc = _toc_link(sid, "排除项说明")
    return toc, section


def _collapsible(sid: str, title: str, body: str) -> tuple[str, str]:
    """统一的可折叠 section 外壳。返回 (toc_entry, section_html)。"""
    section = _collapsible_html(sid, title, body)
    toc = _toc_link(sid, title)
    return toc, section


_REVIEW_PROMPT_VERSION = "v5-two-stage-role-gate"  # 改 prompt 即 bump，使旧缓存自动失效

_REVIEW_ROLE_SYSTEM = """你是 OS 内核函数职责分析助手。只判断给定两个函数的职责是否一致，不判断代码借鉴。
职责必须综合输入输出、主要副作用、操作对象和在子系统中的作用，不能只看函数名或局部语句。
输出要求：
- 只输出一个单行 JSON 对象，禁止 Markdown 和额外文字；
- responsibility 只能是“一致”“部分一致”或“不一致”；
- responsibility_reason 必须非空，并具体说明双方各自做什么；
- evidence_anchors 必须包含 1～4 个从代码中逐字复制、可定位的标识符、常量或短表达式。
严格格式：
{"responsibility":"一致|部分一致|不一致","responsibility_reason":"不超过80字的中文职责依据","evidence_anchors":["代码中的原文锚点"]}"""


_REVIEW_SIM_SYSTEM = """你是 OS 内核代码查重复核助手。职责分析阶段已经确认两个函数职责一致或部分一致。
现在只判断新作品是否借鉴（复制 / 改名 / 改写）了历史函数。

代码相似复核要区分 OS 内核常见的教科书式通用写法与实质性自研重构（双方各自独立实现 / 为不同目标改写）。
判定参考：
- 整体逻辑、结构、命名高度一致，且并非人尽皆知的通用套路、也无实质性机制改动 → 借鉴
- 属通用算法 / 框架套路（RR 调度、buddy 分配、链表增删、RISC-V trap 上下文、寄存器读写宏等），\
结构相似但属常识 → 非借鉴
- 介于两者之间、证据不足 → 疑似
**判定纪律（评审实测暴露的误报高发区，务必遵守）：**
1. **看实质机制改动，不只看字段/逐行重合度**：若新作品为适配不同架构 / 异步范式 / 并发模型\
而改了**数据结构、控制流或同步机制**（哪怕大量字段名或行逐字重合），属自研重构 → 判**非借鉴**。\
典型：Process/Task 结构体的 new()——即使多数字段与历史一致，但新增了异步调度所需字段\
（如 child_exit_event/exit_event）、改用 Arc/Future/无锁结构 → 非借鉴。**不要因为「字段结构、\
命名高度一致」就判借鉴**，OS 内核同类结构体字段本就大量重合，关键看有没有为新目标做结构/机制改造。
2. 同步↔异步范式重构：一方阻塞式内核线程循环 fn f()，一方改写为 async Future 轮询\
fn f(cx:&mut Context)->Poll<()>。仅因处理同一队列用了相似出队语法，但执行机制不同 → 非借鉴。
3. 锁/数据结构不同：顶层逻辑相同（如「回调推入全局数组」），但一方无锁/裸指针\
（NoPreemptIrqSave+current_ref_mut_raw），另一方 RefCell borrow_mut/trap::disable_local → 非借鉴。
4. 标准协议 / 硬编码常数约束（规范唯一性）：POSIX 信号号（SIGALRM=14/SIGVTALRM=26/SIGPROF=27）、\
文件系统魔数（EXT4=0xef53）、枚举→数字映射（SchedPolicy→0,1,2）、/proc/[pid]/stat 字段拼接、\
robust futex 流程、struct kstat 转换——值/流程由标准硬性规定，只有一种正确写法 → 非借鉴。
5. 第三方库胶水代码：在 chrono/fatfs 等库 API 间转换（读 .year()/.month() 重组），标准解法唯一 → 非借鉴。
6. Rust trait 标准方法（from/fmt/into/default/clone/eq/cmp）/ VFS 教科书样板（.与..排序的 cmp）\
的短小实现：全网千篇一律 → 非借鉴。
输出必须满足以下全部要求：
- 只输出一个单行 JSON 对象，禁止 Markdown 代码块和任何额外文字；
- 所有字段必须存在且非空；reason 必须写出可由代码直接核验的事实；
- evidence_anchors 必须包含 1～4 个从所给两段代码中逐字复制的标识符、常量或短表达式，禁止概括性词语；
严格格式：
{"verdict":"借鉴|疑似|非借鉴","reason":"不超过120字的中文复核理由","evidence_anchors":["代码中的原文锚点"]}"""


def _review_failure(detail: str) -> dict:
    """构造明确的复核失败结果；失败不等价于模型仍存疑。"""
    return {
        "verdict": "复核失败",
        "reason": f"复核失败：{detail}"[:180],
        "responsibility": "未判定",
        "responsibility_reason": "",
        "evidence_anchors": [],
    }


def _parse_json_object(text: str) -> dict:
    """只接受无围栏、无前后缀的单一 JSON 对象。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("不是严格的单一 JSON 对象") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")
    return data


def _validate_evidence_anchors(raw_anchors: object, query_code: str,
                               ref_code: str) -> list[str]:
    """验证 1～4 个证据锚点确实逐字存在于本次输入代码中。"""
    if not isinstance(raw_anchors, list) or not 1 <= len(raw_anchors) <= 4:
        raise ValueError("证据锚点必须为 1～4 项数组")
    code = f"{query_code}\n{ref_code}"
    clean_anchors: list[str] = []
    for raw in raw_anchors:
        anchor = str(raw or "").strip()
        if len(anchor) < 2 or len(anchor) > 100:
            raise ValueError("证据锚点长度无效")
        if anchor not in code:
            raise ValueError(f"证据锚点无法在代码中定位：{anchor[:24]}")
        clean_anchors.append(anchor)
    return clean_anchors


def _parse_role_payload(text: str, query_code: str, ref_code: str) -> dict:
    """校验第一阶段职责判断。"""
    data = _parse_json_object(text)
    required = {"responsibility", "responsibility_reason", "evidence_anchors"}
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError("职责判断缺少字段：" + "、".join(missing))
    responsibility = str(data.get("responsibility") or "").strip()
    reason = str(data.get("responsibility_reason") or "").strip()
    if responsibility not in ("一致", "部分一致", "不一致"):
        raise ValueError("responsibility 取值无效")
    if len(reason) < 6:
        raise ValueError("职责依据为空或过于笼统")
    return {
        "responsibility": responsibility,
        "responsibility_reason": reason[:80],
        "evidence_anchors": _validate_evidence_anchors(
            data.get("evidence_anchors"), query_code, ref_code),
    }


def _parse_verdict_payload(text: str, query_code: str, ref_code: str) -> dict:
    """校验第二阶段同源结论；该阶段只会在职责门控通过后调用。"""
    data = _parse_json_object(text)
    required = {"verdict", "reason", "evidence_anchors"}
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError("代码复核缺少字段：" + "、".join(missing))
    verdict = str(data.get("verdict") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if verdict not in ("借鉴", "疑似", "非借鉴"):
        raise ValueError("verdict 取值无效")
    if len(reason) < 6:
        raise ValueError("复核理由为空或过于笼统")
    return {
        "verdict": verdict,
        "reason": reason[:120],
        "evidence_anchors": _validate_evidence_anchors(
            data.get("evidence_anchors"), query_code, ref_code),
    }


def _parse_review_payload(text: str, query_code: str, ref_code: str) -> dict:
    """校验两阶段合并后的缓存协议。"""
    data = _parse_json_object(text)

    required = {"responsibility", "responsibility_reason", "verdict", "reason",
                "evidence_anchors"}
    missing = sorted(required - data.keys())
    if missing:
        raise ValueError("缺少字段：" + "、".join(missing))

    responsibility = str(data.get("responsibility") or "").strip()
    responsibility_reason = str(data.get("responsibility_reason") or "").strip()
    verdict = str(data.get("verdict") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if responsibility not in ("一致", "部分一致", "不一致"):
        raise ValueError("responsibility 取值无效")
    if verdict not in ("借鉴", "疑似", "非借鉴"):
        raise ValueError("verdict 取值无效")
    if len(responsibility_reason) < 6:
        raise ValueError("职责依据为空或过于笼统")
    if len(reason) < 6:
        raise ValueError("复核理由为空或过于笼统")
    if responsibility == "不一致" and verdict != "非借鉴":
        raise ValueError("职责不一致时必须判为非借鉴")
    clean_anchors = _validate_evidence_anchors(
        data.get("evidence_anchors"), query_code, ref_code)

    return {
        "verdict": verdict,
        "reason": reason[:120],
        "responsibility": responsibility,
        "responsibility_reason": responsibility_reason[:80],
        "evidence_anchors": clean_anchors,
    }


def _review_one(client, model: str, g: dict, timeout: int) -> dict:
    """执行两个独立模型阶段：职责不一致时不发起第二阶段代码同源判断。"""
    cand = (g.get("candidates") or [{}])[0]
    query_code = g.get("query_code", "")
    ref_code = cand.get("ref_code", "")
    code_context = (
        f"【新作品函数】{g.get('query_func','')}（{g.get('query_file','')}）：\n"
        f"```\n{query_code}\n```\n\n"
        f"【历史库最相似函数】{cand.get('ref_func','')}"
        f"（{cand.get('ref_repo','')}/{cand.get('ref_file','')}）：\n"
        f"```\n{ref_code}\n```"
    )
    stage = "职责判断"
    try:
        role_resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _REVIEW_ROLE_SYSTEM},
                      {"role": "user", "content": code_context
                       + "\n\n请只判断两个函数的职责关系。"}],
            temperature=0.0, max_tokens=250, timeout=timeout,
        )
        role = _parse_role_payload(
            (role_resp.choices[0].message.content or "").strip(), query_code, ref_code)

        if role["responsibility"] == "不一致":
            result = {
                **role,
                "verdict": "非借鉴",
                "reason": ("职责不一致，候选配对不成立："
                           + role["responsibility_reason"])[:120],
            }
            return _parse_review_payload(
                json.dumps(result, ensure_ascii=False), query_code, ref_code)

        stage = "代码同源复核"
        sim_resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _REVIEW_SIM_SYSTEM},
                      {"role": "user", "content": (
                          code_context
                          + f"\n\n职责阶段结论：{role['responsibility']}；"
                          + role["responsibility_reason"]
                          + f"\n向量相似度 {g.get('overall_sim','')}；匹配证据：{_clone_summary(g)}。"
                          + "\n请进行代码同源复核。") }],
            temperature=0.0, max_tokens=350, timeout=timeout,
        )
        verdict = _parse_verdict_payload(
            (sim_resp.choices[0].message.content or "").strip(), query_code, ref_code)
        anchors = list(dict.fromkeys(
            role["evidence_anchors"] + verdict["evidence_anchors"]))[:4]
        result = {
            "responsibility": role["responsibility"],
            "responsibility_reason": role["responsibility_reason"],
            "verdict": verdict["verdict"],
            "reason": verdict["reason"],
            "evidence_anchors": anchors,
        }
        return _parse_review_payload(
            json.dumps(result, ensure_ascii=False), query_code, ref_code)
    except ValueError as exc:
        return _review_failure(f"{stage}输出格式或证据字段无效（{exc}）")
    except Exception as e:
        return _review_failure(f"{stage}调用异常（{type(e).__name__}）")


# Rust trait 标准方法名 + 通用短函数名——confirmed 档里这类函数最可能是 ABI/样板误报，
# 一并送 LLM 复核（confirmed 正常不进复核，这些是例外）。
_BOILERPLATE_NAMES = {
    "from", "fmt", "into", "default", "new", "as_ref", "as_mut", "deref", "deref_mut",
    "clone", "eq", "ne", "cmp", "partial_cmp", "hash", "drop", "iter", "next", "len",
    "is_empty", "clear", "index", "index_mut",
}
# 短于等于此非空行的 confirmed 函数也送复核（短函数最易撞文本骨架）。
_BOILERPLATE_MAX_LINES = 12


def _is_boilerplate_candidate(g: dict) -> bool:
    """confirmed 函数是否需送 LLM 复核：函数名是通用 trait 方法，或函数体很短。"""
    name = (g.get("query_func") or "").lower().strip()
    if name in _BOILERPLATE_NAMES:
        return True
    code = g.get("query_code") or ""
    nonblank = sum(1 for ln in code.splitlines() if ln.strip())
    return 0 < nonblank <= _BOILERPLATE_MAX_LINES


def run_review_judgment(review_pairs: list[dict], work_dir: Path,
                        model: str | None = None, timeout: int = 30,
                        workers: int = 5) -> None:
    """对候选逐对执行“职责门控→同源复核”，原地写入严格校验后的结果与证据。

    模型默认 deepseek-v4-flash（可经环境变量 REVIEW_MODEL 覆盖）；曾用 qwen-turbo，但实测
    qwen-turbo 无法识别「异步重构 / 不同锁机制 / ABI 受限字段拼接」等语义级非借鉴（把
    register_timer_callback、fmt 误判为借鉴），deepseek-v4-flash 能正确识别（24 vs 8 个非借鉴）。
    base_url / api_key 复用 config.toml 的 [api]。职责不一致不会调用第二阶段；有效结果逐对缓存，
    失败结果下次运行自动重试。
    """
    if not review_pairs:
        return
    model = model or os.getenv("REVIEW_MODEL", "deepseek-v4-flash")
    try:
        from oskernel_agent import config as _cfg
        api_key  = _cfg.api.get("key", "").strip()
        base_url = _cfg.api.get("base_url", "https://api.deepseek.com/v1").strip()
    except Exception:
        api_key = base_url = ""

    work_dir.mkdir(parents=True, exist_ok=True)
    cache_file = work_dir / f"review_judgment_{model}.json"
    cache: dict = {}
    if cache_file.exists():
        try:
            cache = json.loads(cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}

    def _key(g: dict) -> str:
        cand = (g.get("candidates") or [{}])[0]
        # 含 prompt 版本：改 prompt 后旧缓存键不命中，自动按新 prompt 重判
        return _cache_key(_REVIEW_PROMPT_VERSION, model, g.get("query_func", ""),
                          cand.get("ref_func", ""), g.get("query_code", ""),
                          cand.get("ref_code", ""))

    def _valid_cached_result(value: object, g: dict) -> bool:
        if not isinstance(value, dict):
            return False
        verdict = value.get("verdict")
        if verdict == "复核失败":
            return False  # 失败结果只供本次报告展示，下次运行必须重试
        if verdict == "未复核":
            return False
        cand = (g.get("candidates") or [{}])[0]
        try:
            _parse_review_payload(
                json.dumps(value, ensure_ascii=False),
                g.get("query_code", ""), cand.get("ref_code", ""))
            return True
        except ValueError:
            return False

    if not api_key:
        logger.warning("[review] 未配置 API key，疑似借鉴跳过 LLM 复核")
        for g in review_pairs:
            g.update({
                "review_verdict": "未复核",
                "review_reason": "未配置 API key",
                "review_responsibility": "未判定",
                "review_responsibility_reason": "",
                "review_evidence_anchors": [],
            })
        return

    from openai import OpenAI
    from concurrent.futures import ThreadPoolExecutor
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    pending = [g for g in review_pairs
               if not _valid_cached_result(cache.get(_key(g)), g)]
    logger.info("[review] 疑似借鉴 {} 对，缓存命中 {}，用 {} 复核 {} 对",
                len(review_pairs), len(review_pairs) - len(pending), model, len(pending))

    def _work(g: dict):
        return _key(g), _review_one(client, model, g, timeout)

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for k, result in ex.map(_work, pending):
                cache[k] = result
        try:
            cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except OSError:
            pass

    for g in review_pairs:
        c = cache.get(_key(g), {})
        g["review_verdict"] = c.get("verdict", "未复核")
        g["review_reason"] = c.get("reason", "本次未获得有效模型结论")
        g["review_responsibility"] = c.get("responsibility", "未判定")
        g["review_responsibility_reason"] = c.get("responsibility_reason", "")
        g["review_evidence_anchors"] = c.get("evidence_anchors", [])


def _apply_review_verdicts(suspects: list[dict], groups: list[dict]) -> tuple[int, int]:
    """据 LLM 复核结论保守改写 tier，不把“不确定”降成未命中。
      - review/weak：借鉴 → confirmed；疑似 → 保留；非借鉴 → dismissed
      - confirmed（全量送复核）：**仅** 非借鉴 → dismissed（保守，不丢失信号）；借鉴/疑似保留 confirmed
      - 复核失败 / 未复核 → 保持原档不动，并单独记录状态，绝不折算为“疑似”
    confirmed 用保守口径：文本相似≠借鉴，但只在 LLM 明确判「非借鉴」（ABI/规范/标准算法/不同机制）
    时才降级；疑似（拿不准）不降，避免误降真实借鉴。返回 (升档数, 明确排除数)。
    """
    result_map = {
        (g["query_file"], g["query_func"], g["query_start"]): g
        for g in groups
    }
    up = dn = 0
    for s in suspects:
        tier = s.get("tier")
        if tier not in ("review", "weak", "confirmed"):
            continue
        q = s.get("query_func", {})
        result = result_map.get(
            (q.get("file_path", ""), q.get("func_name", ""), q.get("start_line", 0)))
        if not result:
            continue
        v = result.get("review_verdict", "未复核")
        s["review_verdict"] = v
        s["review_reason"] = result.get("review_reason", "")
        s["review_responsibility"] = result.get("review_responsibility", "未判定")
        s["review_responsibility_reason"] = result.get(
            "review_responsibility_reason", "")
        s["review_evidence_anchors"] = result.get("review_evidence_anchors", [])
        if v == "借鉴":
            if tier != "confirmed":       # review/weak 升为 confirmed；已 confirmed 不动
                s["tier"] = "confirmed"
                s["confirm_via"] = "review_llm"
                up += 1
        elif v == "非借鉴":
            s["tier"] = "dismissed"
            s["dismiss_reason"] = "review_非借鉴"
            dn += 1
        elif v == "疑似":
            # 不确定不是阴性证据：所有档位原样保留，交给人工复核。
            pass
    return up, dn


def _review_section(review_pairs: list[dict], linker, query_repo_id: str) -> tuple[str, str]:
    """拆分展示有效存疑、复核失败与未复核；流程失败绝不伪装成风险结论。"""
    if not review_pairs:
        body = '<p class="text-sm text-slate-500">当前没有待处理的模型复核函数。</p>'
        section = _collapsible_html(
            "sec-review", "模型复核与待处理队列", body, tone="review",
            subtitle="仅有效返回“疑似”的函数计入存疑；失败和未复核单独列示",
        )
        return _toc_link("sec-review", "模型复核与待处理队列", "0", "zero"), section

    for g in review_pairs:
        g["review_priority"] = _review_priority(g)
    review_pairs = sorted(
        review_pairs,
        key=lambda g: (-g["review_priority"]["score"], -float(g.get("overall_sim") or 0.0),
                       g.get("query_file", ""), g.get("query_func", "")),
    )
    unique_review: dict[tuple, dict] = {}
    for g in review_pairs:
        unique_review.setdefault((g.get("query_file", ""), g.get("query_func", "")), g)
    rows = list(unique_review.values())
    uncertain = [g for g in rows if g.get("review_verdict") == "疑似"]
    failed = [g for g in rows if g.get("review_verdict") == "复核失败"]
    pending = [g for g in rows if g.get("review_verdict") not in ("疑似", "复核失败")]
    n_funcs, n_uncertain, n_failed, n_pending = (
        len(rows), len(uncertain), len(failed), len(pending))

    intro = ('<p class="text-sm text-slate-600 mb-3">'
             f'下列 <b>{n_funcs}</b> 个函数已进入职责门控与代码同源复核。模型必须先判断双方职责，'
             '职责一致或部分一致时才继续代码相似判断，并提供能在代码中定位的证据锚点。'
             f'其中有效返回“疑似” <b>{n_uncertain}</b> 个、复核失败 <b>{n_failed}</b> 个、'
             f'未复核 <b>{n_pending}</b> 个。复核失败和未复核只是流程状态，<b>不计入模型仍存疑</b>。'
             '队列按可解释优先级排序：综合相似度、有效代码规模、内核核心子系统重要度、克隆类型与来源数量；'
             '模型明确判为非借鉴的项目已移出相似清单，不在本节重复展示。</p>')

    chips = []
    for verdict, count in (("疑似", n_uncertain), ("复核失败", n_failed), ("未复核", n_pending)):
        if count:
            cls, label = _REVIEW_VERDICT_STYLE[verdict]
            chips.append(f'<span class="px-2 py-0.5 rounded {cls}">{label} {count}</span>')
    summary = ('<div class="review-note"><b>状态口径</b>：只有通过 JSON、非空理由和代码锚点校验后'
               '仍返回“疑似”的结果才进入存疑统计；任何格式错误、空理由、锚点无法定位或模型调用异常'
               '均显示为“复核失败”。<div class="flex flex-wrap gap-2 mt-2 text-sm">'
               + "".join(chips) + '</div></div>')

    tables = (
        _groups_table("模型有效复核后仍存疑", uncertain, linker, query_repo_id,
                      "text-amber-700", show_verdict=True, show_priority=True)
        + _groups_table("复核失败（不计为存疑）", failed, linker, query_repo_id,
                        "text-rose-700", show_verdict=True, show_priority=True)
        + _groups_table("复核未完成（不计为存疑）", pending, linker, query_repo_id,
                        "text-slate-600", show_verdict=True, show_priority=True)
    )
    section = _collapsible_html(
        "sec-review", "模型复核与待处理队列", intro + summary + tables,
        tone="review", subtitle="职责先行；有效存疑、复核失败和未复核采用互斥口径",
    )
    return _toc_link("sec-review", "模型复核与待处理队列", str(n_funcs), "review"), section


_AI_STAGE_DISP = {
    "fast_filter": "快筛（高置信）",
    "perturbation": "扰动复核",
    "npr": "扰动复核",
    "stage2": "扰动复核",
}


def _ai_detect_section(ai_data: dict | None, linker, query_repo_id: str) -> tuple[str, str]:
    """AI 生成代码检测：整体 KPI + 疑似 AI 函数明细（含指标通俗说明）。"""
    if not ai_data or ai_data.get("status") != "ok":
        body = '<p class="text-sm text-slate-500">当前批次没有可用的 AI 生成代码检测产物；该缺失不影响同源分析。</p>'
        section = _collapsible_html(
            "sec-aidetect", "附录：AI 生成代码检测", body, default_open=False,
            tone="appendix", subtitle="独立辅助信号，不改变同源代码与候选创新结论",
        )
        return _toc_link("sec-aidetect", "AI 生成代码检测附录", "0", "zero"), section
    ov = (ai_data.get("aggregated") or {}).get("overall") or {}
    if not ov:
        body = '<p class="text-sm text-slate-500">AI 检测产物中没有整体统计，无法形成有效附录。</p>'
        section = _collapsible_html(
            "sec-aidetect", "附录：AI 生成代码检测", body, default_open=False,
            tone="appendix", subtitle="独立辅助信号，不改变同源代码与候选创新结论",
        )
        return _toc_link("sec-aidetect", "AI 生成代码检测附录", "0", "zero"), section
    ag = ai_data["aggregated"]
    kpis = (
        '<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-1 mb-3">'
        + _kpi(f'{ov.get("llm_count", 0)}', "疑似 AI 生成（函数）", "#9333ea")
        + _kpi(f'{ov.get("human_count", 0)}', "判为人工编写（函数）", "#16a34a")
        + _kpi(f'{ov.get("uncertain_count", 0)}', "无法判定（信号不足）", "#64748b")
        + _kpi(f'{ov.get("llm_ratio_by_count", 0.0)*100:.0f}%', "疑似 AI 占比（按函数）", "#9333ea")
        + '</div>'
    )
    meta = (f'<p class="text-sm text-slate-600 mb-3">本节<b>独立于上方查重</b>，检测疑似「未声明使用 AI 生成」的代码。'
            f'在 <b>{ov.get("total_functions", 0)}</b> 个非借鉴函数中检测'
            f'（此口径含框架等代码，与上方查重「自研函数」数不同，属正常）；疑似 AI 占比按代码行数为 '
            f'{ov.get("llm_ratio_by_loc", 0.0)*100:.0f}%。打分模型 '
            f'<code>{html.escape(ai_data.get("model_id", "") or "")}</code>。'
            '原理：把代码喂给一个代码大模型，统计它对每个 token 的“眼熟程度”——'
            'AI 写的代码模型普遍更“眼熟”，人写的更“意外”，据此区分。结果仅供参考，'
            '存在误判，<b>不作为认定依据</b>。</p>')

    sf = ag.get("suspicious_functions") or []
    sf_table = ""
    if sf:
        srows = []
        for s in sf[:30]:
            lr = s.get("log_rank")
            lr_disp = f"{lr:.3f}" if isinstance(lr, (int, float)) else "—"
            fp = (s.get("file_path", "") or "").replace("\\", "/")
            stage_disp = _AI_STAGE_DISP.get(s.get("stage", ""), s.get("stage", "") or "—")
            srows.append(
                '<tr>'
                '<td class="font-mono text-xs">'
                + _make_gitlab_anchor(linker, query_repo_id, fp,
                                      s.get("start_line", 0), s.get("end_line", 0))
                + '</td>'
                f'<td class="text-xs font-mono">{html.escape(s.get("function_name", "") or "")}</td>'
                f'<td class="text-xs">{s.get("loc", 0)} 行</td>'
                f'<td class="text-xs font-semibold text-purple-700">{s.get("confidence", 0.0)*100:.0f}%</td>'
                f'<td class="text-xs">{lr_disp}</td>'
                f'<td class="text-xs text-slate-500">{html.escape(stage_disp)}</td>'
                '</tr>'
            )
        legend = (
            '<p class="text-xs text-slate-500 mt-2 leading-relaxed">'
            '指标说明：'
            '<b>“眼熟值”(log-rank)</b> = 代码在大模型里的平均 token 排名（对数），'
            '<b>数值越低越像 AI 生成</b>（本系统判定线约 0.31，真人代码平均约 0.62）；'
            '<b>把握度</b> = 该条判定的可信程度；'
            '<b>判定依据</b>：「快筛」=仅凭“眼熟值”就能高把握判定，'
            '「扰动复核」=“眼熟值”在模糊地带、再做多次微改后看变化（NPR 信号）二次确认。'
            '</p>'
        )
        sf_table = (
            '<div class="text-sm font-semibold text-slate-700 mb-1">疑似 AI 生成函数（点击文件名跳转源码）</div>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">文件:行（可点击）</th><th class="text-left p-2 border-b">函数</th>'
            '<th class="text-left p-2 border-b">规模</th>'
            '<th class="text-left p-2 border-b">把握度</th>'
            '<th class="text-left p-2 border-b">“眼熟值”(log-rank)</th>'
            '<th class="text-left p-2 border-b">判定依据</th>'
            '</tr></thead>'
            f'<tbody>{"".join(srows)}</tbody></table></div>'
            + legend
        )

    disclaimer = (
        '<div class="mb-3 p-2.5 rounded bg-amber-50 border border-amber-200 '
        'text-xs text-amber-800">⚠️ <b>仅供参考</b>：本结果基于免训练统计信号'
        '（“眼熟值”/扰动复核）推断，<b>存在误判，不作为 AI 生成的最终判定依据</b>；'
        '判定阈值与打分模型强相关、整体准确率（AUC）约 0.86，“无法判定”的函数尤其需要人工核查。</div>'
    )
    section = _collapsible_html(
        "sec-aidetect", "附录：AI 生成代码检测", disclaimer + kpis + meta + sf_table,
        default_open=False, tone="appendix",
        subtitle="独立辅助信号，不改变同源代码与候选创新结论",
    )
    return _toc_link("sec-aidetect", "AI 生成代码检测附录",
                     str(len(sf)), "review"), section


def _license_files(query_repo_path: Path | None) -> list[str]:
    if not query_repo_path or not query_repo_path.is_dir():
        return []
    names = re.compile(r"^(?:license|copying|notice)(?:\..*)?$", re.IGNORECASE)
    found = []
    for path in query_repo_path.rglob("*"):
        if path.is_file() and names.match(path.name) and ".git" not in path.parts:
            try:
                found.append(path.relative_to(query_repo_path).as_posix())
            except ValueError:
                continue
        if len(found) >= 30:
            break
    return sorted(found)


def _compliance_section(query_repo_path: Path | None, linker, query_repo_id: str,
                        suspects: list[dict],
                        lib_stats: list[dict], cc_funcs: list[dict], fp_funcs: list[dict],
                        ub_funcs: list[dict], base_funcs: list[dict]) -> tuple[str, str]:
    """合法复用与许可证证据总览；只做材料检查，不给法律结论。"""
    license_files = _license_files(query_repo_path)
    exclusion = _exclusion_totals(suspects)
    unique_excluded = int(exclusion.get("total_excluded") or 0)
    categories = [
        ("第三方库复用", sum(int(x.get("func_count") or 0) for x in lib_stats), "检查来源、版本和许可证"),
        ("公共/样板代码", len(cc_funcs), "不计入同源结论，保留规则证据"),
        ("机械误报", len(fp_funcs), "跨架构/模板汇编等已降级"),
        ("共同上游与 ABI", len(ub_funcs), "核验上游归属及修改义务"),
        ("基线衍生", len(base_funcs), "按比赛允许基线单列"),
    ]
    rows = "".join(
        f'<tr><td>{html.escape(name)}</td><td>{count}</td><td>{html.escape(action)}</td></tr>'
        for name, count, action in categories
    )
    license_links = "".join(
        '<li>' + _make_gitlab_anchor(linker, query_repo_id, path, 1) + '</li>'
        for path in license_files
    ) or '<li class="text-amber-700">未自动发现 LICENSE / COPYING / NOTICE 文件</li>'
    body = (
        '<div class="section-intro"><b>本节回答“相似代码是否属于可解释、可许可的复用”。</b>'
        '系统把第三方库、共同上游、ABI 约束、公共样板、机械误报和比赛基线与高置信同源代码分开。'
        '许可证文件“存在”不等于合规完成，仍需核对具体许可证、版权声明、修改说明和比赛规则。</div>'
        '<p class="text-xs text-slate-500 mb-3">类别表保留各检测标签的原始数量，同一函数可能同时具有'
        '多个标签；“唯一排除函数”按互斥优先级归一去重，与摘要过滤透明度保持同一口径。</p>'
        '<div class="compliance-grid"><div><span>许可证/声明文件</span>'
        f'<b>{len(license_files)}</b></div><div><span>排除类别</span><b>{sum(1 for _, n, _ in categories if n)}</b></div>'
        f'<div><span>唯一排除函数</span><b>{unique_excluded}</b></div></div>'
        '<div class="grid grid-cols-1 lg:grid-cols-[1fr_17rem] gap-3">'
        '<div class="overflow-x-auto"><table><thead><tr><th>复用/排除类型</th><th>数量</th><th>评审核查项</th>'
        f'</tr></thead><tbody>{rows}</tbody></table></div>'
        '<div class="license-panel"><b>已发现的许可证材料</b><ul>' + license_links + '</ul></div></div>'
    )
    section = _collapsible_html(
        "sec-compliance", "合法复用与许可证合规", body, tone="compliance",
        subtitle="把允许复用、共同上游和待补许可证材料与同源代码结论分开",
    )
    return _toc_link("sec-compliance", "合法复用与许可证合规",
                     str(unique_excluded), "excluded"), section


def _technical_appendix(retrieval_contract: dict | None, analysis_mode: str) -> tuple[str, str]:
    contract = retrieval_contract or {}
    coverage = contract.get("history_coverage") or {}
    channels = contract.get("channels") or []
    errors = contract_errors(contract)
    status = "通过" if not errors else "未通过：" + "；".join(errors)
    body = (
        '<div class="technical-grid">'
        f'<div><span>报告生成时间</span><b>{datetime.now().astimezone().isoformat(timespec="seconds")}</b></div>'
        f'<div><span>召回契约</span><b>v{html.escape(str(contract.get("version") or "—"))} · {html.escape(status)}</b></div>'
        f'<div><span>历史库覆盖</span><b>{coverage.get("covered", "—")}/{coverage.get("configured", "—")}</b></div>'
        f'<div><span>语言边界</span><b>{"仅同语言比较" if contract.get("same_language_only") else "未声明"}</b></div>'
        f'<div><span>召回通道</span><b>{html.escape("、".join(channels) or "—")}</b></div>'
        f'<div><span>模型分析模式</span><b>{html.escape(analysis_mode)}</b></div>'
        f'<div><span>语义提示版本</span><b>{_SEMANTIC_PROMPT_VERSION}</b></div>'
        f'<div><span>创新提示版本</span><b>{_INNOVATION_PROMPT_VERSION}</b></div>'
        '</div>'
        '<div class="section-intro">高置信同源来源按“来源仓库内的唯一目标函数”计数，并以匹配行证据或'
        '函数规模×最终相似度估算有效相似行；不会用同一函数的候选 pair 数放大来源排名。'
        '功能簇与复核优先级只用于组织人工审阅，不替代代码证据、时间证据或最终评审结论。'
        '阈值、规则和模型结论都可能受历史库覆盖与实现形态影响。</div>'
    )
    section = _collapsible_html(
        "sec-technical", "附录：完整技术证据与口径", body, default_open=False,
        tone="appendix", subtitle="召回契约、覆盖范围、模型版本、统计单位与限制",
    )
    return _toc_link("sec-technical", "完整技术证据与口径"), section


def generate_comparison_html(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_pairs: list[dict],
    analysis_html: str,
    original_funcs: list[dict],
    innovation_points: list[dict] | None = None,
    review_pairs: list[dict] | None = None,
    ai_detect_data: dict | None = None,
    query_repo_path: Path | None = None,
    linker=None,
    file_matches: list[dict] | None = None,
    file_similar: list[dict] | None = None,
    lib_stats: list[dict] | None = None,
    cc_funcs: list[dict] | None = None,
    base_funcs: list[dict] | None = None,
    fp_funcs: list[dict] | None = None,
    ub_funcs: list[dict] | None = None,
    retrieval_contract: dict | None = None,
    analysis_mode: str = "模型分析（失败时规则兜底）",
) -> str:
    """组装完整的查重对比 HTML 报告（直接产出，不经 Markdown 转换）。"""
    file_matches = file_matches or []
    file_similar = file_similar or []
    summary_html = _summary_card(query_repo_id, suspects, submodule_stats,
                                 file_match_count=len(file_matches),
                                 file_similar_count=len(file_similar),
                                 retrieval_contract=retrieval_contract)
    toc_lineage, sec_lineage = _lineage_section(query_repo_id, suspects, linker)
    toc_clusters, sec_clusters = _cluster_section(
        file_pairs, analysis_html, linker, query_repo_id)
    toc_review, sec_review = _review_section(review_pairs or [], linker, query_repo_id)
    toc_files, sec_files = _file_level_section(file_matches, file_similar, linker, query_repo_id)
    toc_innovation, sec_innovation = _innovation_section(
        innovation_points or [], linker, query_repo_id, query_repo_path)
    toc_compliance, sec_compliance = _compliance_section(
        query_repo_path, linker, query_repo_id, suspects, lib_stats or [], cc_funcs or [],
        fp_funcs or [], ub_funcs or [], base_funcs or [])
    toc_ai, sec_ai = _ai_detect_section(ai_detect_data, linker, query_repo_id)
    toc_orig, sec_orig = _original_section(original_funcs, linker, query_repo_id)
    toc_technical, sec_technical = _technical_appendix(retrieval_contract, analysis_mode)

    excluded_toc: list[str] = []
    excluded_sections: list[str] = []
    for toc, section in [
        _reused_libraries_section(lib_stats or [], query_repo_id),
        _common_code_section(cc_funcs or [], linker, query_repo_id),
        _false_positive_section(fp_funcs or [], linker, query_repo_id),
        _upstream_baseline_section(ub_funcs or [], linker, query_repo_id),
        _baseline_section(base_funcs or [], linker, query_repo_id),
    ]:
        if toc:
            excluded_toc.append(toc)
            excluded_sections.append(section)

    toc_html = (
        '<div class="toc-card">'
        '<div class="toc-header"><span class="toc-kicker">COMPARISON REPORT</span>'
        '<strong>报告目录</strong>'
        f'<span class="toc-repo" title="{html.escape(query_repo_id)}">{html.escape(query_repo_id)}</span>'
        '</div><div class="toc-scroll">'
        + _toc_group("评审结论", [_toc_link("summary", "评审结论摘要")])
        + _toc_group("同源判断", [toc_lineage, toc_clusters, toc_review, toc_files])
        + _toc_group("候选创新", [toc_innovation])
        + _toc_group("合规复用", [toc_compliance] + excluded_toc)
        + _toc_group("附录", [toc_ai, toc_orig, toc_technical])
        + '</div></div>'
    )

    body_parts = [
        _chapter_heading("01", "评审结论摘要", "先给出可执行结论、证据规模、来源排名和完整性口径。"),
        summary_html,
        _chapter_heading("02", "共同上游判断", "解释可归因的公共来源，并将这些代码从同源证据中分离。"),
        sec_lineage,
        _chapter_heading("03", "高置信同源功能簇", "以功能级同源事件替代零散函数堆叠，簇内保留全部代码映射。"),
        sec_clusters,
        _chapter_heading("04", "模型复核与待处理队列", "先做函数职责门控；有效存疑、复核失败和未复核分栏统计。"),
        sec_review,
        _chapter_heading("05", "文件级和非函数代码证据", "补充整文件、结构体、宏、汇编、链接脚本与配置证据。"),
        sec_files,
        _chapter_heading("06", "相对参考实现的候选创新", "将参考基线、实现代码、调用入口、影响范围、验证证据和反证绑定。"),
        sec_innovation,
        _chapter_heading("07", "合法复用与许可证合规", "允许复用、共同上游和比赛基线独立核查，不混入同源结论。"),
        sec_compliance,
        *excluded_sections,
        _chapter_heading("08", "AI 生成代码检测附录", "独立辅助信号，不改变同源代码和候选创新判断。"),
        sec_ai,
        _chapter_heading("09", "暂未检出及完整技术证据附录", "大清单默认折叠；保留召回契约、覆盖范围、模型版本和统计口径。"),
        sec_orig,
        sec_technical,
    ]

    main_html = "\n".join(part for part in body_parts if part)
    title = f"{html.escape(query_repo_id)} 查重对比分析报告"

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
    <header class="report-header">
      <span class="report-kicker">CODE SIMILARITY · SAME-LANGUAGE ONLY</span>
      <h1>{title}</h1>
      <p>同语言代码相似性、模型复核、参考实现差异与排除口径的一体化审阅视图</p>
    </header>
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
    top_per_module: int = 20,
    skip_opencode: bool = False,
    filematch_path: str | Path | None = None,
    ai_detect_path: str | Path | None = None,
    functions_db_path: str | Path | None = None,
    require_complete_recall: bool = True,
) -> dict:
    """主入口：suspects.json → LLM 语义分析 → 直接 HTML 报告。

    Args:
        suspects_path:   exact 阶段产出的 *_suspects.json 路径
        query_repo_path: 新作品本地克隆路径（用于文件链接 + 语义分析）
        recall_path:     embed 阶段产出的 *_recall.json（用于计算函数总数 / 原创函数）
        output_dir:      HTML 输出目录
        top_per_module:  每个子模块送入 LLM 语义分析的最大代码对数（默认 20；
                         模块借鉴对 <20 时即全部做语义分析，仅超量时截断以控 token）
        skip_opencode:   True 时跳过 LLM，仅用规则生成报告（调试用）
        filematch_path:  fastpath（L0 文件指纹层）产出的 *_filematch.json（整文件复制清单）
        functions_db_path: 历史函数库路径，用于读取参考 repo 的函数源码并生成创新实现地图
    """
    suspects_path = Path(suspects_path)
    data          = json.loads(suspects_path.read_text(encoding="utf-8"))
    suspects      = data.get("suspects", [])
    query_repo_id = data.get("query_repo_id") or suspects_path.stem.split("_suspects")[0]
    reuse_n       = tag_library_reuse(suspects)  # 标注 vendored 库复用，供各图/清单剔除
    fp_counts     = tag_false_positives(suspects)  # 标注跨架构/跨语言/样板汇编误报（降级，不丢弃）
    ub_counts     = tag_upstream_baselines(suspects)  # 标注上游基线 vendored / ABI 受限代码（降级）

    recall: dict | None = None
    if recall_path and Path(recall_path).exists():
        recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))
    if require_complete_recall:
        if recall is None:
            raise RuntimeError("生成查重报告必须提供 recall 产物并通过完整性契约校验")
        require_complete_contract(recall.get("retrieval_contract"), artifact="报告召回产物")
        require_complete_contract(data.get("retrieval_contract"), artifact="嫌疑对产物")
        if data.get("retrieval_contract") != recall.get("retrieval_contract"):
            raise RuntimeError("嫌疑对与召回产物的完整性契约不一致，拒绝混用不同批次产物")

    # L0 文件指纹结果（整文件相同）。file_similar 的后聚合放到所有标注 + 复核之后，
    # 以便用「已剔除嫌疑对」的口径计算（vendored 上游 / ABI / 库复用 / 公共样板 不计入
    # 文件整体相似），避免把 arceos/build.rs 等脚手架文件报为整体相似。
    file_matches: list[dict] = []
    if filematch_path and Path(filematch_path).exists():
        file_matches = json.loads(Path(filematch_path).read_text(encoding="utf-8")).get("matched_files", [])

    # 输出 / 工作目录（复核与语义分析共用）
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / f"{query_repo_id}_semantic_work"

    # 先对 review/weak 档做两阶段模型复核：职责不一致直接排除；职责门控通过后才做同源判断。
    # 判「借鉴」升为 confirmed，判“非借鉴”降为 dismissed；只有格式有效的“疑似”保留信号，
    # 格式/调用失败单列状态，不能冒充“模型仍存疑”。
    # 必须在统计 / file_pairs / 未检出清单计算之前。
    review_judgments: list[dict] = []
    if not skip_opencode:
        review_judgments = collect_file_pairs(suspects, keep_tiers=("review", "weak"))
        # 系统化语义复核：**全部** confirmed 对都送 LLM 复核（不只样板候选）——文本相似不等于
        # 借鉴，ABI/规范/标准算法/不同机制实现的误报只能靠语义判断逐对排除，无法靠枚举模式覆盖。
        # 保守口径：confirmed 仅当职责不一致或 LLM 明确判「非借鉴」才降为 dismissed；
        # review/weak：借鉴升 confirmed，疑似保留，只有非借鉴降 dismissed。
        confirmed_groups = collect_file_pairs(suspects, keep_tiers=("confirmed",))
        review_judgments.extend(confirmed_groups)
        run_review_judgment(review_judgments, work_dir)
        up, dn = _apply_review_verdicts(suspects, review_judgments)
        if up or dn:
            logger.info("[review] 复核：判借鉴 {} 对升档，明确非借鉴 {} 对移出相似清单"
                        "（含 confirmed 全量送复核 {} 个）",
                        up, dn, len(confirmed_groups))

    # 内部跨架构硬拷贝复用标注：须在复核升档之后（覆盖升上来的 confirmed），统计之前。
    dup_n = tag_internal_arch_dups(suspects)
    if any(fp_counts.values()) or dup_n:
        logger.info("[compare] 疑似误报降级：跨架构 {} / 跨语言 {} / 样板汇编 {} / 内部跨架构复用 {}（均不计入借鉴，单列「疑似误报」节）",
                    fp_counts["cross_arch"], fp_counts["cross_lang"],
                    fp_counts["boilerplate_asm"], dup_n)
    if any(ub_counts.values()):
        logger.info("[compare] 上游基线/ABI 降级：vendored 上游 {} / ABI 受限 {}（不计入借鉴，单列「上游基线/ABI 受限」节）",
                    ub_counts["upstream_vendored"], ub_counts["abi_constrained"])

    # 文件整体相似：用「已剔除嫌疑对」口径计算（vendored 上游 / ABI / 库复用 / 公共样板 /
    # 误报 不计入），这样 arceos/build.rs、macros.rs、C 库、examples 等脚手架文件不会被
    # 报为整体相似。file_matches（逐字节整文件相同）按路径口径剔除同类脚手架。
    non_excluded = [s for s in suspects if not _is_excluded_pair(s)]
    file_similar = aggregate_file_similarity(non_excluded, recall, query_repo_path=query_repo_path)
    file_matches = [m for m in file_matches
                    if not match_library(m.get("query_file"))
                    and not is_excluded_file_path(m.get("query_file", ""))]
    file_similar = [f for f in file_similar
                    if not match_library(f.get("file_path"))
                    and not is_excluded_file_path(f.get("file_path", ""))]

    logger.info("[compare] 新作品 {}：{} 个嫌疑对（剔除库复用 {} / 上游基线 {} / ABI {} / 误报 {} / 内部复用 {}），整文件相同 {} 个，整体相似 {} 个",
                query_repo_id, len(suspects), reuse_n, ub_counts["upstream_vendored"],
                ub_counts["abi_constrained"], sum(fp_counts.values()), dup_n,
                len(file_matches), len(file_similar))

    # 统计采用复核后的档位与互斥复核状态：有效疑似、失败、未复核分开。
    submodule_stats = compute_submodule_stats(suspects, recall)
    lib_stats       = reused_library_stats(suspects, recall)
    cc_funcs        = common_code_stats(suspects)
    fp_funcs        = false_positive_stats(suspects)
    ub_funcs        = upstream_baseline_stats(suspects)
    base_funcs      = baseline_stats(suspects)
    # 表格展示：confirmed 全量；送 LLM 做语义分析：每模块取 sim 最高的 top_per_module 个
    file_pairs = collect_file_pairs(suspects)
    final_review_pairs = collect_file_pairs(suspects, keep_tiers=("review", "weak"))
    # 统计口径把同一文件中的同名函数视为一个评审单元并取最高档；若某个起始行已经有
    # confirmed 证据，就不要让同名的另一起始行再次出现在“模型仍存疑”中。
    final_review_pairs = _exclude_confirmed_review_groups(final_review_pairs, file_pairs)
    # collect_file_pairs 会按最终档位重聚合，补回复核阶段写在 group 上的模型结论和理由，
    # 让有效存疑、复核失败与未复核都能在正文逐条查看，且不混用统计口径。
    verdict_map = {
        (g.get("query_file", ""), g.get("query_func", ""), g.get("query_start", 0)):
        {
            "review_verdict": g.get("review_verdict", "未复核"),
            "review_reason": g.get("review_reason", ""),
            "review_responsibility": g.get("review_responsibility", "未判定"),
            "review_responsibility_reason": g.get("review_responsibility_reason", ""),
            "review_evidence_anchors": g.get("review_evidence_anchors", []),
        }
        for g in review_judgments
    }
    for g in final_review_pairs:
        result = verdict_map.get(
            (g.get("query_file", ""), g.get("query_func", ""), g.get("query_start", 0)),
            {
                "review_verdict": "未复核",
                "review_reason": "本次未获得有效模型结论",
                "review_responsibility": "未判定",
                "review_responsibility_reason": "",
                "review_evidence_anchors": [],
            },
        )
        g.update(result)
    llm_pairs  = _limit_per_module(file_pairs, top_per_module)

    # AI 生成代码检测结果（独立链路产物，可选并入报告）
    ai_detect_data = None
    if ai_detect_path and Path(ai_detect_path).exists():
        try:
            ai_detect_data = json.loads(Path(ai_detect_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[compare] 读取 ai_detect 结果失败：{}", e)

    # 暂未命中函数（含复核判「非借鉴」而降级的函数）
    original_funcs  = _original_functions(recall, suspects) if recall else []

    # 独立创新实现地图：从暂未命中函数出发，绑定各模块主要参考 repo 的最近实现后再做
    # 代码级归纳。文档不进入该输入，“未命中”也不会直接升级为创新结论。
    innovation_candidates = build_innovation_candidates(
        recall, suspects, functions_db_path=functions_db_path)
    innovation_points = run_innovation_analysis(
        query_repo_id, innovation_candidates, work_dir, skip_llm=skip_opencode)

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
    ref_repos |= {
        r.get("repo", "") for point in innovation_points
        for r in point.get("references", []) if r.get("repo")
    }
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
        innovation_points = innovation_points,
        review_pairs    = final_review_pairs,
        ai_detect_data  = ai_detect_data,
        query_repo_path = Path(query_repo_path).resolve() if query_repo_path else None,
        linker          = linker,
        file_matches    = file_matches,
        file_similar    = file_similar,
        lib_stats       = lib_stats,
        cc_funcs        = cc_funcs,
        base_funcs      = base_funcs,
        fp_funcs        = fp_funcs,
        ub_funcs        = ub_funcs,
        retrieval_contract = recall.get("retrieval_contract") if recall else None,
        analysis_mode   = ("规则分析（显式跳过模型）" if skip_opencode
                           else "DeepSeek 模型复核与语义分析（失败时规则兜底）"),
    )

    # 档位标签统一（高置信同源代码 / 模型复核后仍存疑 / 暂未检出相似），避免各处叫法不一
    from .label_normalize import normalize_labels
    html_text = normalize_labels(html_text)

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
        "innovation_points": len(innovation_points),
        "file_matches":     len(file_matches),
        "file_similar":     len(file_similar),
        "library_reuse_pairs": reuse_n,
        "reused_libraries":    lib_stats,
        "common_code_funcs":   len(cc_funcs),
        "false_positive_funcs": len(fp_funcs),
        "false_positive_counts": {**fp_counts, "internal_dup": dup_n},
        "upstream_baseline_funcs": len(ub_funcs),
        "upstream_baseline_counts": ub_counts,
    }
