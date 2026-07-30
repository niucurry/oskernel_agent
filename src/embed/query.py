"""检索：对新作品归一化→嵌入→查询 Qdrant top_k，写 recall.json 并出快速统计。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

from loguru import logger

from src.exact.identity import (
    MIN_IDENTITY_SCORE,
    FunctionIdentityFeatures,
    compare_function_identity_features,
    function_identity_features,
)
from src.normalize.runner import derive_repo_id, normalize_repo
from src.normalize.store import FunctionStore as NormStore, normalized_code_hash
from src.models import is_baseline_repo
from src.retrieval_contract import build_retrieval_contract

from .embedder import BaseEmbedder
from .vector_store import VectorStore

DEFAULT_OUTPUT_DIR = "data/output"
MAX_STRUCTURAL_CANDIDATES = 10_000
MAX_IDENTITY_NEIGHBORS_SCANNED = 10_000
MAX_IDENTITY_DOMAIN_CACHE = 2_048
MAX_IDENTITY_FEATURE_CACHE = 50_000

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
    candidate_ids: list[int] | None = None, lang: str | None = None,
) -> list[dict]:
    """全模块向量检索（取全局 top_k）。

    向量相似度是召回主信号，**不按 module_tag 硬过滤**：抄袭者常改动文件路径/目录结构，
    导致新作品函数的 module_tag 与历史源不一致；若先按模块过滤再检索，模块子集足以凑满
    top_k 时「不足才放开」的兜底永不触发，跨模块克隆会被整体漏召回（实测漏 ~80%）。
    module_tag 仅作为下游证据/展示信号，不参与召回过滤。

    candidate_ids 给定时把检索限定在该 id 集合内（用于在 SimHash 候选池内取向量 top_k，
    作为全局召回的并集补充——而非对全局召回做前置过滤）。
    """
    if getattr(store, "supports_language_filter", False):
        return store.search(
            vec, top_k, exclude_repo_id=exclude_repo_id, module_tag=None,
            candidate_ids=candidate_ids, lang=lang,
        )
    # 旧 Qdrant payload 可能没有 lang；先扩大候选窗，随后用 functions.db 做权威过滤。
    return store.search(
        vec, top_k * 4, exclude_repo_id=exclude_repo_id,
        module_tag=None, candidate_ids=candidate_ids,
    )


def _same_language_candidates(
    conn: sqlite3.Connection, candidates: list[dict], lang: str, *, limit: int | None = None,
    language_cache: dict[int, str | None] | None = None,
) -> list[dict]:
    """按 functions.db 的 lang 字段过滤候选；保持原排名并可截取同语言 top-k。"""
    ids = list(dict.fromkeys(int(c["id"]) for c in candidates if c.get("id") is not None))
    if not ids:
        return []
    cache = language_cache if language_cache is not None else {}
    missing = [func_id for func_id in ids if func_id not in cache]
    for start in range(0, len(missing), 900):
        chunk = missing[start:start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id, lang FROM functions WHERE id IN ({placeholders})", chunk,
        ).fetchall()
        found = {int(row[0]): str(row[1] or "").lower() for row in rows}
        for func_id in chunk:
            cache[func_id] = found.get(func_id)
    wanted = lang.lower()
    allowed = {func_id for func_id in ids if cache.get(func_id) == wanted}
    result = [c for c in candidates if int(c["id"]) in allowed]
    return result[:limit] if limit is not None else result


def _fingerprint_candidates(conn: sqlite3.Connection, normalized_code: str,
                            exclude_repo_id: str, lang: str | None = None) -> list[dict]:
    """召回归一化代码完全相同的全部历史来源，每仓保留一个代表函数。

    该通道直接走 SQLite 指纹索引，不受 ANN top-k、向量阈值或同类候选拥挤影响。
    """
    if not normalized_code.strip():
        return []
    conn.row_factory = sqlite3.Row
    params: tuple = (normalized_code_hash(normalized_code), exclude_repo_id)
    lang_clause = ""
    if lang is not None:
        lang_clause = " AND lang=?"
        params += (lang.lower(),)
    rows = conn.execute(
        """SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag,
                  lang, normalized_code
           FROM functions
           WHERE normalized_hash=? AND repo_id<>?"""
        + lang_clause + " ORDER BY repo_id, id",
        params,
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
                "lang": r["lang"],
                "is_baseline": is_baseline_repo(r["repo_id"]),
            },
        })
    return out


def _name_candidates(conn: sqlite3.Connection, func_name: str,
                     exclude_repo_id: str, lang: str | None = None) -> list[dict]:
    """同名方法确定性补召回；取全库结果后每仓留一个，不做数量截断。"""
    if len(func_name) < 5:
        return []
    conn.row_factory = sqlite3.Row
    params: tuple = (func_name, exclude_repo_id)
    lang_clause = ""
    if lang is not None:
        lang_clause = " AND lang=?"
        params += (lang.lower(),)
    rows = conn.execute(
        """SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag, lang
           FROM functions WHERE func_name=? AND repo_id<>?"""
        + lang_clause + " ORDER BY repo_id, id",
        params,
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
                "lang": r["lang"],
                "is_baseline": is_baseline_repo(r["repo_id"]),
            },
        })
    return out


def _structural_candidates(conn: sqlite3.Connection, matches: dict[int, int],
                           exclude_repo_id: str, lang: str | None = None) -> list[dict]:
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
        params: list = [*chunk, exclude_repo_id]
        lang_clause = ""
        if lang is not None:
            lang_clause = " AND lang=?"
            params.append(lang.lower())
        rows.extend(conn.execute(
            f"""SELECT id, repo_id, file_path, start_line, end_line, func_name, module_tag, lang
                 FROM functions WHERE id IN ({placeholders}) AND repo_id<>?"""
            + lang_clause,
            params,
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
                "lang": r["lang"],
                "is_baseline": is_baseline_repo(r["repo_id"]),
            },
        })
    return sorted(out, key=lambda c: (c["code_simhash_distance"], c["id"]))


def _identity_neighbor_candidates(
    conn: sqlite3.Connection,
    query: dict | sqlite3.Row,
    candidates: list[dict],
    exclude_repo_id: str,
    *,
    seed_location_cache: dict[int, tuple[str, str] | None] | None = None,
    domain_rows_cache: dict[tuple[str, str, str], list[dict]] | None = None,
    feature_cache: dict[int, FunctionIdentityFeatures] | None = None,
) -> tuple[list[dict], int]:
    """扩展已召回候选所在文件中的身份兼容函数。

    向量经常命中“同一文件内使用相同模板的邻近操作”。这里以这些文件为有界候选域，
    再按函数名、签名和行为 token 找具体对应函数；不会扫描或硬编码某个仓库路径。
    """
    seed_ids = list(dict.fromkeys(
        int(candidate["id"])
        for candidate in candidates
        if candidate.get("id") is not None
        and candidate.get("recall_source") != "function_name"
    ))
    if not seed_ids:
        return [], 0
    conn.row_factory = sqlite3.Row
    location_cache = seed_location_cache if seed_location_cache is not None else {}
    missing_seed_ids = [func_id for func_id in seed_ids if func_id not in location_cache]
    for start in range(0, len(missing_seed_ids), 900):
        chunk = missing_seed_ids[start:start + 900]
        placeholders = ",".join("?" for _ in chunk)
        fetched = conn.execute(
            f"SELECT id, repo_id, file_path FROM functions WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        found = {
            int(row["id"]): (str(row["repo_id"]), str(row["file_path"]))
            for row in fetched
        }
        for func_id in chunk:
            location_cache[func_id] = found.get(func_id)
    domains = sorted({location_cache[func_id] for func_id in seed_ids
                      if location_cache.get(func_id) is not None
                      and location_cache[func_id][0] != exclude_repo_id})

    cached_domains = domain_rows_cache if domain_rows_cache is not None else {}
    rows: list[dict] = []
    for repo_id, file_path in domains:
        domain_key = (repo_id, file_path, str(query["lang"]).lower())
        domain_rows = cached_domains.get(domain_key)
        if domain_rows is None:
            domain_rows = [dict(row) for row in conn.execute(
                """SELECT id, repo_id, file_path, start_line, end_line, func_name,
                          module_tag, lang, raw_code
                   FROM functions
                   WHERE repo_id=? AND file_path=? AND lang=?
                   ORDER BY start_line, id""",
                (repo_id, file_path, str(query["lang"]).lower()),
            ).fetchall()]
            if domain_rows_cache is not None:
                if len(cached_domains) >= MAX_IDENTITY_DOMAIN_CACHE:
                    cached_domains.pop(next(iter(cached_domains)))
                cached_domains[domain_key] = domain_rows
        rows.extend(domain_rows)
        if len(rows) > MAX_IDENTITY_NEIGHBORS_SCANNED:
            raise RuntimeError(
                f"身份邻域扫描 {len(rows)} 个函数超过安全上限 "
                f"{MAX_IDENTITY_NEIGHBORS_SCANNED}；拒绝静默截断候选"
            )

    existing = {int(candidate["id"]): candidate for candidate in candidates}
    added: list[dict] = []
    query_features = function_identity_features(
        query["func_name"], query["raw_code"],
    )
    for row in rows:
        candidate_features = feature_cache.get(int(row["id"])) if feature_cache is not None else None
        if candidate_features is None:
            candidate_features = function_identity_features(
                row["func_name"], row["raw_code"] or "",
            )
            if feature_cache is not None:
                if len(feature_cache) >= MAX_IDENTITY_FEATURE_CACHE:
                    feature_cache.pop(next(iter(feature_cache)))
                feature_cache[int(row["id"])] = candidate_features
        identity = compare_function_identity_features(
            query_features, candidate_features,
        )
        if row["id"] in existing:
            existing[row["id"]]["identity_score"] = identity["score"]
            existing[row["id"]]["identity_components"] = identity
            continue
        # 非同名函数只有在行为也有交集时才扩展；同名仍需总身份分通过签名约束。
        if identity["score"] < MIN_IDENTITY_SCORE:
            continue
        if not identity["exact_name"] and identity["behavior"] < 0.25:
            continue
        year_head = row["repo_id"].split("/", 1)[0]
        added.append({
            "id": row["id"],
            "score": 0.0,
            "recall_source": "identity_neighbor",
            "identity_expansion": True,
            "identity_score": identity["score"],
            "identity_components": identity,
            "payload": {
                "repo_id": row["repo_id"],
                "year": int(year_head) if year_head.isdigit() else None,
                "file_path": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "func_name": row["func_name"],
                "module_tag": row["module_tag"],
                "lang": row["lang"],
                "is_baseline": is_baseline_repo(row["repo_id"]),
            },
        })
    return added, len(rows)


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
    db_signature: dict | None = None,
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
    query_started = time.perf_counter()
    repo_path = Path(repo_path)
    if code_simhash_query is not None and db_path is None:
        raise ValueError("结构 SimHash 召回必须同时提供 db_path")
    repos_root = Path(repos_root)
    phase_started = time.perf_counter()
    repo_id, rows = _normalize_query_repo(repo_path, repos_root)
    normalize_elapsed = time.perf_counter() - phase_started
    if skip_files:
        before = len(rows)
        rows = [r for r in rows if r["file_path"] not in skip_files]
        logger.info("[{}] L0 文件指纹命中 {} 个文件，跳过其 {} 个函数的嵌入/检索",
                    repo_id, len(skip_files), before - len(rows))
    logger.info("[{}] 待检索函数 {} 个（SimHash 粗筛：{}）", repo_id, len(rows), "开" if simhash_query else "关")

    # 优先用 faiss HNSW（若索引存在）——比 qdrant-local SQLite 快约 10000×
    _search_store = store
    from .faiss_store import FaissVectorStore, load_faiss_index
    phase_started = time.perf_counter()
    _fi = load_faiss_index(
        faiss_index_path, faiss_ids_path, db_path=db_path,
        db_signature=db_signature,
    )
    index_load_elapsed = time.perf_counter() - phase_started
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

    phase_started = time.perf_counter()
    vecs = embedder.encode_batch([r["normalized_code"] for r in rows])
    embedding_elapsed = time.perf_counter() - phase_started

    # 指纹通道使用 functions.db。显式传入才启用，避免仅传内存 VectorStore 的库调用
    # 意外读取生产数据库；CLI/全流水线始终传入 --db。
    fp_conn: sqlite3.Connection | None = None
    phase_started = time.perf_counter()
    if db_path is not None:
        # 触发旧库 normalized_hash 的一次性迁移，再以只读查询连接复用整个作品。
        with NormStore(db_path):
            pass
        fp_conn = sqlite3.connect(db_path)
    db_prepare_elapsed = time.perf_counter() - phase_started
    language_cache: dict[int, str | None] = {}
    identity_seed_cache: dict[int, tuple[str, str] | None] = {}
    identity_domain_cache: dict[tuple[str, str, str], list[dict]] = {}
    identity_feature_cache: dict[int, FunctionIdentityFeatures] = {}
    store_filters_language = bool(
        getattr(_search_store, "supports_language_filter", False)
    )

    results = []
    cand_sizes: list[int] = []
    n_vector = 0          # 全局向量通道贡献的候选数
    n_simhash_added = 0   # SimHash 通道额外补充（全局向量未覆盖）的候选数
    n_fingerprint_added = 0
    n_name_added = 0
    n_structural_added = 0
    n_identity_added = 0
    n_identity_scanned = 0
    n_cross_language_filtered = 0
    channel_elapsed: Counter[str] = Counter()
    t0 = time.perf_counter()
    for row, vec in zip(rows, vecs):
        # 主通道：全局向量 top_k（不受 SimHash 候选池限制）
        phase_started = time.perf_counter()
        cands = _vector_search(
            _search_store, vec, top_k, exclude_repo_id=repo_id, lang=row["lang"])
        channel_elapsed["vector_search"] += time.perf_counter() - phase_started
        if fp_conn is not None and not store_filters_language:
            before = len(cands)
            phase_started = time.perf_counter()
            cands = _same_language_candidates(
                fp_conn, cands, row["lang"], limit=top_k,
                language_cache=language_cache,
            )
            channel_elapsed["language_filter"] += time.perf_counter() - phase_started
            n_cross_language_filtered += before - len(cands)
        for c in cands:
            c["recall_source"] = "vector"
        n_vector += len(cands)

        # 补充通道：SimHash 候选池内的向量 top_k，去重后并入（并集，非交集过滤）
        if simhash_query is not None:
            phase_started = time.perf_counter()
            sh_ids = sorted(simhash_query.query(json.loads(row["feature_tokens"] or "[]")))
            channel_elapsed["feature_simhash_lookup"] += time.perf_counter() - phase_started
            cand_sizes.append(len(sh_ids))
            if sh_ids:
                seen = {c["id"] for c in cands}
                phase_started = time.perf_counter()
                extra = [
                    c for c in _vector_search(
                        _search_store, vec, top_k, exclude_repo_id=repo_id,
                        candidate_ids=sh_ids, lang=row["lang"])
                    if c["id"] not in seen
                ]
                channel_elapsed["feature_simhash_vector"] += time.perf_counter() - phase_started
                if fp_conn is not None and not store_filters_language:
                    before = len(extra)
                    phase_started = time.perf_counter()
                    extra = _same_language_candidates(
                        fp_conn, extra, row["lang"], limit=top_k,
                        language_cache=language_cache,
                    )
                    channel_elapsed["language_filter"] += time.perf_counter() - phase_started
                    n_cross_language_filtered += before - len(extra)
                for c in extra:
                    c["recall_source"] = "simhash"
                cands += extra
                n_simhash_added += len(extra)

        # 硬召回通道：完全归一化指纹相同的历史来源全部并入，不被 top-k 挤掉。
        if fp_conn is not None:
            seen = {c["id"] for c in cands}
            phase_started = time.perf_counter()
            exact = _fingerprint_candidates(
                fp_conn, row["normalized_code"], repo_id, row["lang"])
            channel_elapsed["fingerprint"] += time.perf_counter() - phase_started
            exact_ids = {x["id"] for x in exact}
            for c in cands:
                if c["id"] in exact_ids:
                    c["fingerprint_match"] = True
            added = [c for c in exact if c["id"] not in seen]
            cands += added
            n_fingerprint_added += len(added)

            seen = {c["id"] for c in cands}
            phase_started = time.perf_counter()
            named = _name_candidates(fp_conn, row["func_name"], repo_id, row["lang"])
            channel_elapsed["function_name"] += time.perf_counter() - phase_started
            named_ids = {x["id"] for x in named}
            for c in cands:
                if c["id"] in named_ids:
                    c["name_match"] = True
            added = [c for c in named if c["id"] not in seen]
            cands += added
            n_name_added += len(added)

            if code_simhash_query is not None:
                seen = {c["id"] for c in cands}
                phase_started = time.perf_counter()
                structural = _structural_candidates(
                    fp_conn, code_simhash_query.query(row["normalized_code"]),
                    repo_id, row["lang"])
                channel_elapsed["code_simhash"] += time.perf_counter() - phase_started
                structural_ids = {x["id"] for x in structural}
                by_id = {x["id"]: x for x in structural}
                for c in cands:
                    if c["id"] in structural_ids:
                        c["structural_hash_match"] = True
                        c["code_simhash_distance"] = by_id[c["id"]]["code_simhash_distance"]
                added = [c for c in structural if c["id"] not in seen]
                cands += added
                n_structural_added += len(added)

            # 末端硬过滤兼容旧向量 payload 与未来新增召回通道：任何跨语言候选都不得
            # 写入 recall.json，更不会进入后续精确核验或创新实现地图。
            before = len(cands)
            phase_started = time.perf_counter()
            cands = _same_language_candidates(
                fp_conn, cands, row["lang"], language_cache=language_cache,
            )
            channel_elapsed["language_filter"] += time.perf_counter() - phase_started
            n_cross_language_filtered += before - len(cands)

            phase_started = time.perf_counter()
            identity_added, identity_scanned = _identity_neighbor_candidates(
                fp_conn, row, cands, repo_id,
                seed_location_cache=identity_seed_cache,
                domain_rows_cache=identity_domain_cache,
                feature_cache=identity_feature_cache,
            )
            channel_elapsed["identity_neighbor"] += time.perf_counter() - phase_started
            if identity_added:
                cands += identity_added
                n_identity_added += len(identity_added)
            n_identity_scanned += identity_scanned

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
                           "code_simhash_added": n_structural_added,
                           "identity_neighbor_added": n_identity_added},
        "cross_language_filtered": n_cross_language_filtered,
        "identity_neighbors_scanned": n_identity_scanned,
        "timings_sec": {
            "normalize": round(normalize_elapsed, 3),
            "index_load": round(index_load_elapsed, 3),
            "embedding": round(embedding_elapsed, 3),
            "history_db_prepare": round(db_prepare_elapsed, 3),
            "search_loop": round(search_elapsed, 3),
            **{name: round(value, 3) for name, value in sorted(channel_elapsed.items())},
            "query_before_output": round(time.perf_counter() - query_started, 3),
        },
    }
    if simhash_query is not None:
        simhash_stats["avg_candidate_pool"] = round(sum(cand_sizes) / len(cand_sizes), 1) if cand_sizes else 0
    logger.info(
        "[{}] 检索耗时 {:.2f}s，召回候选 {} 条（仅同语言；向量 {} ∪ 特征SimHash补 {} ∪ 指纹补 {} ∪ 同名补 {} ∪ 结构SimHash补 {} ∪ 身份邻域补 {}；过滤跨语言 {}）{}",
        repo_id, search_elapsed, total_recalled, n_vector, n_simhash_added,
        n_fingerprint_added, n_name_added, n_structural_added, n_identity_added,
        n_cross_language_filtered,
        f"，SimHash 候选池均值 {simhash_stats['avg_candidate_pool']}" if simhash_query else "",
    )
    logger.info("[{}] 召回分阶段耗时（秒）{}", repo_id, simhash_stats["timings_sec"])

    recall = {
        "query_repo_id": repo_id,
        "top_k": top_k,
        "comparison_scope": {"same_language_only": True},
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
