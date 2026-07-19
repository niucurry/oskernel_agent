"""检索：对新作品归一化→嵌入→查询 Qdrant top_k，写 recall.json 并出快速统计。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from loguru import logger

from src.normalize.runner import derive_repo_id, normalize_repo
from src.normalize.store import FunctionStore as NormStore, normalized_code_hash
from src.models import is_baseline_repo
from src.retrieval_contract import build_retrieval_contract

from .embedder import BaseEmbedder
from .vector_store import VectorStore

DEFAULT_OUTPUT_DIR = "data/output"
MAX_STRUCTURAL_CANDIDATES = 10_000

_QUERY_SELECT = (
    "SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag, lang, "
    "raw_code, normalized_code, feature_tokens FROM functions ORDER BY id"
)


def _normalize_query_repo(repo_path: Path, repos_root: Path) -> tuple[str, list]:
    """归一化新作品（临时内存库），返回 (repo_id, 函数行列表)。"""
    repo_id = derive_repo_id(repo_path, repos_root)
    with NormStore(":memory:") as ns:
        normalize_repo(repo_path, ns, repo_id=repo_id, repos_root=repos_root)
        ns.conn.row_factory = __import__("sqlite3").Row
        rows = ns.conn.execute(_QUERY_SELECT).fetchall()
    return repo_id, rows


def _vector_search(
    store: VectorStore, vec, top_k: int, *, exclude_repo_id: str,
    candidate_ids: list[int] | None = None,
) -> list[dict]:
    """全模块向量检索（取全局 top_k）。

    向量相似度是召回主信号，**不按 module_tag 硬过滤**：抄袭者常改动文件路径/目录结构，
    导致新作品函数的 module_tag 与历史源不一致；若先按模块过滤再检索，模块子集足以凑满
    top_k 时「不足才放开」的兜底永不触发，跨模块克隆会被整体漏召回（实测漏 ~80%）。
    module_tag 仅作为下游证据/展示信号，不参与召回过滤。

    candidate_ids 给定时把检索限定在该 id 集合内（用于在 SimHash 候选池内取向量 top_k，
    作为全局召回的并集补充——而非对全局召回做前置过滤）。
    """
    return store.search(vec, top_k, exclude_repo_id=exclude_repo_id, module_tag=None, candidate_ids=candidate_ids)


def _fingerprint_candidates(conn: sqlite3.Connection, normalized_code: str,
                            exclude_repo_id: str) -> list[dict]:
    """召回归一化代码完全相同的全部历史来源，每仓保留一个代表函数。

    该通道直接走 SQLite 指纹索引，不受 ANN top-k、向量阈值或同类候选拥挤影响。
    """
    if not normalized_code.strip():
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag,
                  normalized_code
           FROM functions
           WHERE normalized_hash=? AND repo_id<>?
           ORDER BY repo_id, id""",
        (normalized_code_hash(normalized_code), exclude_repo_id),
    ).fetchall()
    out: list[dict] = []
    seen_repos: set[str] = set()
    for r in rows:
        # SHA-256 碰撞双保险，也兼容未来指纹算法迁移。
        if r["normalized_code"] != normalized_code or r["repo_id"] in seen_repos:
            continue
        seen_repos.add(r["repo_id"])
        year_head = r["repo_id"].split("/", 1)[0]
        out.append({
            "id": r["id"],
            "score": 1.0,
            "recall_source": "fingerprint",
            "fingerprint_match": True,
            "payload": {
                "repo_id": r["repo_id"],
                "year": int(year_head) if year_head.isdigit() else None,
                "file_path": r["file_path"],
                "start_line": r["start_line"],
                "end_line": r["end_line"],
                "func_name": r["func_name"],
                "module_tag": r["module_tag"],
                "is_baseline": is_baseline_repo(r["repo_id"]),
            },
        })
    return out


