"""src.embed 测试：嵌入语义、建库/检索（内存 Qdrant）。

需要真实加载 codet5p 模型（首次会联网下载，之后走本地缓存）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from src.embed.build import build_index
from src.embed.embedder import get_embedder
from src.embed.query import print_report, query_repo
from src.embed.vector_store import VectorStore
from src.normalize.normalizer import normalize_snippet
from src.normalize.runner import normalize_repo
from src.normalize.store import FunctionStore

REPO = Path(__file__).parent / "fixtures" / "sample_repo"


@pytest.fixture(scope="module")
def embedder():
    try:
        return get_embedder(show_progress=False)
    except Exception as exc:  # 模型不可用（离线且无缓存）时跳过整组
        pytest.skip(f"codet5p 模型不可用：{exc}")


def _cos(x, y) -> float:
    return float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))


def test_rename_pair_cosine_high_and_separated(embedder):
    f1 = "fn run(queue: &Vec<usize>) -> usize { let head = queue[0]; let lim = 0x10; step(head, lim); head }"
    f2 = "fn run(q: &Vec<usize>) -> usize { let h = q[0]; let l = 0x10; step(h, l); h }"  # 改名版
    f3 = "int total(int a, int b) { int s = a + b; return s * 2; }"  # 无关

    n1 = normalize_snippet(f1, "rust").code
    n2 = normalize_snippet(f2, "rust").code
    n3 = normalize_snippet(f3, "c").code
    vecs = embedder.encode_batch([n1, n2, n3])

    sim_pair = _cos(vecs[0], vecs[1])
    sim_unrel = _cos(vecs[0], vecs[2])
    assert sim_pair > 0.95
    assert sim_pair > sim_unrel + 0.2  # 显著高于无关对


def test_embedder_dim(embedder):
    assert embedder.dim == 256


def _build_functions_db(tmp_path: Path, repo_id: str) -> Path:
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        # 固定 min_lines=1：嵌入测试依赖样例仓库的小函数，与 D4 调高的默认下限解耦
        normalize_repo(REPO, store, repo_id=repo_id, min_lines=1)
    return db


def test_build_index_incremental(tmp_path, embedder):
    db = _build_functions_db(tmp_path, "2024/original")
    store = VectorStore("os_functions_test", in_memory=True)
    res = build_index(db, store, embedder)
    assert res["added"] >= 3
    assert store.count() == res["added"]
    # 再次建库应全部跳过（增量）
    res2 = build_index(db, store, embedder)
    assert res2["added"] == 0


def test_query_top1_hit_on_modified_copy(tmp_path, embedder):
    # 1) 建库：原始仓库
    db = _build_functions_db(tmp_path, "2024/original")
    store = VectorStore("os_functions_test2", in_memory=True)
    build_index(db, store, embedder)

    # 2) 制造"轻微修改版"：复制仓库并把变量名改掉（归一化后等价）
    modified = tmp_path / "modified"
    shutil.copytree(REPO, modified)
    task = modified / "os/src/sched/task.rs"
    text = task.read_text(encoding="utf-8")
    text = text.replace("ready_queue", "rq").replace("chosen", "picked").replace("threshold", "thr")
    task.write_text(text, encoding="utf-8")

    # 3) 检索（faiss 路径显式指向不存在的文件：隔离本机可能存在的生产索引
    #    data/db/faiss_hnsw.index，强制走传入的内存 store，保证任何机器上结果一致）
    recall = query_repo(
        modified, store, embedder, top_k=20, repos_root=tmp_path, output_dir=tmp_path / "out",
        faiss_index_path=tmp_path / "no.index", faiss_ids_path=tmp_path / "no.npy",
        db_path=db,
    )
    assert recall["query_repo_id"] == "modified"
    assert Path(recall["_output_path"]).exists()

    # pick_next 的 Top-1 候选应命中原库的 pick_next
    by_name = {r["query"]["func_name"]: r for r in recall["results"]}
    assert "pick_next" in by_name
    top1 = by_name["pick_next"]["candidates"][0]
    assert top1["payload"]["repo_id"] == "2024/original"
    assert top1["payload"]["func_name"] == "pick_next"
    assert top1["score"] > 0.9

    # 4) 统计报告：原库应进入 Top-5
    stats = print_report(recall)
    top_repo_ids = [r for r, _ in stats["top_repos"]]
    assert "2024/original" in top_repo_ids
