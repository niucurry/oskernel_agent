"""
项目级共享事实档案：Python 预处理，避免 5 个 LLM 分会话各自重算口径不一致。

入口：build_repo_facts(repo_path, repo_name, ts) -> dict
"""

import re
from datetime import date
from pathlib import Path

from ..parsers.code_parser import build_profile
from ..tools.tool_dispatcher import _STANDARD_SYSCALLS
from ..tools.tool_handlers import _REFERENCE_OS_FUNCS, _extract_repo_funcs
from ..cli.fetch_repo import summarize_commits
from ..cli.agent import _build_structure


# 关键文件候选名（用于体积/行数采样）
_KEY_FILE_CANDIDATES = (
    "kernel/syscall.c", "kernel/syscall.rs", "src/syscall.rs",
    "kernel/main.c", "kernel/main.rs", "src/main.rs",
    "kernel/proc.c", "kernel/task.rs", "src/task/mod.rs",
    "kernel/vm.c", "kernel/mm.c", "src/mm/mod.rs",
    "kernel/trap.c", "src/trap/mod.rs",
)

# SMP 探测关键词
_SMP_NCPU_RE = re.compile(r"^\s*#?\s*define\s+(?:NCPU|CPUS|NR_CPUS|NUMCPU)\s+(\d+)", re.M)
_SMP_RUST_RE = re.compile(r"const\s+(?:NCPU|CPUS|NR_CPUS|NUMCPU)\s*:\s*\w+\s*=\s*(\d+)")
_SMP_WAKEUP_RE = re.compile(r"\b(startothers|hart_start|secondary_start|smp_init|"
                            r"start_secondary|cpu_up|hart_wakeup)\b")


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _probe_standard_syscalls(repo_path: Path) -> tuple[int, list[str]]:
    """轻量探测标准 syscall 实现数（仅扫 sys_xxx/syscall_xxx 函数名）。

    不调用 ToolDispatcher（那个要 engine + level2_index 太重），仅做正则扫描。
    与 list_implemented_syscalls 的 function_name 策略对齐。
    """
    func_c = re.compile(r"^\s*(?:static\s+)?(?:int|long|isize_t|ssize_t|void|"
                        r"u?int\d+_t)\s+(\w+)\s*\(", re.M)
    func_rs = re.compile(r"^\s*(?:pub\s+)?fn\s+(\w+)\s*\(", re.M)
    skip = {"vendor", "third_party", "target", ".git", "node_modules"}

    implemented: set[str] = set()
    for src in repo_path.rglob("*"):
        if not src.is_file():
            continue
        if any(p in skip for p in src.relative_to(repo_path).parts):
            continue
        if src.suffix not in (".c", ".rs", ".h"):
            continue
        text = _read_text_safe(src)
        if not text:
            continue
        regex = func_rs if src.suffix == ".rs" else func_c
        for m in regex.finditer(text):
            name = m.group(1)
            canonical = None
            if name.startswith("sys_"):
                canonical = name[4:]
            elif name.startswith("syscall_"):
                canonical = name[8:]
            if canonical and canonical.lower() in _STANDARD_SYSCALLS:
                implemented.add(canonical.lower())

    return len(implemented), sorted(implemented)


def _probe_key_files(repo_path: Path) -> list[dict]:
    """采样关键内核文件的体积/行数（用于跨分片口径对齐）。"""
    facts: list[dict] = []
    seen: set[str] = set()
    for rel in _KEY_FILE_CANDIDATES:
        p = repo_path / rel
        if not p.exists() or rel in seen:
            continue
        seen.add(rel)
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            facts.append({
                "path":  rel,
                "bytes": p.stat().st_size,
                "lines": text.count("\n") + 1,
            })
        except Exception:
            continue
    # 还兜底扫一下 syscall.c 同名文件（不限路径），保证 syscall.c 一定被采到
    if not any("syscall" in f["path"] for f in facts):
        for cand in repo_path.rglob("syscall.c"):
            if any(p in cand.parts for p in ("vendor", "third_party", "target")):
                continue
            rel = str(cand.relative_to(repo_path))
            try:
                text = cand.read_text(encoding="utf-8", errors="replace")
                facts.append({
                    "path":  rel,
                    "bytes": cand.stat().st_size,
                    "lines": text.count("\n") + 1,
                })
                break
            except Exception:
                continue
    return facts


