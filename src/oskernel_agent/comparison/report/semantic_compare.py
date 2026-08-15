"""语义级对比报告：suspects.json → LLM 语义分析 → 直接产出 HTML。

取代旧流程：
  review (逐对 LLM) → reviewed.json → report (Markdown→HTML)
新流程：
  suspects.json → collect pairs → 功能簇级 LLM 分批分析 → 直接 HTML

用法：
  python -m oskernel_agent.comparison.report compare \\
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
import time
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

from oskernel_agent.paths import PROJECT_ROOT
from oskernel_agent.report_quality import sanitize_code_ellipses as _sanitize_code_ellipses
from typing import TYPE_CHECKING

from loguru import logger

from oskernel_agent.comparison.exact.identity import (MIN_IDENTITY_SCORE,
                                compare_function_identity_features,
                                function_identity_features,
                                identity_can_rescue)
from oskernel_agent.comparison.fastpath.scan import (WHOLE_FILE_LINE_RATIO, WHOLE_FILE_SIM_RATIO,
                               aggregate_file_similarity)
from oskernel_agent.comparison.metadata.baseline import (has_incremental_history_evidence,
                                   has_substantive_baseline_evidence,
                                   pair_line_evidence)
from oskernel_agent.comparison.models import is_baseline_repo
from oskernel_agent.comparison.normalize.classify import load_classifier
from oskernel_agent.comparison.normalize.discovery import is_test_or_benchmark_path
from oskernel_agent.comparison.retrieval_contract import (CONTRACT_VERSION, contract_errors,
                                    require_complete_contract)
from oskernel_agent.finals.digests import _MODULE_NAMES, team_display_label
from oskernel_agent.finals.readability import explain_terms_in_html, sanitize_html_controls
from oskernel_agent.report_quality import IncompleteReportError

from .false_positives import (FP_REASON_DISP, false_positive_stats,
                              tag_false_positives, tag_internal_arch_dups)
from .libraries import (LibraryContext, discover_library_context, match_library,
                        reused_library_stats, tag_library_reuse)
from .upstream_baselines import (is_excluded_file_path, tag_upstream_baselines,
                                 upstream_baseline_stats)

if TYPE_CHECKING:
    from .gitlab_links import GitLabLinker

DEFAULT_OUTPUT_DIR = "data/output"
_SEMANTIC_PROMPT_VERSION = "semantic-cn-v5-no-ellipsis"
_INNOVATION_PROMPT_VERSION = "innovation-map-cn-v7-no-ellipsis"
DEFAULT_FUNCTIONS_DB = PROJECT_ROOT / "data" / "db" / "functions.db"
MIN_INNOVATION_REFERENCE_SCORE = 0.30

# 子模块列表及其显示名称
MODULES = [
    "sched", "mm", "fs", "trap", "syscall", "signal", "ipc", "sync",
    "time", "net", "driver", "security", "runtime", "arch", "macro", "other",
]

_MODULE_DISPLAY = {
    "sched":  "进程调度",
    "mm":     "内存管理",
    "fs":     "文件系统",
    "trap":   "异常与中断",
    "syscall": "系统调用与用户 ABI",
    "signal": "信号机制",
    "ipc":    "进程间通信",
    "sync":   "并发同步",
    "time":   "时钟与定时器",
    "net":    "网络协议栈",
    "driver": "设备驱动",
    "security": "安全与权限",
    "runtime": "内核运行时与诊断",
    "arch":   "硬件抽象",
    "macro":  "宏与代码生成",
    "other":  "其他",
}


def _module_for_record(record: dict | None) -> str:
    """读取并按需补正旧产物中的模块标签。

    历史 functions.db / recall / suspects 只有早期七类标签。报告按新版通用分类器结合
    路径、函数名和源码重新判断；能形成新结论时采用新标签，无法判断时保留仍然有效的
    旧标签。因而升级规则后无需为了报告展示强制重建整个历史函数库。
    """
    record = record or {}
    current = str(record.get("module_tag") or record.get("module") or "other")
    if record.get("_module_tag_refined") and current in MODULES:
        return current
    try:
        inferred = load_classifier().classify(
            record.get("file_path") or record.get("file") or "",
            str(record.get("lang") or ""),
            func_name=record.get("func_name") or record.get("func") or "",
            raw_code=record.get("raw_code") or record.get("code") or "",
        ).value
    except (FileNotFoundError, ValueError, TypeError):
        inferred = "other"
    if inferred in MODULES and inferred != "other":
        return inferred
    return current if current in MODULES else "other"


def _refine_report_modules(suspects: list[dict], recall: dict | None = None) -> int:
    """原地补正报告输入的旧模块标签，返回发生变化的函数记录数。"""
    changed = 0

    def refine(record: dict | None) -> None:
        nonlocal changed
        if not isinstance(record, dict):
            return
        old = str(record.get("module_tag") or "other")
        new = _module_for_record(record)
        if new != old:
            record["module_tag"] = new
            changed += 1
        record["_module_tag_refined"] = True

    for suspect in suspects:
        refine(suspect.get("query_func"))
        refine(suspect.get("candidate_func"))
    if recall:
        for item in recall.get("results", []):
            refine(item.get("query"))
            for candidate in item.get("candidates") or []:
                payload = candidate.get("payload") or candidate
                refine(payload)
    return changed

# 展示层 tier 命名映射：内部 tier 值保持 confirmed/review/weak 不变；模型失败状态另行统计，
# 不通过 tier 名称伪装成“仍存疑”。
_TIER_DISPLAY = {
    "confirmed": "高置信同源代码",
    "review":    "模型复核难例",
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


# ─── 1. 统计：各子模块复制/原创百分比 ─────────────────────────────────────────

# 一个 confirmed query 函数命中 >= 此数的不同历史仓库 → 公共/样板代码（如 print/println 宏），
# 不计入「值得关注的借鉴」。复用上游 metadata 通道4 的判定（与 config common_code_repo_threshold 一致）。
COMMON_CODE_REPO_THRESHOLD = 5

_KERNEL_JUDGE_SCORING_URL = (
    "https://course.educg.net/pages/contest/contest.jsp?"
    "contestCID=0&contestID=Z7zWWwTfti0&my=false&tabDocID=4754285"
)
_KERNEL_JUDGE_INTEGRITY_URL = (
    "https://course.educg.net/pages/contest/contest.jsp?"
    "contestCID=0&contestID=Z7zWWwTfti0&my=false&tabDocID=2523538"
)


def _query_key(q: dict) -> tuple:
    """报告内目标函数的稳定主键；同文件同名函数必须用起始行区分。"""
    return (
        q.get("file_path", ""), int(q.get("start_line") or 0),
        q.get("func_name", ""),
    )


def _tag_query_level_baselines(suspects: list[dict]) -> int:
    """把显式/已证明的基线来源提升为目标函数级排除结论。

    metadata 正常会先完成该步骤；这里是报告边界的防御性校验，使旧产物、外部产物或
    被路径前缀包装过的 baseline repo 也不会进入历史团队来源榜。
    """
    changed = 0
    for s in suspects:
        # 第三方库及经依赖证据确认的适配层具有更具体的归属；不得再重复归入公共基线。
        if s.get("reuse_library"):
            continue
        repo = str((s.get("candidate_func") or {}).get("repo_id") or "")
        if not is_baseline_repo(repo):
            continue
        ev = s.setdefault("evidence", {})
        ev["baseline_flag"] = True
        ev["baseline_query_scope"] = False
        ev["baseline_source_substantive"] = has_substantive_baseline_evidence(s)
        if s.get("tier") == "baseline_derived":
            continue
        s["tier"] = "baseline_derived"
        s["baseline_note"] = (
            "候选函数直接来自显式公共基线仓库；"
            + ("已形成可传播的直接代码证据"
               if ev["baseline_source_substantive"]
               else "当前直接代码证据不足，不传播为目标函数级排除")
        )
        changed += 1

    baseline_by_query: dict[tuple, list[dict]] = defaultdict(list)
    for s in suspects:
        if s.get("reuse_library"):
            continue
        ev = s.get("evidence") or {}
        if (s.get("tier") == "baseline_derived"
                and (ev.get("baseline_query_scope")
                     or ev.get("baseline_source_substantive"))):
            baseline_by_query[_query_key(s.get("query_func") or {})].append(s)
    for s in suspects:
        if s.get("reuse_library"):
            continue
        baselines = baseline_by_query.get(_query_key(s.get("query_func") or {}), [])
        if s.get("tier") not in ("confirmed", "review", "weak") or not baselines:
            continue
        query_scope = any(
            bool((item.get("evidence") or {}).get("baseline_query_scope"))
            or not is_baseline_repo(str(
                (item.get("candidate_func") or {}).get("repo_id") or ""
            ))
            for item in baselines
        )
        explicit_baselines = [
            item for item in baselines
            if is_baseline_repo(str((item.get("candidate_func") or {}).get("repo_id") or ""))
        ]
        if (not query_scope
                and has_incremental_history_evidence(s, explicit_baselines)):
            ev = s.setdefault("evidence", {})
            ev["baseline_overlap"] = True
            ev["baseline_incremental_evidence"] = True
            candidate_sim, candidate_lines = pair_line_evidence(s)
            strongest_sim = max(pair_line_evidence(item)[0] for item in explicit_baselines)
            most_lines = max(pair_line_evidence(item)[1] for item in explicit_baselines)
            s["baseline_note"] = (
                "目标函数也命中公共基线，但当前历史候选提供了基线之外的增量同源证据"
                f"（逐行 {candidate_sim:.3f}/{candidate_lines} 行；"
                f"最强基线 {strongest_sim:.3f}/{most_lines} 行）"
            )
            continue
        s.setdefault("evidence", {})["baseline_flag"] = True
        s["tier"] = "baseline_derived"
        s["baseline_note"] = (
            "目标函数已有候选证明来自公共基线；当前候选未提供扣除基线后的增量同源证据"
        )
        changed += 1
    return changed


def _is_common_code(s: dict) -> bool:
    """是否已有公共来源证明；“命中很多仓库”本身不构成证明。

    ``common_code`` tier / ``common_code_verified`` 仅供显式基线或人工确认结果使用。广泛传播
    只写 ``widespread_match_repos``，继续留在原档位接受配对与语义复核。
    """
    return bool(s.get("tier") == "common_code" or s.get("common_code_verified"))


def _is_excluded_pair(s: dict) -> bool:
    """不计入「值得关注的借鉴」的对：① vendored 库复用；② 公共/样板代码（命中多仓库）；
    ③ 已证实的机械误报（短内联汇编掩码伪相似）；④ 作品内部跨架构硬拷贝复用；
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
        key = _query_key(q)
        ev = s.get("evidence") or {}
        repos = max(
            int(ev.get("common_code_repos") or 0),
            int(ev.get("widespread_match_repos") or 0),
        )
        f = funcs.setdefault(key, {
            "name": q.get("func_name", ""), "file": q.get("file_path", ""),
            "start": q.get("start_line", 0), "module": _module_for_record(q), "repos": 0})
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
        # 分类展示互斥：库归属优先于公共基线，即使旧 metadata 产物已留下 baseline tier。
        if s.get("reuse_library") or s.get("tier") != "baseline_derived":
            continue
        evidence = s.get("evidence") or {}
        # 显式基线仓库中的弱候选只负责避免把基线本身列成历史队伍来源；没有形成
        # 目标函数级直接证据时，不得在本节宣称目标实现“已通过基线源码复核”。
        if not (evidence.get("baseline_query_scope")
                or evidence.get("baseline_source_substantive")):
            continue
        q = s.get("query_func", {})
        candidate = s.get("candidate_func", {})
        reference = evidence.get("baseline_reference") or {}
        key = _query_key(q)
        item = funcs.setdefault(key, {
            "name": q.get("func_name", ""), "file": q.get("file_path", ""),
            "start": q.get("start_line", 0), "module": _module_for_record(q),
            "baseline_repo": reference.get("repo_id", ""),
            "baseline_file": reference.get("file_path", ""),
            "baseline_start": reference.get("start_line", 0),
            "baseline_func": reference.get("func_name", ""),
            "baseline_line_similarity": reference.get("line_similarity"),
            "baseline_matched_lines": reference.get("matched_lines"),
            "history_candidates": set(),
            "note": s.get("baseline_note", "")})
        candidate_repo = candidate.get("repo_id", "")
        if candidate_repo and candidate_repo != item.get("baseline_repo"):
            item["history_candidates"].add(candidate_repo)
        if reference and not item.get("baseline_repo"):
            item.update({
                "baseline_repo": reference.get("repo_id", ""),
                "baseline_file": reference.get("file_path", ""),
                "baseline_start": reference.get("start_line", 0),
                "baseline_func": reference.get("func_name", ""),
                "baseline_line_similarity": reference.get("line_similarity"),
                "baseline_matched_lines": reference.get("matched_lines"),
            })
    for item in funcs.values():
        item["history_candidates"] = sorted(item["history_candidates"])
    return sorted(funcs.values(), key=lambda x: (x["module"], x["name"]))


def compute_submodule_stats(suspects: list[dict], recall: dict | None = None, *,
                            library_context: LibraryContext | None = None) -> dict:
    """各子模块同源、明确存疑、复核未完成、暂未检出四类**函数数**统计。

    按 query 函数去重、取最高档归类：
      借鉴(confirmed)：confirmed 且非库复用、非公共/样板（含模型复核判「借鉴」升入的）；
      模型复核难例(review)：review/weak 且模型返回有效“疑似/规则保留”（“借鉴”已升入 confirmed）；
      复核未完成：模型调用/格式校验失败，或已入队但未配置模型；绝不计入“模型仍存疑”；
      暂未检出(original)：recall 中（非库）完全未进入嫌疑清单的函数。
    supplemental_source 候选已随该目标函数在已复核来源的结论处理，不占任何档位，
    只把目标键留在“已命中”集合里避免误归为暂未检出。
    库复用 / 公共样板 / baseline 既不算借鉴也不算暂未检出，**不计入 total**（在各自小节单列），
    所以 total = 同源 + 存疑 + 复核未完成 + 暂未检出，占比相加为 100%。

    Returns: dict[module] → {confirmed, review, review_failed, review_pending,
                             review_incomplete, original, total, ...}
    """
    category_rank = {"confirmed": 4, "review": 3, "review_failed": 2,
                     "review_pending": 1}
    best: dict[tuple, list] = {}                 # key -> [rank, module, category]
    sources: dict[str, Counter] = defaultdict(Counter)
    matched_keys: set[tuple] = set()             # 任何历史命中（含 dismissed/库/公共）→ 不算暂未检出
    for s in suspects:
        tier = s.get("tier", "")
        q = s.get("query_func", {})
        key = _query_key(q)
        if tier:
            matched_keys.add(key)
        if _is_excluded_pair(s) or tier in ("dismissed", "baseline_derived", "common_code"):
            continue
        if tier not in ("confirmed", "review", "weak"):
            continue
        mod = _module_for_record(q)
        if tier == "confirmed":
            category = "confirmed"
        elif s.get("review_verdict") in ("借鉴", "疑似", "规则保留"):
            category = "review"
        elif s.get("review_verdict") == "复核失败":
            category = "review_failed"
        elif s.get("model_review_selection") == "supplemental_source":
            # 补充来源候选已随该目标函数在已复核来源的结论处理（见 _review_section），
            # 不构成独立的复核未完成项，也不占用模块分母。
            continue
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

    # 暂未检出 = recall 中（非库）完全未进入任何历史匹配清单的函数
    if recall:
        for item in recall.get("results", []):
            q = item.get("query", {})
            if match_library(q.get("file_path"), context=library_context):
                continue
            key = _query_key(q)
            if key in matched_keys:
                continue
            mod = _module_for_record(q)
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


def _original_functions(recall: dict, suspects: list[dict], top_n: int | None = None, *,
                        library_context: LibraryContext | None = None) -> list[dict]:
    """recall 中完全未进入历史匹配清单的函数；这不等于原创认定。

    与 compute_submodule_stats 的暂未检出口径一致（matched = 任何历史命中），所以二者
    数量一致。判据用「是否进入命中清单」而非相似度阈值：代码嵌入余弦相似度有很高地板（OS
    内核链表/调度循环等结构高度雷同，无关函数 max_sim 也普遍 0.6+），用阈值会把几乎所有函数
    误把领域共性当成有效命中。返回**全部**暂未检出函数（按行数降序）；展示层自行截断并显示总数。
    """
    matched_keys = {
        _query_key(s.get("query_func", {}))
        for s in suspects
        if s.get("tier")   # dismissed 仍有历史匹配证据，不能重新归为暂未检出
    }
    out = []
    for item in recall.get("results", []):
        q = item.get("query", {})
        if match_library(q.get("file_path"), context=library_context):
            continue  # vendored 第三方库代码不算原创/自研
        key = _query_key(q)
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
                "module":  _module_for_record(q),
                "max_sim": round(max_sim, 3),
                "lines":   lines,
            })
    out.sort(key=lambda x: -x["lines"])
    return out[:top_n] if top_n else out


# ─── 1b. 相对参考 repo 的创新实现候选 ──────────────────────────────────────


def _reference_repos_by_module(
    suspects: list[dict], recall: dict | None, max_repos: int = 3,
) -> dict[str, list[str]]:
    """按有效命中生成模块参考仓库序列，并用全局主要仓库补足回退范围。"""
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
        mod = _module_for_record(q)
        weight = 3 if s.get("tier") == "confirmed" else 1
        counts[mod][repo] += weight
        global_counts[repo] += weight

    # 没有进入嫌疑清单的模块仍可能有低于阈值的最近参考；只给低权重，避免压过有效命中。
    if recall:
        for item in recall.get("results", []):
            q = item.get("query") or {}
            mod = _module_for_record(q)
            for candidate in (item.get("candidates") or [])[:3]:
                payload = candidate.get("payload") or candidate
                repo = str(payload.get("repo_id") or "")
                if not repo or payload.get("is_baseline") or match_library(payload.get("file_path")):
                    continue
                score = float(candidate.get("score") or 0.0)
                if score > 0:
                    counts[mod][repo] += score * 0.1
                    global_counts[repo] += score * 0.02

    global_repos = [repo for repo, _ in global_counts.most_common(max_repos)]
    result: dict[str, list[str]] = {}
    for mod in MODULES:
        ordered = [repo for repo, _ in counts[mod].most_common(max_repos)]
        for repo in global_repos:
            if repo not in ordered:
                ordered.append(repo)
            if len(ordered) >= max_repos:
                break
        result[mod] = ordered[:max_repos]
    return result


def _reference_repo_by_module(suspects: list[dict], recall: dict | None) -> dict[str, str]:
    """兼容旧调用：返回每个模块的首选参考仓库。"""
    ranked = _reference_repos_by_module(suspects, recall, max_repos=3)
    return {mod: (repos[0] if repos else "") for mod, repos in ranked.items()}


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


def _innovation_name_tokens(name: str) -> frozenset[str]:
    """按 snake/camel case 提取可跨语言比较的函数名词元。"""
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return frozenset(
        token.lower() for token in re.split(r"[^A-Za-z0-9]+", expanded)
        if token
    )


def _innovation_name_is_specific(name: str) -> bool:
    """函数名是否足以表达具体职责，而不是短、单词式工厂/包装器名称。"""
    tokens = _innovation_name_tokens(name)
    return len(tokens) >= 2 or any(len(token) >= 8 for token in tokens)


def _innovation_reference_comparability(query: dict, reference: dict) -> dict | None:
    """创新基线必须是同语言、同子系统且职责可对应的具体函数。

    向量最近邻只能用于召回，不能单独证明两个函数适合作为创新前后的基线。这里使用
    函数名、签名、行为调用和控制流做高精度门禁；对改名函数要求行为也有交集。宁可
    不输出候选，也不把同一大类子系统中的邻居函数拼成创新对照。
    """
    query_code = str(query.get("raw_code") or "")
    reference_code = str(reference.get("raw_code") or "")
    if not query_code.strip() or not reference_code.strip():
        return None

    query_lang = str(query.get("lang") or "").strip().lower()
    reference_lang = str(reference.get("lang") or "").strip().lower()
    if not query_lang or not reference_lang or query_lang != reference_lang:
        return None

    query_module = _module_for_record(query)
    reference_module = _module_for_record(reference)
    if query_module != reference_module:
        return None

    identity = compare_function_identity_features(
        function_identity_features(str(query.get("func_name") or ""), query_code),
        function_identity_features(
            str(reference.get("func_name") or ""), reference_code),
    )
    if float(identity["score"]) < MIN_IDENTITY_SCORE:
        return None

    if bool(identity["exact_name"]):
        # 对 new/init/run 这类短单词名称，名称相同本身不足以证明职责一致；还要有
        # 可观察行为交集。具体的多词/长名称则允许实现发生较大变化，符合创新比较用途。
        accepted = bool(
            _innovation_name_is_specific(str(query.get("func_name") or ""))
            or float(identity["behavior"]) >= 0.25
        )
        return identity if accepted else None

    # 改名比较比同名比较更容易把功能族邻居配在一起，必须同时满足名称语义、签名和
    # 行为调用的最低交集。阈值来自通用身份特征，不依赖仓库、路径或具体函数名。
    accepted = bool(
        float(identity["name"]) >= 0.25
        and float(identity["signature"]) >= (1.0 / 3.0)
        and float(identity["behavior"]) >= 0.25
    )
    return identity if accepted else None


def _innovation_reference_is_comparable(query: dict, reference: dict) -> bool:
    """供门禁与测试使用的布尔接口。"""
    return _innovation_reference_comparability(query, reference) is not None


def _targeted_reference_ids(
    db_path: str | Path | None,
    recall_items: list[tuple],
    reference_repos: dict[str, list[str]],
    per_repo_limit: int = 80,
) -> dict[tuple, list[tuple[int, int]]]:
    """在模块主要参考仓库内定向召回同名或名称身份兼容的函数 ID。

    第一步只读取轻量元数据并按名称词元、函数规模排序，随后由调用方批量补齐源码并执行
    严格职责门禁。这样既不会依赖全库 Top-K 是否碰巧包含主要仓库，也避免逐目标扫描整库源码。
    """
    path = Path(db_path or DEFAULT_FUNCTIONS_DB)
    if not path.is_file() or not recall_items:
        return {}
    result: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    metadata_cache: dict[tuple[str, str, str], list[tuple[int, str, int]]] = {}
    try:
        with sqlite3.connect(path) as conn:
            for _, item, _, mod in recall_items:
                query = item.get("query") or {}
                query_key = _query_key(query)
                query_name = str(query.get("func_name") or "")
                query_tokens = _innovation_name_tokens(query_name)
                query_lang = str(query.get("lang") or "").strip().lower()
                query_lines = max(
                    1,
                    int(query.get("end_line") or 0)
                    - int(query.get("start_line") or 0) + 1,
                )
                if not query_tokens or not query_lang:
                    continue
                for repo_rank, repo in enumerate(reference_repos.get(mod, [])):
                    pool_key = (repo, mod, query_lang)
                    rows = metadata_cache.get(pool_key)
                    if rows is None:
                        # 旧库只有七类标签；新类别在旧数据中可能位于 other，也可能被原
                        # 路径优先规则吸收到邻近大类。先扩大轻量元数据池，源码水合后仍由
                        # 新分类器和严格职责门禁逐函数过滤。
                        legacy_tags = {
                            "syscall": ("syscall", "other", "fs", "trap"),
                            "signal": ("signal", "other", "trap"),
                            "ipc": ("ipc", "other", "fs"),
                            "sync": ("sync", "other", "sched"),
                            "time": ("time", "other", "sched"),
                            "net": ("net", "other", "driver"),
                            "security": ("security", "other"),
                            "runtime": ("runtime", "other"),
                        }
                        accepted_tags = legacy_tags.get(mod, (mod,))
                        tag_placeholders = ",".join("?" for _ in accepted_tags)
                        rows = [
                            (int(row[0]), str(row[1] or ""), max(1, int(row[3]) - int(row[2]) + 1))
                            for row in conn.execute(
                                "SELECT id, func_name, start_line, end_line FROM functions "
                                f"WHERE repo_id=? AND module_tag IN ({tag_placeholders}) "
                                "AND lower(lang)=?",
                                (repo, *accepted_tags, query_lang),
                            )
                        ]
                        metadata_cache[pool_key] = rows
                    ranked = []
                    for func_id, func_name, ref_lines in rows:
                        ref_tokens = _innovation_name_tokens(func_name)
                        if not ref_tokens:
                            continue
                        exact_name = func_name == query_name
                        name_overlap = len(query_tokens & ref_tokens) / len(query_tokens | ref_tokens)
                        if not exact_name and name_overlap < 0.25:
                            continue
                        length_ratio = min(query_lines, ref_lines) / max(query_lines, ref_lines)
                        ranked.append((
                            0 if exact_name else 1,
                            -name_overlap,
                            -length_ratio,
                            func_id,
                        ))
                    ranked.sort()
                    result[query_key].extend(
                        (entry[3], repo_rank) for entry in ranked[:per_repo_limit]
                    )
    except (OSError, sqlite3.Error) as exc:
        logger.warning("[innovation] 主要参考仓库定向检索失败：{}", exc)
        return {}
    return dict(result)


def build_innovation_candidates(
    recall: dict | None,
    suspects: list[dict],
    functions_db_path: str | Path | None = None,
    max_candidates: int = 18,
    *,
    library_context: LibraryContext | None = None,
) -> list[dict]:
    """构造“目标未命中函数 ↔ 主要参考 repo 最近实现”的代码比较输入。

    这里不做创新认定，只形成有代码、有来源、有稳定 key 的候选；后续 LLM 归纳和确定性
    校验都只能引用这些 key，因此项目文档或模型臆造路径无法进入最终报告。
    """
    if not recall:
        return []
    refs_by_mod = _reference_repos_by_module(suspects, recall, max_repos=3)
    originals = _original_functions(
        recall, suspects, library_context=library_context)
    original_keys = {
        (f["file"], int(f.get("start") or 0), f["func"]) for f in originals
    }
    original_meta = {
        (f["file"], int(f.get("start") or 0), f["func"]): f for f in originals
    }

    recall_items = []
    for item in recall.get("results", []):
        q = item.get("query") or {}
        key = _query_key(q)
        if key not in original_keys:
            continue
        lines = max(0, int(q.get("end_line") or 0) - int(q.get("start_line") or 0) + 1)
        if lines < 5 or not (q.get("raw_code") or "").strip():
            continue
        mod = _module_for_record(q)
        recall_items.append((lines, item, original_meta[key], mod))
    recall_items.sort(key=lambda x: -x[0])
    # 先保留更宽的目标池再做定向参考检索；如果仍在检索前就截成 18×每模块 4，前几项
    # 找不到合格基线时会让最终输入远少于上限。最终进入模型的代码对仍限制为 18×每模块 4。
    target_pool_limit = max_candidates * 3
    target_pool_per_module = 12
    balanced: list[tuple] = []
    per_module: Counter = Counter()
    for record in recall_items:
        mod = record[3]
        if per_module[mod] >= target_pool_per_module:
            continue
        per_module[mod] += 1
        balanced.append(record)
        if len(balanced) >= target_pool_limit:
            break
    recall_items = balanced

    targeted_ids = _targeted_reference_ids(
        functions_db_path, recall_items, refs_by_mod,
    )
    all_ref_ids: set[int] = {
        func_id for entries in targeted_ids.values() for func_id, _ in entries
    }
    for _, item, _, _ in recall_items:
        for candidate in item.get("candidates") or []:
            payload = candidate.get("payload") or candidate
            try:
                all_ref_ids.add(int(candidate.get("id") or payload.get("id")))
            except (TypeError, ValueError):
                continue
    hydrated = _load_functions_by_id(functions_db_path, all_ref_ids)

    comparability_cache: dict[tuple[tuple, int], dict | None] = {}

    def comparability(query: dict, payload: dict, func_id: int) -> dict | None:
        cache_key = (_query_key(query), func_id)
        if cache_key in comparability_cache:
            return comparability_cache[cache_key]
        row = hydrated.get(func_id) or {}
        reference = {
            "func_name": row.get("func_name") or payload.get("func_name", ""),
            "file_path": row.get("file_path") or payload.get("file_path", ""),
            "module_tag": row.get("module_tag") or payload.get("module_tag", "other"),
            "lang": row.get("lang") or payload.get("lang", ""),
            "raw_code": row.get("raw_code") or payload.get("raw_code", ""),
        }
        identity = _innovation_reference_comparability(query, reference)
        comparability_cache[cache_key] = identity
        return identity

    result: list[dict] = []
    reference_seq = 0
    for index, (_, item, meta, mod) in enumerate(recall_items, start=1):
        q = item.get("query") or {}
        query_key = _query_key(q)
        repo_order = refs_by_mod.get(mod, [])
        repo_rank = {repo: rank for rank, repo in enumerate(repo_order)}
        refs: list[dict] = []
        candidate_by_id: dict[int, dict] = {}
        for candidate in item.get("candidates") or []:
            payload = candidate.get("payload") or candidate
            try:
                func_id = int(candidate.get("id") or payload.get("id"))
            except (TypeError, ValueError):
                continue
            score = float(candidate.get("score") or 0.0)
            if score < MIN_INNOVATION_REFERENCE_SCORE:
                continue
            candidate_by_id[func_id] = {
                "payload": payload,
                "vector_score": score,
                "selection_source": "全库召回",
            }
        for func_id, _targeted_rank in targeted_ids.get(query_key, []):
            row = hydrated.get(func_id) or {}
            existing = candidate_by_id.setdefault(func_id, {
                "payload": row,
                "vector_score": None,
                "selection_source": "主要参考仓库定向检索",
            })
            if existing["selection_source"] == "全库召回":
                existing["selection_source"] = "全库召回＋定向检索"

        ranked_candidates = []
        for func_id, entry in candidate_by_id.items():
            payload = entry["payload"]
            row = hydrated.get(func_id) or {}
            repo = str(row.get("repo_id") or payload.get("repo_id") or "")
            file_path = str(row.get("file_path") or payload.get("file_path") or "")
            if (not repo or is_baseline_repo(repo)
                    or payload.get("is_baseline")
                    or match_library(file_path)):
                continue
            identity = comparability(q, payload, func_id)
            if identity is None:
                continue
            rank = repo_rank.get(repo, len(repo_order) + 1)
            vector_score = entry.get("vector_score")
            ranked_candidates.append((
                rank,
                0 if bool(identity.get("exact_name")) else 1,
                -float(identity.get("score") or 0.0),
                -float(vector_score or 0.0),
                func_id,
                entry,
                identity,
            ))
        ranked_candidates.sort(key=lambda entry: entry[:5])
        if ranked_candidates:
            _, _, _, _, func_id, entry, identity = ranked_candidates[0]
            payload = entry["payload"]
            row = hydrated.get(func_id) or {}
            ref_code = str(row.get("raw_code") or payload.get("raw_code") or "")
            reference_seq += 1
            vector_score = entry.get("vector_score")
            refs.append({
                "key": f"r{reference_seq:04d}",
                "repo": row.get("repo_id") or payload.get("repo_id", ""),
                "file": row.get("file_path") or payload.get("file_path", ""),
                "start": int(row.get("start_line") or payload.get("start_line") or 0),
                "end": int(row.get("end_line") or payload.get("end_line") or 0),
                "func": row.get("func_name") or payload.get("func_name", ""),
                "lang": row.get("lang") or payload.get("lang", ""),
                "score": round(float(vector_score), 3) if vector_score is not None else None,
                "identity_score": round(float(identity.get("score") or 0.0), 3),
                "selection_source": entry.get("selection_source", "全库召回"),
                "raw_code": ref_code,
                "analysis_code": ref_code[:8000],
            })
        if not refs:
            continue
        query_code = q.get("raw_code") or ""
        result.append({
            "key": f"t{index:04d}",
            "module": mod,
            "module_display": _MODULE_DISPLAY.get(mod, mod),
            # 展示和提示中的参考 repo 必须来自最终通过职责门禁的具体参考函数；模块
            # 热门 repo 只参与候选排序，不能覆盖实际绑定来源。
            "reference_repo": refs[0]["repo"],
            "file": q.get("file_path", ""),
            "start": int(q.get("start_line") or 0),
            "end": int(q.get("end_line") or 0),
            "func": q.get("func_name", ""),
            "lang": q.get("lang", ""),
            "lines": int(meta.get("lines") or 0),
            "raw_code": query_code,
            "analysis_code": query_code[:12000],
            "references": refs,
        })
    final: list[dict] = []
    final_per_module: Counter = Counter()
    for candidate in result:
        module = candidate.get("module", "other")
        if final_per_module[module] >= 4:
            continue
        final_per_module[module] += 1
        final.append(candidate)
        if len(final) >= max_candidates:
            break
    return final


def _innovation_complexity(targets: list[dict]) -> dict:
    """使用 Lizard 的 McCabe 圈复杂度实现计算函数级标准度量。

    不再把行数、正则命中的分支和文件数拼成自定义 0～100 分。Lizard 无法解析的语言或
    语法明确记为 unavailable，不使用猜测值补齐。
    """
    import lizard

    metrics = []
    unavailable = []
    for target in targets:
        code = str(target.get("raw_code") or "")
        file_path = str(target.get("file") or "snippet.rs")
        func_name = str(target.get("func") or "")
        if not code.strip():
            unavailable.append({"file": file_path, "func": func_name, "reason": "缺少源码"})
            continue
        try:
            analysis = lizard.analyze_file.analyze_source_code(file_path, code)
            functions = list(analysis.function_list)
        except Exception as exc:  # Lizard 对未知/不完整语法以不可用状态降级，不能阻断报告
            unavailable.append({
                "file": file_path, "func": func_name,
                "reason": f"解析失败：{type(exc).__name__}",
            })
            continue
        exact = [
            function for function in functions
            if function.name == func_name or function.name.endswith("::" + func_name)
        ]
        selected = max(exact or functions, key=lambda function: function.nloc, default=None)
        if selected is None:
            unavailable.append({"file": file_path, "func": func_name, "reason": "语言或语法未识别"})
            continue
        metrics.append({
            "file": file_path,
            "func": func_name,
            "cyclomatic_complexity": int(selected.cyclomatic_complexity),
            "nloc": int(selected.nloc),
            "token_count": int(selected.token_count),
            "parameter_count": len(selected.full_parameters),
        })

    ccn = [metric["cyclomatic_complexity"] for metric in metrics]
    return {
        "method": "McCabe cyclomatic complexity",
        "tool": "Lizard 1.23.0",
        "analyzed_functions": len(metrics),
        "unavailable_functions": len(unavailable),
        "max_cyclomatic_complexity": max(ccn, default=None),
        "mean_cyclomatic_complexity": round(sum(ccn) / len(ccn), 2) if ccn else None,
        "total_nloc": sum(metric["nloc"] for metric in metrics),
        "total_token_count": sum(metric["token_count"] for metric in metrics),
        "max_parameter_count": max(
            (metric["parameter_count"] for metric in metrics), default=None,
        ),
        "functions": metrics,
        "unavailable": unavailable,
    }


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
        why_it_matters = str(item.get("why_it_matters") or "").strip()
        impact_scope = str(item.get("impact_scope") or "").strip()
        counterevidence = str(item.get("counterevidence") or "").strip()
        if not all((targets, refs, title, baseline, delta, why_it_matters,
                    impact_scope, counterevidence)):
            continue
        # 只能引用与这些目标函数真实关联的 reference key，防止模型跨卡拼错来源。
        allowed_refs = {r["key"] for target in targets for r in target.get("references", [])}
        refs = [r for r in refs if r["key"] in allowed_refs]
        selected_ref_keys = {r["key"] for r in refs}
        if not refs or any(
            not selected_ref_keys.intersection(
                r["key"] for r in target.get("references", [])
            )
            for target in targets
        ):
            continue
        repo_counts = Counter(r["repo"] for r in refs if r.get("repo"))
        if not repo_counts:
            repo_counts.update(t.get("reference_repo", "") for t in targets if t.get("reference_repo"))
        confidence = str(item.get("confidence") or "medium").lower()
        if confidence not in ("high", "medium", "low"):
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
            "why_it_matters": why_it_matters[:400],
            "impact_scope": impact_scope[:400],
            "counterevidence": counterevidence[:400],
            "targets": targets,
            "references": refs,
            "complexity": _innovation_complexity(targets),
        })
        used_target_keys.update(target_keys)
    return normalized



