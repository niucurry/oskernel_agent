"""跨架构 / 跨语言 / 样板汇编 / 内部跨架构复用 误报识别测试（oskernel_agent.comparison.report.false_positives）。

对应评审实测暴露的四类对比报告假阳性：__switch 样板、龙芯 write_csr vs RISC-V
exchange_trap_frame、find_nul(safe Rust) vs c_strlen(unsafe C)、src/ 与 src-la/ 硬拷贝。
"""

from __future__ import annotations

from oskernel_agent.comparison.report import false_positives as FP
from oskernel_agent.comparison.report import semantic_compare as SC

# ── 真实片段（精简） ─────────────────────────────────────────────────────────
_RV_SWITCH = """\
__switch:
    sd sp, 8(a0)
    sd ra, 0(a0)
    sd s0, 16(a0)
    sd s1, 24(a0)
    ld ra, 0(a1)
    ld s0, 16(a1)
    ld sp, 8(a1)
    ret
"""

_WRITE_CSR_LA = """\
pub unsafe fn write_csr(val: usize) {
    core::arch::asm!("csrwr {}, 0x1", in(reg) val);
}
"""

_EXCHANGE_TRAP_RV = """\
pub unsafe fn exchange_trap_frame(val: usize) -> usize {
    let r: usize;
    core::arch::asm!("csrrw {}, sscratch, {}", out(reg) r, in(reg) val);
    r
}
"""

_FIND_NUL_RS = """\
fn find_nul(buf: &[u8]) -> usize {
    let mut i = 0;
    while i < buf.len() && buf[i] != 0 {
        i += 1;
    }
    i
}
"""

_C_STRLEN_C = """\
size_t c_strlen(const char *ptr) {
    size_t i = 0;
    while (*ptr.add(i) != 0) {
        i += 1;
    }
    return i;
}
"""


def _q(file_path, func_name, raw, lang, start=10, module="arch"):
    return {"repo_id": "2026/new", "file_path": file_path, "func_name": func_name,
            "start_line": start, "end_line": start + raw.count("\n"),
            "module_tag": module, "lang": lang, "raw_code": raw}


def _pair(qf, cf, tier="confirmed", score=0.9):
    return {"tier": tier, "final_score": score, "query_func": qf, "candidate_func": cf,
            "evidence": {}}


# ── detect_isa ───────────────────────────────────────────────────────────────

def test_detect_isa_from_path():
    assert FP.detect_isa("os/src/arch/loongarch64/switch.rs") == "loongarch"
    assert FP.detect_isa("os/src/arch/riscv64/trap.rs") == "riscv"
    assert FP.detect_isa("os/src/fs/inode.rs") is None        # 无架构信号 → 不判


def test_detect_isa_from_inline_asm_code():
    # 路径无信号，靠内联汇编助记符区分龙芯 / RISC-V
    assert FP.detect_isa("src/reg.rs", _WRITE_CSR_LA, "rust") == "loongarch"
    assert FP.detect_isa("src/reg.rs", _EXCHANGE_TRAP_RV, "rust") == "riscv"


def test_detect_isa_conflict_returns_none():
    # 路径说 riscv、代码说 loongarch → 存疑不判（不误降真借鉴）
    assert FP.detect_isa("os/src/arch/riscv64/x.rs", _WRITE_CSR_LA, "rust") is None


# ── 跨架构 / 跨语言 ───────────────────────────────────────────────────────────

def test_cross_arch_write_csr_vs_exchange_trap():
    s = _pair(_q("src-la/reg.rs", "write_csr", _WRITE_CSR_LA, "rust"),
              _q("os/src/trap/mod.rs", "exchange_trap_frame", _EXCHANGE_TRAP_RV, "rust"))
    assert FP.is_cross_arch(s) is True


