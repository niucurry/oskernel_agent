"""src.simhash 测试：汉明距离性质、IDF、分段索引、建库、向量库 id 过滤。"""

from __future__ import annotations

import json
import random
from pathlib import Path
from statistics import mean

import numpy as np

from src.embed.vector_store import VectorStore
from src.normalize.extract import extract_functions
from src.normalize.runner import normalize_repo
from src.normalize.store import FunctionStore
from src.models import FunctionRecord, ModuleTag
from src.normalize.normalizer import normalize_snippet
from src.simhash.build import SimHashQuery, build_index, compute_idf
from src.simhash.code_index import CodeSimHashQuery, build_code_index
from src.simhash.index import SegmentedIndex
from src.simhash.simhash import SimHasher, hamming

REPO = Path(__file__).parent / "fixtures" / "sample_repo"

CODE_A = """fn run(queue: &Vec<usize>) -> usize {
    let head = queue[0];
    let total = compute(head);
    if total > 0 { return total; }
    head
}"""
# 仅重命名局部变量/参数（queue→q, head→h, total→t），调用名 compute 不变
CODE_B = """fn run(q: &Vec<usize>) -> usize {
    let h = q[0];
    let t = compute(h);
    if t > 0 { return t; }
    h
}"""


# ---------- 汉明距离性质 ----------

def test_renamed_variables_keep_feature_tokens_identical():
    ta = extract_functions(CODE_A, "rust", min_lines=1)[0].feature_tokens
    tb = extract_functions(CODE_B, "rust", min_lines=1)[0].feature_tokens
    assert ta == tb  # 变量改名不影响 cf/ty/call 特征


def test_renamed_pair_hamming_le_8():
    ta = extract_functions(CODE_A, "rust", min_lines=1)[0].feature_tokens
    tb = extract_functions(CODE_B, "rust", min_lines=1)[0].feature_tokens
    corpus = [set(ta), {"cf:while", "call:foo", "ty:u32"}, {"call:bar", "ty:String", "lib:println"}]
    idf, dw = compute_idf(corpus)
    h = SimHasher(idf, dw)
    fa, _ = h.compute(ta)
    fb, _ = h.compute(tb)
    assert hamming(fa, fb) <= 8


def test_random_unrelated_avg_distance_28_36():
    rng = random.Random(1234)
    vocab = [f"{c}:{i}" for c in ("cf", "ty", "call", "lib") for i in range(60)]
    idf = {t: rng.uniform(0.5, 4.0) for t in vocab}
    h = SimHasher(idf, default_weight=2.0)
    dists = []
    for _ in range(300):
        a = h.compute(rng.sample(vocab, 12))[0]
        b = h.compute(rng.sample(vocab, 12))[0]
        dists.append(hamming(a, b))
    assert 28 <= mean(dists) <= 36


# ---------- IDF ----------

def test_compute_idf_weights():
    import math
    idf, dw = compute_idf([{"a", "b"}, {"a"}, {"c"}])  # N=3, df: a=2 b=1 c=1
    assert idf["a"] == math.log(3 / 2)
    assert idf["b"] == math.log(3 / 1)
    assert dw == math.log(3)


# ---------- 分段索引 ----------

def test_index_query_and_relaxation_superset():
    h = SimHasher({}, 1.0)
    fp1, ba1 = h.compute(["cf:if", "ty:usize", "call:foo", "lib:println"])
    fp2, _ = h.compute(["cf:while", "ty:String", "call:bar"])
    idx = SegmentedIndex()
    idx.add(10, fp1)
    idx.add(20, fp2)
    assert 10 in idx.query(fp1, ba1, relax=False)
    # 松弛查询是非松弛的超集
    assert idx.query(fp1, ba1, relax=False) <= idx.query(fp1, ba1, relax=True)


def test_index_save_load_roundtrip(tmp_path):
    h = SimHasher({}, 1.0)
    fp, ba = h.compute(["cf:if", "ty:usize", "call:foo"])
    idx = SegmentedIndex()
    idx.add(7, fp)
    p = tmp_path / "idx.pkl"
    idx.save(p)
    loaded = SegmentedIndex.load(p)
    assert 7 in loaded.query(fp, ba)
    assert len(loaded) == 1


def test_multiprobe_guarantees_recall_within_15_bits():
    original = 0x123456789ABCDEF0
    # 四段分别翻 4/4/4/3 位，总距离 15；必有一段在 3-bit probe 范围内。
    flips = [0, 1, 2, 3, 16, 17, 18, 19, 32, 33, 34, 35, 48, 49, 50]
    changed = original
    for bit in flips:
        changed ^= 1 << bit
    idx = SegmentedIndex(); idx.add(42, original)
    assert 42 in idx.query_multiprobe(changed, bits_per_segment=3)


def test_code_simhash_recalls_renamed_function_with_local_additions(tmp_path):
    old = """fn run_tasks(queue: &mut Vec<Task>) {
        loop {
            let task = queue.pop().unwrap();
            task.switch_to();
            queue.push(task);
        }
    }"""
    changed = """fn execute_loop(tasks: &mut Vec<Task>) {
        loop {
            check_timer();
            let current = tasks.pop().unwrap();
            current.switch_to();
            if current.ready() { tasks.push(current); }
        }
    }"""
    db = tmp_path / "functions.db"
    normalized = normalize_snippet(old, "rust").code
    with FunctionStore(db) as store:
        rec = FunctionRecord(
            repo_id="2025/history", file_path="processor.rs", start_line=1, end_line=8,
            func_name="run_tasks", module_tag=ModuleTag.SCHED, lang="rust",
            raw_code=old, normalized_code=normalized,
        )
        fid = store.add_function(rec, []); store.conn.commit()
    index_path = tmp_path / "code.idx"
    build_code_index(db, index_path)
    query = CodeSimHashQuery(index_path, db_path=db)
    hits = query.query(normalize_snippet(changed, "rust").code)
    assert fid in hits and hits[fid] <= 15


# ---------- 建库 + 查询 ----------

def test_build_index_and_query(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        normalize_repo(REPO, store, repo_id="t/sample", min_lines=1)
        row = store.conn.execute(
            "SELECT id, feature_tokens FROM functions WHERE func_name='alloc_block'"
        ).fetchone()
    fid, tokens = row[0], json.loads(row[1])

    idf_path = tmp_path / "idf.json"
    index_path = tmp_path / "index.pkl"
    res = build_index(db, idf_path=idf_path, index_path=index_path)
    assert res["functions"] >= 3
    assert idf_path.exists() and index_path.exists()

    sq = SimHashQuery(idf_path, index_path)
    cands = sq.query(tokens)
    assert fid in cands  # 用自己的特征 token 必能召回自己


# ---------- 向量库 id 过滤（SimHash → Qdrant filter 的桥） ----------

def test_vector_store_candidate_id_filter():
    vs = VectorStore("t_simhash_filter", in_memory=True)
    vs.ensure_collection(4)
    vecs = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], dtype=np.float32)
    vs.upsert([1, 2, 3], vecs, [{"repo_id": "r", "module_tag": "x"}] * 3)

    q = np.array([1, 0, 0, 0], dtype=np.float32)
    res = vs.search(q, 10, candidate_ids=[2, 3])
    ids = {r["id"] for r in res}
    assert ids <= {2, 3} and 1 not in ids       # 被 id 过滤限制
    assert vs.search(q, 10, candidate_ids=[]) == []  # 空候选 → 无结果
