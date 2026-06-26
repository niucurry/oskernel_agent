"""报告五章节的结构化数据组装与代码生成表格（不经过 LLM 的部分）。

输入：reviewed.json / suspects_final.json 的 suspects 列表 + recall.json。
"""

from __future__ import annotations

from collections import Counter, defaultdict

# tier 加权（用于溯源排名）；公共/模板代码权重 0，不计入溯源
_TIER_WEIGHT = {"confirmed": 3, "review": 2, "weak": 1,
                "baseline_derived": 0, "dismissed": 0, "common_code": 0}
# 不计抄袭、从溯源/模块对照中排除的档位
_NON_PLAGIARISM = ("dismissed", "baseline_derived", "common_code")
MODULES = ["sched", "mm", "fs", "trap", "driver"]
INNOVATION_SIM_MAX = 0.5
INNOVATION_MIN_LINES = 30


def _q(s):  # query_func
    return s["query_func"]


def _c(s):  # candidate_func
    return s["candidate_func"]


def _verdict(s) -> str | None:
    rv = s.get("review")
    return rv.get("verdict") if isinstance(rv, dict) else None


# ---------- 一、溯源结论 ----------

def trace_top_repos(suspects: list[dict], top_n: int = 3) -> list[dict]:
    """按 tier 加权次数排名的 Top-N 历史仓库及其统计。"""
    agg: dict[str, dict] = defaultdict(lambda: {"weight": 0, "tiers": Counter(), "modules": set(), "pairs": 0})
    for s in suspects:
        repo = _c(s)["repo_id"]
        tier = s.get("tier", "")
        a = agg[repo]
        a["weight"] += _TIER_WEIGHT.get(tier, 0)
        a["tiers"][tier] += 1
        a["modules"].add(_q(s).get("module_tag", "other"))
        if tier not in _NON_PLAGIARISM:
            a["pairs"] += 1
    ranked = sorted(agg.items(), key=lambda kv: kv[1]["weight"], reverse=True)
    out = []
    for repo, a in ranked[:top_n]:
        if a["weight"] <= 0:
            continue
        out.append({
            "repo_id": repo,
            "weight": a["weight"],
            "pairs": a["pairs"],
            "confirmed": a["tiers"].get("confirmed", 0),
            "review": a["tiers"].get("review", 0),
            "weak": a["tiers"].get("weak", 0),
            "modules": sorted(a["modules"]),
        })
    return out


# ---------- 二、模块级对照表（纯代码生成） ----------

def module_table_rows(suspects: list[dict]) -> list[dict]:
    rows = []
    for mod in MODULES:
        best = None
        for s in suspects:
            if _q(s).get("module_tag") != mod or s.get("tier") in _NON_PLAGIARISM:
                continue
            score = float(s.get("final_score") or 0.0)
            if best is None or score > best["sim"]:
                best = {
                    "module": mod, "repo_id": _c(s)["repo_id"],
                    "cand_func": _c(s)["func_name"], "sim": round(score, 3),
                }
        rows.append(best or {"module": mod, "repo_id": "—", "cand_func": "—", "sim": 0.0})
    return rows


def render_module_table(rows: list[dict]) -> str:
    lines = ["| 模块 | 最相似历史来源 | 候选函数 | 相似度 |", "| --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['module']} | {r['repo_id']} | {r['cand_func']} | {r['sim']} |")
    return "\n".join(lines)


# ---------- 三、高相似代码段清单 ----------

def high_similarity_pairs(suspects: list[dict], recall: dict | None = None,
                          linker=None) -> list[dict]:
    """confirmed 或 verdict=likely_clone 的对。linker 非空时 ref 渲染为 GitLab 链接。"""
    query_repo_id = (recall or {}).get("query_repo_id")
    if linker and query_repo_id:
        linker.mark_query_repo(query_repo_id)
    out = []
    for s in suspects:
        if s.get("tier") == "confirmed" or _verdict(s) == "likely_clone":
            q, c = _q(s), _c(s)
            rv = s.get("review") if isinstance(s.get("review"), dict) else {}
            q_text = f"{q['file_path']}:{q['start_line']}-{q['end_line']}"
            c_text = f"{c['repo_id']}/{c['file_path']}:{c['start_line']}-{c['end_line']}"
            if linker:
                new_ref = linker.link(query_repo_id, q["file_path"],
                                      q["start_line"], q["end_line"], text=q_text)
                old_ref = linker.link(c["repo_id"], c["file_path"],
                                      c["start_line"], c["end_line"], text=c_text)
            else:
                new_ref, old_ref = q_text, c_text
            out.append({
                "new_ref": new_ref,
                "old_ref": old_ref,
                "sim": round(float(s.get("final_score") or 0.0), 3),
                "clone_type": rv.get("clone_type") or (s.get("match_type_per_span") or ["—"])[0],
                "reasoning": rv.get("reasoning", ""),
            })
    out.sort(key=lambda x: x["sim"], reverse=True)
    return out


