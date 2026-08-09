"""评测脚本：把样本注入流水线各层，统计召回率、精确率与耗时。

  python -m tests.evaluation.run [--eval-set ...] [--manual ...] [--top-k 20]
                                 [--check]
输出 Markdown 报告并保存历史结果到 tests/evaluation/history/{date}.json。
--check：T1/T2 最终召回 < 0.95 或 T3 < 0.80 时以非零退出（供 CI 回归基线）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from loguru import logger

from oskernel_agent.comparison.exact.matcher import ExactMatcher
from oskernel_agent.comparison.normalize.store import DEFAULT_DB

HISTORY_DIR = "tests/evaluation/history"
DEFAULT_EVAL_SET = "tests/fixtures/eval_set.json"
DEFAULT_MANUAL = "tests/fixtures/manual_labeled.yaml"
DETECT_RATIO = 0.5            # exact 相似行占比 >= 此值 视为命中嫌疑
CI_THRESHOLDS = {"T1": 0.95, "T2": 0.95, "T3": 0.80}


def _raw_by_id(db_path: str | Path) -> dict[int, str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, raw_code FROM functions").fetchall()
    conn.close()
    return {i: r for i, r in rows}


def _resolve_manual(db_path: str | Path, pairs: list[dict]) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    out = []
    for p in pairs:
        def find(loc):
            return conn.execute(
                "SELECT id, raw_code, lang, normalized_code, feature_tokens FROM functions "
                "WHERE repo_id=? AND file_path=? AND start_line=?",
                (loc["repo_id"], loc["file_path"], loc["start_line"]),
            ).fetchone()
        a, b = find(p["a"]), find(p["b"])
        if a and b:
            out.append({"cls": "MANUAL", "label": int(p["label"]),
                        "target": {"id": a["id"], "raw_code": a["raw_code"]},
                        "variant": {"raw_code": b["raw_code"], "lang": b["lang"],
                                    "normalized_code": b["normalized_code"],
                                    "feature_tokens": json.loads(b["feature_tokens"] or "[]")},
                        "manual": True})
        else:
            logger.warning("人工样本定位失败：{}", p)
    conn.close()
    return out


def evaluate(
    eval_set: dict,
    *,
    db_path: str | Path = DEFAULT_DB,
    qdrant_path: str = "data/db/qdrant_local",
    idf_path: str = "data/db/idf.json",
    index_path: str = "data/db/simhash_index.pkl",
    code_index_path: str = "data/db/code_simhash_index.pkl",
    faiss_index_path: str = "data/db/faiss_hnsw.index",
    faiss_ids_path: str = "data/db/faiss_ids.npy",
    top_k: int = 20,
    manual: list[dict] | None = None,
) -> dict:
    from oskernel_agent.comparison.embed.embedder import get_embedder
    from oskernel_agent.comparison.embed.faiss_store import FaissVectorStore, load_faiss_index
    from oskernel_agent.comparison.simhash.build import SimHashQuery
    from oskernel_agent.comparison.simhash.code_index import CodeSimHashQuery

    samples = list(eval_set["samples"]) + (manual or [])
    raw_by_id = _raw_by_id(db_path)
    matcher = ExactMatcher()
    sq = SimHashQuery(idf_path, index_path, db_path=db_path)
    code_sq = CodeSimHashQuery(code_index_path, db_path=db_path)
    fi = load_faiss_index(faiss_index_path, faiss_ids_path, db_path=db_path)
    if fi is None:
        raise RuntimeError("评测要求与 functions.db 同代的 FAISS 索引，拒绝在过期索引上给出召回率")
    store = FaissVectorStore(fi[0], fi[1], db_path=db_path)
    embedder = get_embedder(show_progress=False)

    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    vecs = embedder.encode_batch([s["variant"]["normalized_code"] or " " for s in samples])
    timings["embed_all"] = round(time.perf_counter() - t0, 2)

    # 逐样本判定
    per = defaultdict(lambda: {"n": 0, "l1": 0, "l2": 0, "code": 0,
                               "casc": 0, "final": 0})
    fp = 0
    neg_total = 0
    t1 = time.perf_counter()
    for s, vec in zip(samples, vecs):
        cls, label = s["cls"], s["label"]
        tgt_id = s["target"]["id"]
        tgt_raw = s["target"].get("raw_code") or raw_by_id.get(tgt_id, "")
        var = s["variant"]

        cand_sh = sq.query(var.get("feature_tokens", []))
        l1 = tgt_id in cand_sh
        v_ids = {h["id"] for h in store.search(vec, top_k)}
        l2 = tgt_id in v_ids
        sh_ids = {h["id"] for h in store.search(
            vec, top_k, candidate_ids=sorted(cand_sh) or [-1])}
        code_ids = code_sq.query(var.get("normalized_code", ""))
        code_hit = tgt_id in code_ids
        # 结构 SimHash 命中直接进入 exact/segment，不再经过 ANN top-k 二次截断。
        casc = l2 or (tgt_id in sh_ids) or code_hit
        ratio = matcher.match(
            var["raw_code"], tgt_raw, var.get("lang", "rust")).similar_line_ratio
        final = casc and (ratio >= DETECT_RATIO or code_hit)

        if label == 1:
            p = per[cls]
            p["n"] += 1
            p["l1"] += l1
            p["l2"] += l2
            p["code"] += code_hit
            p["casc"] += casc
            p["final"] += final
        else:
            neg_total += 1
            if final:
                fp += 1
    timings["pipeline"] = round(time.perf_counter() - t1, 2)

    # 汇总
    classes = {}
    tp_total = 0
    pos_total = 0
    for cls, p in per.items():
        n = p["n"] or 1
        classes[cls] = {
            "n": p["n"],
            "recall_layer1": round(p["l1"] / n, 3),
            "recall_layer2": round(p["l2"] / n, 3),
            "recall_code_simhash": round(p["code"] / n, 3),
            "recall_cascade": round(p["casc"] / n, 3),
            "recall_final": round(p["final"] / n, 3),
        }
        tp_total += p["final"]
        pos_total += p["n"]
    precision = round(tp_total / (tp_total + fp), 3) if (tp_total + fp) else 0.0
    recall_overall = round(tp_total / pos_total, 3) if pos_total else 0.0

    result = {
        "date": date.today().isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": eval_set.get("counts", {}),
        "by_class": classes,
        "overall": {"precision": precision, "recall": recall_overall,
                    "tp": tp_total, "fp": fp, "neg_total": neg_total, "pos_total": pos_total},
        "timings_sec": timings,
    }

    return result


def render_report(result: dict) -> str:
    lines = [f"# 查重系统评测报告（{result['date']}）", "",
             f"样本：{result['counts']}", "",
             "## 各类召回率（特征 SimHash / 向量 / 结构 SimHash / 并集 / 最终）", "",
             "| 类别 | 样本数 | 特征召回 | 向量召回 | 结构召回 | 并集召回 | 最终召回 |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for cls in ("T1", "T2", "T3", "T4", "MANUAL"):
        c = result["by_class"].get(cls)
        if c:
            lines.append(f"| {cls} | {c['n']} | {c['recall_layer1']} | {c['recall_layer2']} "
                         f"| {c.get('recall_code_simhash', 'n/a')} | {c['recall_cascade']} "
                         f"| {c['recall_final']} |")
    o = result["overall"]
    lines += ["", "## 总体", "",
              f"- precision = {o['precision']}（TP={o['tp']} / FP={o['fp']}）",
              f"- recall = {o['recall']}（TP={o['tp']} / 正样本={o['pos_total']}）",
              f"- 负样本 {o['neg_total']}", "",
              f"## 耗时(s)：{result['timings_sec']}"]
    return "\n".join(lines)


def check_thresholds(result: dict) -> list[str]:
    fails = []
    for cls, thr in CI_THRESHOLDS.items():
        c = result["by_class"].get(cls)
        if c and c["recall_final"] < thr:
            fails.append(f"{cls} 最终召回 {c['recall_final']} < {thr}")
    return fails


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser(prog="python -m tests.evaluation.run")
    p.add_argument("--eval-set", default=DEFAULT_EVAL_SET)
    p.add_argument("--manual", default=DEFAULT_MANUAL)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--qdrant-path", default="data/db/qdrant_local")
    p.add_argument("--code-simhash-index", default="data/db/code_simhash_index.pkl")
    p.add_argument("--faiss-index", default="data/db/faiss_hnsw.index")
    p.add_argument("--faiss-ids", default="data/db/faiss_ids.npy")
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--check", action="store_true", help="召回低于回归阈值则非零退出")
    args = p.parse_args(argv)

    eval_set = json.loads(Path(args.eval_set).read_text(encoding="utf-8"))
    manual = []
    mp = Path(args.manual)
    if mp.is_file():
        pairs = (yaml.safe_load(mp.read_text(encoding="utf-8")) or {}).get("pairs") or []
        if pairs:
            manual = _resolve_manual(args.db, pairs)

    result = evaluate(eval_set, db_path=args.db, qdrant_path=args.qdrant_path,
                      code_index_path=args.code_simhash_index,
                      faiss_index_path=args.faiss_index, faiss_ids_path=args.faiss_ids,
                      top_k=args.top_k,
                      manual=manual)

    report = render_report(result)
    print(report)
    hist = Path(HISTORY_DIR)
    hist.mkdir(parents=True, exist_ok=True)
    (hist / f"{result['date']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (hist / f"{result['date']}_report.md").write_text(report, encoding="utf-8")
    logger.info("评测结果保存到 {}/{}.json", HISTORY_DIR, result["date"])

    if args.check:
        fails = check_thresholds(result)
        if fails:
            logger.error("回归基线未达标：{}", "; ".join(fails))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