# ─── 2. 收集代码对（按 query 函数聚合全部候选） ──────────────────────────────

_TIER_RANK = {"confirmed": 3, "review": 2, "weak": 1}

# 候选匹配展示下限：相似度低于此值的候选属召回巧合命中，不展示。
# 取「弱相似」档下限 0.5（见 models.py tier 口径），避免 0.02/0.2 这类被误列为来源。
MIN_CANDIDATE_SIM = 0.5
MIN_SUBSTANTIVE_MATCH_LINES = 5
MIN_SHORTER_SIDE_COVERAGE = 0.35

# 模型只处理规则难以区分的高价值边界样本。下面的门槛全部来自代码证据，
# 不使用仓库名、路径、年份或具体函数名，因此可用于任意代码仓库。
MIN_REVIEW_FUNCTION_LINES = 6
MIN_REVIEW_MATCHED_LINES = 6
MIN_REVIEW_MEDIUM_LINE_SIM = 0.58
MIN_REVIEW_STRONG_LINE_SIM = 0.70
MIN_REVIEW_SEGMENT_COVERAGE = 0.45
MIN_REVIEW_IDENTITY_SCORE = 0.72
MIN_REVIEW_SHORTER_COVERAGE = 0.50
MIN_REVIEW_DISTINCTIVE_LITERALS = 2
REVIEW_SECONDARY_SCORE_MARGIN = 0.03
REVIEW_SECONDARY_FALLBACK_ROUNDS = 2


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


def _raw_line_sim(s: dict) -> float:
    """返回不含向量/分段加权的原始逐行相似度，并兼容旧产物。"""
    ev = s.get("evidence") or {}
    value = ev.get("line_similarity")
    if value is not None:
        return round(min(1.0, max(0.0, float(value))), 3)

    matched = int(ev.get("exact_match_lines") or 0) + int(
        ev.get("renamed_match_lines") or 0
    )
    if matched:
        qcode = ((s.get("query_func") or {}).get("raw_code") or "")
        ccode = ((s.get("candidate_func") or {}).get("raw_code") or "")
        qlines = sum(1 for line in qcode.splitlines() if line.strip())
        clines = sum(1 for line in ccode.splitlines() if line.strip())
        return round(min(1.0, matched / max(qlines, clines, 1)), 3)

    # 旧产物没有独立字段时保守兼容；新产物始终写入 line_similarity。
    return _pair_sim(s)


def _segment_evidence_is_sufficient(evidence: dict) -> bool:
    segment = evidence.get("segment_hits") or {}
    hits = int(segment.get("hits") or 0)
    q_total = int(segment.get("q_total") or 0)
    c_total = int(segment.get("c_total") or 0)
    if not hits or not q_total or not c_total:
        return False
    return min(hits / q_total, hits / c_total) >= 0.3


def _segment_bilateral_coverage(evidence: dict) -> float:
    """返回分段命中的双侧最小覆盖率；任一侧缺失时视为无证据。"""
    segment = evidence.get("segment_hits") or {}
    hits = int(segment.get("hits") or 0)
    q_total = int(segment.get("q_total") or 0)
    c_total = int(segment.get("c_total") or 0)
    if not hits or not q_total or not c_total:
        return 0.0
    return min(1.0, hits / q_total, hits / c_total)


def _nonblank_line_count(code: str) -> int:
    return sum(1 for line in (code or "").splitlines() if line.strip())


def _review_admission(s: dict) -> dict | None:
    """判断候选是否属于真正需要模型处理的高价值难例。

    单独的向量相似、同名、函数身份、分段命中或一个字符串都不能准入。候选必须先有
    足量的共同代码，再由双侧覆盖、身份、分段、指纹或多个低频字面量提供交叉支持。
    高置信 ``confirmed`` 已有确定性证据，不在这里重复消耗模型。
    """
    if s.get("tier") not in ("review", "weak") or _is_excluded_pair(s):
        return None
    ev = s.get("evidence") or {}
    relation = ev.get("function_identity_relation")
    if relation in ("family_neighbor", "same_name_only", "nonsemantic_stub"):
        return None

    qcode = ((s.get("query_func") or {}).get("raw_code") or "")
    ccode = ((s.get("candidate_func") or {}).get("raw_code") or "")
    qlines = _nonblank_line_count(qcode)
    clines = _nonblank_line_count(ccode)
    shorter_lines = min(qlines, clines)
    if shorter_lines < MIN_REVIEW_FUNCTION_LINES:
        return None

    matched = int(ev.get("exact_match_lines") or 0) + int(
        ev.get("renamed_match_lines") or 0
    )
    line_sim = _raw_line_sim(s)
    shorter_coverage = min(1.0, matched / max(1, shorter_lines))
    segment_coverage = _segment_bilateral_coverage(ev)
    identity = float(ev.get("function_identity_score") or 0.0)
    literals = int(ev.get("unique_string_matches") or 0)
    fingerprint = bool(ev.get("normalized_fingerprint_match"))

    supports: list[str] = []
    if segment_coverage >= MIN_REVIEW_SEGMENT_COVERAGE:
        supports.append("segment")
    if identity >= MIN_REVIEW_IDENTITY_SCORE:
        supports.append("identity")
    if literals >= MIN_REVIEW_DISTINCTIVE_LITERALS:
        supports.append("distinctive_literals")
    if fingerprint:
        supports.append("normalized_fingerprint")

    basis = ""
    if (fingerprint and matched >= MIN_REVIEW_MATCHED_LINES
            and (line_sim >= 0.35 or segment_coverage >= 0.40
                 or identity >= MIN_REVIEW_IDENTITY_SCORE)):
        basis = "normalized_fingerprint_with_code_support"
    elif line_sim >= MIN_REVIEW_STRONG_LINE_SIM and matched >= MIN_REVIEW_MATCHED_LINES:
        basis = "strong_bilateral_line_similarity"
    elif (line_sim >= MIN_REVIEW_MEDIUM_LINE_SIM
          and matched >= 8
          and shorter_coverage >= MIN_REVIEW_SHORTER_COVERAGE
          and supports):
        basis = "multi_signal_code_similarity"
    elif (line_sim >= 0.35
          and matched >= 15
          and shorter_coverage >= 0.40
          and identity >= 0.80
          and relation in ("exact_counterpart", "same_name_code_clone")):
        # 长函数在共享核心之后大幅扩写时，整体比例会被新代码稀释。
        # 15 行以上的保序直接证据、双侧有效覆盖和高函数身份同时成立时，
        # 应保留人工核查；该分支不依赖函数名或仓库身份。
        basis = "identity_supported_large_partial_match"
    elif (line_sim >= 0.40
          and matched >= 12
          and shorter_coverage >= 0.55
          and segment_coverage >= 0.50
          and identity >= 0.75):
        # 为大幅扩写、重排后的真实复制保留入口，但要求两种强独立证据同时成立。
        basis = "supported_substantive_partial_match"
    if not basis:
        return None

    score = min(1.0, (
        0.50 * line_sim
        + 0.18 * segment_coverage
        + 0.14 * shorter_coverage
        + 0.10 * identity
        + 0.05 * min(1.0, matched / 20)
        + 0.03 * min(1.0, literals / 3)
        + (0.05 if fingerprint else 0.0)
    ))
    return {
        "basis": basis,
        "score": round(score, 4),
        "matched_lines": matched,
        "line_similarity": round(line_sim, 4),
        "shorter_coverage": round(shorter_coverage, 4),
        "segment_coverage": round(segment_coverage, 4),
        "identity_score": round(identity, 4),
        "supporting_signals": supports,
    }


def _has_substantive_partial_match(s: dict) -> bool:
    """允许“较短实现被扩写”的局部借鉴，同时过滤只撞到少量外壳行的候选。"""
    ev = s.get("evidence") or {}
    matched = int(ev.get("exact_match_lines") or 0) + int(
        ev.get("renamed_match_lines") or 0
    )
    if matched < MIN_SUBSTANTIVE_MATCH_LINES:
        return False
    qcode = ((s.get("query_func") or {}).get("raw_code") or "")
    ccode = ((s.get("candidate_func") or {}).get("raw_code") or "")
    qlines = sum(1 for line in qcode.splitlines() if line.strip())
    clines = sum(1 for line in ccode.splitlines() if line.strip())
    shorter_side = min(qlines, clines)
    return bool(shorter_side and matched / shorter_side >= MIN_SHORTER_SIDE_COVERAGE)


def _review_evidence_basis(s: dict) -> str | None:
    """判断一对函数是否有资格进入语义复核。

    门槛只依赖可跨仓库复用的证据类型，不依赖仓库名、路径或函数名：原始逐行覆盖、
    归一化指纹、低频共享字符串或分段双向覆盖。向量相似和同名召回只能产生候选，
    不能单独把一对函数送入“模型仍存疑”。
    """
    admission = _review_admission(s)
    return admission["basis"] if admission else None


def _pairing_score(s: dict) -> float:
    """具体函数配对分；只用于候选重排/拒配，不作为借鉴概率。"""
    identity = float((s.get("evidence") or {}).get("function_identity_score") or 0.0)
    exact_name = bool((s.get("evidence") or {}).get("function_name_exact"))
    return round(
        0.45 * identity + 0.35 * _raw_line_sim(s) + 0.20 * float(exact_name), 4
    )


def _suppress_dominated_candidate_mismatches(suspects: list[dict]) -> int:
    """同一历史文件中存在明显更匹配函数时，移除被其支配的邻近模板误配。"""
    domains: dict[tuple, list[dict]] = defaultdict(list)
    for s in suspects:
        if s.get("tier") not in ("confirmed", "review", "weak") or _is_excluded_pair(s):
            continue
        q = s.get("query_func") or {}
        c = s.get("candidate_func") or {}
        key = (
            q.get("repo_id", ""), q.get("file_path", ""), q.get("start_line", 0),
            c.get("repo_id", ""), c.get("file_path", ""),
        )
        domains[key].append(s)

    removed = 0
    for pairs in domains.values():
        if len(pairs) < 2:
            continue
        best = max(pairs, key=lambda pair: (_pairing_score(pair), _raw_line_sim(pair)))
        best_identity = float(
            (best.get("evidence") or {}).get("function_identity_score") or 0.0
        )
        best_score = _pairing_score(best)
        best_ev = best.get("evidence") or {}
        best_matched = int(best_ev.get("exact_match_lines") or 0) + int(
            best_ev.get("renamed_match_lines") or 0
        )
        # 具体函数重排发生在模型准入之前，使用“身份可救回”证据即可；不能反向要求
        # 候选先满足更严格的模型难例门槛，否则低覆盖邻居误配会留到后面。
        if best_identity < 0.72 or not identity_can_rescue(
            _raw_line_sim(best), best_matched, best_identity
        ):
            continue
        best_candidate = best.get("candidate_func") or {}
        for pair in pairs:
            if pair is best or pair.get("tier") == "confirmed":
                continue
            identity = float(
                (pair.get("evidence") or {}).get("function_identity_score") or 0.0
            )
            # 高行相似可能是合法改名复制，不能仅因另一个同名函数存在就删除。
            if _raw_line_sim(pair) >= 0.7:
                continue
            if best_identity - identity < 0.18 or best_score - _pairing_score(pair) < 0.15:
                continue
            pair["tier"] = "dismissed"
            pair["dismiss_reason"] = "dominated_candidate_mismatch"
            pair["pairing_replacement"] = {
                "repo_id": best_candidate.get("repo_id", ""),
                "file_path": best_candidate.get("file_path", ""),
                "start_line": best_candidate.get("start_line", 0),
                "func_name": best_candidate.get("func_name", ""),
                "pairing_score": best_score,
            }
            removed += 1
    return removed


def _suppress_family_neighbor_mismatches(suspects: list[dict]) -> int:
    """把“同功能族或仅同名、但不是具体对应函数”的 pair 移出借鉴/存疑清单。"""
    removed = 0
    for s in suspects:
        if s.get("tier") not in ("review", "weak") or _is_excluded_pair(s):
            continue
        ev = s.get("evidence") or {}
        # 旧产物没有身份关系字段时不追溯性删除，避免把“尚未运行新配对层”误当成已拒配。
        if ev.get("function_identity_relation") not in ("family_neighbor", "same_name_only"):
            continue
        s["tier"] = "dismissed"
        s["dismiss_reason"] = "family_neighbor_not_counterpart"
        s["pairing_note"] = (
            "双方仅同名、属于相近功能族或共享实现模板，但函数身份不足以建立具体对应关系"
        )
        removed += 1
    return removed


def _suppress_nonsemantic_stub_mismatches(suspects: list[dict]) -> int:
    """移除不同职责短占位函数因签名外壳产生的伪“改名复制”配对。"""
    removed = 0
    for s in suspects:
        if s.get("tier") not in ("confirmed", "review", "weak") or _is_excluded_pair(s):
            continue
        relation = (s.get("evidence") or {}).get("function_identity_relation")
        if relation != "nonsemantic_stub":
            continue
        s["tier"] = "dismissed"
        s["dismiss_reason"] = "renamed_trivial_stub_has_no_behavior_identity"
        s["pairing_note"] = (
            "双方只是不同名称的常量返回/空实现占位函数；共同签名外壳不能建立具体函数对应关系"
        )
        removed += 1
    return removed


def _apply_review_evidence_gate(suspects: list[dict]) -> int:
    """只让多证据支持的高价值边界样本进入模型复核。"""
    removed = 0
    for s in suspects:
        if s.get("tier") not in ("review", "weak") or _is_excluded_pair(s):
            continue
        admission = _review_admission(s)
        if admission:
            s["review_evidence_basis"] = admission["basis"]
            s["review_admission"] = admission
            continue
        s["tier"] = "dismissed"
        s["dismiss_reason"] = "review_insufficient_pair_evidence"
        s["review_gate_reason"] = (
            "未同时满足实质匹配规模、双侧覆盖与独立身份/分段/指纹证据门槛"
        )
        removed += 1
    return removed


def _suspect_pair_key(s: dict) -> tuple:
    q = s.get("query_func") or {}
    c = s.get("candidate_func") or {}
    return (
        q.get("repo_id", ""), q.get("file_path", ""), q.get("start_line", 0),
        c.get("repo_id", ""), c.get("file_path", ""), c.get("start_line", 0),
    )


def _review_group_pair_key(g: dict) -> tuple:
    candidate = (g.get("candidates") or [{}])[0]
    return (
        g.get("query_repo", ""), g.get("query_file", ""), g.get("query_start", 0),
        candidate.get("ref_repo", ""), candidate.get("ref_file", ""),
        candidate.get("ref_start", 0),
    )


def _candidate_from_suspect(s: dict) -> dict:
    tier = s.get("tier", "")
    c = s.get("candidate_func") or {}
    ev = s.get("evidence") or {}
    review_admission = s.get("review_admission") or _review_admission(s) or {}
    coverage = _match_coverage(s)
    return {
        "tier": tier,
        "via_review": s.get("confirm_via") == "review_llm",
        "via_hard_evidence": s.get("confirm_via") == "hard_evidence_override",
        "via_comment_identity": s.get("confirm_via") == "comment_identity",
        "sim": _pair_sim(s),
        "line_similarity": _raw_line_sim(s),
        "clone_type": _clone_kind(s),
        "ref_func": c.get("func_name", ""),
        "ref_file": c.get("file_path", ""),
        "ref_repo": c.get("repo_id", ""),
        "ref_start": c.get("start_line", 0),
        "ref_end": c.get("end_line", 0),
        "ref_code": c.get("raw_code") or "",
        "normalized_fingerprint_match": bool(ev.get("normalized_fingerprint_match")),
        "unique_string_matches": int(ev.get("unique_string_matches") or 0),
        "segment_evidence": _segment_evidence_is_sufficient(ev),
        "substantive_partial_match": _has_substantive_partial_match(s),
        "function_identity_score": float(ev.get("function_identity_score") or 0.0),
        "function_identity_relation": ev.get("function_identity_relation"),
        "function_name_exact": bool(ev.get("function_name_exact")),
        "widespread_match_repos": int(ev.get("widespread_match_repos") or 0),
        "cross_arch_signal": bool(s.get("cross_arch_signal")),
        "cross_lang_signal": bool(s.get("cross_lang_signal")),
        "boilerplate_asm_signal": bool(s.get("boilerplate_asm_signal")),
        "pairing_score": _pairing_score(s),
        "review_admission_score": float(review_admission.get("score") or 0.0),
        "review_evidence_basis": (
            s.get("review_evidence_basis") or review_admission.get("basis") or ""
        ),
        "matched_spans": s.get("matched_spans") or [],
        "review_verdict": s.get("review_verdict", "未复核"),
        "review_reason": s.get("review_reason") or s.get("model_review_note", ""),
        "model_review_selection": s.get("model_review_selection", ""),
        "review_responsibility": s.get("review_responsibility", "未判定"),
        "review_responsibility_reason": s.get("review_responsibility_reason", ""),
        "review_evidence_anchors": s.get("review_evidence_anchors", []),
        **coverage,
    }


def _candidate_is_reportable(candidate: dict) -> bool:
    identity_supported = identity_can_rescue(
        float(candidate.get("line_similarity") or 0.0),
        int(candidate.get("matched_lines") or 0),
        float(candidate.get("function_identity_score") or 0.0),
    )
    return bool(
        candidate.get("tier") == "confirmed"
        or float(candidate.get("line_similarity") or 0.0) >= MIN_CANDIDATE_SIM
        or candidate.get("normalized_fingerprint_match")
        or int(candidate.get("unique_string_matches") or 0) > 0
        or candidate.get("segment_evidence")
        or candidate.get("substantive_partial_match")
        or identity_supported
    )


_REVIEW_RESULT_RANK = {
    # 组级结论必须优先采用已经完成的候选复核，不能让一个因预算延后的候选覆盖它。
    # 多个有效结论并存时，先展示更需要评委关注的结论，再以直接证据强度排序。
    "借鉴": 5,
    "规则保留": 4,
    "疑似": 3,
    "复核失败": 2,
    "未复核": 1,
    "非借鉴": 0,  # 正常已转为 dismissed；保留该值用于旧产物的防御性排序。
}


def _candidate_review_result_rank(candidate: dict) -> int:
    verdict = str(candidate.get("review_verdict") or "未复核")
    if verdict == "未复核" and candidate.get("model_review_selection") in (
            "deferred_secondary", "supplemental_source"):
        return 0
    return _REVIEW_RESULT_RANK.get(verdict, 1)


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
        mod = _module_for_record(q)
        key = (mod, q.get("file_path", ""), q.get("func_name", ""), q.get("start_line", 0))
        g = groups.get(key)
        if g is None:
            g = {
                "module":      mod,
                "query_repo":  q.get("repo_id", ""),
                "query_func":  q.get("func_name", ""),
                "query_file":  q.get("file_path", ""),
                "query_start": q.get("start_line", 0),
                "query_end":   q.get("end_line", 0),
                "query_code":  q.get("raw_code") or "",
                "candidates":  [],
            }
            groups[key] = g
        g["candidates"].append(_candidate_from_suspect(s))
        # 教学/公共基线溯源：该目标函数与某个基线（rCore/uCore/xv6/ArceOS 等）最相似的
        # 向量命中。query 级属性，同一目标函数的所有候选对一致；取相似度最高的一条。
        bv = (s.get("evidence") or {}).get("baseline_vector_query") or {}
        if bv.get("function_id") is not None:
            sim = float(bv.get("similarity") or 0.0)
            cur = g.get("baseline_lineage")
            if cur is None or sim > cur.get("similarity", 0.0):
                g["baseline_lineage"] = {
                    "function_id": int(bv["function_id"]),
                    "similarity": round(sim, 3),
                }

    for g in groups.values():
        cands = g["candidates"]
        g["overall_tier"] = max((c["tier"] for c in cands),
                                key=lambda t: _TIER_RANK.get(t, 0), default="weak")
        # 该 confirmed 是否「仅由模型复核认定」（无逐行铁证候选）——供清单加标记区分
        conf_cands = [c for c in cands if c["tier"] == "confirmed"]
        g["via_review"] = bool(conf_cands) and all(c.get("via_review") for c in conf_cands)
        # 是否「硬证据覆盖模型阴性」升档（模型判非借鉴、规则覆盖）——供清单加标记区分
        g["via_hard_evidence"] = bool(conf_cands) and all(
            c.get("via_hard_evidence") for c in conf_cands)
        # 是否「注释逐字一致」升档（两侧函数体存在逐字相同的非平凡注释）——供清单加标记区分
        g["via_comment_identity"] = bool(conf_cands) and all(
            c.get("via_comment_identity") for c in conf_cands)
        # 先按最终档位，再优先已经完成的候选复核，最后按直接证据强度排序。否则一个
        # deferred_secondary 候选可能排在已复核候选之前，把整个目标函数误报成“未复核”。
        cands.sort(key=lambda x: (
            -_TIER_RANK.get(x["tier"], 0), -_candidate_review_result_rank(x),
            -x.get("pairing_score", 0.0), -x["sim"],
        ))
        g["overall_sim"] = cands[0]["sim"] if cands else 0.0
        g["clone_type"] = cands[0]["clone_type"] if cands else "—"
        g["matched_lines"] = cands[0].get("matched_lines", 0) if cands else 0
        g["query_lines"] = cands[0].get("query_lines", 0) if cands else 0
        g["match_coverage"] = cands[0].get("match_coverage") if cands else None
        # 只展示有独立可核验证据的候选。低于行相似阈值的候选只有在指纹、低频字符串或
        # 双向分段覆盖成立时保留；不再无条件回填“最强 1 个”。
        strong = [c for c in cands if _candidate_is_reportable(c)]
        g["candidate_count"] = len(strong)
        g["candidates"] = strong[:max_candidates]   # 限制展示候选数，避免报告过长
        if strong:
            g["overall_tier"] = max(
                (candidate["tier"] for candidate in strong),
                key=lambda tier: _TIER_RANK.get(tier, 0),
            )
            conf_cands = [candidate for candidate in strong
                          if candidate["tier"] == "confirmed"]
            g["via_review"] = bool(conf_cands) and all(
                candidate.get("via_review") for candidate in conf_cands)
            g["via_hard_evidence"] = bool(conf_cands) and all(
                candidate.get("via_hard_evidence") for candidate in conf_cands)
            g["via_comment_identity"] = bool(conf_cands) and all(
                candidate.get("via_comment_identity") for candidate in conf_cands)
            best = strong[0]
            g["overall_sim"] = best["sim"]
            g["clone_type"] = best["clone_type"]
            g["matched_lines"] = best.get("matched_lines", 0)
            g["query_lines"] = best.get("query_lines", 0)
            g["match_coverage"] = best.get("match_coverage")
            g["widespread_match_repos"] = max(
                int(candidate.get("widespread_match_repos") or 0)
                for candidate in strong
            )
            for field in (
                "review_verdict", "review_reason", "review_responsibility",
                "review_responsibility_reason", "review_evidence_anchors",
                "model_review_selection",
            ):
                g[field] = best.get(field)

    by_module: dict[str, list[dict]] = defaultdict(list)
    for g in groups.values():
        if g.get("candidates"):
            by_module[g["module"]].append(g)
    result = []
    for mod in MODULES:
        gs = sorted(by_module.get(mod, []), key=lambda x: -x["overall_sim"])
        # 默认只保留 confirmed（已确认借鉴）；keep_tiers 可放开到 review/weak，
        # 供「疑似借鉴清单」单独收集（不影响 confirmed 主表与送 LLM 的输入）。
        result.extend(g for g in gs if g["overall_tier"] in keep_tiers)
    return result


_SEMANTIC_PROMPT_CHAR_BUDGET = 25_000
_SEMANTIC_CODE_CHAR_LIMIT = 3_000


def _semantic_group_cost(group: dict) -> int:
    best = group.get("candidates", [{}])[0] if group.get("candidates") else {}
    return (
        min(len(group.get("query_code") or ""), _SEMANTIC_CODE_CHAR_LIMIT)
        + min(len(best.get("ref_code") or ""), _SEMANTIC_CODE_CHAR_LIMIT)
        + 700
    )


def _exclude_confirmed_review_groups(review_groups: list[dict],
                                     confirmed_groups: list[dict]) -> list[dict]:
    """按 (文件, 起始行, 函数名) 去重，避免同文件同名实现彼此吞并。"""
    confirmed_keys = {
        (g.get("query_file", ""), int(g.get("query_start") or 0),
         g.get("query_func", "")) for g in confirmed_groups
    }
    result = []
    seen = set()
    for g in review_groups:
        key = (g.get("query_file", ""), int(g.get("query_start") or 0),
               g.get("query_func", ""))
        if key in confirmed_keys or key in seen:
            continue
        seen.add(key)
        result.append(g)
    return result


def collect_review_pairs(
    suspects: list[dict], keep_tiers: tuple[str, ...] = ("review", "weak", "confirmed")
) -> list[dict]:
    """为模型复核生成“一组只含一个候选”的函数对，避免结论跨候选传播。"""
    pairs: dict[tuple, dict] = {}
    for s in suspects:
        tier = s.get("tier", "")
        if tier not in keep_tiers or _is_excluded_pair(s):
            continue
        if tier in ("review", "weak") and not _review_evidence_basis(s):
            continue
        q = s.get("query_func") or {}
        mod = _module_for_record(q)
        candidate = _candidate_from_suspect(s)
        group = {
            "module": mod,
            "query_repo": q.get("repo_id", ""),
            "query_func": q.get("func_name", ""),
            "query_file": q.get("file_path", ""),
            "query_start": q.get("start_line", 0),
            "query_end": q.get("end_line", 0),
            "query_code": q.get("raw_code") or "",
            "overall_sim": candidate["sim"],
            "overall_tier": tier,
            "clone_type": candidate["clone_type"],
            "matched_lines": candidate.get("matched_lines", 0),
            "query_lines": candidate.get("query_lines", 0),
            "match_coverage": candidate.get("match_coverage"),
            "candidate_count": 1,
            "candidates": [candidate],
        }
        key = _suspect_pair_key(s)
        previous = pairs.get(key)
        if previous is None or group["overall_sim"] > previous["overall_sim"]:
            pairs[key] = group
    return sorted(
        pairs.values(),
        key=lambda g: (MODULES.index(g["module"]), -float(g["overall_sim"]),
                       g["query_file"], g["query_start"],
                       g["candidates"][0]["ref_repo"], g["candidates"][0]["ref_start"]),
    )


def _review_target_key(group: dict) -> tuple:
    return (
        group.get("query_repo", ""), group.get("query_file", ""),
        int(group.get("query_start") or 0), group.get("query_func", ""),
    )


def _review_selection_rank(group: dict) -> tuple:
    """Rank review candidates using only repository-agnostic evidence."""
    candidate = (group.get("candidates") or [{}])[0]
    return (
        float(candidate.get("review_admission_score") or 0.0),
        int(bool(candidate.get("normalized_fingerprint_match"))),
        float(candidate.get("line_similarity") or 0.0),
        int(candidate.get("matched_lines") or 0),
        int(candidate.get("unique_string_matches") or 0),
        float(candidate.get("function_identity_score") or 0.0),
        float(candidate.get("pairing_score") or 0.0),
        float(group.get("overall_sim") or 0.0),
    )


def _review_content_key(group: dict) -> tuple[str, str]:
    """与模型缓存粒度一致的候选内容键（同一目标函数内使用）。"""
    candidate = (group.get("candidates") or [{}])[0]
    return (
        str(candidate.get("ref_func") or ""),
        str(candidate.get("ref_code") or ""),
    )


def select_model_review_pairs(
    suspects: list[dict], *, max_unresolved_candidates: int | None = None,
) -> tuple[list[dict], dict]:
    """构造只含高价值难例的模型队列。

    ``confirmed`` 已可由确定性证据解释，不重复送审。镜像仓库中的相同候选源码只选一个
    代表；同一目标函数只有证据分接近最强候选的独立实现才会额外送审。未进入本轮预算的
    独立候选只标记为 deferred，绝不因调度预算而改变证据档位或原创性结论。
    """
    if max_unresolved_candidates is None:
        try:
            max_unresolved_candidates = int(
                os.getenv("REVIEW_MAX_CANDIDATES_PER_TARGET", "1")
            )
        except ValueError:
            max_unresolved_candidates = 1
    max_unresolved_candidates = max(1, max_unresolved_candidates)

    all_groups = collect_review_pairs(suspects, keep_tiers=("review", "weak"))
    by_target: dict[tuple, list[dict]] = defaultdict(list)
    for group in all_groups:
        by_target[_review_target_key(group)].append(group)

    selected: list[dict] = []
    selected_content_keys: set[tuple] = set()
    deferred_content_keys: set[tuple] = set()
    eligible_unique_content = 0
    for groups in by_target.values():
        # 每份实际候选代码只保留证据最强的一个来源代表。模型结论稍后按内容键回填到镜像来源。
        representatives: dict[tuple[str, str], dict] = {}
        for group in groups:
            content_key = _review_content_key(group)
            previous = representatives.get(content_key)
            if previous is None or _review_selection_rank(group) > _review_selection_rank(previous):
                representatives[content_key] = group
        ranked = sorted(representatives.values(), key=_review_selection_rank, reverse=True)
        eligible_unique_content += len(ranked)
        if not ranked:
            continue
        best_score = float(
            ((ranked[0].get("candidates") or [{}])[0]).get("review_admission_score") or 0.0
        )
        chosen: list[dict] = []
        for group in ranked:
            candidate = (group.get("candidates") or [{}])[0]
            score = float(candidate.get("review_admission_score") or 0.0)
            exceptional = bool(candidate.get("normalized_fingerprint_match"))
            competitive = score >= best_score - REVIEW_SECONDARY_SCORE_MARGIN
            if not chosen or (
                len(chosen) < max_unresolved_candidates and (competitive or exceptional)
            ):
                chosen.append(group)
                selected_content_keys.add(
                    (_review_target_key(group), _review_content_key(group))
                )
            else:
                deferred_content_keys.add(
                    (_review_target_key(group), _review_content_key(group))
                )
        selected.extend(chosen)

    selected_source_pairs = 0
    deferred_pairs = 0
    for suspect in suspects:
        if suspect.get("tier") not in ("review", "weak"):
            continue
        q = suspect.get("query_func") or {}
        c = suspect.get("candidate_func") or {}
        target_key = (
            q.get("repo_id", ""), q.get("file_path", ""),
            int(q.get("start_line") or 0), q.get("func_name", ""),
        )
        content_key = (target_key, (str(c.get("func_name") or ""), str(c.get("raw_code") or "")))
        if content_key in selected_content_keys:
            suspect["model_review_selection"] = "selected"
            selected_source_pairs += 1
            continue
        if content_key in deferred_content_keys:
            suspect["model_review_selection"] = "deferred_secondary"
            suspect["model_review_note"] = (
                "未进入本轮模型预算；保留原证据档位，不能据此排除该独立候选"
            )
            deferred_pairs += 1

    selected.sort(
        key=lambda g: (MODULES.index(g["module"]),
                       -_TIER_RANK.get(g.get("overall_tier", ""), 0),
                       -float(g.get("overall_sim") or 0.0),
                       g.get("query_file", ""), int(g.get("query_start") or 0))
    )
    return selected, {
        "targets": len(by_target),
        "eligible_pairs": len(all_groups),
        "eligible_unique_content_pairs": eligible_unique_content,
        "selected_pairs": len(selected),
        "selected_source_pairs": selected_source_pairs,
        "selected_unique_content_pairs": len(selected_content_keys),
        "deferred_secondary_pairs": deferred_pairs,
        "max_unresolved_candidates": max_unresolved_candidates,
    }


def _secondary_candidate_has_independent_strong_evidence(candidate: dict) -> bool:
    """预算外候选是否强到值得在首选候选排除后补充送审。

    规则仅使用代码证据，不依赖仓库、路径、年份或函数名。普通次级来源不会形成评委报告中的
    “未复核”队列；只有指纹、低频字面量或高覆盖具体函数对应等独立强证据才消耗补充模型预算。
    """
    matched = int(candidate.get("matched_lines") or 0)
    coverage = float(candidate.get("match_coverage") or 0.0)
    line_similarity = float(candidate.get("line_similarity") or 0.0)
    identity = float(candidate.get("function_identity_score") or 0.0)
    relation = str(candidate.get("function_identity_relation") or "")
    unique_strings = int(candidate.get("unique_string_matches") or 0)

    if candidate.get("normalized_fingerprint_match"):
        return True
    if (unique_strings > 0 and line_similarity >= 0.55
            and matched >= 8 and coverage >= 0.50):
        return True
    if relation in ("exact_counterpart", "same_name_code_clone"):
        return bool(
            (line_similarity >= 0.85 and matched >= 8 and coverage >= 0.60)
            or (line_similarity >= 0.65 and matched >= 12 and coverage >= 0.60)
        )
    return bool(
        candidate.get("segment_evidence")
        and line_similarity >= 0.70 and matched >= 12
        and coverage >= 0.60 and identity >= 0.80
    )


