"""src.fastpath（L0 文件指纹层）测试：整文件复制检测 + 文件整体相似后聚合。"""

from __future__ import annotations

from src.fastpath.scan import aggregate_file_similarity, scan_repo
from src.normalize.runner import normalize_repo
from src.normalize.store import FunctionStore

FILE_A = (
    "// 头部注释\n"
    "fn compute(x: i32) -> i32 {\n"
    "    let a = x + 1;\n"
    "    let b = a * 2;\n"
    "    let c = b - 3;\n"
    "    let d = c / 4;\n"
    "    d\n"
    "}\n"
)
# 仅改空格 / 注释 / 空行：规范化哈希应一致
FILE_A_REFORMATTED = (
    "fn compute(x: i32) -> i32 {\n"
    "        let a = x + 1;   // 改了注释\n"
    "    let b   =   a * 2;\n"
    "\n"
    "    let c = b - 3;\n"
    "    let d = c / 4;\n"
    "    d\n"
    "}\n"
)


def _norm(paths):
    return {p.replace("\\", "/") for p in paths}


def test_fastpath_detects_whole_file_copy_ignoring_format(tmp_path):
    repos_root = tmp_path / "repos"
    hist = repos_root / "2021" / "hist"
    (hist / "src").mkdir(parents=True)
    (hist / "src" / "compute.rs").write_text(FILE_A, encoding="utf-8")

    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        normalize_repo(hist, store, repo_id="2021/hist", repos_root=repos_root)

    newrepo = tmp_path / "new"
    (newrepo / "src").mkdir(parents=True)
    (newrepo / "src" / "compute.rs").write_text(FILE_A_REFORMATTED, encoding="utf-8")

    res = scan_repo(newrepo, repo_id="2024/new", db_path=db, output_dir=tmp_path / "out")

    assert "src/compute.rs" in _norm(res["skip_files"])
    assert len(res["matched_files"]) == 1
    assert res["matched_files"][0]["matches"][0]["repo_id"] == "2021/hist"


def test_fastpath_baseline_match_not_reported_as_copy(tmp_path):
    # 命中基线库（公共/第三方库）→ 判公共代码：跳过召回但不报为复制
    repos_root = tmp_path / "repos"
    base = repos_root / "0" / "baseline_lwext4"
    (base / "src").mkdir(parents=True)
    (base / "src" / "compute.rs").write_text(FILE_A, encoding="utf-8")
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        normalize_repo(base, store, repo_id="0/baseline_lwext4", repos_root=repos_root)

    newrepo = tmp_path / "new"
    (newrepo / "src").mkdir(parents=True)
    (newrepo / "src" / "compute.rs").write_text(FILE_A_REFORMATTED, encoding="utf-8")

    res = scan_repo(newrepo, repo_id="2024/new", db_path=db, output_dir=tmp_path / "out")

    assert res["matched_files"] == []                       # 不报为复制
    assert res["common_files"] == 1                          # 计入公共代码
    assert "src/compute.rs" in _norm(res["skip_files"])      # 仍跳过召回省算力


def test_fastpath_no_false_match_for_different_file(tmp_path):
    repos_root = tmp_path / "repos"
    hist = repos_root / "2021" / "hist"
    (hist / "src").mkdir(parents=True)
    (hist / "src" / "compute.rs").write_text(FILE_A, encoding="utf-8")
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        normalize_repo(hist, store, repo_id="2021/hist", repos_root=repos_root)

    newrepo = tmp_path / "new"
    (newrepo / "src").mkdir(parents=True)
    different = FILE_A.replace("c / 4", "c % 7").replace("a * 2", "a << 2")
    (newrepo / "src" / "compute.rs").write_text(different, encoding="utf-8")

    res = scan_repo(newrepo, repo_id="2024/new", db_path=db, output_dir=tmp_path / "out")
    assert res["skip_files"] == []
    assert res["matched_files"] == []


def test_aggregate_file_similarity_marks_whole_file():
    # 同一文件 2 个函数全部 confirmed，recall 报告该文件共 2 个函数 → 整体相似
    def sp(fn):
        return {"tier": "confirmed",
                "query_func": {"file_path": "src/fs.rs", "func_name": fn, "module_tag": "fs"},
                "candidate_func": {"repo_id": "2021/hist"}}
    suspects = [sp("read"), sp("write")]
    recall = {"results": [
        {"query": {"file_path": "src/fs.rs", "func_name": "read", "module_tag": "fs"}},
        {"query": {"file_path": "src/fs.rs", "func_name": "write", "module_tag": "fs"}},
    ]}
    out = aggregate_file_similarity(suspects, recall)
    assert len(out) == 1
    assert out[0]["file_path"] == "src/fs.rs"
    assert out[0]["ratio"] == 1.0
    assert out[0]["top_source"] == "2021/hist"