def test_cross_lang_find_nul_vs_c_strlen():
    s = _pair(_q("src/util.rs", "find_nul", _FIND_NUL_RS, "rust", module="other"),
              _q("lib/str.c", "c_strlen", _C_STRLEN_C, "c", module="other"))
    assert FP.is_cross_lang(s) is True
    # 同语言不算跨语言
    s2 = _pair(_q("a.rs", "f", _FIND_NUL_RS, "rust"), _q("b.rs", "g", _FIND_NUL_RS, "rust"))
    assert FP.is_cross_lang(s2) is False


# ── 样板汇编 ─────────────────────────────────────────────────────────────────

def test_boilerplate_switch_asm():
    assert FP.is_boilerplate_asm(_q("os/src/task/switch.S", "__switch", _RV_SWITCH, "asm")) is True
    # 普通 Rust 函数不是样板汇编
    assert FP.is_boilerplate_asm(_q("a.rs", "find_nul", _FIND_NUL_RS, "rust")) is False


# ── 标注 + 排除集成 ──────────────────────────────────────────────────────────

def test_tag_false_positives_counts_and_excludes():
    suspects = [
        _pair(_q("src-la/reg.rs", "write_csr", _WRITE_CSR_LA, "rust"),
              _q("os/src/trap/mod.rs", "exchange_trap_frame", _EXCHANGE_TRAP_RV, "rust")),
        _pair(_q("src/util.rs", "find_nul", _FIND_NUL_RS, "rust", module="other"),
              _q("lib/str.c", "c_strlen", _C_STRLEN_C, "c", module="other")),
        _pair(_q("os/src/task/switch.S", "__switch", _RV_SWITCH, "asm"),
              _q("hist/switch.S", "__switch", _RV_SWITCH, "asm")),
    ]
    counts = FP.tag_false_positives(suspects)
    assert counts == {"boilerplate_asm": 0, "cross_arch": 1, "cross_lang": 0}
    assert SC._is_excluded_pair(suspects[0])
    assert not SC._is_excluded_pair(suspects[1])
    assert not SC._is_excluded_pair(suspects[2])
    assert suspects[1]["cross_lang_signal"] is True
    assert suspects[2]["boilerplate_asm_signal"] is True
    # 幂等：重复调用计数一致、不重复堆标
    assert FP.tag_false_positives(suspects) == counts

    # 只有已证明是短内联汇编掩码伪相似的具体 pair 被排除；移植/样板提示继续复核。
    groups = SC.collect_file_pairs(suspects)
    assert {g["query_func"] for g in groups} == {"find_nul", "__switch"}


def test_cross_arch_substantive_port_is_not_automatically_excluded():
    qcode = _WRITE_CSR_LA + "\nfn migrate_state() { for page in pages() { copy(page); } }"
    ccode = _EXCHANGE_TRAP_RV + "\nfn migrate_state() { for page in pages() { copy(page); } }"
    s = _pair(_q("src-la/mm.rs", "migrate_state", qcode, "rust"),
              _q("src-rv/mm.rs", "migrate_state", ccode, "rust"))
    FP.tag_false_positives([s])
    assert s["cross_arch_signal"] is True
    assert not s.get("false_positive")


def test_tag_false_positives_idempotent_clears_stale():
    s = _pair(_q("src-la/reg.rs", "write_csr", _WRITE_CSR_LA, "rust"),
              _q("os/src/trap/mod.rs", "exchange_trap_frame", _EXCHANGE_TRAP_RV, "rust"))
    FP.tag_false_positives([s])
    assert s.get("false_positive") == "cross_arch"
    # 候选改成同架构后重算应清掉旧标
    s["candidate_func"] = _q("hist/reg.rs", "write_csr2", _WRITE_CSR_LA, "rust")
    FP.tag_false_positives([s])
    assert "false_positive" not in s and "fp_cross_arch" not in s


# ── 内部跨架构复用（src/ vs src-la/） ─────────────────────────────────────────