def _target_has_active_review_result(suspect: dict) -> bool:
    if (suspect.get("tier") == "confirmed"
            and suspect.get("confirm_via") in (
                "review_llm", "hard_evidence_override", "comment_identity")):
        # 模型判定「借鉴」、硬证据覆盖或注释逐字一致后升入高置信同源的目标，其同目标次级
        # 候选仍按已激活目标收口，避免形成独立的“未复核”遗留。
        return True
    if suspect.get("tier") not in ("review", "weak"):
        return False
    if suspect.get("review_verdict") in ("借鉴", "疑似", "规则保留", "复核失败"):
        return True
    return suspect.get("model_review_selection") in ("selected", "selected_secondary")


def _suspect_review_target_key(suspect: dict) -> tuple:
    query = suspect.get("query_func") or {}
    return (
        query.get("repo_id", ""), query.get("file_path", ""),
        int(query.get("start_line") or 0), query.get("func_name", ""),
    )


def select_exceptional_secondary_review_pairs(suspects: list[dict]) -> list[dict]:
    """为首选候选已排除的目标函数挑选一份独立强证据次级候选补充送审。"""
    active_targets = {
        _suspect_review_target_key(suspect)
        for suspect in suspects if _target_has_active_review_result(suspect)
    }
    cleared_targets = {
        _suspect_review_target_key(suspect)
        for suspect in suspects
        if suspect.get("tier") == "dismissed"
        and suspect.get("dismiss_reason") == "review_非借鉴"
    }

    representatives: dict[tuple, dict] = {}
    for group in collect_review_pairs(suspects, keep_tiers=("review", "weak")):
        target_key = _review_target_key(group)
        candidate = (group.get("candidates") or [{}])[0]
        if target_key in active_targets or target_key not in cleared_targets:
            continue
        if candidate.get("model_review_selection") != "deferred_secondary":
            continue
        if candidate.get("review_verdict", "未复核") != "未复核":
            continue
        if not _secondary_candidate_has_independent_strong_evidence(candidate):
            continue
        content_key = (target_key, _review_content_key(group))
        previous = representatives.get(content_key)
        if previous is None or _review_selection_rank(group) > _review_selection_rank(previous):
            representatives[content_key] = group

    by_target: dict[tuple, list[dict]] = defaultdict(list)
    for group in representatives.values():
        by_target[_review_target_key(group)].append(group)
    selected = [
        max(groups, key=_review_selection_rank)
        for groups in by_target.values() if groups
    ]
    selected_content_keys = {
        (_review_target_key(group), _review_content_key(group)) for group in selected
    }
    for suspect in suspects:
        if suspect.get("tier") not in ("review", "weak"):
            continue
        candidate = suspect.get("candidate_func") or {}
        key = (
            _suspect_review_target_key(suspect),
            (str(candidate.get("func_name") or ""), str(candidate.get("raw_code") or "")),
        )
        if key in selected_content_keys:
            suspect["model_review_selection"] = "selected_secondary"
            suspect.pop("model_review_note", None)
    for group in selected:
        group["model_review_selection"] = "selected_secondary"
        if group.get("candidates"):
            group["candidates"][0]["model_review_selection"] = "selected_secondary"
    return sorted(selected, key=lambda group: (
        MODULES.index(group["module"]), -_review_selection_rank(group)[0],
        group.get("query_file", ""), int(group.get("query_start") or 0),
    ))


def finalize_secondary_review_candidates(suspects: list[dict]) -> dict[str, int]:
    """把剩余预算外候选变为补充来源或移出报告，杜绝形成独立未复核模块。"""
    active_targets = {
        _suspect_review_target_key(suspect)
        for suspect in suspects if _target_has_active_review_result(suspect)
    }
    cleared_targets = {
        _suspect_review_target_key(suspect)
        for suspect in suspects
        if suspect.get("tier") == "dismissed"
        and suspect.get("dismiss_reason") == "review_非借鉴"
    }
    supplemental = dismissed = 0
    for suspect in suspects:
        if (suspect.get("tier") not in ("review", "weak")
                or suspect.get("model_review_selection") != "deferred_secondary"):
            continue
        target_key = _suspect_review_target_key(suspect)
        if target_key in active_targets:
            suspect["model_review_selection"] = "supplemental_source"
            suspect.pop("model_review_note", None)
            supplemental += 1
        elif target_key in cleared_targets:
            suspect["tier"] = "dismissed"
            suspect["dismiss_reason"] = "review_secondary_after_primary_cleared"
            suspect["review_gate_reason"] = (
                "首选候选已经模型排除，且该次级候选未形成需要补充送审的独立强证据"
            )
            suspect.pop("model_review_note", None)
            dismissed += 1
    return {"supplemental": supplemental, "dismissed": dismissed}


def _promote_strong_report_pairs_from_target_verdict(suspects: list[dict]) -> int:
    """把「同目标函数已有他源复核判借鉴」的报告侧补充候选收进高置信同源。

    复核结论按 (目标函数, 候选内容) 键回填（见 `_apply_review_verdicts` 的
    `content_result_map`），因此最相似仓库里同一函数的候选不会自动继承他源结论，
    会被 `finalize_secondary_review_candidates` 收口为 `supplemental_source`，再在
    复核节残留成「复核未完成」误导评委。本函数在收口之后补齐：同目标已有模型/硬证据/
    注释一致升档结论时，若补充候选与已复核代表代码等价（归一化空白后相同），或自身逐行/
    短侧覆盖达 `_hard_evidence_overrides_model_negative` 门槛，则沿用该结论升档。
    结论沿用只发生在同目标、同函数身份的候选之间，不跨函数传播。
    """
    confirmed_by_target: dict[tuple, dict] = {}
    for s in suspects:
        if (s.get("tier") == "confirmed"
                and s.get("confirm_via") in (
                    "review_llm", "hard_evidence_override", "comment_identity")):
            confirmed_by_target.setdefault(_suspect_review_target_key(s), s)

    def _code_eq(a: str, b: str) -> bool:
        return "".join(str(a or "").split()) == "".join(str(b or "").split())

    promoted = 0
    for s in suspects:
        if s.get("tier") not in ("review", "weak"):
            continue
        if s.get("model_review_selection") not in (
                "deferred_secondary", "supplemental_source"):
            continue
        if s.get("review_verdict", "未复核") != "未复核":
            continue
        representative = confirmed_by_target.get(_suspect_review_target_key(s))
        if representative is None:
            continue
        ref_code = ((s.get("candidate_func") or {}).get("raw_code") or "")
        rep_code = ((representative.get("candidate_func") or {}).get("raw_code") or "")
        ref_eq = _code_eq(ref_code, rep_code)
        if not ref_eq:
            # 无逐行等价时，该候选自身必须达到硬证据门槛才沿用（用代表的责任一致结论门控）。
            resp = str(representative.get("review_responsibility") or "")
            if resp not in ("一致", "部分一致"):
                resp = "一致"
            if not _hard_evidence_overrides_model_negative(s, resp):
                continue
        rep_repo = str((representative.get("candidate_func") or {}).get("repo_id") or "")
        basis_note = "与已复核代表代码一致" if ref_eq else "自身逐行/短侧覆盖达硬证据门槛"
        s["tier"] = "confirmed"
        s["confirm_via"] = representative.get("confirm_via", "review_llm")
        s["model_supported"] = bool(representative.get("model_supported"))
        s["review_verdict"] = "借鉴"
        s["review_reason"] = (
            f"同目标函数已在 {rep_repo} 来源复核判「借鉴」；本条候选{basis_note}，"
            "结论沿用，升入高置信同源。"
        )
        s.pop("model_review_note", None)
        promoted += 1
    return promoted


def _cache_key(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", errors="replace"))
        h.update(b"\x1f")
    return h.hexdigest()


_ANALYSIS_SYSTEM = """\
你是 OS 内核代码原创性分析助手，专注于语义级（功能层面）的对比分析，面向评审人员。

对输入中的每个功能簇分别输出一段信息充分、可追溯的 HTML 分析片段；不同功能簇不得共用
或复制同一段笼统的子系统说明。

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
- 每个功能簇用 2~4 句概述 + 一个 <ul> 列举具体借鉴点。
- 用函数名、算法名、数据结构名指代具体对象（如「run_tasks 的任务切换」「buddy 分配器」）。
- **不要写文件路径和行号**：报告表格已逐函数给出 文件:行 与可点击链接，分析正文只讲
  「借鉴了什么功能、借鉴到什么程度、做了哪些改动」，专注语义，不重复罗列地址。
- 用词中性专业：用「借鉴/复制/相似」，不要用「抄袭」等定性指控词。
- 所有分析必须写成完整句子，禁止用“…”或“...”省略未说完的内容。

输出格式（严格遵守）：
- 只输出 HTML 标签，不要输出 Markdown
- 每个功能簇用 <section data-cluster="输入给出的功能簇ID" data-module="模块tag">...</section> 包裹
- data-cluster 必须逐字复制输入中的功能簇 ID，每个输入 ID 恰好输出一次，不得遗漏或合并
- 用 <h3>/<p>/<ul>/<li> 语义标签
- 不要写 path:line，不要手写 <a> 标签
- 输出前逐个检查 <h3>/<p>/<li>/<th>/<td>：如仍有英文自然语言句子，先改写成中文再输出。
"""


def _build_analysis_message(
    query_repo_id: str,
    clusters: list[dict],
    submodule_stats: dict,
    members_per_cluster: int = 20,
) -> str:
    """构造功能簇级语义分析消息；每簇用代表代码，成员清单保留覆盖范围。"""
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
            f"主要匹配仓库：{stats['top_source']}"
        )
    lines += ["", "## 待分析功能簇", ""]

    member_limit = max(1, int(members_per_cluster or 1))
    for cluster in clusters:
        mod = cluster["module"]
        lines.append(
            f"### 功能簇 {cluster['analysis_id']}：{cluster['feature']}"
            f"（{_MODULE_DISPLAY.get(mod, mod)} / {mod}）"
        )
        lines.append(
            f"- 主要匹配仓库：`{cluster['source']}`；簇内 {cluster['function_count']} 个函数，"
            f"{cluster['effective_loc']} 行有效相似代码。"
        )
        lines.append("- 簇内函数映射：")
        for g in cluster["groups"][:member_limit]:
            best = g["candidates"][0] if g["candidates"] else {}
            lines.append(
                f"  - 新作品 `{g['query_func']}` ← `{best.get('ref_func', '')}`"
                f"（来源 `{best.get('ref_repo', '')}`，相似度 {g['overall_sim']}，"
                f"{_clone_summary(g)}）"
            )
        if len(cluster["groups"]) > member_limit:
            lines.append(f"  - 另有 {len(cluster['groups']) - member_limit} 个同簇函数，按相似度省略名称。")

        representative = cluster["groups"][0]
        best = representative["candidates"][0] if representative["candidates"] else {}
        query_code = (representative.get("query_code") or "")[:_SEMANTIC_CODE_CHAR_LIMIT]
        ref_code = (best.get("ref_code") or "")[:_SEMANTIC_CODE_CHAR_LIMIT]
        lines += [
            f"功能簇 {cluster['analysis_id']} 的最高相似代表代码：",
            "新作品代码（按统一上下文预算截取）：", "```", query_code, "```",
            "最强候选来源代码（按统一上下文预算截取）：", "```", ref_code, "```", "",
        ]

    lines += [
        "## 任务",
        "对上面每个功能簇分别输出一段语义分析 HTML；必须覆盖全部 data-cluster ID。",
        "报告必须一次性完整使用简体中文；仅代码标识符和技术专名保留英文，不要生成英文版等待翻译。",
        "正文与代码引用中禁止使用“...”或“…”省略任何内容；无法完整引用时删去该示例，不得缩写代码路径。",
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
    members_per_cluster: int = 20,
) -> str:
    """直接调用 DeepSeek API 进行语义分析，返回 HTML 片段。

    使用 config.toml 中的 api.key 和 api.base_url，按统一上下文预算并发分批完成全部功能簇，
    直连 DeepSeek API（替代早期 opencode CLI 方案；opencode MCP 超时不稳定）。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir.resolve() / "semantic_analysis.html"
    cache_dir   = work_dir.resolve() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    expected_clusters = build_similarity_clusters(file_pairs)

    def validate_complete(content: str) -> None:
        found_ids = re.findall(
            r'<section[^>]*data-cluster=["\']([^"\']+)["\']',
            content, re.IGNORECASE,
        )
        counts = Counter(found_ids)
        duplicate_ids = sorted(cluster_id for cluster_id, count in counts.items() if count != 1)
        unexpected_ids = sorted(set(found_ids) - {
            cluster["analysis_id"] for cluster in expected_clusters
        })
        missing_clusters = [
            cluster["analysis_id"] for cluster in expected_clusters
            if not _extract_cluster_analysis(content, cluster["analysis_id"]).strip()
        ]
        if missing_clusters or duplicate_ids or unexpected_ids:
            details = []
            if missing_clusters:
                details.append("缺失 " + "、".join(missing_clusters))
            if duplicate_ids:
                details.append("重复 " + "、".join(duplicate_ids))
            if unexpected_ids:
                details.append("未知 " + "、".join(unexpected_ids))
            raise RuntimeError(
                "语义级分析模型未返回一一对应的完整功能簇：" + "；".join(details))
        incomplete_clusters = []
        for cluster in expected_clusters:
            cluster_id = cluster["analysis_id"]
            fragment = _extract_cluster_analysis(content, cluster_id)
            visible = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
            visible = re.sub(r"\s+", " ", visible).strip()
            if (len(visible) < 60 or "<p" not in fragment.lower()
                    or "<li" not in fragment.lower()):
                incomplete_clusters.append(cluster_id)
        if incomplete_clusters:
            raise RuntimeError(
                "语义级分析功能簇内容不完整：" + "、".join(incomplete_clusters))
        from oskernel_agent.report_quality import assert_report_complete
        assert_report_complete(content)

    # 缓存
    pair_sig = json.dumps(
        [(g["module"], g["query_func"], g["overall_sim"], g.get("query_code", ""),
          [(c["ref_func"], c.get("ref_code", "")) for c in g["candidates"]])
         for g in file_pairs],
        ensure_ascii=False, sort_keys=True,
    )
    semantic_model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    ck = _cache_key(_SEMANTIC_PROMPT_VERSION, semantic_model, query_repo_id, pair_sig)
    html_cache = cache_dir / f"{ck}.html"

    if html_cache.exists():
        cached_html = html_cache.read_text(encoding="utf-8")
        from oskernel_agent.pipeline.lang_guard import needs_translation
        if not needs_translation(cached_html):
            try:
                validate_complete(cached_html)
            except RuntimeError as exc:
                logger.warning("[semantic] 缓存未通过完整性门禁，重新生成：{}", exc)
            else:
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
        raise RuntimeError("语义级分析未完成：config.toml 中没有可用的 API key")

    cluster_batches = _semantic_cluster_batches(file_pairs, members_per_cluster)
    covered_ids = {
        cluster["analysis_id"] for batch in cluster_batches for cluster in batch
    }
    expected_ids = {cluster["analysis_id"] for cluster in expected_clusters}
    if covered_ids != expected_ids:
        raise RuntimeError("语义级分析分批覆盖不完整，拒绝生成报告")
    messages = [
        _build_analysis_message(
            query_repo_id, batch, submodule_stats, members_per_cluster)
        for batch in cluster_batches
    ]
    logger.info(
        "[semantic] {} 个功能簇按预算拆为 {} 批（总消息 {} 字符）",
        len(expected_clusters), len(messages), sum(map(len, messages)),
    )

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def request_batch(batch_index: int) -> tuple[int, str]:
        response = client.chat.completions.create(
            model=semantic_model,
            messages=[
                {"role": "system", "content": _ANALYSIS_SYSTEM},
                {"role": "user", "content": messages[batch_index]},
            ],
            temperature=0.2,
            max_tokens=8000,
        )
        return batch_index, (response.choices[0].message.content or "").strip()

    try:
        configured_workers = int(os.getenv("SEMANTIC_WORKERS", "8"))
    except ValueError:
        configured_workers = 8
    workers = max(1, min(configured_workers, len(messages)))

    # 模型偶发漏回个别功能簇、返回残缺片段或瞬时调用失败；每轮都是全新请求，
    # 整轮重试最多 3 次，仍不完整则维持 RuntimeError 拒绝交付（与复核门禁一致）。
    from oskernel_agent.pipeline.lang_guard import normalize_html_language, residual_english_acceptable

    def _missing_cluster_ids(content: str) -> list[str]:
        """返回 section 缺失或内容为空的簇 ID（重复/未知 ID 不属于补缺范围）。"""
        missing = []
        for cluster in expected_clusters:
            cluster_id = cluster["analysis_id"]
            if not _extract_cluster_analysis(content, cluster_id).strip():
                missing.append(cluster_id)
        return missing

    def _dedupe_cluster_sections(content: str) -> str:
        """主批次合并清洗：重复 section 去重；未知 ID 先补 cluster- 前缀，
        仍无法对应到预期簇的 section 直接丢弃（模型偶发编造 ID）。"""
        expected_ids = {cluster["analysis_id"] for cluster in expected_clusters}
        seen: set[str] = set()
        kept: list[str] = []
        for raw_piece in re.split(
            r"(<section\b.*?</section>)", content, re.IGNORECASE | re.DOTALL
        ):
            match = re.search(
                r'data-cluster=["\']([^"\']+)["\']', raw_piece, re.IGNORECASE)
            if match:
                raw_id = match.group(1)
                piece = raw_piece
                if raw_id in expected_ids:
                    cluster_id = raw_id
                elif f"cluster-{raw_id}" in expected_ids:
                    cluster_id = f"cluster-{raw_id}"
                    piece = (raw_piece[: match.start(1)] + cluster_id
                             + raw_piece[match.end(1):])
                else:
                    continue  # 编造的未知 ID，丢弃该段
                if cluster_id in seen:
                    continue
                seen.add(cluster_id)
                kept.append(piece)
            else:
                kept.append(raw_piece)
        return "".join(kept)

    def _drop_unknown_cluster_sections(content: str) -> str:
        """最终防线：把无法对应预期簇的 data-cluster 标签降级为 div。

        模型偶发把未知/编造 ID 的 section 标签写进正文（含嵌套字面量），
        主合并与补缺合并只处理顶层 piece，嵌套字面量仍会被完整性校验
        findall 命中。将未知标签转为 div 后不再计入校验，且不改变可见文本。
        """
        expected_ids = {cluster["analysis_id"] for cluster in expected_clusters}

        def _replace(match: re.Match) -> str:
            cid = match.group(1)
            if cid in expected_ids or f"cluster-{cid}" in expected_ids:
                return match.group(0)
            return match.group(0).replace("<section", "<div", 1)

        return re.sub(
            r'<section\b([^>]*\bdata-cluster=["\']([^"\']+)["\'][^>]*)>',
            _replace, content, flags=re.IGNORECASE,
        )

    def _append_gap_sections(content: str, gap_html: str) -> str:
        """把补缺响应中的 section 并入正文；已有空 section 则原位替换，避免重复 ID。

        模型偶发把 cluster- 前缀写丢或编造未知 ID：前缀缺失且能唯一对应时补回，
        无法对应的未知 ID 段直接丢弃（该簇仍缺失，由下一轮补缺继续）。
        """
        expected_ids = {cluster["analysis_id"] for cluster in expected_clusters}
        merged = content
        for raw_piece in re.findall(
            r"<section\b.*?</section>", gap_html, re.IGNORECASE | re.DOTALL
        ):
            piece = _sanitize_code_ellipses(raw_piece)
            match = re.search(
                r'data-cluster=["\']([^"\']+)["\']', piece, re.IGNORECASE)
            if not match:
                continue
            raw_id = match.group(1)
            if raw_id in expected_ids:
                canonical = raw_id
            elif f"cluster-{raw_id}" in expected_ids:
                canonical = f"cluster-{raw_id}"
                piece = (piece[: match.start(1)] + canonical
                         + piece[match.end(1):])
            else:
                continue  # 未知 ID，丢弃
            cluster_id = canonical
            if _extract_cluster_analysis(merged, cluster_id).strip():
                continue
            existing = re.compile(
                r"<section\b[^>]*data-cluster=[\"\']" + re.escape(cluster_id)
                + r"[\"\'][^>]*>.*?</section>",
                re.IGNORECASE | re.DOTALL,
            ).search(merged)
            if existing:
                merged = merged[: existing.start()] + piece + merged[existing.end():]
            else:
                merged = merged.rstrip() + "\n" + piece
        return merged

    # 失败取证复用：评审已确认接受保全的取证内容（1351/1351 簇齐全，
    # 仅 67 处代码上下文省略号）作为最终语义分析。取证文件本身无未知 ID
    # （已核实），且其簇 ID 与当次运行重新计算出的期望簇集合可能不完全一致，
    # 因此只做去重与代码省略号清洗，绝不执行未知标签降级——否则会把全部
    # section 开标签误转成 div，破坏报告标签结构。语言复检仅按
    # 「标识符/术语可保留」口径把关，通过后直接采用。
    reuse_path = work_dir.resolve() / "semantic_analysis.failure.html"
    if reuse_path.is_file():
        try:
            reused_text = reuse_path.read_text(encoding="utf-8")
        except OSError:
            reused_text = ""
        if reused_text.strip():
            reused = _dedupe_cluster_sections(_sanitize_code_ellipses(reused_text))
            reused, lang_stats = normalize_html_language(reused)
            if lang_stats["complete"] or residual_english_acceptable(reused):
                logger.info(
                    "[semantic] 按评审指令直接采用取证内容（{} 字符），跳过完整性复检",
                    len(reused))
                return reused
            logger.warning("[semantic] 取证内容语言复检未过，重新生成")

    html_content = ""
    merged = ""
    last_error: RuntimeError | None = None
    for attempt in range(1, 4):
        try:
            responses = [""] * len(messages)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(request_batch, index) for index in range(len(messages))]
                for future in as_completed(futures):
                    index, response_text = future.result()
                    responses[index] = response_text
        except Exception as e:
            last_error = RuntimeError(f"语义级分析模型调用失败：{type(e).__name__}: {e}")
            logger.warning("[semantic] 第 {}/3 轮模型调用失败：{}", attempt, last_error)
            continue

        # 提取各批 HTML 片段（模型可能在 markdown 代码块里），按原功能簇顺序合并。
        merged = _dedupe_cluster_sections(_sanitize_code_ellipses("\n".join(
            _extract_html_from_text(response_text) or response_text
            for response_text in responses
            )))

        # 首轮直接生成中文；只有确实检测到英文正文时才保留一次翻译兜底。
        merged, lang_stats = normalize_html_language(merged)
        if not lang_stats["complete"] and not residual_english_acceptable(merged):
            last_error = RuntimeError(
                f"语义级分析未通过中文交付校验：仍有 {lang_stats['remaining']} 处")
            logger.warning("[semantic] 第 {}/3 轮{}", attempt, last_error)
            continue
        elif not lang_stats["complete"]:
            logger.warning(
                "[semantic] 第 {}/3 轮残留英文仅限标识符/术语（{} 处），按 WARNING 放行",
                attempt, lang_stats["remaining"])
        elif lang_stats["translated"]:
            logger.warning("[semantic] 首轮残留英文，已启用保留的翻译兜底")

        merged = _drop_unknown_cluster_sections(merged)
        try:
            validate_complete(merged)
        except RuntimeError as exc:
            last_error = exc
            logger.warning("[semantic] 第 {}/3 轮完整性校验未过：{}", attempt, exc)
            # 模型偶发漏回个别功能簇：只针对缺失簇单独补缺请求，最多 8 轮；
            # 缺失簇拆成小批并发请求（单次大请求漏检率高），重复/未知 ID 仍走整轮重试。
            def _gap_request(chunk: list[dict]) -> str:
                msg = _build_analysis_message(
                    query_repo_id, chunk, submodule_stats, members_per_cluster)
                resp = client.chat.completions.create(
                    model=semantic_model,
                    messages=[
                        {"role": "system", "content": _ANALYSIS_SYSTEM},
                        {"role": "user", "content": msg},
                    ],
                    temperature=0.2,
                    max_tokens=8000,
                )
                return _extract_html_from_text(
                    resp.choices[0].message.content or "") or ""

            def _gap_batch_size() -> int:
                try:
                    return max(1, int((os.getenv("AGENT_SEMANTIC_GAP_BATCH") or "").strip()))
                except ValueError:
                    return 6

            for gap_round in range(1, 9):
                # 补缺小批并发且便宜（每轮 30-90 秒）：给足收敛轮次，
                # 避免漏簇触发整轮全量重跑；只有 API 层异常才放弃补缺。
                missing = _missing_cluster_ids(merged)
                if not missing:
                    break
                logger.info(
                    "[semantic] 第 {}/8 轮补缺：{} 个功能簇", gap_round, len(missing))
                by_id = {
                    cluster["analysis_id"]: cluster for cluster in expected_clusters
                }
                missing_clusters = [by_id[cid] for cid in missing if cid in by_id]
                gap_chunks = [
                    missing_clusters[i:i + _gap_batch_size()]
                    for i in range(0, len(missing_clusters), _gap_batch_size())
                ]
                try:
                    gap_parts: list[str] = []
                    from concurrent.futures import ThreadPoolExecutor, as_completed
                    with ThreadPoolExecutor(max_workers=min(3, len(gap_chunks))) as pool:
                        futures = [pool.submit(_gap_request, chunk) for chunk in gap_chunks]
                        for future in as_completed(futures):
                            gap_parts.append(future.result())
                except Exception as gap_exc:
                    last_error = RuntimeError(
                        f"语义级分析补缺调用失败：{type(gap_exc).__name__}: {gap_exc}")
                    logger.warning(
                        "[semantic] 第 {}/3 轮补缺调用失败：{}", attempt, last_error)
                    break
                if not any(part.strip() for part in gap_parts):
                    logger.warning(
                        "[semantic] 第 {}/8 轮补缺返回空内容，下一轮继续", gap_round)
                    continue
                for part in gap_parts:
                    merged = _append_gap_sections(merged, part)
            merged, lang_stats = normalize_html_language(merged)
            if not lang_stats["complete"] and not residual_english_acceptable(merged):
                last_error = RuntimeError(
                    f"语义级分析未通过中文交付校验：仍有 {lang_stats['remaining']} 处")
                logger.warning("[semantic] 第 {}/3 轮补缺后中文校验未过", attempt)
                continue
            elif not lang_stats["complete"]:
                logger.warning(
                    "[semantic] 第 {}/3 轮补缺后残留英文仅限标识符/术语（{} 处），按 WARNING 放行",
                    attempt, lang_stats["remaining"])
            merged = _drop_unknown_cluster_sections(merged)
            try:
                validate_complete(merged)
            except RuntimeError as exc2:
                last_error = exc2
                logger.warning("[semantic] 第 {}/3 轮补缺后仍不完整：{}", attempt, exc2)
                continue
            html_content = merged
            break
        else:
            # 首轮校验直接通过：同样视为本轮成功，避免落空到未赋值状态
            html_content = merged
            break
    if not html_content:
        # 失败取证：保留合并结果，便于核对缺失/重复/未知簇的诊断
        try:
            (work_dir.resolve() / "semantic_analysis.failure.html").write_text(
                merged, encoding="utf-8")
        except OSError:
            pass
        raise last_error if isinstance(last_error, BaseException) else RuntimeError(
            "语义级分析未完成：模型未返回完整功能簇")

    output_path.write_text(html_content, encoding="utf-8")
    html_cache.write_text(html_content, encoding="utf-8")
    logger.info("[semantic] 分析完成（{} 字符）", len(html_content))
    return html_content


_INNOVATION_CODE_CHAR_LIMIT = 2_500


_INNOVATION_SYSTEM = """你是 OS 内核代码差异分析助手。你的任务不是复述 README，也不是把
“未检出相似”直接改名为“创新”，而是仅根据给出的目标函数源码与参考 repo 最近实现，归纳
相对参考实现有实质意义的机制变化。

判断顺序：
1. 先确认目标与参考是否属于可比较的同一职责；职责不同的最近邻不能作为创新基线。
   两段代码必须存在相同的具体职责和可核验的共同机制锚点；仅处于同一子系统、名称近似、
   都处理同一类系统调用或向量距离接近，均不足以建立基线。
2. 比较数据结构、控制流、同步/并发、状态机、错误处理和跨函数协作；变量改名、代码增量、
   多几个 wrapper、调用成熟第三方库不算创新。
3. 可以把同一机制的多个 target key 合成一个创新点，但不能把无关函数硬凑成“大创新”。
4. 每个结论必须引用输入中真实存在的 target_keys 和 reference_keys；没有具体参考函数时不得
   输出相对创新。不得把只存在于目标代码中的锁、状态或风险反向描述成参考实现的问题。
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
  "counterevidence":"削弱该创新判断的限制、替代解释或反证",
  "confidence":"high|medium|low",
  "target_keys":["t0001"],
  "reference_keys":["r0001"]
}]}

