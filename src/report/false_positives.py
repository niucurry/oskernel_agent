"""跨架构 / 跨语言 / 行业样板 / 内部跨架构复用 的误报识别（报告层）。

查重主链路按「掩码后逐行匹配比例」分流 confirmed（src/exact），这会在四类场景产生
假阳性（均为评审实测暴露）：

1. 行业样板汇编（boilerplate_asm）：__switch 等任务切换的寄存器存取序列，RISC-V/龙芯
   下写法本就固定，掩码后近 100% 命中却非借鉴。
2. 跨指令集架构（cross_arch）：龙芯 csrwr 与 RISC-V csrrw 同被 core::arch::asm!("…")
   外壳包裹，指令本体在字符串字面量里被掩码成 STR，只剩外壳相同 → 误判高相似。两者面向
   不同 CPU，逻辑上不可能是逐字借鉴。
3. 跨编程语言（cross_lang）：safe Rust 切片遍历 vs unsafe C 裸指针，语言/安全范式不同，
   仅因短小循环骨架掩码后相近而误判。
4. 内部跨架构复用（internal_dup）：作品自身 src/（RISC-V）与 src-la/（LoongArch）硬拷贝
   同名函数，同一次外部借鉴被两个目录各记一次，虚高外部重复率。

本模块只「降级 / 归类」不「丢弃」：命中项打标签、从「已确认借鉴」KPI 与清单剔除，另在报告
「疑似误报（需人工确认）」小节单列，人工仍可核。安全口径：仅在证据明确（双侧 ISA 都能确定
且不同 / 语言确定不同 / 寄存器搬运占绝对多数 / 函数体近乎逐字相同）时才标记，存疑不动——
宁可漏标一个误报，也不误降一个真借鉴。

注意：真实数据 file_path 用反斜杠，匹配前统一归一为正斜杠 + 小写（见 [[libraries]]）。
"""

from __future__ import annotations

import re

# ── ISA 识别 ─────────────────────────────────────────────────────────────────
# 路径里出现这些子串 → 该 ISA。仅取「唯一命中」，多 ISA 同时命中视为不可判（None）。
_ARCH_PATH_HINTS: dict[str, tuple[str, ...]] = {
    "loongarch": ("loongarch", "loongson", "loong64", "la64", "larch"),
    "riscv":     ("riscv", "riscv64", "riscv32", "rv64", "rv32"),
    "x86":       ("x86", "x86_64", "amd64"),
    "aarch64":   ("aarch64", "arm64", "armv8"),
}

# 仅出现在某 ISA 汇编 / 内联汇编里的助记符 / 寄存器（在小写后的代码上匹配）。
# 只在「确为汇编上下文」（lang==asm / 含 asm! / .S 文件）时扫描，避免误伤普通标识符。
_ARCH_CODE_HINTS: dict[str, tuple[str, ...]] = {
    "loongarch": (r"\bcsrwr\b", r"\bcsrrd\b", r"\bcsrxchg\b", r"\bertn\b",
                  r"\blu12i\b", r"\bpcaddu", r"\$r\d", r"\$a\d", r"\$t\d", r"\$s\d", r"\$sp\b"),
    "riscv":     (r"\bcsrrw\b", r"\bcsrrs\b", r"\bcsrrc\b", r"\bsret\b", r"\bmret\b",
                  r"\becall\b", r"\bsscratch\b", r"\bsstatus\b", r"\bsepc\b", r"\bmhartid\b"),
    "x86":       (r"\brax\b", r"\brbx\b", r"\brsp\b", r"\biretq\b", r"\bsyscall\b", r"\bwrmsr\b"),
    "aarch64":   (r"\beret\b", r"\bsvc\b", r"\btpidr_el", r"\bsp_el\d", r"\bmsr\s+\w", r"\bmrs\s+\w"),
}