def test_internal_arch_dup_collapses_hard_copy():
    body = "fn sys_brk(addr: usize) -> isize {\n    let old = current_brk();\n    set_brk(addr);\n    old as isize\n}\n"
    suspects = [
        _pair(_q("src/mm/syscall.rs", "sys_brk", body, "rust", module="mm"),
              _q("hist/mm.rs", "sys_brk", body, "rust", module="mm")),
        _pair(_q("src-la/mm/syscall.rs", "sys_brk", body, "rust", module="mm"),
              _q("hist/mm.rs", "sys_brk", body, "rust", module="mm")),
    ]
    n = FP.tag_internal_arch_dups(suspects)
    assert n == 1
    # src/ 字典序在 src-la/ 前 → src/ 为主借鉴，src-la/ 标内部复用
    canonical = [s for s in suspects if not s.get("internal_arch_dup")][0]
    dup = [s for s in suspects if s.get("internal_arch_dup")][0]
    assert canonical["query_func"]["file_path"] == "src/mm/syscall.rs"
    assert dup["query_func"]["file_path"] == "src-la/mm/syscall.rs"
    assert dup["internal_arch_dup"] == "src/mm/syscall.rs"
    assert SC._is_excluded_pair(dup) and not SC._is_excluded_pair(canonical)


def test_internal_arch_dup_keeps_distinct_functions():
    # 同名但函数体差异大 → 不当作硬拷贝复用
    a = "fn f() { do_riscv_thing(); }\n"
    b = "fn f() { totally_different_loongarch_logic(); another(); third(); }\n"
    suspects = [
        _pair(_q("src/a.rs", "f", a, "rust"), _q("h/x.rs", "f", a, "rust")),
        _pair(_q("src-la/a.rs", "f", b, "rust"), _q("h/x.rs", "f", b, "rust")),
    ]
    assert FP.tag_internal_arch_dups(suspects) == 0


# ── 清单数据 ─────────────────────────────────────────────────────────────────

def test_false_positive_stats_grouping():
    suspects = [
        _pair(_q("src-la/reg.rs", "write_csr", _WRITE_CSR_LA, "rust"),
              _q("os/src/trap/mod.rs", "exchange_trap_frame", _EXCHANGE_TRAP_RV, "rust")),
        _pair(_q("os/src/task/switch.S", "__switch", _RV_SWITCH, "asm"),
              _q("hist/switch.S", "__switch", _RV_SWITCH, "asm")),
    ]
    FP.tag_false_positives(suspects)
    stats = FP.false_positive_stats(suspects)
    reasons = {x["name"]: x["reason"] for x in stats}
    assert reasons["write_csr"] == "cross_arch"
    assert "__switch" not in reasons
    assert suspects[1]["boilerplate_asm_signal"] is True


# ── 语义分析正文省略号清洗 ─────────────────────────────────────────────────────

def test_semantic_sanitizer_wraps_code_ellipses_only():
    sanitized = SC._sanitize_code_ellipses(
        "错误处理从 bail!(EPERM, ...) 改为 ax_bail!(OperationNotPermitted, e)。"
    )
    assert "<code>bail!(EPERM, ...)</code>" in sanitized
    assert "<code>ax_bail!(OperationNotPermitted, e)</code>" not in sanitized

    struct_literal = SC._sanitize_code_ellipses(
        "通过 Self { this: this.clone(), ... } 建立终端自引用对象。"
    )
    assert "<code>Self { this: this.clone(), ... }</code>" in struct_literal

    generics = SC._sanitize_code_ellipses(
        "文件描述符表采用 Vec<Option<...>> 保存句柄。"
    )
    assert "<code>Vec<Option<...></code>" in generics

    tag_intact = SC._sanitize_code_ellipses(
        '见 <span title="a...b">正文</span> 标签完整。'
    )
    assert '<span title="a...b">' in tag_intact

    prose = SC._sanitize_code_ellipses("普通省略号……这里没有代码形态，不应被包。")
    assert prose.count("<code>") == 0

    existing = SC._sanitize_code_ellipses("已有 <code>foo(...)</code> 标签不受影响。")
    assert existing.count("<code>") == 1
    assert existing.count("</code>") == 1