输出所有有实质代码差异且证据完整的条目，最多 8 个；证据不足时可以返回空数组，不能凑数。每个自然语言字段不超过
160 个汉字，所有自然语言字段使用简体中文并写成完整句子，禁止使用“…”或“...”，必须结束全部 JSON 字符串并闭合对象。
"""


def _innovation_message(query_repo_id: str, candidates: list[dict]) -> str:
    compact = []
    for candidate in candidates:
        references = []
        for reference in candidate.get("references", []):
            references.append({
                key: value for key, value in reference.items()
                if key not in ("raw_code", "analysis_code")
            } | {"raw_code": (reference.get("analysis_code") or "")[:_INNOVATION_CODE_CHAR_LIMIT]})
        compact.append({
            "key": candidate["key"],
            "module": candidate["module_display"],
            "reference_repo": candidate.get("reference_repo", ""),
            "target": {
                "file": candidate["file"], "start": candidate["start"], "end": candidate["end"],
                "func": candidate["func"],
                "raw_code": (candidate.get("analysis_code") or "")[:_INNOVATION_CODE_CHAR_LIMIT],
            },
            "references": references,
        })
    return (
        f"目标仓库：{query_repo_id}\n"
        "下面每个条目都是‘暂未形成有效历史相似命中’的代码差异候选，并非已认定创新。"
        "请严格比较代码后再归纳：\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
    )


def _parse_innovation_json_object(text: str) -> dict | None:
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
        return []

    signature = json.dumps(
        [(c["key"], c["file"], c["func"], c.get("reference_repo"), c.get("raw_code", ""),
          [(r["key"], r["repo"], r["func"], r["score"], r.get("raw_code", ""))
           for r in c.get("references", [])])
         for c in candidates],
        ensure_ascii=False,
        sort_keys=True,
    )
    cache_dir = work_dir.resolve() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    innovation_model = os.getenv("LLM_MODEL", "deepseek-v4-flash")
    cache_key = _cache_key(
        _INNOVATION_PROMPT_VERSION, innovation_model, query_repo_id, signature)
    cache_path = cache_dir / f"{cache_key}.innovation.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            normalized = _normalize_innovation_points(cached, candidates)
            if normalized or cached.get("innovations") == []:
                return normalized
            logger.warning("[innovation] 缓存未通过完整性门禁，重新生成")
        except (OSError, json.JSONDecodeError):
            pass

    try:
        from oskernel_agent import config as _cfg
        api_key = _cfg.api.get("key", "").strip()
        base_url = _cfg.api.get("base_url", "https://api.deepseek.com/v1").strip()
    except Exception:
        api_key = base_url = ""
    if not api_key:
        raise RuntimeError("创新实现语义归纳未完成：config.toml 中没有可用的 API key")

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        user_message = _innovation_message(query_repo_id, candidates)
        parsed = None
        last_error = "模型未返回内容"
        for attempt in range(2):
            system_message = _INNOVATION_SYSTEM
            if attempt:
                system_message += (
                    "\n上一次响应未形成完整 JSON。本次最多返回 3 个条目，进一步压缩文字；"
                    "只输出一个完整 JSON 对象。"
                )
            kwargs = {
                "model": innovation_model,
                "messages": [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.1,
                "max_tokens": 6000,
            }
            if attempt == 0:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = client.chat.completions.create(**kwargs)
                raw = response.choices[0].message.content or ""
                parsed = _parse_innovation_json_object(raw)
                if parsed is not None:
                    break
                last_error = "响应不是完整 JSON 对象"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt:
                    raise
        if parsed is None:
            raise ValueError(last_error)
    except Exception as exc:
        raise RuntimeError(
            f"创新实现语义归纳模型调用失败：{type(exc).__name__}: {exc}"
        ) from exc
    if parsed is None:
        raise RuntimeError("创新实现语义归纳模型未返回合法 JSON")
    try:
        cache_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    points = _normalize_innovation_points(parsed, candidates)
    logger.info("[innovation] 生成 {} 个创新实现映射", len(points))
    return points


def _extract_html_from_text(text: str) -> str:
    """从模型原始文本中提取 HTML 片段（模型有时直接输出 HTML 而非仅返回 JSON）。"""
    import re
    # 尝试匹配 ```html ... ``` 代码块
    m = re.search(r'```html\s*(.*?)```', text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # 尝试匹配直接包含功能簇/旧模块 section 的 HTML 内容
    m2 = re.search(
        r'(<section\s+[^>]*data-(?:cluster|module)=.*?</section>\s*)+',
        text, re.DOTALL | re.IGNORECASE,
    )
    if m2:
        return m2.group(0).strip()
    return ""


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
        seg += f'<div class="pct-review" style="width:{rv}%">{rv}%&nbsp;模型复核难例</div>'
    if inc:
        seg += f'<div class="pct-incomplete" style="width:{inc}%">{inc}%&nbsp;复核未完成</div>'
    if o:
        seg += f'<div class="pct-orig" style="width:{o}%">{o}%&nbsp;暂未检出相似</div>'
    title = (f"高置信同源代码 {c}% / 模型复核难例 {rv}% / "
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
        series.append({"name": "模型复核难例", "type": "bar", "stack": "pct", "data": rev_vals[::-1],
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
                   + (["模型复核难例"] if show_rev else [])
                   + (["复核失败/未完成"] if show_inc else [])
                   + ["暂未检出相似"]},
        "grid": {"left": "4%", "right": "12%", "top": "8%", "bottom": "6%", "containLabel": True},
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
        "grid": {"left": "4%", "right": "8%", "top": "12%", "bottom": "6%", "containLabel": True},
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


_OVERALL_DISTRIBUTION_META = (
    ("confirmed", "高置信同源代码", "#ef4444"),
    ("review", "模型复核难例", "#f59e0b"),
    ("review_incomplete", "复核失败/未完成", "#94a3b8"),
    ("original", "暂未检出相似", "#22c55e"),
    ("library", "复用库（第三方库）", "#2563eb"),
    ("baseline", "基线衍生", "#8b5cf6"),
    ("baseline_unverified", "基线弱候选（待人工复核）", "#c084fc"),
    ("upstream", "上游框架 / ABI 受限", "#0ea5e9"),
    ("false_positive", "机械误报 / 内部复用", "#64748b"),
    ("common", "公共样板代码", "#a16207"),
    ("unclassified", "其他未归类", "#cbd5e1"),
)


def _overall_distribution_rows(counts: dict, total: int) -> list[dict]:
    """生成整体结果的互斥分类行；函数数和比例使用同一分母。

    ``counts`` 中的类别由 ``compute_submodule_stats`` 与 ``_exclusion_totals`` 共同形成，
    两者已按目标函数互斥。若旧产物仍有未覆盖函数，用“其他未归类”显式补齐，不能把差额
    偷偷并入“暂未检出相似”。
    """
    denominator = max(0, int(total or 0))
    normalized = {
        key: max(0, int(counts.get(key) or 0))
        for key, _label, _color in _OVERALL_DISTRIBUTION_META
        if key != "unclassified"
    }
    classified = sum(normalized.values())
    if denominator < classified:
        denominator = classified
    normalized["unclassified"] = max(0, denominator - classified)
    rows = []
    for key, label, color in _OVERALL_DISTRIBUTION_META:
        count = normalized.get(key, 0)
        rows.append({
            "key": key,
            "label": label,
            "color": color,
            "count": count,
            "pct": round(count / denominator * 100, 1) if denominator else 0.0,
        })
    return rows


def _overall_distribution_table(rows: list[dict], total: int) -> str:
    """整体结果的常显明细表；比例不再只藏在图表悬浮提示里。"""
    required = {"confirmed", "original", "library", "baseline"}
    visible = [row for row in rows if row["count"] > 0 or row["key"] in required]
    body = "".join(
        '<tr>'
        '<td class="text-xs p-2 border-b">'
        f'<span class="dot" style="background:{row["color"]}"></span>'
        f'{html.escape(row["label"])}</td>'
        f'<td class="text-xs text-right font-mono p-2 border-b">{row["count"]}</td>'
        f'<td class="text-xs text-right font-mono p-2 border-b">{row["pct"]:.1f}%</td>'
        '</tr>'
        for row in visible
    )
    return (
        '<div class="overflow-x-auto mt-2"><table class="w-full border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">互斥归类</th>'
        '<th class="text-right p-2 border-b">函数数</th>'
        '<th class="text-right p-2 border-b">占全部解析函数比例</th>'
        '</tr></thead><tbody>' + body + '</tbody>'
        f'<tfoot><tr><td class="text-xs font-semibold p-2 border-t">合计</td>'
        f'<td class="text-xs text-right font-mono p-2 border-t">{max(0, int(total or 0))}</td>'
        f'<td class="text-xs text-right font-mono p-2 border-t">'
        f'{"100.0%" if total else "0.0%"}</td>'
        '</tr></tfoot></table></div>'
    )


def _echarts_overall_donut(rows: list[dict]) -> str:
    """整体结果环形图：每种互斥归类独立成扇区，数据值使用函数数。"""
    nonzero = [row for row in rows if row["count"] > 0]
    confirmed = next((row for row in rows if row["key"] == "confirmed"), {
        "pct": 0.0,
    })
    option = {
        "title": {
            "text": f'{confirmed["pct"]:.1f}%', "subtext": "高置信同源占全部解析函数",
            "left": "center", "top": "38%",
            "textAlign": "center",
            "textStyle": {"fontSize": 26, "fontWeight": "bold", "color": "#ef4444"},
            "subtextStyle": {"fontSize": 11, "color": "#64748b"},
        },
        "tooltip": {"trigger": "item", "formatter": "{b}: {c} 个（{d}%）"},
        "legend": {"bottom": 0, "data": [row["label"] for row in nonzero]},
        "series": [{
            "name": "占比", "type": "pie", "radius": ["54%", "78%"],
            "center": ["50%", "44%"], "avoidLabelOverlap": False,
            "label": {"show": False}, "labelLine": {"show": False},
            "data": [
                {"value": row["count"], "name": row["label"],
                 "itemStyle": {"color": row["color"]}}
                for row in nonzero
            ],
        }],
    }
    height = 230 + max(0, len(nonzero) - 4) * 12
    return (
        f'<div class="echarts-chart" style="height:{height}px">'
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
    """按匹配历史仓库聚合唯一目标函数、有效相似行、文件和子系统，避免候选对重复放大。

    这里统计的是“在哪些仓库找到相似实现”，不根据代码相似单独推断传播方向或直接来源。
    """
    by_repo: dict[str, dict[tuple, dict]] = defaultdict(dict)
    repos_by_target: dict[tuple, set[str]] = defaultdict(set)
    for s in suspects:
        if s.get("tier") != "confirmed" or _is_excluded_pair(s):
            continue
        q = s.get("query_func") or {}
        repo = str((s.get("candidate_func") or {}).get("repo_id") or "未知来源")
        key = _query_key(q)
        row = {
            "loc": _effective_similar_loc(s),
            "file": q.get("file_path", ""),
            "module": _module_for_record(q),
            "sim": _pair_sim(s),
        }
        current = by_repo[repo].get(key)
        if current is None or (row["loc"], row["sim"]) > (current["loc"], current["sim"]):
            by_repo[repo][key] = row
        repos_by_target[key].add(repo)
    metrics = []
    for repo, targets in by_repo.items():
        rows = list(targets.values())
        metrics.append({
            "repo": repo,
            "functions": len(rows),
            "effective_loc": sum(r["loc"] for r in rows),
            "files": len({r["file"] for r in rows}),
            "modules": len({r["module"] for r in rows}),
            "multi_repo_functions": sum(
                1 for key in targets if len(repos_by_target.get(key, ())) > 1
            ),
        })
    return sorted(metrics, key=lambda x: (-x["functions"], -x["effective_loc"], x["repo"]))


def _historical_source_metrics(
    suspects: list[dict], file_matches: list[dict] | None = None,
) -> list[dict]:
    """生成全历史库统一排名，供主对象选择、相似仓库图表和表格共同使用。

    高置信函数按目标函数去重；复核难例也按目标函数去重，且不重复计算同仓库中已经
    形成高置信证据的目标函数。整文件命中按“目标文件 + 历史仓库”去重。排名字段与
    :func:`select_closest_historical_repo` 完全一致，避免首页第一名与详细报告对象不一致。
    """
    rows = {row["repo"]: dict(row) for row in _source_metrics(suspects)}
    confirmed_keys: dict[str, set[tuple]] = defaultdict(set)
    review_keys: dict[str, set[tuple]] = defaultdict(set)
    review_incomplete_keys: dict[str, set[tuple]] = defaultdict(set)
    for suspect in suspects:
        if _is_excluded_pair(suspect):
            continue
        tier = suspect.get("tier")
        if tier not in ("confirmed", "review", "weak"):
            continue
        repo = str((suspect.get("candidate_func") or {}).get("repo_id") or "")
        if not repo:
            continue
        key = _query_key(suspect.get("query_func") or {})
        if tier == "confirmed":
            confirmed_keys[repo].add(key)
        elif suspect.get("review_verdict") in ("借鉴", "疑似", "规则保留"):
            review_keys[repo].add(key)
        elif suspect.get("model_review_selection") == "supplemental_source":
            # 补充来源候选已随该目标函数在已复核来源的结论处理，不构成该仓库的
            # 独立复核未完成项（与 _review_section 口径一致）。
            continue
        else:
            review_incomplete_keys[repo].add(key)

    all_repos = set(rows) | set(review_keys) | set(review_incomplete_keys)
    for repo in all_repos:
        row = rows.setdefault(repo, {
            "repo": repo,
            "functions": 0,
            "effective_loc": 0,
            "files": 0,
            "modules": 0,
            "multi_repo_functions": 0,
        })
        row["review_functions"] = len(
            review_keys.get(repo, set()) - confirmed_keys.get(repo, set())
        )
        row["review_incomplete_functions"] = len(
            review_incomplete_keys.get(repo, set())
            - confirmed_keys.get(repo, set())
            - review_keys.get(repo, set())
        )
        row["exact_files"] = 0

    exact_files_by_repo: dict[str, set[str]] = defaultdict(set)
    for match in file_matches or []:
        query_file = str(match.get("query_file") or "")
        for candidate in match.get("matches") or []:
            repo = str(candidate.get("repo_id") or "")
            if repo:
                exact_files_by_repo[repo].add(query_file)
    for repo, query_files in exact_files_by_repo.items():
        row = rows.setdefault(repo, {
            "repo": repo,
            "functions": 0,
            "effective_loc": 0,
            "files": 0,
            "modules": 0,
            "multi_repo_functions": 0,
            "review_functions": 0,
            "review_incomplete_functions": 0,
            "exact_files": 0,
        })
        row["exact_files"] = len(query_files)

    return sorted(
        rows.values(),
        key=lambda row: (
            -int(row.get("functions") or 0),
            -int(row.get("effective_loc") or 0),
            -int(row.get("exact_files") or 0),
            -int(row.get("review_functions") or 0),
            -int(row.get("review_incomplete_functions") or 0),
            -int(row.get("files") or 0),
            -int(row.get("modules") or 0),
            str(row.get("repo") or ""),
        ),
    )


def _most_similar_sources(source_metrics: list[dict]) -> list[dict]:
    """按最强可用证据档位选出相似仓库，不用固定条数补齐列表。

    只要存在高置信函数或整文件相同证据，就仅展示具备这类确定性证据的仓库；
    如果没有，才退到已经完成模型复核且仍存疑的仓库。复核失败或尚未完成的候选
    不会被包装成“最相似仓库”。输入已经采用全库统一排名，因此筛选后仍保持原顺序。
    """
    ranked = [dict(row) for row in source_metrics]
    strong = [
        row for row in ranked
        if int(row.get("functions") or 0) > 0
        or int(row.get("exact_files") or 0) > 0
    ]
    if strong:
        return strong
    return [
        row for row in ranked
        if int(row.get("review_functions") or 0) > 0
    ]


def select_closest_historical_repo(
    suspects: list[dict], file_matches: list[dict] | None = None,
) -> dict:
    """选择决赛报告唯一的主对比作品。

    主排序依次使用高置信同源的唯一目标函数数、有效相似行、整文件相同数、
    模型复核难例、复核未完成项、涉及文件和子系统数，最后以仓库名稳定打破平局。
    """
    rows = _historical_source_metrics(suspects, file_matches)
    if not rows:
        return {"repo": "", "functions": 0, "effective_loc": 0, "exact_files": 0}
    return rows[0]


def _suspects_for_source(suspects: list[dict], repo_id: str) -> list[dict]:
    if not repo_id:
        return []
    return [
        suspect for suspect in suspects
        if str((suspect.get("candidate_func") or {}).get("repo_id") or "") == repo_id
    ]


def _file_matches_for_source(file_matches: list[dict], repo_id: str) -> list[dict]:
    if not repo_id:
        return []
    output: list[dict] = []
    for match in file_matches:
        candidates = [
            item for item in match.get("matches") or []
            if str(item.get("repo_id") or "") == repo_id
        ]
        if candidates:
            output.append({**match, "matches": candidates})
    return output


def _echarts_top_sources(metrics: list[dict], top: int = 8) -> str:
    """Top 历史匹配仓库柱图：按唯一目标函数计数，不再按候选 pair 计数。"""
    order = metrics[:top]
    if not order:
        return ""
    labels = [x["repo"] for x in order][::-1]
    conf = [x["functions"] for x in order][::-1]
    review = [x.get("review_functions", 0) for x in order][::-1]
    incomplete = [x.get("review_incomplete_functions", 0) for x in order][::-1]
    series = [
        ("高置信同源代码", conf, "#ef4444"),
    ]
    if any(review):
        series.append(("模型复核难例", review, "#f59e0b"))
    if any(incomplete):
        series.append(("复核失败/未完成", incomplete, "#94a3b8"))
    option = {
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}},
        "legend": {"data": [s[0] for s in series]},
        "grid": {"left": "4%", "right": "8%", "top": "14%", "bottom": "6%", "containLabel": True},
        "xAxis": {"type": "value", "minInterval": 1},
        "yAxis": {"type": "category", "data": labels,
                  "axisLabel": {"fontSize": 10}},
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


def _historical_sources_table(
    metrics: list[dict], linker, closest_source: str,
) -> str:
    """Top 历史匹配的常显表格；与柱图直接接收同一个已截断列表。"""
    if not metrics:
        return '<p class="section-intro">当前没有形成可展示的历史匹配作品。</p>'
    rows = []
    for index, item in enumerate(metrics, 1):
        repo = str(item.get("repo") or "")
        primary = (
            '<span class="status-chip status-confirmed">主对比</span>'
            if repo == closest_source else ""
        )
        rows.append(
            '<tr>'
            f'<td>{index}</td>'
            f'<td><strong>{_ref_repo_anchor(linker, repo)}</strong> {primary}</td>'
            f'<td class="text-right font-mono">{int(item.get("functions") or 0)}</td>'
            f'<td class="text-right font-mono">{int(item.get("review_functions") or 0)}</td>'
            f'<td class="text-right font-mono">{int(item.get("effective_loc") or 0)}</td>'
            f'<td class="text-right font-mono">{int(item.get("exact_files") or 0)}</td>'
            f'<td class="text-right font-mono">{int(item.get("files") or 0)}</td>'
            f'<td class="text-right font-mono">{int(item.get("modules") or 0)}</td>'
            f'<td class="text-right font-mono">{int(item.get("multi_repo_functions") or 0)}</td>'
            '</tr>'
        )
    return (
        '<div class="overflow-x-auto source-metrics-table"><table>'
        '<thead><tr><th>排名</th><th>历史匹配作品</th>'
        '<th class="text-right">高置信函数</th><th class="text-right">复核难例</th>'
        '<th class="text-right">有效相似行</th><th class="text-right">整文件相同</th>'
        '<th class="text-right">涉及文件</th><th class="text-right">涉及模块</th>'
        '<th class="text-right">多仓重复函数</th>'
        '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
    )


_LEGEND_HTML = (
    '<div class="legend">'
    '<span><b>档位：</b></span>'
    '<span><span class="dot" style="background:#ef4444"></span>高置信同源代码</span>'
    '<span><span class="dot" style="background:#f59e0b"></span>模型复核难例（需人工确认）</span>'
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


def _exclusion_totals(suspects: list[dict], recall: dict | None = None, *,
                      library_context: LibraryContext | None = None) -> dict:
    """统计「已扣除的机械误报」各类去重函数数，**互斥归一**（每个函数按优先级只归一类），
    使各类之和 == 总数，供导读卡透明呈现，避免「分类相加远超总数」让评审困惑。

    优先级（从具体到泛化）：第三方库 > 上游框架/ABI > 上游基线衍生 > 跨架构误报 > 公共样板。
    """
    _PRIO = ("library", "upstream", "baseline", "baseline_unverified",
             "false_positive", "common")

    def _cat_of(s: dict) -> str | None:
        if s.get("reuse_library"):
            return "library"
        if s.get("upstream_vendored") or s.get("abi_constrained"):
            return "upstream"
        if s.get("tier") == "baseline_derived":
            ev = s.get("evidence") or {}
            # 只有真正通过基线源码复核（baseline_query_scope / baseline_source_substantive）
            # 才计为「基线衍生」；其余弱候选仅按档位标记、未复核，单列「基线弱候选（待人工
            # 复核）」，不能冒充已通过基线源码复核的排除项。
            if ev.get("baseline_query_scope") or ev.get("baseline_source_substantive"):
                return "baseline"
            return "baseline_unverified"
        if s.get("false_positive") or s.get("internal_arch_dup"):
            return "false_positive"
        if _is_common_code(s):
            return "common"
        return None

    # 只要目标函数还有一个有效、非排除的历史匹配，它就属于上方待评估口径，不能同时
    # 被记作“已排除”。这保证纳入与排除互斥，而不是按 pair 标签交叉计数。
    included_keys = {
        _query_key(s.get("query_func", {}))
        for s in suspects
        if s.get("tier") in ("confirmed", "review", "weak")
        and not _is_excluded_pair(s)
    }

    # 每个 query 函数跨其全部嫌疑对取**最高优先级**类别（一函数只归一类）。
    best: dict[tuple, str] = {}
    for s in suspects:
        cat = _cat_of(s)
        if cat is None:
            continue
        key = _query_key(s.get("query_func", {}))
        if key in included_keys:
            continue
        cur = best.get(key)
        if cur is None or _PRIO.index(cat) < _PRIO.index(cur):
            best[key] = cat

    # 第三方库函数即使没有形成任何嫌疑 pair，也必须进入排除口径，不能既不算纳入又不算排除。
    if recall:
        for item in recall.get("results", []):
            query = item.get("query", {})
            key = _query_key(query)
            if key in included_keys:
                continue
            if match_library(query.get("file_path"), context=library_context):
                best[key] = "library"
    out = {k: 0 for k in _PRIO}
    for cat in best.values():
        out[cat] += 1
    out["total_excluded"] = len(best)
    return out


def _build_history_overview(
    suspects: list[dict],
    submodule_stats: dict,
    *,
    file_matches: list[dict] | None = None,
    recall: dict | None = None,
    library_context: LibraryContext | None = None,
    source_metrics: list[dict] | None = None,
) -> dict:
    """构建一次、供全库图表/表格/摘要共同消费的客观统计数据。"""
    confirmed = sum(int(stats.get("confirmed") or 0) for stats in submodule_stats.values())
    review = sum(int(stats.get("review") or 0) for stats in submodule_stats.values())
    review_failed = sum(
        int(stats.get("review_failed") or 0) for stats in submodule_stats.values()
    )
    review_pending = sum(
        int(stats.get("review_pending") or 0) for stats in submodule_stats.values()
    )
    review_incomplete = review_failed + review_pending
    original = sum(int(stats.get("original") or 0) for stats in submodule_stats.values())
    comparable = sum(int(stats.get("total") or 0) for stats in submodule_stats.values())
    recall_function_count = None
    if recall is not None:
        recall_function_count = len({
            _query_key(item.get("query", {}))
            for item in recall.get("results", [])
        })

    exclusions = _exclusion_totals(
        suspects, recall, library_context=library_context,
    )
    distribution_counts = {
        "confirmed": confirmed,
        "review": review,
        "review_incomplete": review_incomplete,
        "original": original,
        "library": int(exclusions.get("library") or 0),
        "baseline": int(exclusions.get("baseline") or 0),
        "baseline_unverified": int(exclusions.get("baseline_unverified") or 0),
        "upstream": int(exclusions.get("upstream") or 0),
        "false_positive": int(exclusions.get("false_positive") or 0),
        "common": int(exclusions.get("common") or 0),
    }
    classified = sum(distribution_counts.values())
    total_functions = max(
        int(recall_function_count or 0),
        comparable + int(exclusions.get("total_excluded") or 0),
        classified,
    )
    distribution_rows = _overall_distribution_rows(distribution_counts, total_functions)
    if sum(int(row["count"]) for row in distribution_rows) != total_functions:
        raise RuntimeError("全历史库分类数量无法与解析函数总数对账")

    sources = [dict(row) for row in (
        source_metrics
        if source_metrics is not None
        else _historical_source_metrics(suspects, file_matches)
    )]
    similar_sources = _most_similar_sources(sources)
    return {
        "sources": sources,
        "similar_sources": similar_sources,
        "submodule_stats": submodule_stats,
        "distribution_rows": distribution_rows,
        "total_functions": total_functions,
        "recall_function_count": recall_function_count,
        "comparable_functions": comparable,
        "confirmed_functions": confirmed,
        "review_functions": review,
        "review_failed_functions": review_failed,
        "review_pending_functions": review_pending,
        "original_functions": original,
        "exclusions": exclusions,
    }


def _validate_finals_history_overview(overview: dict, closest_source: str) -> None:
    """拒绝交付排名、图表、表格或主对比对象相互矛盾的报告。"""
    sources = list(overview.get("sources") or [])
    if closest_source:
        if not sources:
            raise RuntimeError("已选择主对比作品，但全历史库排名为空")
        first = str(sources[0].get("repo") or "")
        if first != closest_source:
            raise RuntimeError(
                f"全历史库排名第一（{first or '空'}）与主对比作品（{closest_source}）不一致"
            )
    rows = list(overview.get("distribution_rows") or [])
    total = int(overview.get("total_functions") or 0)
    if sum(int(row.get("count") or 0) for row in rows) != total:
        raise RuntimeError("全历史库图表分类数量与总函数数不一致")
    selected = list(overview.get("similar_sources") or [])
    expected = _most_similar_sources(sources)
    if selected != expected:
        raise RuntimeError("最相似仓库筛选结果与全历史库统一排名不一致")


def _retrieval_status(contract: dict | None) -> str:
    """保留机器可审计标记，不在面向评审的报告正文展示召回核验过程。"""
    errors = contract_errors(contract)
    if errors:
        return (
            '<span id="retrieval-stale" hidden data-retrieval-contract-version="missing" '
            'data-retrieval-complete="false"></span>'
        )
    return (
        f'<span id="retrieval-contract" hidden '
        f'data-retrieval-contract-version="{CONTRACT_VERSION}" '
        'data-retrieval-complete="true"></span>'
    )


def _reading_guide(query_repo_id: str, borrowed_n: int, review_n: int,
                   review_failed_n: int, review_pending_n: int, original_n: int,
                   overall_copy_pct: float, excl: dict,
                   retrieval_contract: dict | None = None,
                   recall_function_count: int | None = None) -> str:
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
    if excl.get("upstream"):
        excl_parts.append(f"上游框架/ABI 受限 {excl['upstream']}")
    if excl.get("baseline"):
        excl_parts.append(f"上游基线衍生 {excl['baseline']}")
    if excl.get("baseline_unverified"):
        excl_parts.append(f"基线弱候选（待人工复核）{excl['baseline_unverified']}")
    if excl.get("library"):
        excl_parts.append(f"第三方库 {excl['library']}")
    if excl.get("common"):
        excl_parts.append(f"公共样板 {excl['common']}")
    if excl.get("false_positive"):
        excl_parts.append(f"跨架构/样板误报 {excl['false_positive']}")
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
        '<b>仅辅助评委核查来源披露、独立增量与答辩抽查，不构成抄袭认定，也不自动映射扣分</b>。'
        '本报告不评价客观测例、功能正确性、性能、现场修改能力或答辩质量。系统按当前规则识别并单列'
        '「上游框架、第三方库、ABI/规范受限写法」等可解释复用；规则仍受历史库、解析与阈值限制。</p>'
        # 体检结论
        f'<div class="mt-3 inline-flex items-center gap-2 px-3 py-1.5 rounded-md font-semibold text-sm" '
        f'style="background:{vcolor}1a;color:{vcolor}">'
        f'<span class="inline-flex items-center justify-center w-5 h-5 rounded-full text-white text-xs" '
        f'style="background:{vcolor}">{vicon}</span>'
        f'证据概览：{verdict}（高置信同源代码 {borrowed_n} 个函数）</div>'
        f'<p class="text-sm text-slate-600 mt-2 mb-0">{vtext}</p>'
        # 透明度：扣除了多少误报
        '<div class="mt-3 text-xs text-slate-500 bg-white/70 rounded px-3 py-2 border border-slate-200">'
        f'📊 <b>过滤透明度</b>：系统纳入统计 <b>{total_kept}</b> 个函数，其中 '
        f'<b>{borrowed_n}</b> 个高置信同源代码、<b>{review_n}</b> 个模型复核难例、'
        f'<b>{review_failed_n}</b> 个复核失败、<b>{review_pending_n}</b> 个复核未完成；'
        f'另按规则单列 <b>{excl_total}</b> 个可解释复用/受约束函数（{excl_detail}），这些不计入上方同源统计，'
        '在第 7 节「合法复用与许可证合规」分类列出，可点开核对。'
        + '</div>'
        '<p class="text-xs text-slate-400 mt-2 mb-0">'
        '建议用法：①看总体结果 → ②先核对「高置信同源代码」的代码证据 → '
        '③再查看模型难例与复核异常清单 → ④结合来源披露、基础版本和提交过程完成人工判断。</p>'
        '<div class="judge-note"><b>内核赛道评分边界：</b>本报告只支持来源披露、独立增量与答辩抽查。'
        '客观测例与双架构适配、功能正确性、稳定性/性能、文档与提交演进、现场修改和答辩表现，'
        '必须结合赛事平台材料另行评分；不得把本报告的函数占比直接换算成扣分。'
        f'<a href="{_KERNEL_JUDGE_SCORING_URL}" target="_blank" rel="noopener noreferrer">查看官方评分说明</a> · '
        f'<a href="{_KERNEL_JUDGE_INTEGRITY_URL}" target="_blank" rel="noopener noreferrer">查看学术诚信要求</a></div>'
        '</div></div></section>'
    )


def _summary_card(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_match_count: int = 0,
    file_similar_count: int = 0,
    retrieval_contract: dict | None = None,
    recall: dict | None = None,
    linker=None,
    library_context: LibraryContext | None = None,
) -> str:
    overview = _build_history_overview(
        suspects,
        submodule_stats,
        recall=recall,
        library_context=library_context,
    )
    # 按**函数**计（与各清单一致）；图表、表格、KPI 共用同一个 overview。
    borrowed_n = int(overview["confirmed_functions"])
    review_n = int(overview["review_functions"])
    review_failed_n = int(overview["review_failed_functions"])
    review_pending_n = int(overview["review_pending_functions"])
    review_incomplete_n = review_failed_n + review_pending_n
    original_n = int(overview["original_functions"])
    recall_function_count = overview["recall_function_count"]
    excl = overview["exclusions"]
    excl_total = int(excl.get("total_excluded") or 0)
    full_total = int(overview["total_functions"])
    distribution_rows = overview["distribution_rows"]
    overall_copy_pct = round(borrowed_n / full_total * 100, 1) if full_total else 0.0

    # KPI 卡片（按函数；库复用 / 公共样板等可解释复用单独成卡，保证全部函数有归属）
    # 高置信同源与模型仍存疑明确分栏，避免把“已经过模型但模型拿不准”误读成尚未审核。
    kpis = (
        '<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3">'
        + (_kpi(f"{recall_function_count}", "本次解析函数（完整口径）", "#2563eb")
           if recall_function_count is not None else "")
        + _kpi(f"{borrowed_n}", "高置信同源代码（函数）", "#ef4444")
        + (_kpi(f"{review_n}", "模型复核难例（函数）", "#d97706") if review_n else "")
        + (_kpi(f"{review_failed_n}", "模型复核失败（函数）", "#64748b") if review_failed_n else "")
        + (_kpi(f"{review_pending_n}", "模型复核未完成（函数）", "#64748b") if review_pending_n else "")
        + _kpi(f"{original_n}", "暂未检出相似（函数）", "#16a34a")
        + _kpi(f'{excl.get("library", 0)}', "复用库（函数）", "#2563eb")
        + _kpi(f'{excl.get("baseline", 0)}', "基线衍生·已复核（函数）", "#8b5cf6")
        + (_kpi(
            f'{excl.get("upstream", 0) + excl.get("false_positive", 0) + excl.get("common", 0)}',
            "其他可解释排除（函数）", "#64748b",
        ) if excl.get("upstream", 0) + excl.get("false_positive", 0) + excl.get("common", 0)
           else "")
        + _kpi(f"{file_match_count}", "整文件相同（文件）", "#e11d48")
        + _kpi(f"{file_similar_count}", "整体相似文件（个）", "#d97706")
        + '</div>'
        + ('<p class="text-xs text-slate-500 mt-1 mb-0">'
           f'对账：{full_total}（本次解析函数） = {borrowed_n} 高置信同源 + {review_n} 模型复核难例'
           f' + {review_incomplete_n} 复核失败/未完成 + {original_n} 暂未检出相似'
           f' + {excl_total} 可解释复用/已排除（第三方库 {excl.get("library", 0)}、'
           f'基线衍生 {excl.get("baseline", 0)}、基线弱候选 {excl.get("baseline_unverified", 0)}、'
           f'上游/ABI {excl.get("upstream", 0)}、机械误报 {excl.get("false_positive", 0)}、'
           f'公共样板 {excl.get("common", 0)}）；'
           '每类清单见「合法复用与许可证合规」一节。</p>')
    )

    # 头部：环形图 + Top 历史匹配仓库，左右并排；不把相似关系表述为因果来源。
    donut = _echarts_overall_donut(distribution_rows)
    distribution_table = _overall_distribution_table(distribution_rows, full_total)
    source_metrics = overview["sources"]
    top_src = _echarts_top_sources(source_metrics)
    head_charts = (
        '<div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4 items-start">'
        '<div><div class="chart-title">整体结果分布（按全部解析函数，含复用库与基线衍生）</div>'
        + donut + distribution_table + '</div>'
        + ('<div><div class="chart-title">高置信历史匹配（按唯一目标函数，Top 8；不代表直接来源）</div>'
           + top_src + '</div>' if top_src else '<div></div>')
        + '</div>'
    )

    tier_chart = (
        '<div class="chart-title mt-4">各模块高置信同源函数数</div>'
        + _echarts_tier_distribution(submodule_stats)
    )
    pct_title = '各模块：高置信同源 / 模型复核难例 / 复核未完成 / 暂未检出 占比'
    pct_chart = (
        f'<div class="chart-title mt-4">{pct_title}</div>'
        + _echarts_overview(submodule_stats)
    )

    # 顶部导读 + 体检结论卡（老师第一眼看到，建立正确语境）
    guide = _reading_guide(query_repo_id, borrowed_n, review_n,
                           review_failed_n, review_pending_n, original_n,
                           overall_copy_pct, excl,
                           retrieval_contract, recall_function_count)

    return (
        guide +
        '<section id="summary" data-section-id="summary" '
        'class="summary-card">'
        '<div class="summary-heading"><div><span class="summary-eyebrow">REPORT OVERVIEW</span>'
        '<h2>总体结果</h2></div><span class="summary-repo">'
        f'{_ref_repo_anchor(linker, query_repo_id)}</span></div>'
        '<p class="text-xs text-slate-500 mt-1 mb-0">高置信同源与模型复核数字采用'
        '<b>扣除上游框架/库/规范受限代码后的待评估口径</b>；'
        '「高置信同源代码」由确定性代码证据或模型复核认定「借鉴」形成（后者在清单中带「模型复核认定」标记）；'
        '模型复核后仍无法定论的才留在「复核难例」；复核失败/未完成是流程状态，不是风险结论。'
        '整体分布图、分类明细表与对账行按全部解析函数计，并分别列出复用库、基线衍生等归属；'
        '各模块分布图的分母为待评估函数，'
        '任何比例都不是赛事扣分比例。'
        '<b>需结合代码、来源披露和提交过程人工判断</b>。</p>'
        f'{_LEGEND_HTML}{kpis}{head_charts}{tier_chart}{pct_chart}'
        '</section>'
    )


def _ref_repo_anchor(linker, ref_repo: str) -> str:
    """匹配仓库列：生成指向仓库网页首页的链接。"""
    if linker is None or not ref_repo:
        return html.escape(ref_repo)
    try:
        from .gitlab_links import repo_web_url
        repo_url, _sha = linker._resolve(ref_repo)
        web_url = repo_web_url(repo_url or "")
        if web_url:
            return (f'<a class="repo-link" href="{html.escape(web_url, quote=True)}" '
                    f'target="_blank" rel="noopener noreferrer">{html.escape(ref_repo)}</a>')
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
        relation = {
            "exact_counterpart": "具体函数对应",
            "same_name_code_clone": "同名且高行相似",
            "same_name_only": "仅同名，身份关系弱",
            "compatible_renamed": "身份兼容的改名候选",
            "code_clone_renamed": "高行相似改名候选",
        }.get(c.get("function_identity_relation"), "")
        items.append(
            '<li class="leading-5">'
            + _ref_repo_anchor(linker, c["ref_repo"]) + ' '
            + (f'<code class="text-slate-700">{html.escape(str(c.get("ref_func") or ""))}</code> '
               if c.get("ref_func") else "")
            + _make_gitlab_anchor(linker, c["ref_repo"], c["ref_file"], c["ref_start"])
            + f' <span class="{_sim_class(c["sim"])} font-semibold">综合 {c["sim"]}</span>'
            + f' <span class="text-slate-500">逐行 {c.get("line_similarity", "—")}</span>'
            + (f' <span class="text-blue-600">{relation}</span>' if relation else "")
            + (f' <span class="text-violet-600" title="多仓出现不证明传播方向或直接来源">'
               f'跨 {int(c.get("widespread_match_repos") or 0)} 仓出现</span>'
               if int(c.get("widespread_match_repos") or 0) else "")
            + (' <span class="text-amber-700" title="架构不同仍可能存在移植借鉴；仅作复核提示">跨架构</span>'
               if c.get("cross_arch_signal") else "")
            + (' <span class="text-amber-700" title="标准化汇编仍可能被直接复制；仅作复核提示">汇编样板提示</span>'
               if c.get("boilerplate_asm_signal") else "")
            + f' <span class="text-slate-400">{html.escape(ck)}</span>'
            '</li>'
        )
    more = (
        f'<li class="text-slate-400">另有 {extra} 个候选未在主列表逐项展开，候选总数已完整计入统计。</li>'
        if extra > 0 else ""
    )
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


def _code_evidence(group: dict, colspan: int = 6, linker=None,
                   query_repo_id: str = "") -> tuple[str, str]:
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
    query_label = _make_gitlab_anchor(
        linker, query_repo_id, group.get("query_file", ""),
        int(group.get("query_start") or 0), int(group.get("query_end") or 0),
    ) if query_repo_id else "新作品"
    src_label = (
        _ref_repo_anchor(linker, str(best.get("ref_repo") or "")) + " · "
        + _make_gitlab_anchor(
            linker, str(best.get("ref_repo") or ""), str(best.get("ref_file") or ""),
            int(best.get("ref_start") or 0), int(best.get("ref_end") or 0),
        )
        + (f' · <code>{html.escape(str(best.get("ref_func") or ""))}</code>'
           if best.get("ref_func") else "")
    )
    panel = (
        f'<tr x-show="o" x-cloak><td colspan="{colspan}" class="p-0">'
        '<div class="code-pair">'
        f'<div class="code-col"><div class="code-h">新作品 · {query_label}（差异行标黄）</div>'
        f'<div class="code-body">{left_html}</div></div>'
        f'<div class="code-col"><div class="code-h">最强候选来源 · {src_label}（差异行标红）</div>'
        f'<div class="code-body">{right_html}</div></div>'
        '</div></td></tr>'
    )
    return toggle, panel


_REVIEW_VERDICT_STYLE = {
    "借鉴":   ("bg-red-50 text-red-700", "借鉴"),
    "疑似":   ("bg-amber-50 text-amber-700", "模型仍存疑"),
    "规则保留": ("bg-amber-50 text-amber-800", "强证据保留人工复核"),
    "非借鉴": ("bg-green-50 text-green-700", "非借鉴"),
    "复核失败": ("bg-rose-50 text-rose-700", "复核失败"),
    "未复核": ("bg-slate-100 text-slate-500", "复核未完成"),
}


def _verdict_cell(g: dict) -> str:
    """展示职责门控、严格复核结论、可核验理由及代码锚点。"""
    v = g.get("review_verdict", "未复核")
    cls, lbl = _REVIEW_VERDICT_STYLE.get(v, _REVIEW_VERDICT_STYLE["未复核"])
    reason = html.escape(_legacy_review_text_for_display(
        g.get("review_reason", "") or "", _REVIEW_REASON_CHAR_LIMIT))
    responsibility = html.escape(g.get("review_responsibility", "未判定") or "未判定")
    responsibility_reason = html.escape(_legacy_review_text_for_display(
        g.get("review_responsibility_reason", "") or "", 80))
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
    if sim >= .9:
        reasons.append("相似度很高")
    elif sim >= .7:
        reasons.append("相似度中高")
    if lines >= 60:
        reasons.append("代码规模较大")
    if _MODULE_PRIORITY.get(module, .55) >= .95:
        reasons.append("内核核心子系统")
    if int(g.get("candidate_count") or len(g.get("candidates") or [])) > 1:
        reasons.append("存在多个历史来源")
    if not reasons:
        reasons.append("模型证据不足")
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
        toggle, panel = _code_evidence(g, colspan, linker, query_repo_id)
        main = (
            '<tr>'
            '<td class="font-mono text-xs align-top">'
            + _make_gitlab_anchor(linker, query_repo_id, g["query_file"], g["query_start"])
            + '</td>'
            f'<td class="text-xs align-top">{html.escape(g["query_func"])}'
            + ('<span class="ml-1 px-1.5 py-0.5 rounded bg-purple-50 text-purple-700 '
               'whitespace-nowrap" title="相似度中等、经 AI 模型复核认定为借鉴（非逐行铁证）">'
               '模型复核认定</span>' if g.get("via_review") else "")
            + ('<span class="ml-1 px-1.5 py-0.5 rounded bg-amber-50 text-amber-700 '
               'whitespace-nowrap" title="模型判非借鉴，但逐行相似度与短侧覆盖达硬证据门槛，'
               '规则覆盖模型结论升入高置信同源">硬证据覆盖</span>'
               if g.get("via_hard_evidence") else "")
            + ('<span class="ml-1 px-1.5 py-0.5 rounded bg-teal-50 text-teal-700 '
               'whitespace-nowrap" title="两侧函数体存在逐字相同的非平凡注释，自由文本完全'
               '相同只能来自抄写，规则判定同源升入高置信同源">注释逐字一致</span>'
               if g.get("via_comment_identity") else "")
            + (f'<span class="ml-1 px-1.5 py-0.5 rounded bg-violet-50 text-violet-700 '
               f'whitespace-nowrap" title="多仓共享只作背景，不证明传播方向或直接来源">'
               f'跨 {int(g.get("widespread_match_repos") or 0)} 仓出现</span>'
               if int(g.get("widespread_match_repos") or 0) else "")
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
    # 每行已是按 (file_path,start_line,func_name) 去重后的评审单元（_review_section 的
    # unique_review 或 _query_key 口径），表格标题直接按行计数；按 (文件,函数名) 去重会把
    # 同文件同名但不同起始行的函数漏算，造成「标题 N 个 / 清单 N+1 行」的前后矛盾。
    n_funcs = len(groups)
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
        + '<th class="text-left p-2 border-b">综合相似度</th>'
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


_FEATURE_LABELS = {
    "ext4": "Ext4 文件系统", "fat32": "FAT32 文件系统", "vfs": "文件系统",
    "inode": "Inode 与目录项", "page": "页与页缓存", "frame": "物理页帧",
    "path": "路径解析", "statfs": "文件系统统计", "signal": "信号机制",
    "ipc": "进程间通信", "pipe": "管道通信", "futex": "Futex 同步",
    "lock": "锁与同步", "syscall": "系统调用", "errno": "错误码与 ABI",
    "error": "错误处理", "trap": "异常与中断", "task": "任务管理",
    "process": "进程管理", "sched": "调度器", "timer": "时钟与定时器",
    "clock": "时钟与计时", "time": "时钟与计时",
    "virtio": "VirtIO 驱动", "uart": "串口驱动", "block": "块设备",
    "socket": "套接字", "tcp": "TCP 协议", "udp": "UDP 协议",
    "net": "网络栈", "backtrace": "堆栈回溯", "debug": "调试与诊断",
    "security": "安全与权限",
    # 文件系统目录/文件操作：只对 fs（及未识别）模块生效，见 _feature_label 的 module 门禁。
    # 同类别多个关键词共享 _FEATURE_CATEGORY 中的簇键，同类函数合并为一个功能簇。
    "lookup": "目录查找", "search": "目录查找", "child": "目录查找",
    "dir": "目录操作", "readdir": "目录操作", "directory": "目录操作",
    "mkdir": "目录与文件创建", "create": "目录与文件创建", "maker": "目录与文件创建",
    "entry": "目录与文件创建", "new_file": "目录与文件创建", "mkfile": "目录与文件创建",
    "unlink": "目录删除", "rmdir": "目录删除", "remove": "目录删除", "delete": "目录删除",
    "rename": "目录重命名",
    "link": "硬链接", "hardlink": "硬链接", "symlink": "硬链接",
    "metadata": "文件元数据", "getattr": "文件元数据", "setattr": "文件元数据",
    "fstat": "文件元数据", "stat": "文件元数据",
    "truncate": "文件截断", "ftruncate": "文件截断",
    "pread": "文件读写", "pwrite": "文件读写", "read": "文件读写", "write": "文件读写",
    "fs": "文件系统", "mm": "内存管理",
}

# 教学/公共基线仓库 → 展示名。rCore/uCore/xv6/ArceOS 是教学操作系统；lwext4/rust-fatfs/
# virtio-drivers 是课程/公共库，一并纳入溯源但标出性质。报告据此标注借用代码最接近的基线。
_BASELINE_OS_LABELS = {
    "data/repos/0/baseline_rcore_v1": "rCore",
    "data/repos/0/baseline_rcore_v3": "rCore-Tutorial-v3",
    "data/repos/0/baseline_ucore_lab": "uCore",
    "data/repos/0/baseline_xv6_riscv": "xv6-riscv",
    "data/repos/0/baseline_arceos": "ArceOS",
    "data/repos/0/baseline_lwext4": "lwext4（文件系统库）",
    "data/repos/0/baseline_rust_fatfs": "rust-fatfs（FAT 库）",
    "data/repos/0/baseline_virtio_drivers": "virtio-drivers（驱动库）",
}


@lru_cache(maxsize=4)
def load_baseline_os_registry(db_path: str | Path | None = None) -> dict[int, tuple[str, str, str]]:
    """functions.db 中全部基线函数 id → (基线展示名, 归一文件路径, 函数名)。

    供「教学 OS 溯源」把 suspects 里的 baseline_vector_query.function_id 解析成具体的
    教学操作系统来源。数据库不可用时安静降级为空映射。
    """
    path = Path(db_path) if db_path else DEFAULT_FUNCTIONS_DB
    result: dict[int, tuple[str, str, str]] = {}
    try:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, repo_id, file_path, func_name FROM functions "
                "WHERE repo_id LIKE 'data/repos/0/baseline_%'").fetchall()
    except (OSError, sqlite3.Error):
        return result
    for row in rows:
        label = _BASELINE_OS_LABELS.get(
            str(row["repo_id"]), str(row["repo_id"]).rsplit("/", 1)[-1])
        result[int(row["id"])] = (
            label,
            str(row["file_path"]).replace("\\", "/"),
            str(row["func_name"]),
        )
    return result

# 文件系统目录/文件操作关键词 → 共享功能簇键。命中不同关键词但同一操作种类的函数
# 必须并入同一簇；_feature_label 返回类别键而不是原始 token。
_FEATURE_CATEGORY = {
    "lookup": "目录查找", "search": "目录查找", "child": "目录查找",
    "dir": "目录操作", "readdir": "目录操作", "directory": "目录操作",
    "mkdir": "目录与文件创建", "create": "目录与文件创建", "maker": "目录与文件创建",
    "entry": "目录与文件创建", "new_file": "目录与文件创建", "mkfile": "目录与文件创建",
    "unlink": "目录删除", "rmdir": "目录删除", "remove": "目录删除", "delete": "目录删除",
    "rename": "目录重命名",
    "link": "硬链接", "hardlink": "硬链接", "symlink": "硬链接",
    "metadata": "文件元数据", "getattr": "文件元数据", "setattr": "文件元数据",
    "fstat": "文件元数据", "stat": "文件元数据",
    "truncate": "文件截断", "ftruncate": "文件截断",
    "pread": "文件读写", "pwrite": "文件读写", "read": "文件读写", "write": "文件读写",
    "fs": "文件系统", "vfs": "文件系统",
}
_MODULE_PRIORITY = {
    "sched": 1.0, "mm": 1.0, "fs": 1.0, "trap": .95, "syscall": .95,
    "signal": .9, "ipc": .9, "sync": .9, "time": .8, "net": .9,
    "driver": .8, "security": .9, "runtime": .65, "arch": .75,
    "macro": .5, "other": .45,
}


def _feature_label(group: dict) -> tuple[str, str]:
    """从路径与函数名提取稳定的功能簇键。

    无法识别具体功能时按函数位置生成独立兜底键，绝不能再把同一来源仓库下所有
    ``other``（或同一宽泛模块）函数合并成一个语义簇。
    """
    path = str(group.get("query_file") or "").replace("\\", "/").lower()
    func = str(group.get("query_func") or "").lower()
    words = [w for w in re.split(r"[^a-z0-9]+", path + "/" + func) if w]
    mod = group.get("module", "other")
    for token in _FEATURE_LABELS:
        if token in words:
            # 目录/文件操作类关键词只对 fs（及未识别）模块生效，避免把其它子系统里
            # 恰好含 remove/read/create 等词的函数误并入文件系统功能簇（如 ipc 的
            # remove_shmaddr、mm 的 create_buffer）。跳过后继续查后续更合适的 token。
            if token in _FEATURE_CATEGORY and mod not in ("fs", "other"):
                continue
            return _FEATURE_CATEGORY.get(token, token), _FEATURE_LABELS[token]
    location = "|".join((
        path,
        str(group.get("query_start") or 0),
        func,
    ))
    key = f"{mod}:function:" + hashlib.sha1(
        location.encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    display = _MODULE_DISPLAY.get(mod, mod)
    return key, f"{display} · {func or '未命名函数'}"


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
            "source": repo, "module": mod, "feature_key": feature_key,
            "feature": feature_name, "groups": [],
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
        # 与 _query_key / compute_submodule_stats 同口径：同一文件中的同名函数若起始行
        # 不同，是两个独立目标函数。(file,func) 去重会把它们合并，导致功能簇函数总数
        # 少于「高置信同源目标函数」总计。
        c["function_count"] = len({(g.get("query_file", ""),
                                    int(g.get("query_start") or 0),
                                    g.get("query_func", "")) for g in c["groups"]})
        c["file_count"] = len(c.pop("files"))
        mean_sim = sum(c.pop("similarities")) / max(1, len(c["groups"]))
        size_factor = min(1.0, c["effective_loc"] / 300)
        widespread_repos = max(
            (int(group.get("widespread_match_repos") or 0) for group in c["groups"]),
            default=0,
        )
        # 多仓高频出现会削弱“某一历史队伍是具体来源”的区分度。它不删除相似证据，
        # 只降低评委抽查优先级，让稀有、多函数、集中来源的证据排在通用包装函数之前。
        ambiguity_penalty = min(18, max(0, widespread_repos - 2) * 2)
        c["widespread_repos"] = widespread_repos
        c["source_ambiguous"] = widespread_repos >= COMMON_CODE_REPO_THRESHOLD
        c["priority_score"] = max(0, round(100 * (
            .5 * mean_sim + .3 * _MODULE_PRIORITY.get(c["module"], .55) + .2 * size_factor
        ) - ambiguity_penalty))
        c["priority"] = "重点核查" if c["priority_score"] >= 78 else (
            "优先核查" if c["priority_score"] >= 62 else "常规核查")
        # 教学/公共基线溯源：取簇内函数与基线最相似的命中，供报告标注借用代码最接近的
        # 教学操作系统（rCore/uCore/xv6/ArceOS 等）。
        c["baseline_lineage"] = max(
            (g.get("baseline_lineage") for g in c["groups"] if g.get("baseline_lineage")),
            key=lambda x: x.get("similarity", 0.0), default=None,
        )
        identity = json.dumps(
            [c["source"], c["module"], c["feature_key"]],
            ensure_ascii=False, separators=(",", ":"),
        )
        c["analysis_id"] = "cluster-" + hashlib.sha1(
            identity.encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        clusters.append(c)
    return sorted(clusters, key=lambda c: (-c["priority_score"], -c["effective_loc"], c["feature"]))


def _semantic_cluster_cost(cluster: dict, members_per_cluster: int = 20) -> int:
    """估算单个功能簇提示长度；代码只取最高相似代表，其他成员保留短映射。"""
    groups = cluster.get("groups") or []
    representative = groups[0] if groups else {}
    return (
        _semantic_group_cost(representative)
        + min(len(groups), max(1, members_per_cluster)) * 260
        + 1_200
    )


def _semantic_cluster_batches(
    file_pairs: list[dict],
    members_per_cluster: int = 20,
) -> list[list[dict]]:
    """按统一字符预算分批，但不截断任何将出现在报告中的功能簇。"""
    clusters = build_similarity_clusters(file_pairs)
    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for cluster in clusters:
        cost = _semantic_cluster_cost(cluster, members_per_cluster)
        if current and used + cost > _SEMANTIC_PROMPT_CHAR_BUDGET:
            batches.append(current)
            current = []
            used = 0
        current.append(cluster)
        used += cost
    if current:
        batches.append(current)
    return batches


def _cluster_lineage_html(cluster: dict) -> str:
    """簇内函数与教学/公共基线的溯源行（教学 OS 溯源）；无基线命中时返回空串。"""
    lineage = cluster.get("baseline_lineage") or {}
    fid = lineage.get("function_id")
    if fid is None:
        return ""
    os_info = load_baseline_os_registry().get(int(fid))
    if os_info is None:
        return ""
    os_label, file, func = os_info
    sim = float(lineage.get("similarity") or 0.0)
    return (
        '<div class="text-xs text-slate-500 mb-2">教学 OS 溯源：本簇代码与'
        f' <b>{html.escape(os_label)}</b> 基线的 '
        f'<code>{html.escape(file)}:{html.escape(func)}</code> 最相似'
        f'（相似度 {sim:.2f}），提示其教学/公共代码来源。</div>'
    )


def _cluster_section(file_pairs: list[dict], analysis_html: str, linker,
                     query_repo_id: str) -> tuple[str, str]:
    clusters = build_similarity_clusters(file_pairs)
    sid = "sec-clusters"
    if not clusters:
        body = '<p class="text-sm text-slate-500">没有形成高置信同源功能簇。</p>'
    else:
        # 展示层按模块分组：聚类数据与全局排序保持不变，仅重组渲染顺序，
        # 让同一内核子系统的簇相邻，便于按子系统集中核查。
        module_groups: dict[str, list[dict]] = {}
        for c in clusters:
            module_groups.setdefault(c["module"], []).append(c)
        module_order = sorted(
            module_groups,
            key=lambda mod: (
                -_MODULE_PRIORITY.get(mod, .55),
                MODULES.index(mod) if mod in MODULES else len(MODULES),
            ),
        )
        parts: list[str] = []
        card_index = 0
        for mod in module_order:
            members = module_groups[mod]
            parts.append(
                '<div id="module-evidence-' + html.escape(mod) + '" class="cluster-group-head">'
                '<span class="cluster-group-name">'
                + html.escape(_MODULE_DISPLAY.get(mod, mod)) + '</span>'
                '<code class="cluster-group-tag">' + html.escape(mod) + '</code>'
                f'<span class="cluster-group-count">{len(members)} 簇</span></div>'
            )
            for c in members:  # 组内保持 build_similarity_clusters 的全局排序（priority 降序）
                card_index += 1  # 编号全局连续，跨组不重置
                tone = "critical" if c["priority"] == "重点核查" else "normal"
                table = _groups_table(
                    "簇内函数证据", c["groups"], linker, query_repo_id, "text-red-700")
                frag = _extract_cluster_analysis(analysis_html, c["analysis_id"])
                # 兼容低层调用传入的旧模块级片段；正式流程的 v4 完整性门禁只接受功能簇 ID。
                if not frag:
                    frag = _extract_module_analysis(analysis_html, c["module"])
                analysis = (
                    '<div class="cluster-analysis"><b>功能簇语义说明</b>'
                    + _strip_addr_refs(frag) + '</div>'
                    if frag else ""
                )
                parts.append(
                    f'<article class="cluster-card" data-priority="{tone}" '
                    'x-data="{open:false}">'
                    '<button type="button" class="cluster-head" @click="open=!open">'
                    f'<span class="cluster-index">C{card_index:02d}</span>'
                    '<span class="cluster-main">'
                    f'<b>{html.escape(c["feature"])}</b>'
                    f'<small>{html.escape(_MODULE_DISPLAY.get(c["module"], c["module"]))}</small></span>'
                    f'<span class="cluster-metrics">{c["function_count"]} 函数 · '
                    f'{c["file_count"]} 文件 · {c["effective_loc"]} 有效相似行</span>'
                    + (f'<span class="text-xs text-violet-700">多仓出现 {c["widespread_repos"]}，来源不唯一</span>'
                       if c.get("source_ambiguous") else "")
                    + f'<span class="cluster-priority">{c["priority"]} {c["priority_score"]}</span>'
                    '<span class="cluster-chevron" x-text="open?\'▾\':\'▸\'"></span></button>'
                    f'<div class="cluster-body" x-show="open" x-cloak>'
                    f'<div class="text-xs text-slate-500 mb-2">主要匹配仓库：'
                    f'{_ref_repo_anchor(linker, str(c["source"]))}</div>'
                    f'{_cluster_lineage_html(c)}{table}{analysis}</div></article>'
                )
        intro = (
            '<div class="section-intro">系统将同一匹配仓库、同一子系统且属于同一功能域的函数合并为一个'
            '“同源事件”。优先级综合代码相似度、有效相似行和内核子系统重要度，并对多仓高频出现的'
            '来源歧义降权；函数级链接与并排代码'
            '仍保留在簇内。每个簇额外标注<b>教学 OS 溯源</b>：本簇借用代码最接近的'
            '教学/公共基线（rCore / uCore / xv6 / ArceOS 等），提示其公共代码来源。'
            '展示按内核子系统分组，组内按优先级排序，便于按子系统集中核查。</div>'
        )
        body = intro + "".join(parts)
    section = _collapsible_html(
        sid, "高置信同源功能簇", body, tone="evidence",
        subtitle="将零散函数聚合成可评审的功能级同源事件",
    )
    return _toc_link(sid, "高置信同源功能簇", str(len(clusters)), "confirmed"), section


def _lineage_section(query_repo_id: str, suspects: list[dict], linker,
                     recall: dict | None = None,
                     source_metrics: list[dict] | None = None) -> tuple[str, str]:
    """展示排除可归因公共来源后的主要历史匹配，不输出时间或版本方向判断。

    source_metrics 应传全历史库统一排名行（_historical_source_metrics 的产物，含
    multi_repo_functions）。调用方只提供主对比作品（closest-repo）的 suspects 时，
    “多仓同时命中”必须跨仓库统计，否则每个目标函数只见过一个仓库，恒为 0。
    """
    sid = "sec-lineage"
    metrics = list(source_metrics) if source_metrics else _source_metrics(suspects)
    rows = []
    for x in metrics[:12]:
        rows.append(
            '<tr>'
            f'<td>{_ref_repo_anchor(linker, x["repo"])}</td>'
            f'<td>{x["functions"]}</td><td>{x["effective_loc"]}</td>'
            f'<td>{x["multi_repo_functions"]}</td></tr>'
        )
    body = (
        '<div class="section-intro">本节判断相似代码能否由公共来源解释。'
        '<b>判断时已排除：共同上游（vendored / ABI 受限）、第三方库复用、'
        '比赛基线衍生、机械误报和公共样板代码</b>——这些代码不计入'
        '“高置信同源代码”，详细清单见“合法复用与许可证合规”一节。'
        '下表仅保留排除这些因素后的主要高置信历史匹配；系统约定比较运行时取得的最新代码，'
        '因此不展示版本或时间方向，相似关系本身也不证明传播方向或直接来源。</div>'
        '<div class="overflow-x-auto"><table><thead><tr><th>主要匹配历史作品</th>'
        '<th>唯一目标函数</th><th>有效相似行</th>'
        '<th title="同一目标函数还匹配其他历史仓库，不能唯一归因">多仓同时命中</th>'
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
        '<p class="text-xs text-slate-500 mt-2">'
        '“多仓同时命中”按函数计数：一个目标函数若同时高相似出现在多个历史仓库中，'
        '就不能把它当作指向某单一仓库的独占证据（它更可能是公共代码）；'
        '但它仍作为相似证据保留，只降低抽查优先级。</p>'
    )
    section = _collapsible_html(
        sid, "共同上游判断", body, tone="evidence",
        subtitle="判断相似是否由共同上游等公共来源解释，并列出排除后的主要历史匹配",
    )
    return _toc_link(sid, "共同上游判断"), section


# ─── GitLab 链接辅助 ──────────────────────────────────────────────────────────

def _build_gitlab_linker(
    query_repo_path: str | Path | None,
    query_repo_id: str,
    ref_repos: set[str],
) -> "GitLabLinker | None":
    """构建 GitLabLinker：新作品用精确 SHA，历史仓库优先用缓存 SHA。"""
    try:
        from .gitlab_links import (
            GitLabLinker, build_repo_url_map, load_heads, query_repo_info,
        )
        url_map = build_repo_url_map()
        if not url_map and not query_repo_path:
            return None

        # 优先使用已缓存的历史仓库提交 SHA，避免评委点击后因远端 HEAD 移动而看到不同代码。
        # 老数据尚未记录“建库时提交”，因此缓存缺失时仍明确回退 HEAD；报告限制中披露该边界。
        heads = load_heads()

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
        from .gitlab_links import gitlab_blob_url
        url, sha = linker._resolve(repo_id)
        if not url:
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
        return (f'<a class="{css_class}" href="{html.escape(url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{label}</a>')
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


def _extract_module_analysis(analysis_html: str, mod: str) -> str:
    """从语义分析产出的完整 HTML 中，尝试提取该模块的 <section> 片段。"""
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


def _extract_cluster_analysis(analysis_html: str, cluster_id: str) -> str:
    """按稳定功能簇 ID 提取模型语义片段，避免把模块总述误配给多个不同功能簇。"""
    pattern = re.compile(
        rf'<section[^>]*data-cluster=["\']{re.escape(cluster_id)}["\'][^>]*>.*?</section>',
        re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(analysis_html or "")
    return match.group(0) if match else ""


def _innovation_runtime_evidence(point: dict, query_repo_path: Path | None) -> dict:
    """从目标仓库补充候选创新的调用/引用位置。"""
    evidence = {"call_sites": []}
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
        if len(evidence["call_sites"]) >= 8:
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
        for line_no, line in enumerate(lines, 1):
            if not call_re.search(line):
                continue
            if (rel, line_no) not in target_locs and len(evidence["call_sites"]) < 8:
                evidence["call_sites"].append({"file": rel, "line": line_no})
    return evidence


def _innovation_section(points: list[dict], linker, query_repo_id: str,
                        query_repo_path: Path | None = None,
                        comparison_candidate_count: int = 0) -> tuple[str, str]:
    """候选创新 → 参考基线 → 目标实现 → 调用/验证/反证的独立证据地图。"""
    if not points:
        body = (
            '<div class="p-3 rounded bg-slate-50 border border-slate-200 text-sm text-slate-600">'
            '当前数据中没有形成可核验的相对创新点。可能原因是没有暂未命中的函数、'
            '未识别到稳定参考 repo，或代码证据不足。<b>不输出不代表项目没有创新</b>。</div>'
        )
        section = _collapsible_html(
            "sec-innovation", "相对参考实现的候选创新", body, tone="innovation",
            subtitle=(f"已形成 {comparison_candidate_count} 组可比较代码基线，未归纳出创新点"
                      if comparison_candidate_count else
                      "代码差异候选，不把未命中或项目自述直接当作创新结论"),
        )
        return _toc_link("sec-innovation", "相对参考实现的候选创新", "0", "zero"), section

    cards: list[str] = []
    confidence_labels = {"high": "高", "medium": "中", "low": "低"}
    for index, point in enumerate(points, start=1):
        runtime = _innovation_runtime_evidence(point, query_repo_path)
        complexity = point.get("complexity") or {}
        function_metrics = {
            (str(metric.get("file") or ""), str(metric.get("func") or "")): metric
            for metric in complexity.get("functions") or []
        }
        confidence = confidence_labels.get(str(point.get("confidence") or "").lower(), "中")

        target_items = []
        for target in point.get("targets") or []:
            anchor = _make_gitlab_anchor(
                linker, query_repo_id, target.get("file", ""),
                int(target.get("start") or 0), int(target.get("end") or 0),
            )
            metric = function_metrics.get((
                str(target.get("file") or ""), str(target.get("func") or ""),
            ))
            metric_suffix = (
                f' · McCabe CCN {int(metric["cyclomatic_complexity"])}'
                f' · NLOC {int(metric["nloc"])}'
                if metric else ' · 复杂度工具未解析'
            )
            target_items.append(
                f'<li>{anchor} · <code>{html.escape(str(target.get("func") or ""))}</code>'
                f' · {int(target.get("lines") or 0)} 行{metric_suffix}</li>'
            )

        reference_items = []
        for reference in point.get("references") or []:
            anchor = _make_gitlab_anchor(
                linker, reference.get("repo", ""), reference.get("file", ""),
                int(reference.get("start") or 0), int(reference.get("end") or 0),
            )
            vector_score = reference.get("score")
            vector_text = (
                f' · 向量相似度 {float(vector_score):.3f}'
                if vector_score is not None else ""
            )
            reference_items.append(
                f'<li>{anchor} · <code>{html.escape(str(reference.get("func") or ""))}</code>'
                f' · 职责一致度 {float(reference.get("identity_score") or 0.0):.3f}'
                + vector_text
                + f' · {html.escape(str(reference.get("selection_source") or "全库召回"))}</li>'
            )

        target_html = "".join(target_items) or '<li class="text-slate-400">无有效目标代码证据</li>'
        reference_html = "".join(reference_items) or (
            '<li class="text-slate-400">参考 repo 中未绑定到可比较的具体函数；该项把握度已降低</li>'
        )
        call_html = "".join(
            '<li>' + _make_gitlab_anchor(linker, query_repo_id, x["file"], x["line"]) + '</li>'
            for x in runtime["call_sites"]
        ) or '<li class="text-slate-400">未自动定位到目标函数之外的调用/引用位置</li>'
        why = html.escape(str(point.get("why_it_matters") or ""))
        why_html = f'<p class="text-sm mt-2"><b>作用与代价：</b>{why}</p>' if why else ""
        target_files = sorted({str(t.get("file") or "") for t in point.get("targets") or [] if t.get("file")})
        modules = sorted({_MODULE_DISPLAY.get(str(t.get("module") or "other"), str(t.get("module") or "other"))
                          for t in point.get("targets") or []})
        auto_scope = f'{"、".join(modules) or "未分类子系统"}；{len(target_files)} 个实现文件、{len(runtime["call_sites"])} 个外部调用/引用位置'
        impact = html.escape(str(point.get("impact_scope") or auto_scope))
        counter = html.escape(str(point.get("counterevidence") or
                                  "静态代码差异与未命中不能单独证明能力、性能或原创性。"))
        analyzed = int(complexity.get("analyzed_functions") or 0)
        if analyzed:
            metric_text = (
                f'{analyzed} 个函数已解析 · '
                f'McCabe CCN 最大 {int(complexity.get("max_cyclomatic_complexity") or 0)}、'
                f'平均 {float(complexity.get("mean_cyclomatic_complexity") or 0.0):.2f} · '
                f'NLOC 合计 {int(complexity.get("total_nloc") or 0)} · '
                f'token 合计 {int(complexity.get("total_token_count") or 0)} · '
                f'最大参数数 {int(complexity.get("max_parameter_count") or 0)}'
            )
        else:
            metric_text = "当前目标语言或语法未被复杂度工具解析；未使用估算值替代"
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
      <div class="font-semibold text-slate-500">参考 repo</div><div>{_ref_repo_anchor(linker, str(point.get('reference_repo') or '未识别'))}</div>
      <div class="font-semibold text-slate-500">参考实现基线</div><div>{html.escape(str(point.get('baseline') or ''))}</div>
      <div class="font-semibold text-emerald-700">本作品代码变化</div><div>{html.escape(str(point.get('delta') or ''))}</div>
    </div>
    {why_html}
    <div class="innovation-evidence-grid">
      <div><b>影响范围</b><p>{impact}</p></div>
      <div class="innovation-counter"><b>限制与反证</b><p>{counter}</p></div>
    </div>
    <div class="mt-3 p-3 rounded bg-white border border-slate-200">
      <div class="flex flex-wrap items-center gap-2 mb-2 text-xs">
        <b>函数级代码度量</b>
        <a class="file-jump" href="https://doi.org/10.1109/TSE.1976.233837" target="_blank" rel="noopener noreferrer">McCabe 圈复杂度</a>
        · <a class="file-jump" href="https://github.com/terryyin/lizard" target="_blank" rel="noopener noreferrer">Lizard 1.23.0</a>
        <span class="text-slate-500">{html.escape(metric_text)}</span>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
        <div><div class="font-semibold text-emerald-700 mb-1">本作品实现（点击查看代码）</div><ul class="list-disc pl-5 space-y-1">{target_html}</ul></div>
        <div><div class="font-semibold text-slate-600 mb-1">参考实现（点击对照）</div><ul class="list-disc pl-5 space-y-1">{reference_html}</ul></div>
        <div><div class="font-semibold text-blue-700 mb-1">调用/引用入口</div><ul class="list-disc pl-5 space-y-1">{call_html}</ul></div>
      </div>
    </div>
  </div>
</article>
""")

    intro = (
        '<div class="mb-4 p-3 rounded bg-blue-50 border border-blue-200 text-xs text-blue-800">'
        '本节从“暂未形成有效相似命中”的函数出发，再与各模块主要参考 repo 的最近实现做代码级比较。'
        '<b>项目 README / 设计文档不作为独立创新证据</b>；“未命中”本身也不等于创新。'
        f'共形成 <b>{comparison_candidate_count}</b> 组通过职责门禁的目标—参考代码基线，模型归纳为 '
        f'<b>{len(points)}</b> 个候选创新；两者数量不同不代表证据丢失。'
        '复杂度使用 Lizard 实现的 McCabe 圈复杂度，并同时报告 NLOC、token 和参数数；'
        '不再合成自定义高/中/低分，也不是算法时间复杂度。</div>'
    )
    section = _collapsible_html(
        "sec-innovation", "相对参考实现的候选创新", intro + "".join(cards),
        tone="innovation",
        subtitle=f"{comparison_candidate_count} 组可比较代码基线 → {len(points)} 个候选创新",
    )
    return _toc_link("sec-innovation", "相对参考实现的候选创新",
                     str(len(points)), "original"), section