def detect_isa(file_path: str | None, raw_code: str = "", lang: str = "") -> str | None:
    """判定一个函数面向的指令集架构（riscv/loongarch/x86/aarch64），不可判则 None。

    取路径信号与代码信号的一致结论：两者都指向同一 ISA、或仅其一可判 → 该 ISA；
    两者冲突（指向不同 ISA）→ None（存疑不判，避免误降真借鉴）。
    """
    path = (file_path or "").replace("\\", "/").lower()
    path_hits = {isa for isa, hints in _ARCH_PATH_HINTS.items() if any(h in path for h in hints)}
    path_isa = next(iter(path_hits)) if len(path_hits) == 1 else None

    code_isa = None
    code = raw_code or ""
    scan = (lang or "").lower() == "asm" or "asm!" in code or path.endswith((".s", ".asm"))
    if scan and code:
        low = code.lower()
        counts = {}
        for isa, pats in _ARCH_CODE_HINTS.items():
            n = sum(len(re.findall(p, low)) for p in pats)
            if n:
                counts[isa] = n
        if counts:
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            # 唯一 ISA 有信号，或最强 ISA 至少为次强的 2 倍 → 采信
            if len(ranked) == 1 or ranked[0][1] >= 2 * ranked[1][1]:
                code_isa = ranked[0][0]

    if path_isa and code_isa and path_isa != code_isa:
        return None  # 信号冲突，存疑不判
    return path_isa or code_isa


def _func_isa(func: dict) -> str | None:
    return detect_isa(func.get("file_path"), func.get("raw_code", ""), func.get("lang", ""))


def is_cross_arch(s: dict) -> bool:
    """新作品与候选函数面向**不同且都可判**的指令集架构 → 不可能是逐字借鉴。"""
    q = _func_isa(s.get("query_func") or {})
    c = _func_isa(s.get("candidate_func") or {})
    return bool(q and c and q != c)


def is_cross_lang(s: dict) -> bool:
    """新作品与候选函数语言不同（仅处理 rust↔c；asm 跨语言交给 cross_arch/boilerplate）。"""
    ql = ((s.get("query_func") or {}).get("lang") or "").lower()
    cl = ((s.get("candidate_func") or {}).get("lang") or "").lower()
    if not ql or not cl or ql == cl:
        return False
    return {ql, cl} <= {"rust", "c"}


# ── 行业样板汇编（任务切换 / 寄存器存取序列） ───────────────────────────────────
# 行首是寄存器存取 / csr 读写 / 数据搬运指令 → 视为「寄存器搬运行」。
_REG_MOVE_RE = re.compile(
    r"^\s*(?:s[dwbhx]|l[dwbhx]|st\.[dwbhx]+|ld\.[dwbhx]+|fs[dw]|fl[dw]|"
    r"mov[a-z.]*|str|ldr|stp|ldp|csrw?r[wdx]?|csrxchg|push|pop)\b",
    re.IGNORECASE,
)
# 任务切换 / 上下文保存恢复 的常见函数名（归一化后：去下划线、小写）。
_ASM_SWITCH_NAMES = {
    "switch", "switchto", "switchtask", "taskswitch", "contextswitch", "switchcontext",
    "save", "restore", "savecontext", "restorecontext", "alltraps", "traps", "trap",
    "trapreturn", "trapret", "restoreall", "saveall", "swtch",
}


def _norm_name(name: str) -> str:
    return (name or "").lower().replace("_", "")


def is_boilerplate_asm(func: dict) -> bool:
    """该函数是否为「寄存器存取 / 任务切换」样板汇编（各队写法雷同、非借鉴）。

    口径（保守）：确为汇编上下文，且去掉标签 / 指示符 / 空行后，寄存器搬运行占绝对多数
    （≥80%），或函数名命中切换/保存恢复样板名且搬运行过半。内联汇编（指令在字符串里、
    无法逐行判）不在此列——跨架构内联汇编交给 cross_arch。
    """
    code = func.get("raw_code") or ""
    if not code.strip():
        return False
    lang = (func.get("lang") or "").lower()
    fp = (func.get("file_path") or "").lower()
    is_asm = lang == "asm" or fp.endswith((".s", ".asm"))
    if not is_asm:
        return False
    lines: list[str] = []
    for raw in code.splitlines():
        ln = re.sub(r"[#;].*$", "", raw)
        ln = re.sub(r"//.*$", "", ln).strip()
        if not ln or ln.endswith(":") or ln.startswith(".") or ln in "{}":
            continue
        lines.append(ln)
    if len(lines) < 3:
        return False
    moves = sum(1 for ln in lines if _REG_MOVE_RE.match(ln))
    ratio = moves / len(lines)
    nm = _norm_name(func.get("func_name", ""))
    name_hit = nm in _ASM_SWITCH_NAMES or nm.startswith("switch") or nm.startswith("trap")
    return ratio >= 0.8 or (name_hit and ratio >= 0.5)


# ── 标注：写回 suspects（幂等，可重复调用） ───────────────────────────────────
# false_positive 主因优先级（数字越小越优先展示）。
_FP_ORDER = {"boilerplate_asm": 0, "cross_arch": 1, "cross_lang": 2, "internal_dup": 3}

