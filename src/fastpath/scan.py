"""L0 文件指纹扫描：新作品逐文件规范化哈希 → 查历史 files 表同 hash（异 repo）。

命中即整文件复制：产出 {repo}_filematch.json，并返回需在 recall 跳过的文件清单。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from loguru import logger

from src.exact.matcher import normalized_file_hash, normalized_file_lines
from src.models import is_baseline_repo
from src.normalize.discovery import discover_files
from src.normalize.runner import DEFAULT_MAX_LINES, DEFAULT_REPOS_ROOT, derive_repo_id
from src.normalize.store import DEFAULT_DB, FunctionStore

DEFAULT_OUTPUT_DIR = "data/output"

# 规范化后非空行少于此数的文件不参与文件级匹配（避免空文件/模块声明桩等误命中）。
MIN_FILE_LINES = 5

# 文件内 confirmed/review 命中函数占比 >= 此值 → 判「文件整体相似」（后聚合）。
WHOLE_FILE_SIM_RATIO = 0.95

# 同一规范化文件出现在 >= 此数的历史仓库 → 判「广泛共享的公共代码」（vendored crate、
# sysroot 头文件、官方测试集等），不计作两队之间的整文件复制。目录排除清单只能覆盖已知基础
# 设施名；本阈值按数据自动消化其余第三方/模板文件（实测真实语料 L0 误报绝大多数属此类）。
COMMON_FILE_MIN_REPOS = 3


def scan_repo(
    repo_path: str | Path,
    *,
    repo_id: str | None = None,
    repos_root: str | Path = DEFAULT_REPOS_ROOT,
    db_path: str | Path = DEFAULT_DB,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    max_lines: int = DEFAULT_MAX_LINES,
    min_file_lines: int = MIN_FILE_LINES,
    common_file_min_repos: int = COMMON_FILE_MIN_REPOS,
) -> dict:
    """扫描新作品的整文件复制，写 {repo}_filematch.json，返回结果 dict。

    返回 dict 含 ``skip_files``（list[str]，命中的 query 文件相对路径），供 recall 跳过。
    """
    repo_path = Path(repo_path)
    repo_id = repo_id or derive_repo_id(repo_path, Path(repos_root))
    files = discover_files(repo_path)

    matched: list[dict] = []
    skip_files: list[str] = []
    common_files = 0
    with FunctionStore(db_path) as store:
        for f in files:
            try:
                text = f.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if text.count("\n") + 1 > max_lines:
                continue
            if len(normalized_file_lines(text, f.lang)) < min_file_lines:
                continue
            nh = normalized_file_hash(text, f.lang)
            hist = store.find_files_by_norm_hash(nh, exclude_repo_id=repo_id)
            if not hist:
                continue
            repo_ids = {h["repo_id"] for h in hist}
            # 命中基线库（已知公共/模板/第三方库）→ 公共代码；或出现在 >= 阈值个历史仓库 →
            # 广泛共享。两者都跳过召回省算力，但不报为复制（基线消化 2 仓库级 vendored 库，
            # 阈值消化其余广泛共享文件）。
            if any(is_baseline_repo(r) for r in repo_ids) or len(repo_ids) >= common_file_min_repos:
                common_files += 1
                skip_files.append(f.rel_path)
                continue
            repo_count = len(repo_ids)
            matched.append({
                "query_file": f.rel_path,
                "lang": f.lang,
                "line_count": text.count("\n") + 1,
                "norm_hash": nh,
                "hist_repo_count": repo_count,
                "matches": [
                    {"repo_id": h["repo_id"], "file_path": h["file_path"],
                     "line_count": h["line_count"], "func_count": h["func_count"]}
                    for h in hist
                ],
            })
            skip_files.append(f.rel_path)

    result = {
        "query_repo_id": repo_id,
        "scanned_files": len(files),
        "matched_files": matched,
        "common_files": common_files,
        "skip_files": skip_files,
    }
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{repo_path.name}_filematch.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("[fastpath] 整文件复制 {} 个、公共代码跳过 {} 个（扫描 {} 文件）→ {}",
                len(matched), common_files, len(files), out_path)
    result["_output_path"] = str(out_path)
    return result


def aggregate_file_similarity(
    suspects: list[dict],
    recall: dict | None,
    *,
    ratio: float = WHOLE_FILE_SIM_RATIO,
) -> list[dict]:
    """后聚合：统计每个 query 文件的函数总数与 confirmed/review 命中数，
    命中占比 >= ratio 的文件判「文件整体相似」（区别于 fastpath 的逐字节整文件相同）。

    返回 [{file_path, module, total, hit, ratio, top_source}, ...]，按 ratio 降序。
    """
    # 每文件函数总数（优先用 recall 的 query 函数计数）
    total_by_file: dict[str, int] = defaultdict(int)
    if recall:
        for item in recall.get("results", []):
            q = item.get("query", {})
            total_by_file[q.get("file_path", "")] += 1

    hit_funcs: dict[str, set[str]] = defaultdict(set)
    # 来源按 (repo_id, 来源文件路径) 计数，使「主要来源」能链到对应文件而非仅仓库主页
    sources: dict[str, dict[tuple[str, str], int]] = defaultdict(lambda: defaultdict(int))
    module_of: dict[str, str] = {}
    for s in suspects:
        if s.get("tier") not in ("confirmed", "review"):
            continue
        q = s.get("query_func", {})
        c = s.get("candidate_func", {})
        fp = q.get("file_path", "")
        hit_funcs[fp].add(q.get("func_name", ""))
        module_of.setdefault(fp, q.get("module_tag", "other"))
        sources[fp][(c.get("repo_id", "?"), c.get("file_path", ""))] += 1
        if fp not in total_by_file:
            total_by_file[fp] = 0

    out: list[dict] = []
    for fp, hits in hit_funcs.items():
        total = max(total_by_file.get(fp, 0), len(hits))
        if total <= 0:
            continue
        r = len(hits) / total
        if r < ratio:
            continue
        (top_repo, top_file), _ = max(
            sources[fp].items(), key=lambda kv: kv[1], default=(("—", ""), 0))
        out.append({
            "file_path": fp,
            "module": module_of.get(fp, "other"),
            "total": total,
            "hit": len(hits),
            "ratio": round(r, 3),
            "top_source": top_repo,
            "top_source_file": top_file,
        })
    out.sort(key=lambda x: -x["ratio"])
    return out