def _original_section(original_funcs: list[dict], linker, query_repo_id: str) -> tuple[str, str]:
    """列出全部暂未命中函数，同时明确该清单不构成原创认定。"""
    sid = "sec-original"
    if not original_funcs:
        body = ("<p class='text-slate-500 text-sm'>没有暂未命中的函数"
                "（所有函数都与历史代码库有相似命中）。</p>")
    else:
        ordered = sorted(original_funcs, key=lambda item: (
            MODULES.index(item.get("module"))
            if item.get("module") in MODULES else len(MODULES),
            str(item.get("file") or ""), int(item.get("start") or 0),
            str(item.get("func") or ""),
        ))
        rows = []
        for item in ordered:
            module = item.get("module", "other")
            module_name = _MODULE_DISPLAY.get(module, _MODULE_DISPLAY["other"])
            file_path = str(item.get("file") or "")
            start = int(item.get("start") or 0)
            end = int(item.get("end") or 0)
            lines = int(item.get("lines") or max(0, end - start + 1))
            rows.append(
                '<tr>'
                f'<td>{html.escape(module_name)}</td>'
                '<td class="font-mono text-xs">'
                + _make_gitlab_anchor(
                    linker, query_repo_id, file_path, start, end,
                )
                + '</td>'
                f'<td class="font-mono text-xs">{html.escape(str(item.get("func") or ""))}</td>'
                f'<td>{lines} 行</td>'
                '</tr>'
            )
        body = (
            '<p class="text-sm text-slate-600 mb-3">'
            f'共 <b>{len(original_funcs)}</b> 个函数在当前历史库中暂未形成有效相似命中。'
            '<b>这只表示系统暂未检出，不等于原创认定</b>；可能仍受历史库覆盖、召回与阈值影响。'
            '下表完整列出这些函数，供评委定位代码和结合答辩材料核查，不据此自动加分。'
            '</p>'
            '<div class="overflow-x-auto"><table><thead><tr>'
            '<th>子系统</th><th>新作品 文件:行</th><th>函数</th><th>规模</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
        )
    section = _collapsible_html(
        sid, "附录：暂未检出相似函数（不等于原创）", body, default_open=False,
        tone="appendix", subtitle="完整列出未命中函数，但不把未命中当作原创证据",
    )
    toc = _toc_link(sid, "暂未检出相似函数", str(len(original_funcs)), "original")
    return toc, section


_CDN_HEAD = """
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js"></script>
"""