def _probe_smp(repo_path: Path) -> dict:
    """探测 SMP 状态：NCPU 常量 + 多核唤醒函数是否存在。"""
    numcpu: int | None = None
    evidence: list[str] = []
    wakeup_present = False

    candidate_files = [
        "kernel/param.h", "include/param.h", "src/config.rs",
        "Makefile", "Kbuild", "Kconfig",
    ]
    for rel in candidate_files:
        p = repo_path / rel
        if not p.exists():
            continue
        text = _read_text_safe(p)
        if not text:
            continue
        m = _SMP_NCPU_RE.search(text) or _SMP_RUST_RE.search(text)
        if m and numcpu is None:
            try:
                numcpu = int(m.group(1))
                evidence.append(f"{rel}:{text[:m.start()].count(chr(10)) + 1}")
            except ValueError:
                pass

    # 扫 Makefile 的 CPUS 变量
    mk = repo_path / "Makefile"
    if mk.exists():
        for line in _read_text_safe(mk).splitlines():
            m = re.match(r"^\s*CPUS\s*[:?]?=\s*(\d+)", line)
            if m and numcpu is None:
                try:
                    numcpu = int(m.group(1))
                    evidence.append(f"Makefile: CPUS={numcpu}")
                    break
                except ValueError:
                    pass

    # 扫多核唤醒函数（限于关键目录避免太慢）
    for src in repo_path.rglob("*"):
        if wakeup_present and len(evidence) >= 3:
            break
        if not src.is_file() or src.suffix not in (".c", ".rs", ".h", ".S"):
            continue
        rel_parts = src.relative_to(repo_path).parts
        if any(p in {"vendor", "third_party", "target", ".git"} for p in rel_parts):
            continue
        text = _read_text_safe(src)
        m = _SMP_WAKEUP_RE.search(text)
        if m:
            wakeup_present = True
            lineno = text[: m.start()].count("\n") + 1
            evidence.append(f"{src.relative_to(repo_path)}:{lineno}（{m.group(1)}）")

    if numcpu is None and not wakeup_present:
        summary = "未确认"
    elif (numcpu or 0) <= 1 and not wakeup_present:
        summary = "单核"
    else:
        summary = "多核"

    return {
        "numcpu":         numcpu,
        "wakeup_present": wakeup_present,
        "evidence":       evidence[:3],
        "summary":        summary,
    }


def _probe_reference_overlap(repo_path: Path, ref_name: str | None) -> dict:
    """复用 tool_handlers._extract_repo_funcs + _REFERENCE_OS_FUNCS。"""
    if not ref_name or ref_name not in _REFERENCE_OS_FUNCS:
        return {"ref_overlap": 0, "ref_unique_total": 0, "ref_name_used": None}
    ref_funcs = _REFERENCE_OS_FUNCS[ref_name]
    repo_funcs = _extract_repo_funcs(str(repo_path))
    overlap = repo_funcs & ref_funcs
    unique = repo_funcs - ref_funcs
    return {
        "ref_overlap":      len(overlap),
        "ref_unique_total": len(overlap) + len(unique),
        "ref_name_used":    ref_name,
    }


def build_repo_facts(repo_path: Path, repo_name: str, ts: str) -> dict:
    """采集项目级共享事实档案，5 个分会话共用同一口径。"""
    repo_path = Path(repo_path).resolve()
    structure = _build_structure(repo_path)
    profile   = build_profile(str(repo_path), structure)

    ref_os = profile.get("reference_os") or None

    standard_count, std_list = _probe_standard_syscalls(repo_path)
    ref_data = _probe_reference_overlap(repo_path, ref_os)

    return {
        "meta": {
            "repo_id":      repo_name,
            "repo_name":    repo_name,
            "review_date":  date.today().isoformat(),
            "reference_os": ref_os,
            "fragment_ts":  ts,
        },
        "syscall": {
            "standard_count":   standard_count,
            "standard_total":   len(_STANDARD_SYSCALLS),
            "standard_implemented": std_list,
            **ref_data,
            "source_note": (
                "standard_count 来自仓库内 sys_*/syscall_* 函数定义正则扫描；"
                "ref_* 来自 tool_handlers.compare_with_reference_os 同款函数集对比"
            ),
        },
        "key_files":   _probe_key_files(repo_path),
        "smp":         _probe_smp(repo_path),
        "commits":     summarize_commits(str(repo_path)),
        "profile_lite": {
            "primary_lang":  profile.get("primary_lang"),
            "kernel_type":   profile.get("kernel_type"),
            "target_arch":   profile.get("target_arch"),
            "loc":           profile.get("loc"),
            "ref_evidence":  profile.get("ref_evidence"),
        },
    }