_FP_KEYS = ("fp_cross_arch", "fp_cross_lang", "fp_boilerplate", "false_positive")


def tag_false_positives(suspects: list[dict]) -> dict[str, int]:
    """就地给跨架构 / 跨语言 / 样板汇编的嫌疑对打标签，返回各类计数。幂等。

    库复用对（reuse_library）已单列，不重复打标。每对记主因（样板 > 跨架构 > 跨语言）到
    ``false_positive``，并保留细分布尔标，供来源核对。
    """
    counts = {"boilerplate_asm": 0, "cross_arch": 0, "cross_lang": 0}
    for s in suspects:
        for k in _FP_KEYS:                       # 清旧标（重算幂等）
            s.pop(k, None)
        if s.get("reuse_library"):
            continue
        reasons: list[str] = []
        if is_boilerplate_asm(s.get("query_func") or {}):
            s["fp_boilerplate"] = True
            reasons.append("boilerplate_asm")
            counts["boilerplate_asm"] += 1
        if is_cross_arch(s):
            s["fp_cross_arch"] = True
            reasons.append("cross_arch")
            counts["cross_arch"] += 1
        if is_cross_lang(s):
            s["fp_cross_lang"] = True
            reasons.append("cross_lang")
            counts["cross_lang"] += 1
        if reasons:
            s["false_positive"] = min(reasons, key=lambda r: _FP_ORDER[r])
    return counts


def _line_set(code: str) -> frozenset[str]:
    """函数体归一化非空行集合（去注释 / 折叠空白），供 Jaccard 判「近乎逐字相同」。"""
    out = set()
    for raw in (code or "").splitlines():
        ln = re.sub(r"[#;].*$", "", re.sub(r"//.*$", "", raw))
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            out.add(ln)
    return frozenset(out)


def _body_jaccard(a: str, b: str) -> float:
    sa, sb = _line_set(a), _line_set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# 作品内部并行架构目录标记（路径里出现其一 → 该函数属某架构子树副本）。
_INTERNAL_DUP_JACCARD = 0.9

# 「移植/次要架构」目录标记：含其一的路径在硬拷贝簇里更可能是副本（如 src-la/ 之于 src/），
# 故优先把**不含**这些标记的路径选为「主借鉴」，让标准 src/ 留作外部借鉴、src-la/ 记内部复用。
_PORTED_PATH_HINTS = ("-la", "_la", "/la/", "loongarch", "loongson", "loong64", "la64", "larch")


def _dup_rank(path: str) -> tuple[int, str]:
    """硬拷贝簇内的主副本优先序：不含移植架构标记者优先，再按路径字典序。"""
    p = (path or "").replace("\\", "/").lower()
    ported = 1 if any(h in p for h in _PORTED_PATH_HINTS) else 0
    return (ported, p)


