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

def high_similarity_pairs(suspects: list[dict]) -> list[dict]:
    """confirmed 或 verdict=likely_clone 的对。"""
    out = []
    for s in suspects:
        if s.get("tier") == "confirmed" or _verdict(s) == "likely_clone":
            q, c = _q(s), _c(s)
            rv = s.get("review") if isinstance(s.get("review"), dict) else {}
            out.append({
                "new_ref": f"{q['file_path']}:{q['start_line']}-{q['end_line']}",
                "old_ref": f"{c['repo_id']}/{c['file_path']}:{c['start_line']}-{c['end_line']}",
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

def innovation_functions(recall: dict, top_n: int = 10) -> list[dict]:
    """新作品中与全历史库最高相似度 < 0.5 且行数 > 30 的函数 Top-N（按行数降序）。"""
    out = []
    for item in recall.get("results", []):
        q = item["query"]
        cands = item.get("candidates", [])
        max_sim = max((c["score"] for c in cands), default=0.0)
        lines = q["end_line"] - q["start_line"] + 1
        if max_sim < INNOVATION_SIM_MAX and lines > INNOVATION_MIN_LINES:
            out.append({
                "ref": f"{q['file_path']}:{q['start_line']}-{q['end_line']}",
                "func_name": q["func_name"], "lines": lines,
                "max_sim": round(max_sim, 3), "raw_code": q.get("raw_code", ""),
            })
    out.sort(key=lambda x: x["lines"], reverse=True)
    return out[:top_n]


# ---------- 五、附注信号 ----------

def annotations(suspects: list[dict]) -> dict:
    baseline = [s for s in suspects if s.get("tier") == "baseline_derived"]
    commit_hits = [s for s in suspects if (s.get("evidence") or {}).get("commit_signals")]
    disputed = [s for s in suspects if _verdict(s) == "disputed"]
    # 公共/框架代码：按 query 函数去重（一个函数会有多条对）
    common_q: dict[str, int] = {}
    for s in suspects:
        if s.get("tier") == "common_code":
            ref = f"{_q(s)['file_path']}:{_q(s)['start_line']}"
            common_q[ref] = max(common_q.get(ref, 0), (s.get("evidence") or {}).get("common_code_repos", 0))
    return {
        "common_code_count": len(common_q),
        "common_code": [{"new": ref, "repos": n} for ref, n in sorted(common_q.items())],
        "baseline_count": len(baseline),
        "baseline": [
            {"new": f"{_q(s)['file_path']}:{_q(s)['start_line']}", "note": s.get("baseline_note", "")}
            for s in baseline
        ],
        "commit_signals": [
            {"new": f"{_q(s)['file_path']}:{_q(s)['start_line']}",
             "signals": (s.get("evidence") or {}).get("commit_signals", [])}
            for s in commit_hits
        ],
        "disputed": [
            {"new": f"{_q(s)['file_path']}:{_q(s)['start_line']}",
             "old": f"{_c(s)['repo_id']}/{_c(s)['file_path']}:{_c(s)['start_line']}"}
            for s in disputed
        ],
    }


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