def _name_candidates(conn: sqlite3.Connection, func_name: str,
                     exclude_repo_id: str) -> list[dict]:
    """同名方法确定性补召回；取全库结果后每仓留一个，不做数量截断。"""
    if len(func_name) < 5:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag
           FROM functions WHERE func_name=? AND repo_id<>? ORDER BY repo_id, id""",
        (func_name, exclude_repo_id),
    ).fetchall()
    if not rows:
        return []
    out: list[dict] = []
    seen_repos: set[str] = set()
    for r in rows:
        if r["repo_id"] in seen_repos:
            continue
        seen_repos.add(r["repo_id"])
        year_head = r["repo_id"].split("/", 1)[0]
        out.append({
            "id": r["id"], "score": 0.0, "recall_source": "function_name",
            "name_match": True,
            "payload": {
                "repo_id": r["repo_id"],
                "year": int(year_head) if year_head.isdigit() else None,
                "file_path": r["file_path"], "start_line": r["start_line"],
                "end_line": r["end_line"], "func_name": r["func_name"],
                "module_tag": r["module_tag"],
                "is_baseline": is_baseline_repo(r["repo_id"]),
            },
        })
    return out


def _structural_candidates(conn: sqlite3.Connection, matches: dict[int, int],
                           exclude_repo_id: str) -> list[dict]:
    """把结构 SimHash 命中直接转成 exact 候选，不再经过向量 top-k。"""
    if len(matches) > MAX_STRUCTURAL_CANDIDATES:
        raise RuntimeError(
            f"结构候选池 {len(matches)} 超过安全上限 {MAX_STRUCTURAL_CANDIDATES}；"
            "拒绝截断并误判为未命中，请提高上限或细化索引"
        )
    ids = sorted(matches)
    rows: list[sqlite3.Row] = []
    conn.row_factory = sqlite3.Row
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(conn.execute(
            f"""SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag
                 FROM functions WHERE id IN ({placeholders}) AND repo_id<>?""",
            [*chunk, exclude_repo_id],
        ).fetchall())
    out = []
    for r in rows:
        dist = int(matches[r["id"]])
        year_head = r["repo_id"].split("/", 1)[0]
        out.append({
            "id": r["id"], "score": round(1.0 - dist / 64.0, 6),
            "recall_source": "code_simhash", "structural_hash_match": True,
            "code_simhash_distance": dist,
            "payload": {
                "repo_id": r["repo_id"],
                "year": int(year_head) if year_head.isdigit() else None,
                "file_path": r["file_path"], "start_line": r["start_line"],
                "end_line": r["end_line"], "func_name": r["func_name"],
                "module_tag": r["module_tag"],
                "is_baseline": is_baseline_repo(r["repo_id"]),
            },
        })
    return sorted(out, key=lambda c: (c["code_simhash_distance"], c["id"]))


def query_repo(
    repo_path: str | Path,
    store: VectorStore,
    embedder: BaseEmbedder,
    *,
    top_k: int = 20,
    repos_root: str | Path = "data/repos",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    simhash_query=None,
    faiss_index_path: str | Path = "data/db/faiss_hnsw.index",
    faiss_ids_path: str | Path = "data/db/faiss_ids.npy",
    skip_files: set[str] | None = None,
    db_path: str | Path | None = None,
    code_simhash_query=None,
    history_coverage: dict | None = None,
    require_signed_faiss: bool = False,
) -> dict:
    """对新作品检索召回，写 recall.json，返回召回结果 dict。

    simhash_query 给定（src.simhash.build.SimHashQuery）时，SimHash 作为**并集补充通道**：
    召回 = 全局向量 top_k **∪** SimHash 候选池内的向量 top_k（按 func_id 去重）。
    SimHash 不再作前置硬过滤——改名/重写型克隆的真匹配（归一化后向量≈1.0）始终经全局
    向量通道召回，不被 SimHash 候选池钳制；SimHash 仅额外补召回 + 大规模初筛加速。
    每个候选标 ``recall_source``（vector / simhash）以便漏斗追溯各通道贡献。

    faiss_index_path：若存在则用 faiss HNSW 替代 qdrant-local 做 ANN 检索（快约 10000×）。

    skip_files：L0 文件指纹层命中的整文件复制清单（query 文件相对路径）。命中文件的全部
    函数跳过嵌入与检索（这些文件已由 fastpath 定案为「文件整体相同」），是 P1 提速核心。
    """
    repo_path = Path(repo_path)
    if code_simhash_query is not None and db_path is None:
        raise ValueError("结构 SimHash 召回必须同时提供 db_path")
    repos_root = Path(repos_root)
    repo_id, rows = _normalize_query_repo(repo_path, repos_root)
    if skip_files:
        before = len(rows)
        rows = [r for r in rows if r["file_path"] not in skip_files]
        logger.info("[{}] L0 文件指纹命中 {} 个文件，跳过其 {} 个函数的嵌入/检索",
                    repo_id, len(skip_files), before - len(rows))
    logger.info("[{}] 待检索函数 {} 个（SimHash 粗筛：{}）", repo_id, len(rows), "开" if simhash_query else "关")

    # 优先用 faiss HNSW（若索引存在）——比 qdrant-local SQLite 快约 10000×
    _search_store = store
    from .faiss_store import FaissVectorStore, load_faiss_index
    _fi = load_faiss_index(faiss_index_path, faiss_ids_path, db_path=db_path)
    if _fi is not None:
        _fi_idx, _fi_ids = _fi
        _search_store = FaissVectorStore(_fi_idx, _fi_ids, db_path=db_path or "data/db/functions.db")
        logger.info("[{}] 使用 faiss HNSW 检索（{} 向量）", repo_id, _fi_idx.ntotal)
    else:
        if require_signed_faiss:
            raise RuntimeError(
                "完整查全模式要求与 functions.db 同代的 FAISS 索引；"
                "索引缺失、损坏或签名过期，拒绝回退到未证明同代的向量库"
            )
        logger.warning("[{}] faiss 索引不存在，回退 qdrant-local（慢）；可运行 "
                       "`python -m src.embed build-faiss` 构建", repo_id)

    vecs = embedder.encode_batch([r["normalized_code"] for r in rows])

    # 指纹通道使用 functions.db。显式传入才启用，避免仅传内存 VectorStore 的库调用
    # 意外读取生产数据库；CLI/全流水线始终传入 --db。
    fp_conn: sqlite3.Connection | None = None
    if db_path is not None:
        # 触发旧库 normalized_hash 的一次性迁移，再以只读查询连接复用整个作品。
        with NormStore(db_path):
            pass
        fp_conn = sqlite3.connect(db_path)

    results = []
    cand_sizes: list[int] = []
    n_vector = 0          # 全局向量通道贡献的候选数
    n_simhash_added = 0   # SimHash 通道额外补充（全局向量未覆盖）的候选数
    n_fingerprint_added = 0
    n_name_added = 0
    n_structural_added = 0
    t0 = time.perf_counter()
    for row, vec in zip(rows, vecs):
        # 主通道：全局向量 top_k（不受 SimHash 候选池限制）
        cands = _vector_search(_search_store, vec, top_k, exclude_repo_id=repo_id)
        for c in cands:
            c["recall_source"] = "vector"
        n_vector += len(cands)

        # 补充通道：SimHash 候选池内的向量 top_k，去重后并入（并集，非交集过滤）
        if simhash_query is not None:
            sh_ids = sorted(simhash_query.query(json.loads(row["feature_tokens"] or "[]")))
            cand_sizes.append(len(sh_ids))
            if sh_ids:
                seen = {c["id"] for c in cands}
                extra = [
                    c for c in _vector_search(
                        _search_store, vec, top_k, exclude_repo_id=repo_id, candidate_ids=sh_ids)
                    if c["id"] not in seen
                ]
                for c in extra:
                    c["recall_source"] = "simhash"
                cands += extra
                n_simhash_added += len(extra)

        # 硬召回通道：完全归一化指纹相同的历史来源全部并入，不被 top-k 挤掉。
        if fp_conn is not None:
            seen = {c["id"] for c in cands}
            exact = _fingerprint_candidates(fp_conn, row["normalized_code"], repo_id)
            exact_ids = {x["id"] for x in exact}
            for c in cands:
                if c["id"] in exact_ids:
                    c["fingerprint_match"] = True
            added = [c for c in exact if c["id"] not in seen]
            cands += added
            n_fingerprint_added += len(added)

            seen = {c["id"] for c in cands}
            named = _name_candidates(fp_conn, row["func_name"], repo_id)
            named_ids = {x["id"] for x in named}
            for c in cands:
                if c["id"] in named_ids:
                    c["name_match"] = True
            added = [c for c in named if c["id"] not in seen]
            cands += added
            n_name_added += len(added)

            if code_simhash_query is not None:
                seen = {c["id"] for c in cands}
                structural = _structural_candidates(
                    fp_conn, code_simhash_query.query(row["normalized_code"]), repo_id)
                structural_ids = {x["id"] for x in structural}
                by_id = {x["id"]: x for x in structural}
                for c in cands:
                    if c["id"] in structural_ids:
                        c["structural_hash_match"] = True
                        c["code_simhash_distance"] = by_id[c["id"]]["code_simhash_distance"]
                added = [c for c in structural if c["id"] not in seen]
                cands += added
                n_structural_added += len(added)

        results.append(
            {
                "query": {
                    "repo_id": repo_id,
                    "file_path": row["file_path"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "func_name": row["func_name"],
                    "module_tag": row["module_tag"],
                    "lang": row["lang"],
                    # 供 Layer 4（src.exact）精确比对使用
                    "raw_code": row["raw_code"],
                    "normalized_code": row["normalized_code"],
                },
                "candidates": cands,
            }
        )

    if fp_conn is not None:
        fp_conn.close()
    search_elapsed = time.perf_counter() - t0
    total_recalled = sum(len(r["candidates"]) for r in results)
    simhash_stats = {
        "enabled": simhash_query is not None,
        "search_elapsed_sec": round(search_elapsed, 3),
        "total_recalled": total_recalled,
        "recall_sources": {"vector": n_vector, "simhash_added": n_simhash_added,
                           "fingerprint_added": n_fingerprint_added,
                           "function_name_added": n_name_added,
                           "code_simhash_added": n_structural_added},
    }
    if simhash_query is not None:
        simhash_stats["avg_candidate_pool"] = round(sum(cand_sizes) / len(cand_sizes), 1) if cand_sizes else 0
    logger.info(
        "[{}] 检索耗时 {:.2f}s，召回候选 {} 条（向量 {} ∪ 特征SimHash补 {} ∪ 指纹补 {} ∪ 同名补 {} ∪ 结构SimHash补 {}）{}",
        repo_id, search_elapsed, total_recalled, n_vector, n_simhash_added,
        n_fingerprint_added, n_name_added, n_structural_added,
        f"，SimHash 候选池均值 {simhash_stats['avg_candidate_pool']}" if simhash_query else "",
    )

    recall = {
        "query_repo_id": repo_id,
        "top_k": top_k,
        "retrieval_contract": build_retrieval_contract(
            history_coverage,
            complete=bool(db_path is not None and simhash_query is not None
                          and code_simhash_query is not None and _fi is not None
                          and (history_coverage or {}).get("complete", False)),
        ),
        "simhash": simhash_stats,
        "results": results,
    }
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{repo_path.name}_recall.json"
    out_path.write_text(json.dumps(recall, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("召回结果写入 {}", out_path)
    recall["_output_path"] = str(out_path)
    return recall


# ---------- 快速统计报告 ----------

def summarize(recall: dict) -> dict:
    """统计相似度分档计数 + 按历史仓库聚合的命中 Top-5。"""
    bins = {">0.9": 0, "0.8-0.9": 0, "0.7-0.8": 0}
    repo_hits: Counter[str] = Counter()
    for item in recall["results"]:
        for c in item["candidates"]:
            s = c["score"]
            if s > 0.9:
                bins[">0.9"] += 1
            elif s >= 0.8:
                bins["0.8-0.9"] += 1
            elif s >= 0.7:
                bins["0.7-0.8"] += 1
            else:
                continue
            repo_hits[c["payload"]["repo_id"]] += 1  # 仅统计 >=0.7 的命中
    return {"bins": bins, "top_repos": repo_hits.most_common(5)}


def print_report(recall: dict) -> dict:
    stats = summarize(recall)
    b = stats["bins"]
    logger.info("=== 快速统计（{}）===", recall["query_repo_id"])
    logger.info("相似度 >0.9: {} 对 | 0.8-0.9: {} 对 | 0.7-0.8: {} 对", b[">0.9"], b["0.8-0.9"], b["0.7-0.8"])
    logger.info("最像的历史作品 Top-5（命中次数，score>=0.7）：")
    if not stats["top_repos"]:
        logger.info("  （无 score>=0.7 的命中）")
    for rank, (repo_id, n) in enumerate(stats["top_repos"], 1):
        logger.info("  {}. {}  —— {} 次", rank, repo_id, n)
    return stats