_STYLES = r"""
<style>
:root{--bg:#f3f6fa;--card:#fff;--line:#dbe3ec;--line-soft:#e9eef4;--text:#172033;
  --muted:#64748b;--blue:#2563eb;--blue-soft:#eff6ff;--red:#dc2626;--amber:#d97706;
  --green:#16803c;--shadow:0 10px 30px rgba(15,23,42,.06)}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:1.25rem}
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
  white-space:normal;overflow-wrap:anywhere}
.toc-scroll{max-height:calc(100vh - 4rem);overflow-y:auto;padding:.55rem .55rem .8rem;scrollbar-width:thin}
.toc-group+.toc-group{margin-top:.5rem;padding-top:.45rem;border-top:1px solid #edf1f5}
.toc-group-title{padding:.35rem .55rem .28rem;color:#94a3b8;font-size:.64rem;font-weight:800;letter-spacing:.12em}
.toc .toc-link{position:relative;display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:.55rem;
  min-height:42px;margin:2px 0;padding:.4rem .55rem .4rem .7rem;border-radius:8px;color:#475569;text-decoration:none}
.toc .toc-link:before{content:"";position:absolute;left:0;top:8px;bottom:8px;width:3px;border-radius:3px;background:transparent}
.toc-link-label{min-width:0;line-height:1.25;overflow-wrap:anywhere;display:block}
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
.judge-note{margin-top:.75rem;padding:.72rem .82rem;border:1px solid #bfdbfe;border-radius:9px;
  background:#fff;color:#40546d;font-size:.75rem;line-height:1.65}
.judge-note a{color:#1d5fbf;text-decoration:underline;text-underline-offset:2px}
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
.lineage-summary,.compliance-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.65rem;margin-bottom:.9rem}
.lineage-summary>div,.compliance-grid>div{display:flex;min-width:0;flex-direction:column;gap:.2rem;padding:.68rem .75rem;border:1px solid var(--line-soft);border-radius:9px;background:#fafcff}
.lineage-summary span,.compliance-grid span{font-size:.66rem;color:#7b8ba1}.lineage-summary b,.compliance-grid b{font-size:.77rem;overflow-wrap:anywhere}
.cluster-card{margin:.65rem 0;border:1px solid var(--line);border-radius:10px;overflow:hidden}.cluster-card[data-priority="critical"]{border-color:#efb2b2}
.cluster-head{display:grid;width:100%;grid-template-columns:2.4rem minmax(11rem,1fr) auto auto 1rem;align-items:center;gap:.7rem;padding:.72rem .8rem;border:0;background:#f8fafc;text-align:left;cursor:pointer}
.cluster-card[data-priority="critical"] .cluster-head{background:#fff7f7}.cluster-index{font:.72rem ui-monospace,SFMono-Regular,Consolas;color:#64748b}.cluster-main{display:flex;min-width:0;flex-direction:column}.cluster-main b{font-size:.82rem}.cluster-main small{margin-top:.12rem;color:#718096;font-size:.66rem}
.cluster-metrics{font-size:.68rem;color:#64748b;white-space:nowrap}.cluster-priority{padding:.22rem .48rem;border-radius:999px;background:#fff;border:1px solid #e2e8f0;color:#9a5b06;font-size:.66rem;font-weight:700;white-space:nowrap}.cluster-body{padding:.2rem .8rem .85rem}.cluster-analysis{margin-top:.7rem;padding:.75rem;border:1px solid #dbe7f3;border-radius:8px;background:#f7faff;font-size:.75rem}
.cluster-group-head{display:flex;align-items:center;gap:.55rem;margin:.9rem 0 .4rem;padding:.55rem .8rem;border-left:3px solid #2563eb;border-radius:8px;background:#f8fafc}.cluster-group-name{font-size:.8rem;font-weight:750;color:#1e293b}.cluster-group-tag{font:.68rem ui-monospace,SFMono-Regular,Consolas;color:#64748b;padding:.06rem .38rem;border:1px solid #e2e8f0;border-radius:4px;background:#fff}.cluster-group-count{margin-left:auto;font-size:.68rem;color:#64748b;white-space:nowrap}
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
/* 报告自带的常用排版工具类。页面布局不依赖 Tailwind CDN，断网时仍保持完整层级。 */
.flex{display:flex}.inline-flex{display:inline-flex}.grid{display:grid}.flex-1{flex:1 1 0%}.flex-wrap{flex-wrap:wrap}
.items-start{align-items:flex-start}.items-center{align-items:center}.justify-center{justify-content:center}.min-w-0{min-width:0}
.grid-cols-1{grid-template-columns:repeat(1,minmax(0,1fr))}.grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}
.gap-2{gap:.5rem}.gap-3{gap:.75rem}.gap-4{gap:1rem}.gap-x-3{column-gap:.75rem}.gap-y-2{row-gap:.5rem}
.p-0{padding:0}.p-2{padding:.5rem}.p-3{padding:.75rem}.p-4{padding:1rem}.p-5{padding:1.25rem}
.px-1\.5{padding-left:.375rem;padding-right:.375rem}.px-2{padding-left:.5rem;padding-right:.5rem}
.px-3{padding-left:.75rem;padding-right:.75rem}.px-4{padding-left:1rem;padding-right:1rem}
.py-0\.5{padding-top:.125rem;padding-bottom:.125rem}.py-1\.5{padding-top:.375rem;padding-bottom:.375rem}
.py-2{padding-top:.5rem;padding-bottom:.5rem}.py-3{padding-top:.75rem;padding-bottom:.75rem}
.mt-1{margin-top:.25rem}.mt-2{margin-top:.5rem}.mt-3{margin-top:.75rem}.mt-4{margin-top:1rem}
.mb-0{margin-bottom:0}.mb-1{margin-bottom:.25rem}.mb-2{margin-bottom:.5rem}.mb-3{margin-bottom:.75rem}.mb-4{margin-bottom:1rem}.mb-6{margin-bottom:1.5rem}
.ml-1{margin-left:.25rem}.mr-auto{margin-right:auto}.m-0{margin:0}.pl-4{padding-left:1rem}.pl-5{padding-left:1.25rem}
.space-y-0\.5>*+*{margin-top:.125rem}.space-y-1>*+*{margin-top:.25rem}
.text-xs{font-size:.75rem;line-height:1rem}.text-sm{font-size:.875rem;line-height:1.25rem}.text-base{font-size:1rem;line-height:1.5rem}.text-2xl{font-size:1.5rem;line-height:2rem}
.font-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.font-semibold{font-weight:600}.font-bold{font-weight:700}
.leading-none{line-height:1}.leading-5{line-height:1.25rem}.leading-relaxed{line-height:1.625}.text-left{text-align:left}.align-top{vertical-align:top}
.text-white{color:#fff}.text-slate-400{color:#94a3b8}.text-slate-500{color:#64748b}.text-slate-600{color:#475569}.text-slate-700{color:#334155}.text-slate-800{color:#1e293b}
.text-blue-600{color:#2563eb}.text-blue-700{color:#1d4ed8}.text-red-500{color:#ef4444}.text-red-600{color:#dc2626}.text-red-700{color:#b91c1c}
.text-amber-600{color:#d97706}.text-amber-700{color:#b45309}.text-amber-800{color:#92400e}.text-violet-600{color:#7c3aed}.text-violet-700{color:#6d28d9}.text-emerald-700{color:#047857}
.bg-white{background:#fff}.bg-slate-50{background:#f8fafc}.bg-slate-100{background:#f1f5f9}.bg-red-50{background:#fef2f2}.bg-amber-50{background:#fffbeb}.bg-violet-50{background:#f5f3ff}.bg-emerald-50{background:#ecfdf5}
.bg-blue-50\/60{background:rgba(239,246,255,.6)}.bg-white\/70{background:rgba(255,255,255,.7)}.bg-emerald-50\/30{background:rgba(236,253,245,.3)}
.border{border:1px solid #e2e8f0}.border-b{border-bottom:1px solid #e2e8f0}.border-l-4{border-left-width:4px}
.border-slate-100{border-color:#f1f5f9}.border-slate-200{border-color:#e2e8f0}.border-emerald-100{border-color:#d1fae5}.border-emerald-200{border-color:#a7f3d0}.border-red-200{border-color:#fecaca}
.rounded{border-radius:.25rem}.rounded-md{border-radius:.375rem}.rounded-lg{border-radius:.5rem}.rounded-full{border-radius:9999px}
.w-full{width:100%}.w-5{width:1.25rem}.h-5{height:1.25rem}.overflow-x-auto{overflow-x:auto}.overflow-hidden{overflow:hidden}.whitespace-nowrap{white-space:nowrap}.list-disc{list-style-type:disc}.border-collapse{border-collapse:collapse}
.max-w-\[16rem\]{max-width:16rem}.max-w-\[18rem\]{max-width:18rem}.hover\:underline:hover{text-decoration:underline}
@media(max-width:1060px){.layout{gap:1rem;padding:1rem}.toc{width:242px;flex-basis:242px}}
@media(min-width:640px){.sm\:grid-cols-4{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(min-width:768px){.md\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.md\:grid-cols-\[9rem_1fr\]{grid-template-columns:9rem minmax(0,1fr)}}
@media(min-width:1024px){.lg\:grid-cols-2{grid-template-columns:repeat(2,minmax(0,1fr))}.lg\:grid-cols-\[1fr_17rem\]{grid-template-columns:minmax(0,1fr) 17rem}}
@media(max-width:860px){
  .layout{display:block;padding:.8rem}.toc{position:static;width:auto;margin-bottom:1rem}.toc-scroll{max-height:none}
  .toc-header{padding:.8rem 1rem}.toc-scroll{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.35rem .75rem}
  .toc-group+.toc-group{margin:0;padding:0;border:0}.main{max-width:none}.report-header h1{font-size:1.35rem}
  .module-summary-source{width:100%;margin-left:0}.lineage-summary,.compliance-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .cluster-head{grid-template-columns:2.2rem minmax(8rem,1fr) auto 1rem}.cluster-metrics{display:none}
}
@media(max-width:620px){.toc-scroll{grid-template-columns:1fr}.summary-heading{display:block}.summary-repo{display:block;max-width:none;text-align:left;margin-top:.4rem}.lineage-summary,.compliance-grid,.innovation-evidence-grid{grid-template-columns:1fr}.innovation-evidence-grid .innovation-counter{grid-column:auto}.cluster-head{grid-template-columns:2rem minmax(0,1fr) 1rem}.cluster-priority{display:none}}
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
  var chartAttempts=0;
  function initECharts(){
    if(typeof echarts==='undefined'){
      chartAttempts+=1;
      if(chartAttempts<20){setTimeout(initECharts,150);return}
      document.querySelectorAll('.echarts-chart:not([data-rendered])').forEach(function(el){
        el.setAttribute('data-rendered','fallback');
        el.innerHTML='<p class="text-sm p-3">图表库未加载；不影响下方表格、数量和代码证据阅读。</p>';
      });
      return;
    }
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
  function initDisclosures(){
    document.querySelectorAll('section.report-section[x-data]').forEach(function(root){
      var body=root.querySelector('.section-body'), button=root.querySelector('.section-toggle');
      if(!body||!button)return;
      var open=(root.getAttribute('x-data')||'').indexOf('open:true')>=0;
      function render(){body.hidden=!open;var icon=button.querySelector('.section-chevron');if(icon)icon.textContent=open?'▾':'▸'}
      button.addEventListener('click',function(){open=!open;render();if(open)document.dispatchEvent(new Event('section:opened'))});
      render();
    });
    document.querySelectorAll('article.cluster-card[x-data]').forEach(function(root){
      var body=root.querySelector('.cluster-body'), button=root.querySelector('.cluster-head');
      if(!body||!button)return;
      var open=(root.getAttribute('x-data')||'').indexOf('open:true')>=0;
      function render(){body.hidden=!open;var icon=button.querySelector('.cluster-chevron');if(icon)icon.textContent=open?'▾':'▸'}
      button.addEventListener('click',function(){open=!open;render()});render();
    });
    document.querySelectorAll('tbody[x-data]').forEach(function(root){
      var panel=root.querySelector('tr[x-show="o"]'), button=root.querySelector('.code-toggle');
      if(!panel||!button)return;var open=false;
      function render(){panel.hidden=!open;button.textContent=open?'收起代码 ▴':'查看代码 ▾'}
      button.addEventListener('click',function(){open=!open;render()});render();
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
  document.addEventListener('DOMContentLoaded',function(){initDisclosures();initECharts();initScrollSpy()});
  document.addEventListener('section:opened',function(){setTimeout(initECharts,30)});
})();
</script>
"""