def render_high_sim_table(pairs: list[dict], summaries: list[str]) -> str:
    lines = ["| 新作品 文件:行 | 历史来源 | 相似度 | clone_type | 判定摘要 |",
             "| --- | --- | --- | --- | --- |"]
    for p, summ in zip(pairs, summaries):
        lines.append(f"| {p['new_ref']} | {p['old_ref']} | {p['sim']} | {p['clone_type']} | {summ} |")
    return "\n".join(lines)


# ---------- 四、创新点（从 recall.json） ----------

def innovation_functions(recall: dict, top_n: int = 10, linker=None) -> list[dict]:
    """新作品中与全历史库最高相似度 < 0.5 且行数 > 30 的函数 Top-N（按行数降序）。"""
    query_repo_id = recall.get("query_repo_id")
    if linker and query_repo_id:
        linker.mark_query_repo(query_repo_id)
    out = []
    for item in recall.get("results", []):
        q = item["query"]
        cands = item.get("candidates", [])
        max_sim = max((c["score"] for c in cands), default=0.0)
        lines = q["end_line"] - q["start_line"] + 1
        if max_sim < INNOVATION_SIM_MAX and lines > INNOVATION_MIN_LINES:
            ref_text = f"{q['file_path']}:{q['start_line']}-{q['end_line']}"
            ref = linker.link(query_repo_id, q["file_path"],
                              q["start_line"], q["end_line"], text=ref_text) if linker else ref_text
            out.append({
                "ref": ref,
                "func_name": q["func_name"], "lines": lines,
                "max_sim": round(max_sim, 3), "raw_code": q.get("raw_code", ""),
            })
    out.sort(key=lambda x: x["lines"], reverse=True)
    return out[:top_n]


# ---------- 五、附注信号 ----------

def annotations(suspects: list[dict], linker=None, query_repo_id: str | None = None) -> dict:
    if linker and query_repo_id:
        linker.mark_query_repo(query_repo_id)
    baseline = [s for s in suspects if s.get("tier") == "baseline_derived"]
    commit_hits = [s for s in suspects if (s.get("evidence") or {}).get("commit_signals")]
    disputed = [s for s in suspects if _verdict(s) == "disputed"]

    def _qref(s):
        q = _q(s)
        t = f"{q['file_path']}:{q['start_line']}"
        return linker.link(query_repo_id, q["file_path"], q["start_line"], q["start_line"], text=t) if linker else t

    def _cref(s):
        c = _c(s)
        t = f"{c['repo_id']}/{c['file_path']}:{c['start_line']}"
        return linker.link(c["repo_id"], c["file_path"], c["start_line"], c["start_line"], text=t) if linker else t

    # 公共/框架代码：按 query 函数去重（一个函数会有多条对）
    common_q: dict[str, int] = {}
    for s in suspects:
        if s.get("tier") == "common_code":
            ref = _qref(s)
            common_q[ref] = max(common_q.get(ref, 0), (s.get("evidence") or {}).get("common_code_repos", 0))
    return {
        "common_code_count": len(common_q),
        "common_code": [{"new": ref, "repos": n} for ref, n in sorted(common_q.items())],
        "baseline_count": len(baseline),
        "baseline": [
            {"new": _qref(s), "note": s.get("baseline_note", "")}
            for s in baseline
        ],
        "commit_signals": [
            {"new": _qref(s),
             "signals": (s.get("evidence") or {}).get("commit_signals", [])}
            for s in commit_hits
        ],
        "disputed": [
            {"new": _qref(s), "old": _cref(s)}
            for s in disputed
        ],
    }


def ai_detection_data(ai_report: dict | None, *, top_files: int = 8, top_funcs: int = 10,
                      linker=None, query_repo_id: str | None = None) -> dict:
    """从 {repo}_ai_detect.json 提取「AI 生成代码检测」章节所需结构化数据。

    返回 {"status": ok|skipped|missing, ...}。ok 时含 overall/by_language/high_risk_files/
    suspicious（各取 Top-N，source 截断），供 LLM 撰写与代码生成表格共用。
    """
    if not ai_report:
        return {"status": "missing"}
    status = ai_report.get("status")
    if status != "ok":
        return {"status": status or "missing", "reason": ai_report.get("reason", ""),
                "model_id": ai_report.get("model_id", "")}

    if linker and query_repo_id:
        linker.mark_query_repo(query_repo_id)
    agg = ai_report.get("aggregated", {})
    overall = agg.get("overall", {})
    suspicious = []
    for s in agg.get("suspicious_functions", [])[:top_funcs]:
        src = (s.get("source") or "").strip()
        if len(src) > 800:
            src = src[:800] + "\n…(截断)"
        ref_text = f"{s['file_path']}:{s['start_line']}-{s['end_line']}"
        ref = linker.link(query_repo_id, s["file_path"], s["start_line"], s["end_line"],
                          text=ref_text) if linker else ref_text
        suspicious.append({
            "name": s.get("qualified_name") or s.get("function_name", ""),
            "ref": ref,
            "language": s.get("language", ""),
            "confidence": round(float(s.get("confidence") or 0.0), 3),
            "detect_score": s.get("detect_score"),
            "log_rank": s.get("log_rank"),
            "stage": s.get("stage", ""),
            "source": src,
        })
    return {
        "status": "ok",
        "model_id": ai_report.get("model_id", ""),
        "overall": overall,
        "by_language": agg.get("by_language", []),
        "high_risk_files": [f for f in agg.get("high_risk_files", []) if f.get("llm_count")][:top_files],
        "suspicious": suspicious,
        "git_blame_available": agg.get("git_blame_available", False),
        "by_author": agg.get("by_author", [])[:8],
    }


