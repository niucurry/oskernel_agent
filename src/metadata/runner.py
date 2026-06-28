"""metadata 编排：在 suspects 上叠加三个辅助信号通道，输出 *_final.json。"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from loguru import logger

from src.exact.matcher import ExactMatcher, remap_spans
from src.exact.verify import tier_of
from src.normalize.normalizer import normalize_snippet
from src.normalize.store import DEFAULT_DB

from .baseline import BaselineMatcher, is_baseline_derived
from .commits import analyze_function
from .config import MetadataSettings, load_metadata_settings
from .strings import build_reverse_index, fetch_function, string_hits_for_func

DEFAULT_OUTPUT_DIR = "data/output"


def _suspect_key(s: dict) -> tuple:
    q, c = s["query_func"], s["candidate_func"]
    return (q["file_path"], q["start_line"], c["repo_id"], c["file_path"], c["start_line"])


_EXACT_MATCHER = ExactMatcher()


def _new_suspect(qf: dict, hf: dict, count: int) -> dict:
    """字符串通道新建的嫌疑对（独立召回路径）。

    旁路通道不再硬编码 ``final_score=0``：对新对实跑行级精确比对，用 similar_line_ratio
    填 final_score、tier_of 定档（行级相同的对从此显示真实相似度，回应 D1）。独特字符串
    命中本身即 review 级信号，故 tier 不低于 review（ratio<0.5 时 tier_of 返回 None 也兜底为 review）。
    """
    res = _EXACT_MATCHER.match(qf.get("raw_code", ""), hf.get("raw_code", ""), qf.get("lang", "rust"))
    tier = tier_of(res.similar_line_ratio) or "review"
    abs_spans = remap_spans(res.matched_spans, qf.get("start_line", 1), hf.get("start_line", 1))
    return {
        "tier": tier,
        "source": "string_channel",
        "final_score": res.similar_line_ratio,
        "query_func": qf,
        "candidate_func": {
            "repo_id": hf["repo_id"], "file_path": hf["file_path"],
            "start_line": hf["start_line"], "end_line": hf["end_line"],
            "func_name": hf["func_name"], "module_tag": hf["module_tag"],
            "lang": hf["lang"], "raw_code": hf["raw_code"], "normalized_code": hf["normalized_code"],
        },
        "evidence": {
            "vector_similarity": None,
            "exact_match_lines": res.exact_match_lines,
            "renamed_match_lines": res.renamed_match_lines,
            "unique_string_matches": count,
        },
        "matched_spans": abs_spans,
        "match_type_per_span": res.match_type_per_span,
    }


def channel_unique_strings(data: dict, db_path: str | Path, settings: MetadataSettings) -> int:
    """通道 1：独特字符串召回，bump 已有对或新建对。返回新建数。"""
    suspects: list[dict] = data["suspects"]
    index = build_reverse_index(db_path, generic_threshold=settings.string_generic_repo_threshold)

    existing = {_suspect_key(s): s for s in suspects}
    query_funcs: dict[tuple, dict] = {}
    for s in suspects:
        q = s["query_func"]
        query_funcs.setdefault((q["file_path"], q["start_line"]), q)

    new_count = 0
    for qf in query_funcs.values():
        strings = normalize_snippet(qf.get("raw_code", ""), qf.get("lang", "rust")).strings
        for hist_fid, (hist_repo, count) in string_hits_for_func(
            strings, index, exclude_repo_id=qf.get("repo_id")
        ).items():
            hf = fetch_function(db_path, hist_fid)
            if not hf:
                continue
            key = (qf["file_path"], qf["start_line"], hf["repo_id"], hf["file_path"], hf["start_line"])
            if key in existing:
                ev = existing[key].setdefault("evidence", {})
                ev["unique_string_matches"] = max(ev.get("unique_string_matches") or 0, count)
            else:
                sp = _new_suspect(qf, hf, count)
                existing[key] = sp
                suspects.append(sp)
                new_count += 1
    logger.info("通道1 独特字符串：新建嫌疑对 {} 个", new_count)
    return new_count


def channel_baseline(data: dict, matcher: BaselineMatcher, settings: MetadataSettings) -> int:
    """通道 2：基线扣除。双侧命中同一基线函数则降级 baseline_derived。返回扣除数。"""
    n = 0
    for s in data["suspects"]:
        q_match = matcher.match(s["query_func"].get("normalized_code", ""))
        c_match = matcher.match(s["candidate_func"].get("normalized_code", ""))
        if is_baseline_derived(q_match, c_match, threshold=settings.baseline_sim_threshold):
            ev = s.setdefault("evidence", {})
            ev["baseline_flag"] = True
            s["tier"] = "baseline_derived"
            s["baseline_note"] = (
                f"双侧均与同一基线函数(id={q_match[0]})相似 "
                f"(q={q_match[1]:.3f}, c={c_match[1]:.3f} > {settings.baseline_sim_threshold})"
            )
            n += 1
    logger.info("通道2 基线扣除：{} 个降为 baseline_derived", n)
    return n


def channel_common_code(data: dict, settings: MetadataSettings) -> int:
    """通道 4：公共/框架代码广度过滤。

    一个 query 函数若以高相似度（vector >= common_code_sim_threshold，或已判 confirmed）命中
    >= common_code_repo_threshold 个**不同历史仓库**，则它几乎必然是教学OS/框架公共代码
    （多队合法复用），而非从某一个队抄袭。把该函数的全部嫌疑对降级 common_code（不计抄袭）。
    与“字符串出现在 >N 仓即视为通用”同一思路，无需注册基线即可生效。返回降级的嫌疑对数。
    """
    suspects = data["suspects"]
    by_query: dict[tuple, list[dict]] = defaultdict(list)
    for s in suspects:
        q = s["query_func"]
        by_query[(q["file_path"], q["start_line"])].append(s)

    n = 0
    for group in by_query.values():
        strong_repos = set()
        for s in group:
            vec = (s.get("evidence") or {}).get("vector_similarity")
            if s.get("tier") == "confirmed" or (vec is not None and vec >= settings.common_code_sim_threshold):
                strong_repos.add(s["candidate_func"]["repo_id"])
        if len(strong_repos) >= settings.common_code_repo_threshold:
            for s in group:
                if s.get("tier") == "baseline_derived":  # 更具体的基线信号优先
                    continue
                s.setdefault("evidence", {})["common_code_repos"] = len(strong_repos)
                # confirmed（精确/重命名级完全相同）是确凿事实，必须保留展示在报告中；
                # 仅加「命中多库」标注供人工判断是否通用框架代码，不降级、不计入 common_code。
                if s.get("tier") == "confirmed":
                    s["common_code_note"] = (
                        f"该函数精确命中 {len(strong_repos)} 个不同历史仓库，"
                        f"疑为公共/框架代码（仍按确认借鉴展示，供人工判断）"
                    )
                    continue
                s["tier"] = "common_code"
                s["common_code_note"] = (
                    f"该函数高相似命中 {len(strong_repos)} 个不同历史仓库，判为公共/框架代码（不计入借鉴/复制）"
                )
                n += 1
    logger.info("通道4 公共代码广度过滤：{} 个嫌疑对降为 common_code", n)
    return n


def channel_commits(data: dict, repo_path: str | Path, meta_commits: list[dict], settings: MetadataSettings) -> int:
    """通道 3：commit 异常信号（附注，不改 tier）。返回有信号的嫌疑数。"""
    n = 0
    for s in data["suspects"]:
        signals = analyze_function(repo_path, meta_commits, s["query_func"], settings)
        if signals:
            s.setdefault("evidence", {})["commit_signals"] = signals
            n += 1
    logger.info("通道3 commit 信号：{} 个嫌疑函数命中信号", n)
    return n


def process_metadata(
    data: dict,
    db_path: str | Path = DEFAULT_DB,
    *,
    settings: MetadataSettings | None = None,
    baseline_matcher: BaselineMatcher | None = None,
    query_repo: str | Path | None = None,
    meta_commits: list[dict] | None = None,
) -> dict:
    """按需运行三通道（通道1 总是运行；2/3 视后端可用性）。"""
    settings = settings or load_metadata_settings()
    summary = {"string_new_pairs": channel_unique_strings(data, db_path, settings)}
    summary["common_code_pairs"] = channel_common_code(data, settings)
    if baseline_matcher is not None:
        summary["baseline_derived"] = channel_baseline(data, baseline_matcher, settings)
    if query_repo is not None and meta_commits is not None:
        summary["commit_signal_hits"] = channel_commits(data, query_repo, meta_commits, settings)
    data["metadata_summary"] = summary
    return data


def run_metadata(suspects_path: str | Path, *, db_path=DEFAULT_DB, output_dir=DEFAULT_OUTPUT_DIR,
                 baseline_matcher=None, query_repo=None, meta_commits=None, settings=None) -> dict:
    suspects_path = Path(suspects_path)
    data = json.loads(suspects_path.read_text(encoding="utf-8"))
    data = process_metadata(
        data, db_path, settings=settings, baseline_matcher=baseline_matcher,
        query_repo=query_repo, meta_commits=meta_commits,
    )
    stem = suspects_path.stem
    base = stem[:-3] if stem.endswith("_v2") else stem
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{base}_final.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("metadata 完成：{} → {}", data.get("metadata_summary"), out_path)
    data["_output_path"] = str(out_path)
    return data