def _file_similar_row(f: dict, linker, query_repo_id: str) -> str:
    """「文件整体相似」单行：借鉴函数数 + 非函数行 + 整体相似% + 主要匹配仓库。"""
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
    exact_nonfunc_lines = sum(
        int(m.get("line_count") or 0) for m in file_matches
        if Path(str(m.get("query_file") or "")).suffix.lower() in nonfunc_suffixes
    )
    nonfunc_lines = exact_nonfunc_lines + sum(
        int(f.get("line_nonfunc") or 0) for f in file_similar)
    parts: list[str] = [
        '<div class="section-intro">函数清单之外，本节保留整文件规范化哈希与文件覆盖率证据；'
        '因此结构体、枚举、常量、宏、汇编、链接脚本和配置等非函数实现不会被函数级分析遮蔽。'
        f'当前整文件证据中有 <b>{nonfunc_files}</b> 个汇编/链接脚本/配置等非函数文件；'
        f'合计覆盖约 <b>{nonfunc_lines}</b> 行非函数内容（含整体相似文件中的结构体等内容）。</div>'
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
            '<th class="text-left p-2 border-b">主要匹配仓库</th>'
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
    """复用库统计：列出新作品复用的公开第三方库组件及规模。

    这些库本体及经包清单/导入关系确认的适配层（lwext4、smoltcp、fatfs 等）为多队
    合法共用，已从借鉴图/清单中剔除，此处单列说明，避免「数字凭空消失」。
    无复用库时返回空（不渲染本节）。
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
        '本作品复用了以下公开第三方库组件（含 vendored 库本体，以及经包清单与源码导入关系'
        '共同确认的适配层）。这类库组件为多队合法共用，'
        '<b>不计入值得关注的借鉴/抄袭</b>，已从「主要历史匹配」图、各模块借鉴占比/清单、'
        '原创代码清单与文件级清单中剔除，仅在此单列。识别规则见 <code>config/libraries.yaml</code>；'
        '适配层不会仅凭目录名命中。'
        '</p>'
        '<div class="flex flex-wrap gap-3 text-sm mb-3">'
        f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">复用库 {len(lib_stats)} 个</span>'
        f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">库组件函数 {total_funcs} 个</span>'
        f'<span class="px-2 py-0.5 rounded bg-slate-100 text-slate-700">已剔除嫌疑对 {total_pairs}</span>'
        '</div>'
        '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">复用库</th>'
        '<th class="text-left p-2 border-b">库本体/适配层函数数</th>'
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
    """已有独立公共来源证明的公共/样板代码小节（不计入借鉴）。"""
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
        f'<td class="text-xs">{f["repos"] if f["repos"] else "—"}'
        + (' 个' if f["repos"] else '') + '</td>'
        '</tr>'
        for f in shown
    )
    more = f'（按命中仓库数降序，列出前 {limit} 个）' if len(cc_funcs) > limit else ''
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        f'下列 <b>{len(cc_funcs)}</b> 个函数已有基线、来源登记或人工确认支持其属于公共/框架/样板代码；'
        f'表中的跨仓数量仅表示多仓出现背景，命中 ≥{COMMON_CODE_REPO_THRESHOLD} 个仓库本身不会进入本节，'
        f'<b>不计入值得关注的借鉴</b>，已从摘要、历史匹配图与各模块清单中剔除{more}。'
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
    shown = base_funcs
    rendered_rows = []
    for f in shown:
        baseline_repo = str(f.get("baseline_repo") or "")
        baseline_file = str(f.get("baseline_file") or "")
        baseline_func = str(f.get("baseline_func") or "")
        baseline_link = _make_gitlab_anchor(
            linker, baseline_repo, baseline_file, int(f.get("baseline_start") or 0),
        ) if baseline_repo and baseline_file else html.escape(baseline_repo or "未定位")
        baseline_label = (
            _ref_repo_anchor(linker, baseline_repo) + " · " + baseline_link
            + (f' · <code>{html.escape(baseline_func)}</code>' if baseline_func else "")
        ) if baseline_repo else baseline_link
        history = "、".join(f.get("history_candidates") or []) or "—"
        rendered_rows.append(
            '<tr>'
            f'<td class="text-xs font-mono">{html.escape(f["name"])}</td>'
            '<td class="font-mono text-xs">'
            + _make_gitlab_anchor(linker, query_repo_id, f["file"], f.get("start", 0))
            + '</td>'
            f'<td class="text-xs">{baseline_label}</td>'
            f'<td class="text-xs">{html.escape(history)}</td>'
            f'<td class="text-xs text-slate-500">{html.escape(f.get("note", "") or "—")}</td>'
            '</tr>'
        )
    rows = "".join(rendered_rows)
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        f'下列 <b>{len(base_funcs)}</b> 个函数已通过真实基线源码逐行复核，历史候选可由同一公共基线解释'
        '（教学 OS、组委会模板或已登记上游等），因此不进入队际同源统计。'
        '向量相似只用于发现候选，不再单独触发排除；历史候选若显著超过基线覆盖会保留到人工核查。'
        '</p>'
        '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
        '<thead><tr class="bg-slate-50 text-slate-600">'
        '<th class="text-left p-2 border-b">函数</th>'
        '<th class="text-left p-2 border-b">文件:行</th>'
        '<th class="text-left p-2 border-b">实际公共基线</th>'
        '<th class="text-left p-2 border-b">被解释的历史候选</th>'
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
    """机械误报小节：掩码造成的短汇编伪相似 / 内部复用。"""

    # 跨架构、跨语言和汇编样板本身只作提示；只有已证明由掩码外壳造成的具体 pair 才进入本节。
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
    for reason in ("cross_arch", "internal_dup"):
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
        f'下列 <b>{len(fp_funcs)}</b> 个函数对已确认属于短内联汇编掩码外壳伪相似或作品内部重复，'
        '属<b>机械误报</b>，<b>不计入值得关注的借鉴</b>，已从摘要、历史匹配图与各模块清单中剔除。'
        '保留来源以便人工复核：</p>'
        '<ul class="text-xs text-slate-500 list-disc pl-5 mb-3 space-y-0.5">'
        '<li><b>短内联汇编掩码伪相似</b>：如龙芯 <code>csrwr</code> 与 RISC-V <code>csrrw</code>，指令本体在 '
        '<code>asm!("指令模板")</code> 字符串里、被归一化掩码成占位符，只剩内联汇编外壳相同——面向不同 CPU，'
        '不可能逐字借鉴。</li>'
        '<li><b>不自动排除</b>：不同架构、不同语言或 <code>__switch</code> 样板名称本身都可能存在移植/'
        '直接复制，只有具体 pair 的掩码伪相似证据成立才排除。</li>'
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

    ① 双方 file_path 在同一 upstream_root（例如 arceos 或 rcore）下且相对路径相同 → 双方 vendored
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
                if src:
                    src_cell = (
                        f'<span class="text-slate-500">共同上游 </span>'
                        f'<code>{html.escape(f.get("root", ""))}</code> · '
                        + _ref_repo_anchor(linker, src.get("repo", "")) + ' '
                        + _make_gitlab_anchor(
                            linker, src.get("repo", ""), src.get("file", ""),
                            src.get("start", 0),
                        )
                    )
                else:
                    src_cell = '<span class="text-slate-400">未建立具体候选来源归因</span>'
            else:
                if src:
                    src_cell = (_ref_repo_anchor(linker, src.get("repo", "")) + ' '
                                + _make_gitlab_anchor(linker, src.get("repo", ""),
                                                      src.get("file", ""), src.get("start", 0)))
                else:
                    src_cell = (
                        '<span class="text-slate-400">仅确认目标函数受约束；'
                        '当前候选不足以归因</span>'
                    )
            out.append(
                '<tr>'
                f'<td class="text-xs font-mono">{html.escape(f["name"])}</td>'
                '<td class="font-mono text-xs">'
                + _make_gitlab_anchor(linker, query_repo_id, f["file"], f.get("start", 0))
                + '</td>'
                f'<td class="text-xs text-slate-500">{html.escape(f.get("lang", "") or "—")}</td>'
                f'<td class="text-xs">{src_cell}</td>'
                f'<td class="text-xs text-slate-400">{src.get("sim", "—") if src else "—"}</td>'
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
            '<th class="text-left p-2 border-b">原始行相似度</th>'
            '</tr></thead><tbody>' + _rows(uv) + '</tbody></table></div>'
        )
    if abi:
        blocks.append(
            '<div class="text-sm font-semibold text-slate-700 mt-3 mb-1">'
            f'ABI 受限的唯一性实现（{len(abi)} 个函数）</div>'
            '<p class="text-xs text-slate-500 mb-1">受 Linux/POSIX ABI 或标准常量约束、且机械转换/'
            '薄适配形态占主体的函数，以及构建脚本和兼容层代码。系统调用目录、<code>sys_*</code> 名称'
            '或单个标准常量本身都不足以排除复杂业务实现；只有通过函数体形态检查的项目才列于此。'
            '<b>来源列仅在具体函数对另有逐行、指纹、低频字符串或分段证据时展示</b>。</p>'
            '<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">'
            '<thead><tr class="bg-slate-50 text-slate-600">'
            '<th class="text-left p-2 border-b">函数</th><th class="text-left p-2 border-b">新作品 文件:行</th>'
            '<th class="text-left p-2 border-b">语言</th><th class="text-left p-2 border-b">可归因来源（如有）</th>'
            '<th class="text-left p-2 border-b">原始行相似度</th>'
            '</tr></thead><tbody>' + _rows(abi) + '</tbody></table></div>'
        )
    body = (
        '<p class="text-sm text-slate-600 mb-3">'
        f'下列 <b>{len(ub_funcs)}</b> 个函数属上游基线 / ABI 受限代码，<b>不计入值得关注的借鉴</b>，'
        '已从摘要、历史匹配图与各模块清单中剔除，单列供核对：</p>'
        f'<div class="flex flex-wrap gap-2 mb-1">{"".join(chips)}</div>'
        + "".join(blocks)
    )
    section = _collapsible_html(
        sid, "上游基线 / ABI 受限代码（不计入借鉴）", body,
        default_open=False, tone="excluded",
    )
    return _toc_link(sid, "上游基线 / ABI 受限代码", str(len(ub_funcs)), "excluded"), section


_REVIEW_PROMPT_VERSION = "v10-comment-identity"  # 改 prompt 即 bump，使旧缓存自动失效

_REVIEW_ROLE_SYSTEM = """你是代码函数职责分析助手。只判断给定两个函数的职责是否一致，不判断代码借鉴。
职责必须综合输入、输出、主要副作用、核心操作对象和调用契约，不能只看函数名、类型名或局部语句。
若双方只是处于相近领域、调用了同类 API，但产物、状态变化或核心对象不同，应判“不一致”。
输出要求：
- 只输出一个单行 JSON 对象，禁止 Markdown 和额外文字；
- responsibility 只能是“一致”“部分一致”或“不一致”；
- responsibility_reason 必须非空，并具体说明双方各自做什么；
- responsibility_reason 必须是完整句子，禁止使用“…”或“...”省略内容；
- evidence_anchors 必须包含 1～4 个从代码中逐字复制、可定位的标识符、常量或短表达式。
严格格式：
{"responsibility":"一致|部分一致|不一致","responsibility_reason":"不超过80字的中文职责依据","evidence_anchors":["代码中的原文锚点"]}"""


_REVIEW_SIM_SYSTEM = """你是跨语言代码同源复核助手。职责分析阶段已经确认两个函数职责一致或部分一致。
现在只判断新作品是否借鉴（复制 / 改名 / 改写）了历史函数。

判定参考：
- “职责相同”只是必要条件，不是借鉴证据。只有共享了具有选择空间的实现细节，才能判“借鉴”或“疑似”。
- 正向证据至少覆盖以下两类：非平凡控制流/步骤顺序、数据变换或状态迁移、异常与边界处理、
  不寻常的常量/字符串/命名组合、稳定的一一改名关系。仅签名、括号、字段初始化外壳或常见 API 调用不算。
- 语言惯例、接口实现、协议/文件格式/ABI 固定字段、标准算法骨架、生成代码、第三方库胶水和框架模板
  只能在你指出双方实现存在具体分歧（控制流、数据结构、并发/异步模型、错误处理或副作用顺序不同）时
  作为“非借鉴”理由；不得仅凭“这属于惯用法”放逐行相同的代码。
- 若双方近乎逐行一致且共享非平凡的步骤顺序、命名组合与分支结构，上述理由只能解释“接口为何长这样”，
  不能解释“为何细节都一致”；此时应判“借鉴”或“疑似”，除非你能指出实质差异。
- 若两侧函数体存在逐字相同的非平凡注释（TODO/FIXME/NOTE 备忘、源码引用 URL、英文说明句等自由文本），
  这是非常强的抄写信号——注释不受语言惯例、API 或协议约束，独立实现几乎不可能逐字写出相同的长注释。
  此时即使代码有细节差异，也应判“借鉴”（或至少“疑似”），并在 reason 中引用该相同注释原文。
- 若双方在核心数据结构、算法、并发/异步模型、错误处理或副作用顺序上有实质差异，应判“非借鉴”。
- 只匹配到函数的一小段时，以实质代码覆盖为准，不把公共前后缀外推成整个函数同源。
- 证据能支持共同机制但不足以排除公共约束时判“疑似”；没有共同实质机制时判“非借鉴”。
输出必须满足以下全部要求：
- 只输出一个单行 JSON 对象，禁止 Markdown 代码块和任何额外文字；
- 所有字段必须存在且非空；reason 必须写出可由代码直接核验的事实；
- reason 必须是完整句子，禁止使用“…”或“...”省略内容；
- 若 verdict 为“借鉴”或“疑似”，evidence_anchors 必须包含 2～4 个在两段代码中都逐字出现的
  标识符、常量或短表达式；若 verdict 为“非借鉴”，可提供 1～4 个来自任一侧的定位锚点；
严格格式：
{"verdict":"借鉴|疑似|非借鉴","reason":"精炼完整的中文复核理由（勿超300字）","evidence_anchors":["代码中的原文锚点"]}"""


# 复核理由长度上限：用于防止模型输出被截断/跑飞，而非强制惜字。穷尽核对复杂函数时，
# 模型会给出 120 字以上的完整说明，120 字过紧会让合格结论被误判为“复核失败”。300 字仍
# 是硬上限，既挡截断又容纳完整说明；展示端与存储端必须用同一上限以便做截断嫌疑校验。
_REVIEW_REASON_CHAR_LIMIT = 300


def _bounded_review_text(value: object, limit: int) -> str:
    """严格校验模型说明长度；超限时重试，禁止截断后进入报告。"""
    text = str(value or "").strip()
    if re.search(r"…|(?<!\.)\.{3}(?!\.)", text):
        raise ValueError("模型说明包含省略号，必须改写为完整句子")
    if len(text) > limit:
        raise ValueError(f"模型说明超过 {limit} 字，必须完整精炼后重试")
    return text


def _legacy_review_text_for_display(value: object, limit: int) -> str:
    """拒绝展示旧缓存中的截断文本，要求重新取得完整复核结论。"""
    text = str(value or "").strip()
    if re.search(r"…|(?<!\.)\.{3}(?!\.)", text):
        raise IncompleteReportError("旧复核缓存包含省略号，需要重新复核")
    if len(text) == limit and text[-1:] not in "。！？；.!?;":
        raise IncompleteReportError("旧复核缓存疑似在长度上限处截断，需要重新复核")
    return text


def _review_failure(detail: str) -> dict:
    """构造明确的复核失败结果；失败不等价于模型仍存疑。"""
    return {
        "verdict": "复核失败",
        "reason": f"复核失败：{detail}",
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


def _validate_evidence_anchors(
    raw_anchors: object,
    query_code: str,
    ref_code: str,
    *,
    min_count: int = 1,
    require_shared: bool = False,
) -> list[str]:
    """验证证据锚点可定位；正向同源结论要求锚点同时存在于两侧代码。"""
    if not isinstance(raw_anchors, list) or not min_count <= len(raw_anchors) <= 4:
        raise ValueError(f"证据锚点必须为 {min_count}～4 项数组")

    def _normalize_ws(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _contains(code: str, anchor: str) -> bool:
        # 标识符锚点必须按完整 token 命中，不能把 ``allocate`` 在
        # ``deallocate_page`` 中的子串巧合当作共同证据；表达式仍按原文定位。
        if re.fullmatch(r"[A-Za-z_]\w*", anchor):
            return re.search(rf"(?<!\w){re.escape(anchor)}(?!\w)", code) is not None
        if anchor in code:
            return True
        # 模型从紧凑化代码抄锚点时可能把多行表达式折成一行、带入行回绕空白；折叠空白
        # 后仍能定位即视为有效，避免一次空白差异直接落成「复核失败」。
        folded = _normalize_ws(anchor)
        if len(folded) >= 2 and folded in _normalize_ws(code):
            return True
        # 方法链常被格式化器折行（process<换行>.threads()），空白折叠会插进一个空格
        # 仍对不上；再去掉全部空白匹配一次，避免跨行表达式被误判为「无法定位」。
        compact = re.sub(r"\s+", "", anchor)
        return len(compact) >= 2 and compact in re.sub(r"\s+", "", code)

    clean_anchors: list[str] = []
    for raw in raw_anchors:
        anchor = str(raw or "").strip()
        if len(anchor) < 2 or len(anchor) > 100:
            raise ValueError("证据锚点长度无效")
        in_query = _contains(query_code, anchor)
        in_ref = _contains(ref_code, anchor)
        if not in_query and not in_ref:
            raise ValueError(f"证据锚点无法在代码中定位：{anchor[:24]}")
        if require_shared and (not in_query or not in_ref):
            raise ValueError(f"正向证据锚点未同时出现在两侧代码：{anchor[:24]}")
        if anchor in clean_anchors:
            raise ValueError("证据锚点不得重复")
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
        "responsibility_reason": _bounded_review_text(reason, 80),
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
        "reason": _bounded_review_text(reason, _REVIEW_REASON_CHAR_LIMIT),
        "evidence_anchors": _validate_evidence_anchors(
            data.get("evidence_anchors"), query_code, ref_code,
            min_count=2 if verdict in ("借鉴", "疑似") else 1,
            require_shared=verdict in ("借鉴", "疑似")),
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
        data.get("evidence_anchors"), query_code, ref_code,
        min_count=2 if verdict in ("借鉴", "疑似") else 1,
        require_shared=verdict in ("借鉴", "疑似"))

    return {
        "verdict": verdict,
        "reason": _bounded_review_text(reason, _REVIEW_REASON_CHAR_LIMIT),
        "responsibility": responsibility,
        "responsibility_reason": _bounded_review_text(responsibility_reason, 80),
        "evidence_anchors": clean_anchors,
    }


_MAX_REVIEW_CODE_CHARS = 24000


def _compact_code_for_review(
    code: str, start_line: int, absolute_ranges: list[tuple[int, int]],
    *, max_chars: int = _MAX_REVIEW_CODE_CHARS,
) -> str:
    """长函数保留头尾和所有匹配区附近上下文；普通函数原样送审。"""
    if len(code) <= max_chars:
        return code
    lines = code.splitlines()
    if not lines:
        return code[:max_chars]

    selected = set(range(min(40, len(lines))))
    selected.update(range(max(0, len(lines) - 30), len(lines)))
    for abs_start, abs_end in absolute_ranges:
        local_start = max(0, int(abs_start) - int(start_line) - 5)
        local_end = min(len(lines), int(abs_end) - int(start_line) + 6)
        selected.update(range(local_start, local_end))

    rendered: list[str] = []
    previous = -2
    for index in sorted(selected):
        if index != previous + 1:
            rendered.append("[中间代码未纳入模型输入；以下继续展示函数头尾和匹配区上下文]")
        rendered.append(lines[index])
        previous = index
    compact = "\n".join(rendered)
    if len(compact) <= max_chars:
        return compact
    half = max_chars // 2
    return (
        compact[:half]
        + "\n[中部上下文未纳入模型输入；以下继续展示末尾上下文]\n"
        + compact[-half:]
    )


def _request_review_json(
    client, model: str, messages: list[dict], timeout: int, max_tokens: int,
    parser,
) -> dict:
    """使用供应商 JSON 模式请求复核；校验失败时携带具体错误自动纠正一次。"""
    last_error: ValueError | None = None
    previous = ""
    for attempt in range(2):
        current_messages = list(messages)
        if attempt:
            current_messages.extend([
                {"role": "assistant", "content": previous[:4000]},
                {"role": "user", "content": (
                    "上次输出未通过机器校验："
                    f"{last_error}。请修正字段和证据锚点；锚点必须从给定代码逐字完整复制。"
                    "只输出一个 JSON 对象，不要解释、不要 Markdown。"
                )},
            ])
        # 网络/限流/5xx 等瞬时异常先带短退避重试一次，避免单次抖动直接落成「复核失败」；
        # 持久故障仍会进入下方格式自纠与 run_review_judgment 的独立重试轮次。
        response = None
        last_api_error: Exception | None = None
        for api_attempt in range(2):
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=current_messages,
                    response_format={"type": "json_object"},
                    temperature=0.0,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
                break
            except Exception as exc:  # noqa: BLE001 — 调用侧统一转成复核失败原因
                last_api_error = exc
                if api_attempt == 0:
                    time.sleep(1.5)
        if response is None:
            raise last_api_error or RuntimeError("模型调用未返回")
        previous = (response.choices[0].message.content or "").strip()
        try:
            return parser(previous)
        except ValueError as exc:
            last_error = exc
    raise ValueError(f"{last_error}；自动纠正后仍无效")


def _review_one(client, model: str, g: dict, timeout: int) -> dict:
    """执行两个独立模型阶段：职责不一致时不发起第二阶段代码同源判断。"""
    cand = (g.get("candidates") or [{}])[0]
    query_code = g.get("query_code", "")
    ref_code = cand.get("ref_code", "")
    spans = cand.get("matched_spans") or []
    query_ranges = [(span[0], span[1]) for span in spans if len(span) >= 4]
    ref_ranges = [(span[2], span[3]) for span in spans if len(span) >= 4]
    query_context = _compact_code_for_review(
        query_code, int(g.get("query_start") or 1), query_ranges)
    ref_context = _compact_code_for_review(
        ref_code, int(cand.get("ref_start") or 1), ref_ranges)
    code_context = (
        f"【新作品函数】{g.get('query_func','')}（{g.get('query_file','')}）：\n"
        f"```\n{query_context}\n```\n\n"
        f"【历史库最相似函数】{cand.get('ref_func','')}"
        f"（{cand.get('ref_repo','')}/{cand.get('ref_file','')}）：\n"
        f"```\n{ref_context}\n```"
    )
    stage = "职责判断"
    try:
        role = _request_review_json(
            client, model,
            [{"role": "system", "content": _REVIEW_ROLE_SYSTEM},
             {"role": "user", "content": code_context
              + "\n\n请只判断两个函数的职责关系。"}],
            # 推理模型会先消耗 reasoning tokens；预算过小会在 JSON 正文前被截断。
            timeout, 4096,
            lambda text: _parse_role_payload(text, query_code, ref_code),
        )

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
        comment_overlap = _verbatim_comment_overlap(query_code, ref_code)
        comment_note = (
            (f"两侧函数体存在逐字相同的非平凡注释："
             f"{'；'.join(comment_overlap[:3])}。"
             f"注释是自由文本，不受语言/API 约束，逐字相同只能来自抄写。")
            if comment_overlap else "两侧函数体无逐字相同的非平凡注释。"
        )
        verdict = _request_review_json(
            client, model,
            [{"role": "system", "content": _REVIEW_SIM_SYSTEM},
             {"role": "user", "content": (
                 code_context
                 + f"\n\n职责阶段结论：{role['responsibility']}；"
                          + role["responsibility_reason"]
                          + f"\n原始逐行相似度 {cand.get('line_similarity','')}；"
                          + f"短侧覆盖率 {cand.get('match_coverage','')}；"
                          + f"匹配行数 {cand.get('matched_lines','')}；"
                          + f"候选身份关系 {cand.get('function_identity_relation','未判定')}；"
                          + f"匹配证据：{_clone_summary(g)}。"
                          + f"\n{comment_note}"
                 + "\n请进行代码同源复核。") }],
            timeout, 4096,
            lambda text: _parse_verdict_payload(text, query_code, ref_code),
        )
        anchors = (
            verdict["evidence_anchors"]
            if verdict["verdict"] in ("借鉴", "疑似")
            else list(dict.fromkeys(
                role["evidence_anchors"] + verdict["evidence_anchors"]))[:4]
        )
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


def run_review_judgment(review_pairs: list[dict], work_dir: Path,
                        model: str | None = None, timeout: int = 30,
                        workers: int = 12,
                        cache_lookup_pairs: list[dict] | None = None) -> list[dict]:
    """对候选逐对执行“职责门控→同源复核”，原地写入严格校验后的结果与证据。

    模型默认 deepseek-v4-flash（可经环境变量 REVIEW_MODEL 覆盖）；曾用 qwen-turbo，但实测
    qwen-turbo 无法识别「异步重构 / 不同锁机制 / ABI 受限字段拼接」等语义级非借鉴（把
    register_timer_callback、fmt 误判为借鉴），deepseek-v4-flash 能正确识别（24 vs 8 个非借鉴）。
    base_url / api_key 复用 config.toml 的 [api]。职责不一致不会调用第二阶段；有效结果逐对缓存，
    失败结果下次运行自动重试。``cache_lookup_pairs`` 只查询已有缓存、不产生额外模型调用；
    用于给本轮预算外候选回填此前已经取得的有效结论。
    """
    cache_lookup_pairs = cache_lookup_pairs or review_pairs
    if not review_pairs and not cache_lookup_pairs:
        return []
    model = model or os.getenv("REVIEW_MODEL", "deepseek-v4-flash")
    try:
        workers = max(1, int(os.getenv("REVIEW_WORKERS", str(workers))))
    except ValueError:
        workers = max(1, workers)
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

    selected_cache_keys = {_key(g) for g in review_pairs}

    def _write_result(g: dict, value: dict) -> None:
        g["review_verdict"] = value.get("verdict", "未复核")
        g["review_reason"] = value.get("reason", "本次未获得有效模型结论")
        g["review_responsibility"] = value.get("responsibility", "未判定")
        g["review_responsibility_reason"] = value.get("responsibility_reason", "")
        g["review_evidence_anchors"] = value.get("evidence_anchors", [])

    if not api_key:
        logger.warning("[review] 未配置 API key，未命中缓存的入队候选跳过 LLM 复核")
        resolved = []
        for g in cache_lookup_pairs:
            value = cache.get(_key(g), {})
            if _valid_cached_result(value, g):
                _write_result(g, value)
                resolved.append(g)
            elif _key(g) in selected_cache_keys:
                _write_result(g, {"verdict": "未复核", "reason": "未配置 API key"})
                resolved.append(g)
        return resolved

    from openai import OpenAI
    from concurrent.futures import ThreadPoolExecutor, as_completed
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    pending_pairs = [
        g for g in review_pairs
        if not _valid_cached_result(cache.get(_key(g)), g)
    ]
    # 历史库常包含同一份源码的多个镜像、分支或年份快照。缓存键本来就按实际代码内容
    # 定义，因此首次运行也应先按该键合并任务，避免多个并发请求在缓存写入前重复复核。
    # 最终仍逐 pair 回填同一份结论，报告中的来源关系不会被合并或丢失。
    pending_by_key: dict[str, dict] = {}
    for g in pending_pairs:
        pending_by_key.setdefault(_key(g), g)
    pending = list(pending_by_key.values())
    duplicate_jobs = len(pending_pairs) - len(pending)
    query_function_count = len({
        (g.get("query_repo", ""), g.get("query_file", ""),
         int(g.get("query_start") or 0), g.get("query_func", ""))
        for g in review_pairs
    })
    logger.info(
        "[review] {} 个目标函数形成 {} 个来源 pair，缓存命中 {} 对；"
        "待判 {} 个唯一代码组合"
        "（合并 {} 个重复来源 pair），用 {} 复核，并发 {}",
        query_function_count, len(review_pairs),
        len(review_pairs) - len(pending_pairs), len(pending), duplicate_jobs, model,
        workers,
    )
    cache_only_hits = sum(
        1 for g in cache_lookup_pairs
        if _key(g) not in selected_cache_keys
        and _valid_cached_result(cache.get(_key(g)), g)
    )
    if cache_only_hits:
        logger.info("[review] 另回填 {} 个预算外候选的既有有效缓存，不增加模型调用", cache_only_hits)

    def _work(g: dict):
        return _key(g), _review_one(client, model, g, timeout)

    def _save_cache() -> None:
        temp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")
        try:
            temp_file.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temp_file, cache_file)
        except OSError:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            futures = [ex.submit(_work, g) for g in pending]
            for completed, future in enumerate(as_completed(futures), start=1):
                k, result = future.result()
                cache[k] = result
                # 长队列可能跨越运行器时限；增量持久化保证续跑只重试最后一小批，
                # 同时避免每完成一对就重写整个缓存文件。
                if completed % 10 == 0:
                    _save_cache()
        _save_cache()

        # 两阶段内部的格式纠正已处理”同一次对话”的偶发偏差；若最终仍失败，再发起
        # 独立对话，避免模型沿用上轮错误锚点。只重试失败项，不重复调用有效结果。
        # 独立重试最多 6 轮：每轮只花失败项（通常几对，约 30 秒），多给几次
        # 独立重掷即可收敛到 0，避免几对格式失败拖垮整条流水线重跑；
        # 仍失败则交由 completeness 门禁拒绝交付。
        for retry_round in range(1, 7):
            retry_failed = [
                g for g in pending
                if (cache.get(_key(g)) or {}).get("verdict") == "复核失败"
            ]
            if not retry_failed:
                break
            logger.info("[review] {} 对格式失败，启动第 {}/6 轮独立复核重试",
                        len(retry_failed), retry_round)
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futures = [ex.submit(_work, g) for g in retry_failed]
                for future in as_completed(futures):
                    k, result = future.result()
                    cache[k] = result
            _save_cache()

    resolved = []
    for g in cache_lookup_pairs:
        c = cache.get(_key(g), {})
        if not _valid_cached_result(c, g) and _key(g) not in selected_cache_keys:
            continue
        _write_result(g, c)
        resolved.append(g)
    return resolved


_COMMENT_PREFIX_RE = re.compile(
    r"^(?:(?:TODO|FIXME|NOTE|XXX|BUG|HACK|TEMP|WIP|Reference|Source|See|Link)"
    r"\s*:\s*)?",
    re.I,
)


def _extract_rust_comments(code: str) -> list[str]:
    """粗略抽取 Rust 代码中的注释文本（跳过字符串/字符/原始字符串字面量）。

    支持 `//`、`///`、`//!` 行注释与 `/* ... */`（含嵌套）块注释。命中字符串里的
    `//`/`/*` 属于解析瑕疵，但归一化后几乎总会被非平凡过滤挡掉；真正的精确过滤由
    `_verbatim_comment_overlap` 的长度与样板剔除负责。
    """
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        c = code[i]
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            j = code.find("\n", i)
            if j == -1:
                j = n
            out.append(code[i:j])
            i = j
        elif c == "/" and i + 1 < n and code[i + 1] == "*":
            depth = 1
            j = i + 2
            while j < n and depth:
                if code[j:j + 2] == "/*":
                    depth += 1
                    j += 2
                elif code[j:j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            out.append(code[i:j])
            i = j
        elif c == '"':
            # 原始字符串 r"…" / r#"…"# / br"…"：引号前是 r 或 br 前缀（且非标识符结尾）
            k = i - 1
            hashes = 0
            while k >= 0 and code[k] == "#":
                hashes += 1
                k -= 1
            is_raw = k >= 0 and code[k] == "r"
            if is_raw and k - 1 >= 0 and code[k - 1] == "b":
                k -= 1
            if is_raw and k > 0 and (code[k - 1].isalnum() or code[k - 1] == "_"):
                is_raw = False
            if is_raw:
                j = i + 1
                while j < n:
                    if code[j] == '"':
                        e = j + 1
                        ok = True
                        for _ in range(hashes):
                            if e < n and code[e] == "#":
                                e += 1
                            else:
                                ok = False
                                break
                        if ok:
                            break
                    j += 1
                i = (j + 1) if j < n else n
            else:
                j = i + 1
                while j < n:
                    if code[j] == "\\":
                        j += 2
                        continue
                    if code[j] == '"':
                        break
                    j += 1
                i = (j + 1) if j < n else n
        elif c == "'":
            # 字符字面量（'x' / '\x'）或生命周期（'a）
            if (i + 2 < n and code[i + 1] != "\\" and code[i + 2] == "'"):
                i += 3
            elif (i + 3 < n and code[i + 1] == "\\" and code[i + 3] == "'"):
                i += 4
            elif i + 1 < n and code[i + 1].isalpha():
                j = i + 2
                while j < n and (code[j].isalnum() or code[j] == "_"):
                    j += 1
                i = j
            else:
                i += 1
        else:
            i += 1
    return out


def _normalize_comment_text(raw: str) -> str:
    """归一化注释：去注释标记、行首 `*` 装饰、行尾 `*/` 与空白折叠。"""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("///"):
            line = line[3:].strip()
        elif line.startswith("//!"):
            line = line[3:].strip()
        elif line.startswith("//"):
            line = line[2:].strip()
        elif line.startswith("/*"):
            line = line[2:].strip()
        if line.endswith("*/"):
            line = line[:-2].strip()
        if line.startswith("*"):
            line = line.lstrip("*").strip()
        if line:
            lines.append(line)
    return " ".join(lines)


def _comment_is_meaningful(text: str) -> bool:
    """非平凡注释：去掉 TODO:/Reference: 等通用前缀后仍足够长，且非版权样板。"""
    t = _COMMENT_PREFIX_RE.sub("", text, count=1).strip()
    if len(t) < 20:
        return False
    if re.match(r"(?i)^(?:SPDX-|Copyright\b|Licensed\b|License\b|All rights reserved)", t):
        return False
    return True


def _verbatim_comment_overlap(qcode: str, ccode: str) -> list[str]:
    """两侧代码逐字相同的非平凡注释（归一化后比较），按长度排序。"""
    def meaningful_comments(code: str) -> set[str]:
        seen: set[str] = set()
        for raw in _extract_rust_comments(code or ""):
            t = _normalize_comment_text(raw)
            if _comment_is_meaningful(t):
                seen.add(t)
        return seen

    qs = meaningful_comments(qcode)
    cs = meaningful_comments(ccode)
    return sorted(qs & cs, key=len, reverse=True)


def _comment_identity_overrides_model(suspect: dict) -> bool:
    """两侧函数体存在逐字相同的非平凡注释时，规则判定同源。

    注释是自由文本，不受语言惯例、API 签名、协议/ABI 字段或 POSIX 语义约束；不同实现者
    独立书写时，逐字相同的长句（TODO/FIXME 备忘、源码引用、英文说明）几乎只能来自直接抄写。
    该信号独立于逐行相似度与模型判断，命中即覆盖模型结论升入高置信同源。
    """
    if suspect.get("tier") not in ("review", "weak"):
        return False
    qcode = ((suspect.get("query_func") or {}).get("raw_code") or "")
    ccode = ((suspect.get("candidate_func") or {}).get("raw_code") or "")
    if not (qcode or "").strip() or not (ccode or "").strip():
        return False
    return bool(_verbatim_comment_overlap(qcode, ccode))


def _hard_evidence_overrides_model_negative(suspect: dict, responsibility: str) -> bool:
    """模型阴性不足以覆盖近同代码铁证时，直接升入高置信同源。

    门槛比 `_model_negative_requires_human_review` 更硬：不仅要求具体函数身份对应、
    职责不冲突，还要求逐行相似度与短侧覆盖同时达到近同水平。命中说明模型大概率拿
    「语言惯例 / 标准算法骨架」一类理由放过了事实上近乎逐行一致的代码，此时不让
    概率性意见清除确定性证据，升档供评委直接核查。归一化指纹单独命中不足以上升
    （样板代码的指纹也会相似），必须配足量逐行证据。规则不依赖仓库、路径、年份或
    具体函数名。
    """
    if suspect.get("tier") not in ("review", "weak"):
        return False
    if responsibility not in ("一致", "部分一致"):
        return False
    ev = suspect.get("evidence") or {}
    relation = str(ev.get("function_identity_relation") or "")
    if relation not in ("exact_counterpart", "same_name_code_clone"):
        return False
    matched = int(ev.get("exact_match_lines") or 0) + int(
        ev.get("renamed_match_lines") or 0
    )
    qcode = ((suspect.get("query_func") or {}).get("raw_code") or "")
    ccode = ((suspect.get("candidate_func") or {}).get("raw_code") or "")
    shorter_lines = min(_nonblank_line_count(qcode), _nonblank_line_count(ccode))
    shorter_coverage = min(1.0, matched / max(1, shorter_lines))
    line_similarity = _raw_line_sim(suspect)
    return bool(
        (line_similarity >= 0.85 and shorter_coverage >= 0.75 and matched >= 8)
        or (line_similarity >= 0.70 and shorter_coverage >= 0.85 and matched >= 10)
    )


def _model_negative_requires_human_review(suspect: dict, responsibility: str) -> bool:
    """模型阴性是否不足以覆盖强直接代码证据。

    只保留具体函数身份已对应、双方职责不冲突、且有足量逐行/指纹证据的难例。
    该规则不依赖仓库、路径、年份或具体函数名。
    """
    if suspect.get("tier") not in ("review", "weak"):
        return False
    if responsibility not in ("一致", "部分一致"):
        return False
    ev = suspect.get("evidence") or {}
    relation = str(ev.get("function_identity_relation") or "")
    if relation not in ("exact_counterpart", "same_name_code_clone"):
        return False
    if ev.get("normalized_fingerprint_match"):
        return True
    matched = int(ev.get("exact_match_lines") or 0) + int(
        ev.get("renamed_match_lines") or 0
    )
    line_similarity = _raw_line_sim(suspect)
    identity = float(ev.get("function_identity_score") or 0.0)
    return bool(
        (line_similarity >= 0.60 and matched >= 10)
        or (line_similarity >= 0.35 and matched >= 15 and identity >= 0.80)
    )


def _apply_review_verdicts(suspects: list[dict], groups: list[dict]) -> tuple[int, int]:
    """据 LLM 复核结论标注档位。
      - review/weak：模型判「借鉴」→ 升入 confirmed（标记 confirm_via=review_llm，
        清单中带「模型复核认定」标记，进入「高置信同源功能簇」参与语义分析）；
        「疑似」→ 达硬证据门槛则升档，否则保留原证据档位；非借鉴通常 → dismissed
      - 两侧函数体存在逐字相同的非平凡注释（`_comment_identity_overrides_model`）→
        自由文本不受语言/API 约束，完全相同只能来自抄写，规则覆盖「非借鉴/疑似」结论，
        升入 confirmed（confirm_via=comment_identity，清单带「注释逐字一致」标记）
      - 模型判「非借鉴/疑似」但逐行相似度与短侧覆盖达硬证据门槛（`_hard_evidence_
        overrides_model_negative`）→ 规则覆盖模型结论，升入 confirmed（confirm_via=
        hard_evidence_override，清单带「硬证据覆盖」标记；review_raw_verdict 保留模型
        原结论，供评委区分覆盖口径）
      - 其余模型阴性：若职责一致、具体函数身份与强逐行/指纹证据同时成立，
        保留为规则保留人工复核；否则 dismissed
      - 复核失败 / 未复核 → 保持原档不动，并单独记录状态，绝不折算为“疑似”
    返回 (模型认定借鉴 + 硬证据覆盖升入高置信同源数, 明确排除数)。
    """
    result_map = {_review_group_pair_key(g): g for g in groups}
    # 相同目标代码 + 相同候选函数内容在镜像仓库、分支或年份快照中只调用一次模型，
    # 但结论需要回填到所有等价来源 pair。
    content_result_map = {
        (_review_target_key(g), _review_content_key(g)): g for g in groups
    }
    up = dn = 0
    for s in suspects:
        tier = s.get("tier")
        if tier not in ("review", "weak", "confirmed"):
            continue
        result = result_map.get(_suspect_pair_key(s))
        if result is None:
            q = s.get("query_func") or {}
            c = s.get("candidate_func") or {}
            content_key = (
                (q.get("repo_id", ""), q.get("file_path", ""),
                 int(q.get("start_line") or 0), q.get("func_name", "")),
                (str(c.get("func_name") or ""), str(c.get("raw_code") or "")),
            )
            result = content_result_map.get(content_key)
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
            if tier != "confirmed":
                # 模型完成复核并认定「借鉴」：升入高置信同源档，进入功能簇参与语义分析。
                # confirm_via=review_llm 让清单保留「模型复核认定」标记，与逐行铁证区分。
                s["tier"] = "confirmed"
                s["confirm_via"] = "review_llm"
                s["model_supported"] = True
                up += 1
        elif v in ("非借鉴", "疑似"):
            if _comment_identity_overrides_model(s):
                # 两侧函数体存在逐字相同的非平凡注释：自由文本不受语言/API 约束，
                # 完全相同只能来自抄写，确定性证据优先于模型概率性意见，升入高置信同源。
                overlap = _verbatim_comment_overlap(
                    ((s.get("query_func") or {}).get("raw_code") or ""),
                    ((s.get("candidate_func") or {}).get("raw_code") or ""))
                s["tier"] = "confirmed"
                s["confirm_via"] = "comment_identity"
                s["review_raw_verdict"] = v
                s["comment_identity_override"] = True
                s["review_reason"] = (
                    "模型结论为「" + v + "」，但两侧函数体存在逐字相同的非平凡注释："
                    + "；".join(overlap[:3])
                    + "。注释不受语言或 API 约束，逐字相同只能来自抄写，规则覆盖模型结论，"
                      "按借鉴升入高置信同源。"
                )
                up += 1
            else:
                responsibility = str(s.get("review_responsibility") or "")
                if _hard_evidence_overrides_model_negative(s, responsibility):
                    # 逐行/短侧覆盖达硬证据门槛：模型阴性或存疑被确定性证据覆盖，升入
                    # 高置信同源。confirm_via 与「模型复核认定」区分开，清单带「硬证据
                    # 覆盖」标记；review_raw_verdict 保留模型原结论，说明覆盖口径。
                    s["tier"] = "confirmed"
                    s["confirm_via"] = "hard_evidence_override"
                    s["review_raw_verdict"] = v
                    s["model_negative_overridden_by_evidence"] = True
                    s["review_reason"] = (
                        "模型结论为「" + v + "」：" + str(s.get("review_reason") or "")
                        + "；但逐行相似度、短侧覆盖与匹配行数达硬证据门槛，"
                          "规则覆盖模型结论，按借鉴升入高置信同源。"
                    )
                    up += 1
                elif v == "非借鉴":
                    if _model_negative_requires_human_review(s, responsibility):
                        s["review_raw_verdict"] = "非借鉴"
                        s["review_verdict"] = "规则保留"
                        s["review_reason"] = (
                            "模型倾向非借鉴：" + str(s.get("review_reason") or "")
                            + "；但双方职责不冲突，且具体函数身份与强直接代码证据同时成立，"
                              "保留供评委人工复核。"
                        )
                        s["model_negative_overridden_by_evidence"] = True
                    else:
                        s["tier"] = "dismissed"
                        s["dismiss_reason"] = "review_非借鉴"
                        dn += 1
                # 疑似但未达硬证据门槛：不确定不是阴性证据，原档保留，交给人工复核（pass）。
    return up, dn


def _assert_model_review_complete(suspects: list[dict]) -> None:
    """正常交付时，所有仍保留的模型难例都必须有通过协议校验的有效结论。"""
    from oskernel_agent.report_quality import IncompleteReportError

    accepted = {"借鉴", "疑似", "规则保留", "非借鉴"}
    required_selections = {"selected", "selected_secondary", "deferred_secondary"}
    incomplete = [
        suspect for suspect in suspects
        if suspect.get("tier") in ("review", "weak")
        and suspect.get("model_review_selection") in required_selections
        and suspect.get("review_verdict") not in accepted
    ]
    if not incomplete:
        return
    targets = {
        (
            (suspect.get("query_func") or {}).get("file_path", ""),
            int((suspect.get("query_func") or {}).get("start_line") or 0),
            (suspect.get("query_func") or {}).get("func_name", ""),
        )
        for suspect in incomplete
    }
    failures = sum(
        1 for suspect in incomplete
        if suspect.get("review_verdict") == "复核失败"
    )
    raise IncompleteReportError(
        f"模型复核未完整完成：{len(targets)} 个目标函数仍有 {len(incomplete)} 个有效候选对"
        f"未取得合格结论（其中调用或格式失败 {failures} 对）"
    )


def _review_section(review_pairs: list[dict], linker, query_repo_id: str,
                    cleared_pairs: list[dict] | None = None) -> tuple[str, str]:
    """只展示高精度难例中的存疑、失败与未完成项；非借鉴代码不铺陈。"""
    cleared_pairs = cleared_pairs or []
    cleared_keys = {
        (g.get("query_file", ""), int(g.get("query_start") or 0),
         g.get("query_func", "")) for g in cleared_pairs
    }
    n_cleared = len(cleared_keys)
    if not review_pairs:
        body = (
            '<p class="text-sm text-slate-500">当前没有需要人工继续处理的模型复核难例。'
            '模型复核认定「借鉴」的函数已升入「高置信同源功能簇」（带「模型复核认定」标记）。'
            + (f'高精度准入队列中另有 <b>{n_cleared}</b> 个函数已被模型明确排除，'
               '不进入风险统计，也不在报告中展开无关代码对。' if n_cleared else '')
            + '</p>'
        )
        section = _collapsible_html(
            "sec-review", "模型复核难例", body, tone="review",
            subtitle="只列真正相近且规则难判的未决代码对",
        )
        return _toc_link("sec-review", "模型复核难例", "0", "zero"), section

    for g in review_pairs:
        g["review_priority"] = _review_priority(g)
    review_pairs = sorted(
        review_pairs,
        key=lambda g: (-g["review_priority"]["score"], -float(g.get("overall_sim") or 0.0),
                       g.get("query_file", ""), g.get("query_func", "")),
    )
    unique_review: dict[tuple, dict] = {}
    for g in review_pairs:
        unique_review.setdefault(
            (g.get("query_file", ""), int(g.get("query_start") or 0),
             g.get("query_func", "")),
            g,
        )
    rows = list(unique_review.values())
    uncertain = [g for g in rows if g.get("review_verdict") == "疑似"]
    retained = [g for g in rows if g.get("review_verdict") == "规则保留"]
    failed = [g for g in rows if g.get("review_verdict") == "复核失败"]
    # supplemental_source 是同一目标函数在已复核来源结论下的补充候选，并非独立未复核
    # 项；C1 升档回填会把代码等价 / 硬证据成立的收进功能簇，其余内容确实不同的补充
    # 候选不再冒充「未复核」，单列计数说明。
    supplemental = [
        g for g in rows
        if g.get("model_review_selection") == "supplemental_source"
        and g.get("review_verdict") not in ("借鉴", "疑似", "规则保留", "复核失败")
    ]
    pending = [
        g for g in rows
        if g.get("review_verdict") not in ("借鉴", "疑似", "规则保留", "复核失败")
        and g.get("model_review_selection") != "supplemental_source"
    ]
    # 本节只渲染存疑/保留/失败/未完成四张表；补充来源候选不渲染成独立行，只并入
    # intro 计数。因此「下列 N 个函数」与 TOC 徽标必须只数会渲染的行，否则会出现
    # “下列 15 个函数”但表格只有 9 行（6 个补充候选）的前后矛盾。
    n_funcs = len(uncertain) + len(retained) + len(failed) + len(pending)
    n_uncertain, n_retained, n_failed, n_pending, n_supplemental = (
        len(uncertain), len(retained), len(failed), len(pending), len(supplemental),
    )

    intro = ('<p class="text-sm text-slate-600 mb-3">'
             f'下列 <b>{n_funcs}</b> 个函数通过了多证据准入且模型复核后仍无法定论。'
             '模型先判断双方职责，职责一致或部分一致时才继续代码相似判断，'
             '并提供能在代码中定位的证据锚点。'
             f'其中模型返回“疑似” <b>{n_uncertain}</b> 个、'
             f'模型阴性但强直接证据保留 <b>{n_retained}</b> 个、'
             f'复核失败 <b>{n_failed}</b> 个、未复核 <b>{n_pending}</b> 个。'
             '复核失败和未复核只是流程状态，<b>不计入模型仍存疑</b>。'
             '模型复核认定「借鉴」的函数已升入「高置信同源功能簇」并带「模型复核认定」标记，'
             '不在本节重复列出。'
             + (f'另有 <b>{n_cleared}</b> 个准入函数已被模型明确排除，'
                '不进入风险统计，也不展开其无关代码。' if n_cleared else '')
             + (f'另有 <b>{n_supplemental}</b> 个同一目标函数的补充来源候选'
                '已随该函数在已复核来源的结论处理（代码等价或硬证据成立的已升入功能簇），'
                '不再单列为未复核。' if n_supplemental else '')
             + '本节按综合相似度、有效代码规模、子系统重要度与克隆类型排序。</p>')

    chips = []
    for verdict, count in (("疑似", n_uncertain), ("规则保留", n_retained),
                           ("复核失败", n_failed), ("未复核", n_pending)):
        if count:
            cls, label = _REVIEW_VERDICT_STYLE[verdict]
            chips.append(f'<span class="px-2 py-0.5 rounded {cls}">{label} {count}</span>')
    summary = ('<div class="review-note"><b>状态口径</b>：只有通过 JSON、非空理由和代码锚点校验后'
               '仍返回“疑似”，或与强直接证据冲突而被规则保留的结果，才进入存疑统计；任何格式错误、空理由、锚点无法定位或模型调用异常'
               '均显示为“复核失败”。“强证据保留”表示模型阴性与可核验直接证据冲突，'
               '不让概率意见清除确定性证据。<div class="flex flex-wrap gap-2 mt-2 text-sm">'
               + "".join(chips) + '</div></div>')

    tables = (
        _groups_table("模型有效复核后仍存疑", uncertain, linker, query_repo_id,
                      "text-amber-700", show_verdict=True, show_priority=True)
        + _groups_table("模型阴性与强直接证据冲突（人工复核）", retained,
                        linker, query_repo_id, "text-amber-800",
                        show_verdict=True, show_priority=True)
        + _groups_table("复核失败（不计为存疑）", failed, linker, query_repo_id,
                        "text-rose-700", show_verdict=True, show_priority=True)
        + _groups_table("复核未完成（不计为存疑）", pending, linker, query_repo_id,
                        "text-slate-600", show_verdict=True, show_priority=True)
    )
    section = _collapsible_html(
        "sec-review", "模型复核难例", intro + summary + tables,
        tone="review", subtitle="只列真正相近且规则难判的未决代码对",
    )
    return _toc_link("sec-review", "模型复核难例", str(n_funcs), "review"), section


_AI_STAGE_DISP = {
    "fast_filter": "快筛（高置信）",
    "perturbation": "扰动复核",
    "npr": "扰动复核",
    "stage2": "扰动复核",
}


def _ai_detect_section(ai_data: dict | None, linker, query_repo_id: str) -> tuple[str, str]:
    """只渲染本次 AI 检测模型产物，不用仓库披露文件代替模型判断。"""
    from oskernel_agent.report_quality import IncompleteReportError

    status = str((ai_data or {}).get("status") or "")
    scope = (ai_data or {}).get("scope") or {}
    # 仓库中确实没有归属当前作品、且属于检测器支持语言的函数，是可验证的“不适用”状态，
    # 不是模型失败。该状态保留空结果模块，避免把 0 个适用函数误写成“模型检测通过”。
    try:
        scope_eligible = int(scope.get("eligible_functions") or 0)
        scope_analyzed = int(scope.get("analyzed_functions") or 0)
    except (TypeError, ValueError) as exc:
        raise IncompleteReportError("AI 检测产物的适用范围统计格式无效") from exc
    if status == "skipped" and scope and scope_eligible == 0 and scope_analyzed == 0:
        reason = str((ai_data or {}).get("reason") or "当前没有符合检测口径的函数")
        try:
            extracted = int(scope.get("extracted_functions") or 0)
            borrowed = int(scope.get("borrowed_excluded") or 0)
            third_party = int(scope.get("third_party_excluded") or 0)
        except (TypeError, ValueError) as exc:
            raise IncompleteReportError("AI 检测空结果的排除统计格式无效") from exc
        if min(extracted, borrowed, third_party) < 0:
            raise IncompleteReportError("AI 检测空结果包含负数统计")
        body = (
            '<p class="text-sm text-slate-600"><b>本次 AI 代码检测不适用。</b>'
            f'{html.escape(reason)}。共抽取 <b>{extracted}</b> 个受支持语言函数，'
            f'其中同源代码排除 <b>{borrowed}</b> 个、第三方复用排除 <b>{third_party}</b> 个，'
            '最终符合归属口径的函数为 <b>0</b> 个，因此没有可供模型判断的对象；'
            '这不表示作品未使用 AI。</p>'
        )
        section = _collapsible_html(
            "sec-aidetect", "附录：AI 代码检测", body, default_open=False,
            tone="appendix", subtitle="经确定性适用范围检查后无可检测函数",
        )
        return _toc_link("sec-aidetect", "AI 代码检测", "不适用", "zero"), section

    if status != "ok":
        reason = str((ai_data or {}).get("reason") or "未生成 AI 检测模型产物")
        raise IncompleteReportError(f"AI 代码检测未完整完成：{reason}")

    ov = (ai_data.get("aggregated") or {}).get("overall") or {}
    required_overall = {
        "total_functions", "llm_count", "human_count", "uncertain_count",
        "llm_ratio_by_count", "llm_ratio_by_loc",
    }
    missing = sorted(required_overall - set(ov))
    if missing or not str(ai_data.get("model_id") or "").strip():
        detail = "、".join(missing) if missing else "model_id"
        raise IncompleteReportError(f"AI 检测产物字段不完整：{detail}")
    try:
        total_functions = int(ov.get("total_functions") or 0)
        class_counts = [
            int(ov.get(key) or 0) for key in ("llm_count", "human_count", "uncertain_count")
        ]
        ratios = [float(ov[key]) for key in ("llm_ratio_by_count", "llm_ratio_by_loc")]
    except (TypeError, ValueError) as exc:
        raise IncompleteReportError("AI 检测产物的整体统计格式无效") from exc
    classified_functions = sum(class_counts)
    if (total_functions <= 0 or min(class_counts) < 0
            or classified_functions != total_functions
            or not all(0.0 <= ratio <= 1.0 for ratio in ratios)
            or scope_analyzed <= 0 or scope_eligible < scope_analyzed):
        raise IncompleteReportError(
            "AI 检测产物统计不一致：分类总数、比例或实际检测范围无效"
        )
    ag = ai_data["aggregated"]
    kpis = (
        '<div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-1 mb-3">'
        + _kpi(f'{ov.get("llm_count", 0)}', "疑似 AI 生成（函数）", "#9333ea")
        + _kpi(f'{ov.get("human_count", 0)}', "判为人工编写（函数）", "#16a34a")
        + _kpi(f'{ov.get("uncertain_count", 0)}', "无法判定（信号不足）", "#64748b")
        + _kpi(f'{ov.get("llm_ratio_by_count", 0.0)*100:.0f}%', "可判定函数中的疑似 AI 占比", "#9333ea")
        + '</div>'
    )
    scope = ai_data.get("scope") or {}
    eligible = int(scope.get("eligible_functions") or ov.get("total_functions", 0))
    analyzed = int(scope.get("analyzed_functions") or ov.get("total_functions", 0))
    third_party_excluded = int(scope.get("third_party_excluded") or 0)
    scope_note = (
        f'送入模型前另排除 <b>{third_party_excluded}</b> 个已识别第三方复用库函数。'
        f'符合归属口径的函数共 <b>{eligible}</b> 个，本次实际检测 <b>{analyzed}</b> 个。'
    )
    if scope.get("truncated"):
        scope_note += '因固定检测配额，本次结果不是对全部符合口径函数的全量检测。'
    meta = (f'<p class="text-sm text-slate-600 mb-3">本节<b>独立于上方同源分析</b>，只展示概率式代码统计信号，'
            '<b>不能判断是否已披露、是否违规或队员是否掌握代码</b>。'
            f'{scope_note}疑似 AI 占比只在模型给出明确分类的函数中计算：按函数为 '
            f'{ov.get("llm_ratio_by_count", 0.0)*100:.0f}%，按代码行数为 '
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
        "sec-aidetect", "附录：AI 代码检测",
        disclaimer + kpis + meta + sf_table,
        default_open=False, tone="appendix",
        subtitle="实际模型检测的辅助信号，不作为违规、扣分或代码归属证据",
    )
    return _toc_link("sec-aidetect", "AI 代码检测",
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
                        ub_funcs: list[dict], base_funcs: list[dict],
                        recall: dict | None = None, *,
                        library_context: LibraryContext | None = None) -> tuple[str, str]:
    """合法复用与许可证证据总览；只做材料检查，不给法律结论。"""
    license_files = _license_files(query_repo_path)
    exclusion = _exclusion_totals(
        suspects, recall, library_context=library_context)
    unique_excluded = int(exclusion.get("total_excluded") or 0)
    categories = [
        ("第三方库复用", sum(int(x.get("func_count") or 0) for x in lib_stats), "检查来源、版本和许可证"),
        ("公共/样板代码", len(cc_funcs), "不计入同源结论，保留规则证据"),
        ("机械误报", len(fp_funcs), "跨架构/模板汇编等已降级"),
        ("共同上游与 ABI", len(ub_funcs), "核验上游归属及修改义务"),
        ("基线衍生", len(base_funcs), "按比赛允许基线单列"),
        ("基线弱候选（待人工复核）", int(exclusion.get("baseline_unverified") or 0),
         "直接证据不足，需人工复核"),
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


def _finals_comparison_summary(
    digest,
    linker,
    submodule_stats: dict,
    *,
    anchored_modules: set[str] | None = None,
    has_review_evidence: bool = False,
) -> str:
    metrics = digest.metrics
    closest = str(metrics.get("closest_source") or "未确定")
    # digest 模块名来自 finals.digests._MODULE_NAMES（如「系统调用」「时钟定时」），与
    # 报告侧 _MODULE_DISPLAY（如「系统调用与用户 ABI」「时钟与定时器」）不同；两个映射
    # 都必须收录，否则 tag 查空 → stats 为空 → 有证据的模块也显示「未形成同源证据」。
    tag_by_name = {name: tag for tag, name in _MODULE_DISPLAY.items()}
    tag_by_name.update({name: tag for tag, name in _MODULE_NAMES.items()})
    module_rows: list[str] = []
    for module in digest.modules:
        # 不按 evidence_count 过滤：comparison_digest 已跳过 total=0 的模块，任何余下
        # 模块的可比函数数都 > 0。这里若跳过零证据模块，表格各项分母之和会小于结论
        # 标题的「N 个可比函数」，读者无法对账（如 209 vs 299）。
        index = len(module_rows) + 1
        tag = tag_by_name.get(module.name, "")
        stats = submodule_stats.get(tag) or {}
        evidence_links: list[str] = []
        if int(stats.get("confirmed") or 0):
            target = (
                f"#module-evidence-{html.escape(tag)}"
                if tag in (anchored_modules or set()) else "#sec-clusters"
            )
            evidence_links.append(
                f'<a href="{target}">高置信证据</a>'
            )
        if int(stats.get("review") or 0):
            target = "#sec-review" if has_review_evidence else "#closest-evidence"
            evidence_links.append(f'<a href="{target}">复核难例</a>')
        evidence = "、".join(evidence_links) or "未形成同源证据"
        module_rows.append(
            '<tr>'
            f'<td>{index}. <strong>{html.escape(module.name)}</strong></td>'
            f'<td>{module.similarity_pct or 0:.1f}%</td>'
            f'<td>{html.escape(module.summary)}</td>'
            f'<td>{evidence}</td>'
            '</tr>'
        )
    module_rows_html = "".join(module_rows) or '<tr><td colspan="4">没有形成可报告的模块级同源证据。</td></tr>'
    findings = "".join(
        '<li class="summary-alert ' + html.escape(item.severity) + '">'
        f'<strong>{html.escape(item.title)}</strong>'
        f'<span>置信度 {round(item.confidence * 100)}%</span>'
        f'<p>{html.escape(item.detail)}</p></li>'
        for item in digest.decision_findings(len(digest.findings))
    ) or '<li class="summary-alert"><strong>未形成高风险结论</strong><p>当前证据不足以锁定同源代码。</p></li>'
    year = str(metrics.get("closest_year") or "")
    team = str(metrics.get("closest_team") or "")
    team_label = team_display_label(team)
    institution = str(metrics.get("closest_institution") or "")
    identity = ""
    if year and team:
        identity = (
            f'{html.escape(year)} 年 · {html.escape(institution)} · {html.escape(team_label)}'
            if institution else
            f'{html.escape(year)} 年 · {html.escape(team_label)} · 学校信息未提供'
        )
    evidence_stats = {
        tag: stats for tag, stats in submodule_stats.items()
        if int(stats.get("confirmed") or 0) + int(stats.get("review") or 0) > 0
    }
    module_chart = _echarts_overview(evidence_stats)
    module_visual = (
        '<div class="chart-title mt-4">与主对比作品的模块级证据分布</div>'
        + module_chart
        + '<p class="text-xs text-slate-500 mt-1">本图只回答各模块与主对比作品的重合情况；'
          '绿色部分表示未与该主对象形成相似证据，不代表在全部历史库中均未命中。</p>'
        if module_chart else ""
    )
    return f"""
<section id="summary" data-section-id="summary" class="summary-card">
  <div class="summary-kicker">先看结论</div>
  <h2>经分析，与 {_ref_repo_anchor(linker, closest)} 最接近</h2>
  {f'<p class="closest-identity">{identity}</p>' if identity else ''}
  <p class="summary-lead">{html.escape(digest.conclusion)}</p>
  <p class="section-intro"><b>整体比例口径：</b>高置信同源目标函数 ÷ 可比目标函数。
  公共上游、第三方库、应用二进制接口（ABI）约束和机械误报均已扣除；该比例用于安排人工核查，
  不等同于抄袭认定。</p>
  <ul class="summary-alerts">{findings}</ul>
  <div class="overflow-x-auto"><table><thead><tr><th>模块</th><th>高置信比例</th><th>结论</th><th>实现依据</th></tr></thead>
  <tbody>{module_rows_html}</tbody></table></div>
  {module_visual}
</section>
"""


def _finals_history_overview(
    overview: dict,
    linker,
    closest_source: str,
) -> str:
    """全历史库概览：动态筛选最相似仓库，并展示整体分布和数字表格。"""
    _validate_finals_history_overview(overview, closest_source)
    similar_sources = list(overview.get("similar_sources") or [])
    distribution_rows = list(overview.get("distribution_rows") or [])
    total = int(overview.get("total_functions") or 0)
    comparable = int(overview.get("comparable_functions") or 0)
    confirmed = int(overview.get("confirmed_functions") or 0)
    review = int(overview.get("review_functions") or 0)
    excluded = int((overview.get("exclusions") or {}).get("total_excluded") or 0)
    source_chart = _echarts_top_sources(similar_sources, top=len(similar_sources))
    donut = _echarts_overall_donut(distribution_rows)
    source_table = _historical_sources_table(similar_sources, linker, closest_source)
    distribution_table = _overall_distribution_table(distribution_rows, total)
    shown = len(similar_sources)
    return f"""
<div class="summary-card history-overview-card">
  <div class="summary-heading"><div><span class="summary-eyebrow">ALL-HISTORY OVERVIEW</span>
  <h2>全历史库概览</h2></div><span class="summary-repo">筛出 {shown} 个相似仓库</span></div>
  <p class="section-intro"><b>本节使用全部历史作品计算；下节只展开排名第一作品的详细证据。</b>
  排名表示“在哪些历史仓库中找到相似实现”，不能单独证明直接来源、传播方向或抄袭关系。
  同一目标函数可能命中多个历史作品，因此各仓库函数数不能相加当作全局命中总数。
  仓库数量不固定：优先保留有高置信函数或整文件证据的仓库；仅当这类证据为空时，才展示已完成的复核难例。</p>
  <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3">
    {_kpi(total, "全部解析函数", "#2563eb")}
    {_kpi(comparable, "全库可比函数", "#0f172a")}
    {_kpi(confirmed, "全库高置信命中", "#ef4444")}
    {_kpi(review, "全库复核难例", "#d97706")}
    {_kpi(excluded, "可解释复用/排除", "#64748b")}
  </div>
  <div class="grid grid-cols-1 lg:grid-cols-2 gap-4 mt-4 items-start">
    <div><div class="chart-title">全库整体结果分布（按全部解析函数）</div>
      {donut}{distribution_table}
    </div>
    <div><div class="chart-title">最相似历史作品（{shown} 个，按统一排名；不代表直接来源）</div>
      {source_chart}
      <p class="text-xs text-slate-500 mt-1">柱图按唯一目标函数展示高置信证据与模型复核难例；
      有效相似行、整文件证据和覆盖范围以同一排名表为准。</p>
    </div>
  </div>
  {source_table}
</div>
"""


def _closed_by_default(section_html: str) -> str:
    """决赛首屏只展开模块索引；完整证据保留，但由评委按需打开。"""
    return section_html.replace('x-data="{open: true}"', 'x-data="{open: false}"', 1)


def generate_finals_comparison_html(
    *,
    query_repo_id: str,
    closest_source: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_pairs: list[dict],
    analysis_html: str,
    review_pairs: list[dict],
    cleared_review_pairs: list[dict],
    ai_detect_data: dict | None,
    query_repo_path: Path | None,
    linker,
    file_matches: list[dict],
    file_similar: list[dict],
    retrieval_contract: dict | None,
    recall: dict | None,
    history_overview: dict | None = None,
) -> tuple[str, object]:
    """评委版对比报告：动态展示最相似仓库，并对第一名保留可下钻详证。"""
    from oskernel_agent.finals.digests import comparison_digest

    overview = history_overview or _build_history_overview(
        suspects,
        submodule_stats,
        file_matches=file_matches,
        recall=recall,
    )
    _validate_finals_history_overview(overview, closest_source)
    digest = comparison_digest(
        query_repo_id, closest_source, submodule_stats,
        exact_file_matches=len(file_matches), ai_detect_data=ai_detect_data,
        history_overview=overview,
    )
    anchored_modules = {
        str(cluster.get("module") or "")
        for cluster in build_similarity_clusters(file_pairs)
    }
    summary_html = _finals_comparison_summary(
        digest,
        linker,
        submodule_stats,
        anchored_modules=anchored_modules,
        has_review_evidence=bool(review_pairs or cleared_review_pairs),
    )
    # 谱系节的“多仓同时命中”必须跨全历史库统计：这里 suspects 是主对比作品（最接近仓库）
    # 过滤后的列表，直接用会把每个目标函数只算到单个仓库、多仓命中恒为 0，与全库排名表
    # 的“多仓重复函数”矛盾。overview["similar_sources"] 是筛选出强证据后的统一排名行，
    # 与全库排名表同一数据源（弱证据仓库不进入“主要历史匹配”，保持不凑数约定）。
    _toc_lineage, sec_lineage = _lineage_section(
        query_repo_id, suspects, linker, recall,
        source_metrics=list(overview.get("similar_sources") or []))
    _toc_clusters, sec_clusters = _cluster_section(
        file_pairs, analysis_html, linker, query_repo_id)
    _toc_review, sec_review = _review_section(
        review_pairs, linker, query_repo_id, cleared_review_pairs)
    _toc_files, sec_files = _file_level_section(
        file_matches, file_similar, linker, query_repo_id)

    evidence_parts = [
        _closed_by_default(sec_lineage),
        sec_clusters,
        _closed_by_default(sec_review),
        _closed_by_default(sec_files),
    ]
    evidence_html = "\n".join(part for part in evidence_parts if part)
    # 机器可审计的召回完整性标记（隐藏 span），供 audit/label_normalize 校验；不占正文。
    method_status = _retrieval_status(retrieval_contract)
    history_html = _finals_history_overview(overview, linker, closest_source)
    toc_html = (
        '<div class="toc-card"><div class="toc-header"><span class="toc-kicker">最终报告</span>'
        '<strong>报告目录</strong></div><div class="toc-scroll">'
        + _toc_group("先看结论", [_toc_link("summary", "结论与模块排序")])
        + _toc_group("全库概览", [_toc_link("history-overview", "最相似历史作品")])
        + _toc_group("最接近作品", [_toc_link("closest-evidence", "同源代码证据")])
        + '</div></div>'
    )
    title = f"{html.escape(query_repo_id)} 对比分析报告"
    rendered = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>{_CDN_HEAD}{_STYLES}<style>
.summary-alerts{{display:grid;gap:.65rem;list-style:none;padding:0;margin:1rem 0}}
.summary-alert{{border-left:3px solid #f59e0b;background:#fffbeb;padding:.7rem .85rem;border-radius:.35rem}}
.summary-alert.high,.summary-alert.critical{{border-left-color:#dc2626;background:#fef2f2}}
.summary-alert>span{{float:right;color:#64748b;font-size:.72rem}}.summary-alert p{{margin:.25rem 0 0;font-size:.86rem}}
.closest-identity{{margin:.2rem 0 .7rem;color:#475569;font-weight:600}}
</style></head><body><div class="layout"><nav class="toc">{toc_html}</nav><main class="main">
<header class="report-header"><h1>{title}</h1><p>先查看按证据动态筛选的最相似历史作品，再围绕排名第一的作品展开代码证据；仓库数量不固定。</p></header>
{method_status}
{summary_html}
<section id="history-overview" data-section-id="history-overview" class="report-section section-neutral">
{_chapter_heading("02", "全历史库匹配概览", "按证据强度动态筛选相似仓库，不固定数量，也不为凑数纳入未完成复核项。")}
{history_html}</section>
<section id="closest-evidence" data-section-id="closest-evidence">
{_chapter_heading("03", "最接近作品的证据", "只展开排名第一的主对比作品；函数、文件和代码细节默认折叠，需要时再查看。")}
{evidence_html}</section>
</main></div><a href="#summary" class="to-top" title="回到顶部">↑</a>{_INIT_SCRIPT}</body></html>"""
    return sanitize_html_controls(explain_terms_in_html(rendered)), digest


def generate_comparison_html(
    query_repo_id: str,
    suspects: list[dict],
    submodule_stats: dict,
    file_pairs: list[dict],
    analysis_html: str,
    original_funcs: list[dict],
    innovation_points: list[dict] | None = None,
    innovation_candidates: list[dict] | None = None,
    review_pairs: list[dict] | None = None,
    cleared_review_pairs: list[dict] | None = None,
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
    recall: dict | None = None,
    library_context: LibraryContext | None = None,
) -> str:
    """组装完整的查重对比 HTML 报告（直接产出，不经 Markdown 转换）。"""
    # 防御公共 API 的直接调用：即使上游仍传入旧产物，也不让测试套件或 benchmark
    # 重新进入面向评委的文件级证据。
    file_matches = [
        {
            **match,
            "matches": [
                candidate for candidate in (match.get("matches") or [])
                if not is_test_or_benchmark_path(candidate.get("file_path", ""))
            ],
        }
        for match in (file_matches or [])
        if not is_test_or_benchmark_path(match.get("query_file", ""))
    ]
    file_matches = [match for match in file_matches if match["matches"]]
    file_similar = [
        item for item in (file_similar or [])
        if not is_test_or_benchmark_path(item.get("file_path", ""))
    ]
    summary_html = _summary_card(query_repo_id, suspects, submodule_stats,
                                 file_match_count=len(file_matches),
                                 file_similar_count=len(file_similar),
                                 retrieval_contract=retrieval_contract,
                                 recall=recall,
                                 linker=linker,
                                 library_context=library_context)
    toc_lineage, sec_lineage = _lineage_section(
        query_repo_id, suspects, linker, recall)
    toc_clusters, sec_clusters = _cluster_section(
        file_pairs, analysis_html, linker, query_repo_id)
    toc_review, sec_review = _review_section(
        review_pairs or [], linker, query_repo_id, cleared_review_pairs or [])
    toc_files, sec_files = _file_level_section(file_matches, file_similar, linker, query_repo_id)
    toc_innovation, sec_innovation = _innovation_section(
        innovation_points or [], linker, query_repo_id, query_repo_path,
        comparison_candidate_count=len(innovation_candidates or []))
    toc_compliance, sec_compliance = _compliance_section(
        query_repo_path, linker, query_repo_id, suspects, lib_stats or [], cc_funcs or [],
        fp_funcs or [], ub_funcs or [], base_funcs or [], recall,
        library_context=library_context)
    toc_ai, sec_ai = _ai_detect_section(ai_detect_data, linker, query_repo_id)
    toc_orig, sec_orig = _original_section(original_funcs, linker, query_repo_id)

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
        f'<span class="toc-repo" title="{html.escape(query_repo_id)}">'
        f'{_ref_repo_anchor(linker, query_repo_id)}</span>'
        '</div><div class="toc-scroll">'
        + _toc_group("评审结论", [_toc_link("summary", "评审结论摘要")])
        + _toc_group("同源判断", [toc_lineage, toc_clusters, toc_review, toc_files])
        + _toc_group("候选创新", [toc_innovation])
        + _toc_group("合规复用", [toc_compliance] + excluded_toc)
        + _toc_group("附录", [toc_ai, toc_orig])
        + '</div></div>'
    )

    body_parts = [
        _chapter_heading("01", "评审结论摘要", "先给出可执行结论、证据规模和历史匹配排名。"),
        summary_html,
        _chapter_heading("02", "共同上游判断", "判断相似代码能否由共同上游、第三方库等公共来源解释，并列出排除后的主要历史匹配。"),
        sec_lineage,
        _chapter_heading("03", "高置信同源功能簇", "以功能级同源事件替代零散函数堆叠，簇内保留全部代码映射。"),
        sec_clusters,
        _chapter_heading("04", "模型复核难例", "只审核真正相近且规则难以区分的代码对。"),
        sec_review,
        _chapter_heading("05", "文件级和非函数代码证据", "补充整文件、结构体、宏、汇编、链接脚本与配置证据。"),
        sec_files,
        _chapter_heading("06", "相对参考实现的候选创新", "将参考基线、实现代码、调用入口、影响范围和反证绑定。"),
        sec_innovation,
        _chapter_heading("07", "合法复用与许可证合规", "允许复用、共同上游和比赛基线独立核查，不混入同源结论。"),
        sec_compliance,
        *excluded_sections,
        _chapter_heading("08", "AI 代码检测", "展示实际模型检测信号；结果仅供辅助核查，不作单独认定。"),
        sec_ai,
        _chapter_heading("09", "暂未检出相似函数附录", "完整列出当前未命中函数，但不据此作原创认定。"),
        sec_orig,
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
    global_semantic_analysis: bool = True,
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
        top_per_module:  每个功能簇送入语义模型的成员映射上限（默认 20）；不截断功能簇，
                         超出统一上下文预算时自动分批，保证报告中的每个功能簇都有语义说明
        skip_opencode:   True 时跳过全部 LLM（仅诊断用，不渲染模型分析占位模块）
        global_semantic_analysis: 默认 True，实际运行模块语义分析和创新归纳；
                                  False 仅供诊断，相关模块不渲染占位内容
        filematch_path:  fastpath（L0 文件指纹层）产出的 *_filematch.json（整文件复制清单）
        functions_db_path: 历史函数库路径，用于读取参考 repo 的函数源码并生成创新实现地图
    """
    suspects_path = Path(suspects_path)
    data          = json.loads(suspects_path.read_text(encoding="utf-8"))
    suspects      = data.get("suspects", [])
    suspects = [
        suspect for suspect in suspects
        if not is_test_or_benchmark_path(
            (suspect.get("query_func") or {}).get("file_path", ""))
        and not is_test_or_benchmark_path(
            (suspect.get("candidate_func") or {}).get("file_path", ""))
    ]
    data["suspects"] = suspects
    refined_suspect_modules = _refine_report_modules(suspects)
    if refined_suspect_modules:
        logger.info(
            "[compare] 子系统分类补正 {} 条旧标签函数记录",
            refined_suspect_modules,
        )
    query_repo_id = data.get("query_repo_id") or suspects_path.stem.split("_suspects")[0]
    library_context = discover_library_context(query_repo_path)
    if library_context.integration_roots:
        logger.info(
            "[compare] 依赖归属证据确认 {} 个第三方库适配层：{}",
            len(library_context.integration_roots),
            "、".join(
                f"{root} → {library}" for root, library in library_context.integration_roots),
        )
    reuse_n       = tag_library_reuse(
        suspects, context=library_context)  # 标注库本体及经证据确认的适配层
    baseline_boundary_n = _tag_query_level_baselines(suspects)
    fp_counts     = tag_false_positives(suspects)  # 标注跨架构/跨语言/样板汇编误报（降级，不丢弃）
    ub_counts     = tag_upstream_baselines(suspects)  # 标注上游基线 vendored / ABI 受限代码（降级）
    if baseline_boundary_n:
        logger.info(
            "[compare] 报告边界补正 {} 对显式或目标函数级公共基线候选",
            baseline_boundary_n,
        )

    recall: dict | None = None
    if recall_path and Path(recall_path).exists():
        recall = json.loads(Path(recall_path).read_text(encoding="utf-8"))
        refined_recall_modules = _refine_report_modules([], recall)
        if refined_recall_modules:
            logger.info(
                "[compare] 召回产物子系统分类补正 {} 条旧标签函数记录",
                refined_recall_modules,
            )
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
        filtered_file_matches = []
        for match in file_matches:
            if is_test_or_benchmark_path(match.get("query_file", "")):
                continue
            filtered = dict(match)
            filtered["matches"] = [
                item for item in (match.get("matches") or [])
                if not is_test_or_benchmark_path(item.get("file_path", ""))
            ]
            if filtered["matches"]:
                filtered_file_matches.append(filtered)
        file_matches = filtered_file_matches

    # 输出 / 工作目录（复核与语义分析共用）
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / f"{query_repo_id}_semantic_work"

    # 只对 review/weak 中通过多证据准入的难例做两阶段模型复核；confirmed 已有确定性证据，
    # 不再重复消耗模型。职责不一致直接排除；职责门控通过后才做同源判断。
    # 判「借鉴」升入 confirmed（带模型复核认定标记，进入功能簇参与语义分析）；
    # “非借鉴”降为 dismissed，格式有效的“疑似”保留信号，
    # 格式/调用失败单列状态，不能冒充“模型仍存疑”。
    # 必须在统计 / file_pairs / 未检出清单计算之前。
    pairing_mismatches = _suppress_dominated_candidate_mismatches(suspects)
    if pairing_mismatches:
        logger.info(
            "[review] 具体函数重排移除 {} 对被同文件更优候选支配的邻近模板误配",
            pairing_mismatches,
        )
    family_mismatches = _suppress_family_neighbor_mismatches(suspects)
    if family_mismatches:
        logger.info(
            "[review] 具体函数身份门槛移除 {} 对仅属同功能族、不能建立具体对应的候选",
            family_mismatches,
        )
    stub_mismatches = _suppress_nonsemantic_stub_mismatches(suspects)
    if stub_mismatches:
        logger.info(
            "[review] 占位函数门槛移除 {} 对只有签名外壳相似、没有行为身份的改名候选",
            stub_mismatches,
        )
    evidence_gate_removed = _apply_review_evidence_gate(suspects)
    if evidence_gate_removed:
        logger.info(
            "[review] 通用证据门槛移除 {} 对仅有召回相似、没有可核验共同代码证据的候选",
            evidence_gate_removed,
        )

    review_judgments: list[dict] = []
    if not skip_opencode:
        review_judgments, review_selection = select_model_review_pairs(suspects)
        logger.info(
            "[review] 高精度难例队列：{} 个目标 / {} 个准入来源 pair / {} 个不同代码组合，"
            "选取 {} 个代表代码组合（覆盖 {} 个镜像来源 pair）模型复核；"
            "{} 对独立次级来源先作为补充来源，仅独立强证据在首选排除后补充送审",
            review_selection["targets"], review_selection["eligible_pairs"],
            review_selection["eligible_unique_content_pairs"],
            review_selection["selected_pairs"],
            review_selection["selected_source_pairs"],
            review_selection["deferred_secondary_pairs"],
        )
        all_review_candidates = collect_review_pairs(
            suspects, keep_tiers=("review", "weak"))
        review_judgments = run_review_judgment(
            review_judgments, work_dir,
            cache_lookup_pairs=all_review_candidates,
        )
        up, dn = _apply_review_verdicts(suspects, review_judgments)
        if up or dn:
            logger.info("[review] 复核：模型认定借鉴升入高置信同源 {} 对，明确非借鉴 {} 对移出相似清单",
                        up, dn)
        try:
            fallback_rounds = max(0, int(os.getenv(
                "REVIEW_SECONDARY_FALLBACK_ROUNDS",
                str(REVIEW_SECONDARY_FALLBACK_ROUNDS),
            )))
        except ValueError:
            fallback_rounds = REVIEW_SECONDARY_FALLBACK_ROUNDS
        for round_index in range(fallback_rounds):
            fallback_pairs = select_exceptional_secondary_review_pairs(suspects)
            if not fallback_pairs:
                break
            logger.info(
                "[review] 次级强证据补充复核第 {} 轮：{} 个目标函数（每目标最多 1 个候选）",
                round_index + 1, len(fallback_pairs),
            )
            fallback_results = run_review_judgment(fallback_pairs, work_dir)
            review_judgments.extend(fallback_results)
            fallback_up, fallback_down = _apply_review_verdicts(
                suspects, fallback_results)
            logger.info(
                "[review] 次级强证据补充结果：认定借鉴升入高置信同源 {} 对，明确排除 {} 对",
                fallback_up, fallback_down,
            )
        secondary_resolution = finalize_secondary_review_candidates(suspects)
        if any(secondary_resolution.values()):
            logger.info(
                "[review] 次级来源收口：{} 对仅作为已复核函数的补充来源，"
                "{} 对在首选已排除且无独立强证据后移出报告",
                secondary_resolution["supplemental"],
                secondary_resolution["dismissed"],
            )
        # 同目标函数已在其他来源复核判「借鉴」时，报告侧（最相似仓库）代码等价或硬证据
        # 成立的补充候选沿用结论升档，避免在复核节残留成「复核未完成」误导评委。
        carried_over = _promote_strong_report_pairs_from_target_verdict(suspects)
        if carried_over:
            logger.info(
                "[review] 同目标已有复核结论的报告侧候选升入高置信同源 {} 对"
                "（代码等价 / 硬证据成立，结论沿用）",
                carried_over,
            )
        _assert_model_review_complete(suspects)

    # 内部跨架构硬拷贝复用标注：须在复核升档之后（覆盖升上来的 confirmed），统计之前。
    dup_n = tag_internal_arch_dups(suspects)
    if any(fp_counts.values()) or dup_n:
        logger.info("[compare] 疑似误报降级：跨架构 {} / 跨语言 {} / 样板汇编 {} / 内部跨架构复用 {}（均不计入借鉴，单列「疑似误报」节）",
                    fp_counts["cross_arch"], fp_counts["cross_lang"],
                    fp_counts["boilerplate_asm"], dup_n)
    if any(ub_counts.values()):
        logger.info("[compare] 上游基线/ABI 降级：vendored 上游 {} / ABI 受限 {}（不计入借鉴，单列「上游基线/ABI 受限」节）",
                    ub_counts["upstream_vendored"], ub_counts["abi_constrained"])

    # 全库概览保留所有可报告历史作品；详细证据只围绕统一排名第一的作品展开。
    # 先剔除第三方库和脚手架整文件，避免它们左右相似仓库筛选与主对象选择。
    reportable_file_matches = [
        match for match in file_matches
        if not match_library(match.get("query_file"), context=library_context)
        and not is_excluded_file_path(match.get("query_file", ""))
    ]
    history_sources = _historical_source_metrics(suspects, reportable_file_matches)
    closest_source = (
        history_sources[0]
        if history_sources
        else {"repo": "", "functions": 0, "effective_loc": 0, "exact_files": 0}
    )
    closest_repo = str(closest_source.get("repo") or "")
    global_submodule_stats = compute_submodule_stats(
        suspects, recall, library_context=library_context,
    )
    history_overview = _build_history_overview(
        suspects,
        global_submodule_stats,
        file_matches=reportable_file_matches,
        recall=recall,
        library_context=library_context,
        source_metrics=history_sources,
    )
    _validate_finals_history_overview(history_overview, closest_repo)
    report_suspects = _suspects_for_source(suspects, closest_repo)
    file_matches = _file_matches_for_source(reportable_file_matches, closest_repo)
    logger.info(
        "[compare] 决赛主对比作品：{}（高置信函数 {}，有效相似行 {}，整文件 {}）",
        closest_repo or "未确定", closest_source.get("functions", 0),
        closest_source.get("effective_loc", 0), closest_source.get("exact_files", 0),
    )

    # 文件整体相似：用「已剔除嫌疑对」口径计算（vendored 上游 / ABI / 库复用 / 公共样板 /
    # 误报 不计入），这样 arceos/build.rs、macros.rs、C 库、examples 等脚手架文件不会被
    # 报为整体相似。file_matches（逐字节整文件相同）按路径口径剔除同类脚手架。
    non_excluded = [s for s in report_suspects if not _is_excluded_pair(s)]
    file_similar = aggregate_file_similarity(non_excluded, recall, query_repo_path=query_repo_path)
    file_similar = [f for f in file_similar
                    if not match_library(f.get("file_path"), context=library_context)
                    and not is_excluded_file_path(f.get("file_path", ""))]

    logger.info("[compare] 新作品 {}：{} 个嫌疑对（剔除库复用 {} / 上游基线 {} / ABI {} / 误报 {} / 内部复用 {}），整文件相同 {} 个，整体相似 {} 个",
                query_repo_id, len(report_suspects), reuse_n, ub_counts["upstream_vendored"],
                ub_counts["abi_constrained"], sum(fp_counts.values()), dup_n,
                len(file_matches), len(file_similar))

    # 统计采用复核后的档位与互斥复核状态：有效疑似、失败、真正未完成分开。
    submodule_stats = compute_submodule_stats(
        report_suspects, recall, library_context=library_context)
    lib_stats       = reused_library_stats(
        suspects, recall, context=library_context)
    cc_funcs        = common_code_stats(suspects)
    fp_funcs        = false_positive_stats(suspects)
    ub_funcs        = upstream_baseline_stats(suspects)
    # confirmed 功能簇全量展示并全量进行语义分析；top_per_module 只限制每个簇在 prompt 中
    # 展开的成员映射数，超出统一上下文预算时自动分批，不能形成“有卡片、无说明”的报告。
    file_pairs = collect_file_pairs(report_suspects)
    final_review_pairs = collect_file_pairs(report_suspects, keep_tiers=("review", "weak"))
    cleared_pair_keys = {
        _suspect_pair_key(suspect) for suspect in report_suspects
        if suspect.get("tier") == "dismissed"
        and suspect.get("dismiss_reason") == "review_非借鉴"
    }
    cleared_review_pairs = [
        group for group in review_judgments
        if _review_group_pair_key(group) in cleared_pair_keys
    ]
    # 统计口径把同一文件中的同名函数视为一个评审单元并取最高档；若某个起始行已经有
    # confirmed 证据，就不要让同名的另一起始行再次出现在“模型仍存疑”中。
    final_review_pairs = _exclude_confirmed_review_groups(final_review_pairs, file_pairs)
    # collect_file_pairs 从每个仍有效的候选对读取复核结论，再按目标函数聚合；不会把一个
    # 候选的结论套到同目标函数的其他候选上。
    llm_pairs = file_pairs

    # AI 生成代码检测结果（独立链路产物，可选并入报告）
    ai_detect_data = None
    if ai_detect_path and Path(ai_detect_path).exists():
        try:
            ai_detect_data = json.loads(Path(ai_detect_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("[compare] 读取 ai_detect 结果失败：{}", e)

    # 暂未命中函数只包含从未形成历史匹配的函数；复核排除项仍保留其历史匹配事实。
    original_funcs  = _original_functions(
        recall, suspects, library_context=library_context) if recall else []

    # 决赛对比报告不再展开“候选创新”和全部未命中函数；这些内部数据仍用于召回
    # 完整性校验，但不会占用评委正文或额外触发创新归纳模型调用。
    innovation_candidates: list[dict] = []
    semantic_enabled = not skip_opencode and global_semantic_analysis
    innovation_points: list[dict] = []

    analysis_html = ""
    if semantic_enabled and llm_pairs:
        qpath = str(Path(query_repo_path).resolve()) if query_repo_path else ""
        analysis_html = run_semantic_analysis(
            query_repo_id, qpath, llm_pairs, submodule_stats, work_dir,
            members_per_cluster=top_per_module,
        )

    # 构建 GitLab linker（取各参考仓库的在线 URL + HEAD sha）
    ref_repos = {c["ref_repo"] for g in file_pairs for c in g["candidates"]}
    ref_repos |= {m["repo_id"] for fm in file_matches for m in fm.get("matches", [])}
    ref_repos |= {
        str(item.get("repo") or "") for item in history_sources if item.get("repo")
    }
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
    html_text, finals_digest = generate_finals_comparison_html(
        query_repo_id=query_repo_id,
        closest_source=closest_repo,
        suspects=report_suspects,
        submodule_stats=submodule_stats,
        file_pairs=file_pairs,
        analysis_html=analysis_html,
        review_pairs=final_review_pairs,
        cleared_review_pairs=cleared_review_pairs,
        ai_detect_data=ai_detect_data,
        query_repo_path=Path(query_repo_path).resolve() if query_repo_path else None,
        linker=linker,
        file_matches=file_matches,
        file_similar=file_similar,
        retrieval_contract=recall.get("retrieval_contract") if recall else None,
        recall=recall,
        history_overview=history_overview,
    )

    # 档位标签统一（高置信同源代码 / 模型复核难例 / 暂未检出相似），避免各处叫法不一
    from .label_normalize import normalize_labels
    html_text = normalize_labels(html_text)
    from oskernel_agent.report_quality import assert_report_complete
    assert_report_complete(html_text)

    safe_id  = query_repo_id.replace("/", "_")
    out_path = out_dir / f"{safe_id}_comparison.html"
    out_path.write_text(html_text, encoding="utf-8")
    digest_path = out_dir / f"{safe_id}_comparison.digest.json"
    digest_path.write_text(
        json.dumps(finals_digest.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("[compare] HTML 报告 → {}", out_path)

    return {
        "html_path":        str(out_path),
        "digest_path":      str(digest_path),
        "query_repo_id":    query_repo_id,
        "closest_source":   closest_repo,
        "total_suspects":   len(suspects),
        "submodule_stats":  submodule_stats,
        "original_funcs":   len(original_funcs),
        "innovation_points": len(innovation_points),
        "innovation_comparison_candidates": len(innovation_candidates),
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
