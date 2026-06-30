"""src.normalize 单元测试：发现/归类/切分/归一化/落盘 端到端。"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models import ModuleTag
from src.normalize.classify import load_classifier
from src.normalize.discovery import discover_files
from src.normalize.extract import extract_functions
from src.normalize.normalizer import normalize_asm, normalize_snippet
from src.normalize.runner import normalize_repo
from src.normalize.store import FunctionStore

REPO = Path(__file__).parent / "fixtures" / "sample_repo"


# ---------- 文件发现 ----------

def test_discovery_excludes_build_and_vendor_dirs_by_name():
    rels = {f.rel_path.replace("\\", "/") for f in discover_files(REPO)}
    assert rels == {
        "os/src/sched/task.rs",
        "os/src/mm/heap.c",
        "os/src/arch/boot.S",
        "os/external/foobar/thirdlib.rs",
    }
    # 构建产物 / 明确的 vendored 目录按目录名排除
    assert not any("vendor" in r or "third_party" in r or "target" in r for r in rels)
    # 含 LICENSE 的子目录不再被整棵排除：Rust 工程每个 crate 都带 LICENSE，
    # 旧启发式会误杀参赛队自己的源码（见 discovery.discover_files 注释）。
    assert "os/external/foobar/thirdlib.rs" in rels


def test_discovery_language_detection():
    langs = {f.rel_path.replace("\\", "/"): f.lang for f in discover_files(REPO)}
    assert langs["os/src/sched/task.rs"] == "rust"
    assert langs["os/src/mm/heap.c"] == "c"
    assert langs["os/src/arch/boot.S"] == "asm"


# ---------- 模块归类 ----------

def test_classify_by_path_keyword():
    clf = load_classifier()
    assert clf.classify("os/src/sched/task.rs", "rust") == ModuleTag.SCHED
    assert clf.classify("os/src/mm/heap.c", "c") == ModuleTag.MM
    assert clf.classify("os/src/fs/inode.rs", "rust") == ModuleTag.FS
    assert clf.classify("os/src/trap/handler.rs", "rust") == ModuleTag.TRAP
    assert clf.classify("os/src/drivers/uart.rs", "rust") == ModuleTag.DRIVER
    assert clf.classify("os/src/main.rs", "rust") == ModuleTag.OTHER


def test_classify_asm_is_arch_and_macro_overrides():
    clf = load_classifier()
    assert clf.classify("os/src/anything.S", "asm") == ModuleTag.ARCH
    # 宏定义无视路径，一律 macro
    assert clf.classify("os/src/sched/task.rs", "rust", is_macro=True) == ModuleTag.MACRO


# ---------- 归一化核心：改变量名后必须完全一致 ----------

def test_rename_invariance_rust():
    a = """
    fn run(queue: &Vec<usize>) -> usize {
        let head = queue[0];
        let bound = 0xFF;
        step(head, bound);
        head
    }
    """
    b = """
    fn run(q: &Vec<usize>) -> usize {
        // 改了变量名 + 加注释
        let h = q[0];
        let lim = 0xFF;
        step(h, lim);
        h
    }
    """
    assert normalize_snippet(a, "rust").code == normalize_snippet(b, "rust").code


def test_rename_invariance_c():
    a = """
    int total(int a, int b) {
        int sum = a + b;
        return sum;
    }
    """
    b = """
    int total(int x, int y) {
        int acc = x + y; /* renamed */
        return acc;
    }
    """
    assert normalize_snippet(a, "c").code == normalize_snippet(b, "c").code


def test_normalization_removes_comments_and_keeps_types_and_keep_symbols():
    code = """
    fn demo(items: &Vec<usize>) -> Option<usize> {
        // 注释应被删除
        let first = items[0];
        Some(first)
    }
    """
    out = normalize_snippet(code, "rust").code
    assert "注释" not in out
    assert "Vec" in out and "Option" in out and "usize" in out  # 类型名保留
    assert "Some" in out  # 保留符号
    assert "items" not in out and "first" not in out  # 用户标识符被脱敏


def test_string_and_number_normalization():
    code = """
    fn t() -> usize {
        let s_long = "this is a long string";
        let s_short = "hi";
        let small = 5;
        let big = 9999;
        let hex = 0x1234;
        let _ = (s_long, s_short, small, big, hex);
        small
    }
    """
    res = normalize_snippet(code, "rust")
    assert "STR" in res.code
    assert "INT_S" in res.code and "INT_M" in res.code and "INT_HEX" in res.code
    # 仅收集长度 >= 8 的字符串
    assert "this is a long string" in res.strings
    assert "hi" not in res.strings


# ---------- 切分 ----------

def test_extract_rust_methods_and_macro():
    src = (REPO / "os/src/sched/task.rs").read_text(encoding="utf-8")
    funcs = extract_functions(src, "rust")
    names = {f.func_name for f in funcs}
    assert "pick_next" in names          # impl 块内方法被切出
    macros = [f for f in funcs if f.is_macro]
    assert {m.func_name for m in macros} == {"switch_to"}


def test_extract_skips_short_functions():
    code = "fn tiny() -> i32 { 1 }\n"  # 1 行，应被跳过
    assert extract_functions(code, "rust", min_lines=5) == []


def test_extract_default_min_lines_is_ten():
    # D4：默认下限提到 10 行；5–9 行小函数不再入库（整文件复制由 L0 兜底）
    code = "fn small() -> i32 {\n" + "    let x = 1;\n" * 6 + "    x\n}\n"  # 9 行
    assert extract_functions(code, "rust") == []           # 默认 min_lines=10
    assert extract_functions(code, "rust", min_lines=5) != []


def test_extract_asm_segments_by_label():
    asm = (REPO / "os/src/arch/boot.S").read_text(encoding="utf-8")
    funcs = extract_functions(asm, "asm", min_lines=2)
    names = {f.func_name for f in funcs}
    assert "_start" in names and "park" in names
    assert all(f.lang == "asm" for f in funcs)


def test_extract_asm_skips_preamble_and_boilerplate_headers():
    # 版权头 + 纯指示符骨架（无指令）：preamble 不切段，标号段去注释后不足 min_lines 也跳过。
    asm = (
        "//\n"
        "// Created by Someone 2024\n"
        "// -----------------------\n"
        "//\n"
        '#include "asm.h"\n'
        "\n"
        ".section .text.tlbrentry\n"
        ".globl handle_tlbr\n"
        ".align 0x4\n"
        "handle_tlbr:\n"
        "    // just a stub comment\n"
        "    // another comment line\n"
        "    nop\n"
    )
    funcs = extract_functions(asm, "asm", min_lines=4)
    names = {f.func_name for f in funcs}
    assert "_preamble" not in names       # 版权头/指示符样板不再单独成段
    assert "handle_tlbr" not in names     # 去注释后仅 1 行有效指令 < min_lines，跳过


def test_normalize_asm_strips_comments_and_numbers():
    res = normalize_asm("    li t0, 0x80200000   # comment\n    addi sp, sp, 16\n")
    assert "#" not in res.code and "comment" not in res.code
    assert "INT_HEX" in res.code and "INT_M" in res.code


# ---------- 落盘 + 端到端 ----------

def test_store_roundtrip(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        # 固定 min_lines=1：本测试验证落盘/模块分布/字符串收集，与 D4 调高的默认下限解耦
        res = normalize_repo(REPO, store, repo_id="2024/sample", min_lines=1)
        assert res["functions"] >= 3
        # 模块分布应覆盖多类
        dist = store.module_distribution("2024/sample")
        assert dist.get("sched", 0) >= 1
        assert dist.get("mm", 0) >= 1
        assert dist.get("arch", 0) >= 1
        assert dist.get("macro", 0) >= 1

        # unique_strings 收集了长字符串
        cur = store.conn.execute(
            "SELECT string_value FROM unique_strings WHERE repo_id=?", ("2024/sample",)
        )
        strings = {r[0] for r in cur.fetchall()}
        assert "task switched to next" in strings


def test_store_idempotent_rerun(tmp_path):
    db = tmp_path / "functions.db"
    with FunctionStore(db) as store:
        normalize_repo(REPO, store, repo_id="2024/sample")
        first = store.conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
        normalize_repo(REPO, store, repo_id="2024/sample")  # 重跑
        second = store.conn.execute("SELECT COUNT(*) FROM functions").fetchone()[0]
        assert first == second  # 不重复累加