def tag_internal_arch_dups(suspects: list[dict]) -> int:
    """标注作品自身跨架构目录（src/ vs src-la/ 等）的硬拷贝同名函数为内部复用。

    口径（保守）：在「外部已确认借鉴」(confirmed、非库非样板非跨架构/语言) 的 query 函数里，
    按函数名分组；同名且函数体 Jaccard ≥0.9（近乎逐字相同）、文件路径不同的若干份，视为同一
    次外部借鉴在并行架构目录的硬拷贝——保留路径字典序最靠前的为「主借鉴」，其余打
    ``internal_arch_dup``（=主副本路径）从外部借鉴 KPI/清单剔除，避免同一借鉴被多目录各记一次。

    必须在复核升档（_apply_review_verdicts）之后调用，才能覆盖复核升上来的 confirmed。
    返回标注的副本数。
    """
    from collections import defaultdict

    for s in suspects:                           # 清旧标（重算幂等）
        s.pop("internal_arch_dup", None)

    by_name: dict[str, dict[tuple, dict]] = defaultdict(dict)
    for s in suspects:
        if s.get("tier") != "confirmed":
            continue
        if s.get("reuse_library") or s.get("false_positive"):
            continue
        q = s.get("query_func") or {}
        name = q.get("func_name", "")
        if not name:
            continue
        key = (q.get("file_path", ""), q.get("start_line", 0))
        # 同一 query 函数可能有多对，取其一代表（函数体一致）
        by_name[name].setdefault(key, q)

    n = 0
    marked: set[tuple] = set()
    for name, funcs in by_name.items():
        items = list(funcs.items())              # [((path,start), qfunc), ...]
        if len(items) < 2:
            continue
        # 并查：把函数体近乎相同的同名函数聚为一簇
        used = [False] * len(items)
        for i in range(len(items)):
            if used[i]:
                continue
            cluster = [i]
            used[i] = True
            for j in range(i + 1, len(items)):
                if used[j]:
                    continue
                (pi, _si), qi = items[i]
                (pj, _sj), qj = items[j]
                if pi.replace("\\", "/") == pj.replace("\\", "/"):
                    continue                     # 同文件不算跨目录复用
                if _body_jaccard(qi.get("raw_code", ""), qj.get("raw_code", "")) >= _INTERNAL_DUP_JACCARD:
                    cluster.append(j)
                    used[j] = True
            if len(cluster) < 2:
                continue
            # 主借鉴 = 不含移植架构标记者优先（标准 src/ 而非 src-la/）；其余为内部硬拷贝
            cluster.sort(key=lambda idx: _dup_rank(items[idx][0][0]))
            canonical_path = items[cluster[0]][0][0]
            for idx in cluster[1:]:
                marked.add(items[idx][0])        # (path, start)
            # 簇内主副本路径记给被标项，供报告标注「复用自 …」
            for idx in cluster[1:]:
                items[idx][1]["_canonical_path"] = canonical_path

    # 写回所有命中的 suspect 对（按 query 路径+起始行匹配）
    canonical_by_key: dict[tuple, str] = {}
    for name, funcs in by_name.items():
        for key, q in funcs.items():
            if key in marked:
                canonical_by_key[key] = q.get("_canonical_path", "")
                q.pop("_canonical_path", None)
    for s in suspects:
        q = s.get("query_func") or {}
        key = (q.get("file_path", ""), q.get("start_line", 0))
        if key in canonical_by_key:
            s["internal_arch_dup"] = canonical_by_key[key] or "(内部副本)"
            n += 1
    return n


# ── 报告小节数据 ─────────────────────────────────────────────────────────────
FP_REASON_DISP = {
    "boilerplate_asm": "行业样板汇编（任务切换/寄存器存取，各队写法雷同）",
    "cross_arch":      "跨指令集架构（面向不同 CPU，指令本体不同、仅内联汇编外壳相似）",
    "cross_lang":      "跨编程语言（语言/安全范式不同，仅短小骨架相近）",
    "internal_dup":    "作品内部跨架构复用（自身 src/ 与 src-la/ 等硬拷贝，属内部复用而非外部借鉴）",
}


def false_positive_stats(suspects: list[dict]) -> list[dict]:
    """疑似误报清单（按 query 函数去重）：跨架构/跨语言/样板/内部复用，含一个代表性来源。

    返回 [{name, file, start, module, lang, reason, source:{repo,file,func,start,sim},
           canonical}, ...]，按 reason 优先级、模块、函数名排序。
    """
    seen: dict[tuple, dict] = {}
    for s in suspects:
        if s.get("reuse_library"):
            continue
        reason = s.get("false_positive")
        if not reason and s.get("internal_arch_dup"):
            reason = "internal_dup"
        if not reason:
            continue
        q = s.get("query_func") or {}
        c = s.get("candidate_func") or {}
        key = (q.get("file_path", ""), q.get("func_name", ""), q.get("start_line", 0))
        cand = {
            "repo":  c.get("repo_id", ""), "file": c.get("file_path", ""),
            "func":  c.get("func_name", ""), "start": c.get("start_line", 0),
            "sim":   round(float(s.get("final_score") or 0.0), 3),
        }
        rec = seen.get(key)
        if rec is None:
            seen[key] = {
                "name":    q.get("func_name", ""), "file": q.get("file_path", ""),
                "start":   q.get("start_line", 0), "module": q.get("module_tag", "other"),
                "lang":    q.get("lang", ""), "reason": reason, "source": cand,
                "canonical": s.get("internal_arch_dup", "") if reason == "internal_dup" else "",
            }
        elif _FP_ORDER.get(reason, 9) < _FP_ORDER.get(rec["reason"], 9):
            rec["reason"] = reason
            if reason == "internal_dup":
                rec["canonical"] = s.get("internal_arch_dup", "")
    out = list(seen.values())
    out.sort(key=lambda x: (_FP_ORDER.get(x["reason"], 9), x["module"], x["name"]))
    return out