def _pct(x: float | None) -> str:
    return f"{(x or 0.0) * 100:.1f}%"


def render_ai_overview_table(overall: dict) -> str:
    decided = (overall.get("llm_count", 0) or 0) + (overall.get("human_count", 0) or 0)
    llm_pct = _pct(overall.get("llm_ratio_by_count")) if decided else "—"
    loc_pct = _pct(overall.get("llm_ratio_by_loc")) if overall.get("total_loc") else "—"
    lines = ["| 指标 | 数值 |", "| --- | --- |",
             f"| 函数总数 | {overall.get('total_functions', 0)} |",
             f"| AI 疑似（LLM） | {overall.get('llm_count', 0)}（按函数数 {llm_pct} / 按行数 {loc_pct}） |",
             f"| 人类编写（Human） | {overall.get('human_count', 0)} |",
             f"| 不确定/跳过（Uncertain） | {overall.get('uncertain_count', 0)} |",
             f"| 平均置信度 | {float(overall.get('average_confidence') or 0.0):.2f} |"]
    return "\n".join(lines)


def render_ai_language_table(by_language: list[dict]) -> str:
    lines = ["| 语言 | 函数数 | AI 疑似 | AI 疑似率 |", "| --- | --- | --- | --- |"]
    for ls in sorted(by_language, key=lambda x: -(x.get("llm_count") or 0)):
        lines.append(f"| {ls.get('language', '')} | {ls.get('total_functions', 0)} | "
                     f"{ls.get('llm_count', 0)} | {_pct(ls.get('llm_ratio'))} |")
    return "\n".join(lines)


def render_ai_highrisk_table(files: list[dict]) -> str:
    if not files:
        return "（无 AI 疑似函数聚集的文件）"
    lines = ["| 文件 | AI 疑似 | 函数数 | 疑似率 | 最可疑函数 |", "| --- | --- | --- | --- | --- |"]
    for f in files:
        lines.append(f"| {f.get('path', '')} | {f.get('llm_count', 0)} | {f.get('total_functions', 0)} | "
                     f"{_pct(f.get('llm_ratio'))} | {f.get('most_suspicious_fn', '') or '—'} |")
    return "\n".join(lines)


def render_ai_suspicious_table(funcs: list[dict]) -> str:
    if not funcs:
        return "（无高置信 AI 疑似函数）"
    lines = ["| 函数 | 文件:行 | 语言 | 置信度 | detect_score | 阶段 |", "| --- | --- | --- | --- | --- |"]
    for s in funcs:
        ds = f"{s['detect_score']:.4f}" if s.get("detect_score") is not None else "—"
        lines.append(f"| {s['name']} | {s['ref']} | {s['language']} | {s['confidence']} | {ds} | {s['stage']} |")
    return "\n".join(lines)


def render_ai_author_table(authors: list[dict]) -> str:
    if not authors:
        return ""
    lines = ["", "**按作者（git blame）：**", "", "| 作者 | AI 疑似函数 | 函数数 | AI 疑似率 |", "| --- | --- | --- | --- |"]
    for a in authors:
        lines.append(f"| {a.get('author', '')} | {a.get('llm_count', 0)} | "
                     f"{a.get('total_functions', 0)} | {_pct(a.get('llm_ratio'))} |")
    return "\n".join(lines)


def render_annotations(ann: dict) -> str:
    parts = [f"- **公共/框架代码(common_code)**：{ann.get('common_code_count', 0)} 个函数（高相似命中多个历史仓库，判为教学OS/框架公共代码，不计抄袭）"]
    for c in ann.get("common_code", []):
        parts.append(f"  - {c['new']} — 命中 {c['repos']} 个不同历史仓库")
    parts.append(f"- **基线衍生(baseline_derived)**：{ann['baseline_count']} 对（公共/模板代码，不计抄袭）")
    for b in ann["baseline"]:
        parts.append(f"  - {b['new']} — {b['note']}")
    parts.append(f"- **commit 异常信号**：{len(ann['commit_signals'])} 处")
    for c in ann["commit_signals"]:
        parts.append(f"  - {c['new']} — {', '.join(c['signals'])}")
    parts.append(f"- **结论分歧(disputed)，待人工复核**：{len(ann['disputed'])} 对")
    for d in ann["disputed"]:
        parts.append(f"  - {d['new']} ↔ {d['old']}")
    return "\n".join(parts)
