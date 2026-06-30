"""上游基线 / ABI 受限代码 的识别（报告层，与 libraries.py / false_positives.py 同口径）。

回应评审实测暴露的判别缺陷：报告把「ArceOS 上游 vendored 代码」「受 Linux/POSIX ABI
限制的唯一性实现」当成跨队借鉴，虚高了外部重复率。两类均**降级 / 归类**不丢弃——从借鉴
KPI/清单剔除，另单列小节供人工核。配置见 config/upstream_baselines.yaml。

1. upstream_vendored：双方 file_path 在同一 upstream_root（如 arceos）段下、且该段之后
   的相对路径相同 → 双方都 vendored 了同一份上游文件，非跨队抄袭。安全性：队伍自研的新
   模块（如 arceos/modules/asynctask/）在其他队无同名相对路径，不触发本规则。
2. abi_constrained：按路径 glob + 函数名正则识别 ABI shim / stat 转换 / 系统调用转换 /
   构建脚本——字段签名由规范硬性规定、只有一种正确实现。口径保守，trait 样板与真实逻辑
   留给人工复核。

详见 config/upstream_baselines.yaml 注释。注意 file_path 用反斜杠，匹配前归一为正斜杠+小写
（见 [[libraries]]）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_UPSTREAM_PATH = "config/upstream_baselines.yaml"

# 内置兜底：config 缺失时仍能工作（与 yaml 保持同步）。
_DEFAULT_UPSTREAM_ROOTS = ("arceos", "rcore", "rcore-v3", "xv6-riscv", "ucore", "os_kernel_lab")
_DEFAULT_ABI_PATH_GLOBS = (
    "build.rs", "/scripts/", "/tools/", "/deptool/", "/bwbench_client/",
    "/examples/", "/tests/", "/benches/",
    "ctypes/", "/shim/", "/musl/", "/libc/", "/glibc/", "/axlibc/c/", "/vdso/",
    "api/src/file/", "api/src/syscall/",
)
_DEFAULT_ABI_NAME_PATTERNS = (
    r"_to_kstat$", r"from_kstat$", r"_to_stat$", r"from_stat$", r"^dummy_stat_",
    r"^metadata_to_", r"^sys_", r"_to_errno$", r"errno_to_", r"_syscall$",
    r"robust", r"futex",
)


@lru_cache(maxsize=4)
def load_upstream_registry(path: str | None = None) -> tuple[tuple[str, ...], tuple[str, ...], tuple[re.Pattern, ...]]:
    """加载配置，返回 (upstream_roots, abi_path_globs, abi_name_regexes)。可哈希、可缓存。"""
    roots = _DEFAULT_UPSTREAM_ROOTS
    path_globs = _DEFAULT_ABI_PATH_GLOBS
    name_pats = _DEFAULT_ABI_NAME_PATTERNS
    p = Path(path or DEFAULT_UPSTREAM_PATH)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if data.get("upstream_roots"):
            roots = tuple(data["upstream_roots"])
        ac = data.get("abi_constrained") or {}
        if ac.get("path_globs"):
            path_globs = tuple(ac["path_globs"])
        if ac.get("name_patterns"):
            name_pats = tuple(ac["name_patterns"])
    return (
        tuple(r.lower() for r in roots),
        tuple(g.lower() for g in path_globs),
        tuple(re.compile(p) for p in name_pats),
    )


def _rel_under_root(file_path: str, roots: tuple[str, ...]) -> str | None:
    """file_path 在某 upstream_root 段下的相对路径；不在任一根下返回 None。

    以路径段全等匹配 root（避免子串误伤），取**首个**命中根。如
    `arceos/modules/axhal/x.rs` under `arceos` → `modules/axhal/x.rs`。
    """
    if not file_path:
        return None
    parts = file_path.replace("\\", "/").lower().split("/")
    for i, seg in enumerate(parts):
        if seg in roots and i + 1 < len(parts):
            return "/".join(parts[i + 1:])
    return None


# 上游框架的「固有子目录」段名——这些目录段下的代码属上游框架自带（不是队伍原创）。
# 从已 ingest 的 baseline 仓库自动派生（见 _load_upstream_module_segs），派生不到时用兜底。
_DEFAULT_FRAMEWORK_SEGS = (
    # ArceOS：modules/ 下的 ax* 模块 + ulib + api（上游固有，队伍自研模块名不在此列）
    "axalloc", "axconfig", "axdisplay", "axdma", "axdriver", "axhal", "axipi",
    "axlog", "axmm", "axnet", "axns", "axruntime", "axsync", "axtask", "axfs",
    "arceos_api", "arceos_posix_api", "axlibc", "axstd", "axfeat",
)
_BASELINE_REPOS_GLOB = "data/repos/0/baseline_*"


@lru_cache(maxsize=2)
def _load_upstream_module_segs(baseline_glob: str = _BASELINE_REPOS_GLOB) -> frozenset[str]:
    """从已 ingest 的 baseline 仓库**自动派生**上游框架固有子目录段名。

    扫 baseline 仓库的 `modules/*`、`crates/*`、`ulib/*`、`api/*` 一级子目录名——这些是上游
    框架自带模块（如 ArceOS 的 axfs/axhal/...）。队伍在 `arceos/modules/<seg>/` 下的代码，
    若 seg 属上游固有模块 → 框架代码（不计借鉴）；队伍自研模块（如 asynctask）名不在派生集中，
    自动保留。派生不到（baseline 未 ingest）时用 _DEFAULT_FRAMEWORK_SEGS 兜底。
    """
    segs: set[str] = set()
    try:
        from glob import glob
        for repo in glob(baseline_glob):
            rp = Path(repo)
            for container in ("modules", "crates", "ulib", "api", "src"):
                cdir = rp / container
                if cdir.is_dir():
                    for sub in cdir.iterdir():
                        if sub.is_dir() and not sub.name.startswith("."):
                            segs.add(sub.name.lower())
    except OSError:
        pass
    return frozenset(segs) if segs else frozenset(_DEFAULT_FRAMEWORK_SEGS)


def is_upstream_framework_path(file_path: str, *, roots: tuple[str, ...] | None = None,
                               baseline_glob: str = _BASELINE_REPOS_GLOB) -> bool:
    """file_path 是否落在上游框架的固有模块目录下（如 arceos/modules/axfs/...）。

    系统化判据（替代「双侧路径全等」的脆弱口径）：query 路径在某 upstream_root 下、且其后
    紧跟的某个目录段 ∈ 上游框架固有模块集（从 baseline 仓库派生）→ 框架自带代码。
    这覆盖「候选队把 axfs 改名 axfs-ng」「上游版本号差异拉低向量相似度」等 vendored 上游
    漏判，且队伍自研模块（asynctask/trampoline 等不在派生集）不受影响。
    """
    if roots is None:
        roots, _, _ = load_upstream_registry()
    rel = _rel_under_root(file_path, roots)
    if rel is None:
        return False
    segs = _load_upstream_module_segs(baseline_glob)
    # rel 形如 modules/axfs/src/disk.rs 或 api/src/...；看其任一路径段是否命中上游模块集
    return any(part in segs for part in rel.split("/"))


def is_upstream_vendored_pair(s: dict, *, path: str | None = None) -> str | None:
    """双方都在同一 upstream_root 下、且相对路径相同 → 返回该 root 名，否则 None。

    两条命中路径（任一即可）：① 双方 file_path 在某 root 段下、相对路径相同；
    ② query 在某 root 下、candidate 路径**以 query 的相对路径为后缀**（其他队把上游
    vendored 到不同目录结构下，如 `AstrancE/api/arceos_api/...` 之于 `arceos/api/arceos_api/...`）。
    队伍自研的新模块（其他队无同名相对路径）不命中，故不会被误降。
    """
    roots, _, _ = load_upstream_registry(path)
    q = (s.get("query_func") or {}).get("file_path", "")
    c = (s.get("candidate_func") or {}).get("file_path", "")
    rq = _rel_under_root(q, roots)
    if not rq:
        return None
    rc = _rel_under_root(c, roots)
    same_rel = bool(rc) and rq == rc
    # 后缀兜底：candidate 归一后以 "/{rq}" 结尾，或就等于 rq（其他队结构不同但同文件）
    c_norm = c.replace("\\", "/").lower()
    suffix_match = c_norm.endswith("/" + rq) or c_norm == rq
    if same_rel or suffix_match:
        # 返回 query 侧实际命中的 root 段名（供报告标注）
        qparts = q.replace("\\", "/").lower().split("/")
        for seg in qparts:
            if seg in roots:
                return seg
    return None


def _glob_in_path(glob: str, fp: str) -> bool:
    """glob 的路径段是否连续出现在 fp 的路径段中（段全等，避免子串误伤）。

    `build.rs` → 命中任何名为 build.rs 的文件；`api/src/file/` → 命中 api/src/file/x.rs；
    `scripts/` → 命中 scripts/x.rs 与 os/scripts/x.rs。归一为正斜杠+小写后比较。
    """
    gsegs = [s for s in glob.replace("\\", "/").lower().split("/") if s]
    if not gsegs:
        return False
    fsegs = fp.replace("\\", "/").lower().split("/")
    n, m = len(fsegs), len(gsegs)
    for i in range(n - m + 1):
        if fsegs[i:i + m] == gsegs:
            return True
    return False


# 文件系统 / 标准协议魔数（全球唯一标准值，出现即必然相同，非抄袭）。
_MAGIC_CONSTANTS = (
    "0xef53",    # EXT2/3/4
    "0x2011bab0", "0xf2f52010",  # EROFS / F2FS
    "0x9fa0",    # PROC_SUPER_MAGIC
    "0x01021994",  # TMPFS_MAGIC
    "0x58465342",  # XFS
    "0x6969",    # NFS
    "0x4d44",    # MSDOS/FAT
    "0x73717368",  # SQUASHFS
)
# POSIX 标准信号号（按编号唯一规定）。成组出现（≥3 个，如定时器信号 14/26/27）→ 规范映射。
_POSIX_SIGNALS = {"1", "2", "3", "4", "6", "8", "9", "11", "13", "14", "15",
                  "17", "18", "19", "20", "26", "27", "28", "29", "30"}
# 定时器信号三元组（SIGALRM/SIGVTALRM/SIGPROF）——评审点名的 check_pending_timer_signal 特征。
_TIMER_SIGNALS = {"14", "26", "27"}


def _has_standard_constants(code: str) -> bool:
    """函数体是否硬编码了标准协议/规范常数（魔数 或 成组 POSIX 信号号）→ 规范唯一性，非抄袭。

    保守口径：① 命中任一文件系统魔数；或 ② 同时出现定时器信号三元组(14/26/27)；或
    ③ 出现 ≥4 个不同 POSIX 标准信号号的成组映射。普通含个别数字的函数不命中。
    """
    if not code:
        return False
    low = code.lower()
    if any(m in low for m in _MAGIC_CONSTANTS):
        return True
    nums = set(re.findall(r"\b(\d{1,2})\b", code))
    if _TIMER_SIGNALS <= nums:                       # 14/26/27 同现
        return True
    return len(nums & _POSIX_SIGNALS) >= 4            # ≥4 个标准信号号成组


def _abi_hit(func: dict, path_globs: tuple[str, ...], name_regexes: tuple[re.Pattern, ...]) -> bool:
    fp = (func.get("file_path") or "").replace("\\", "/").lower()
    if any(_glob_in_path(g, fp) for g in path_globs):
        return True
    name = (func.get("func_name") or "").lower()
    if any(p.search(name) for p in name_regexes):
        return True
    return _has_standard_constants(func.get("raw_code") or "")


def is_abi_constrained(s: dict, *, path: str | None = None) -> bool:
    """query 函数命中 ABI 路径 glob / 函数名正则 / 硬编码标准常数 → 受 ABI 限制的唯一性实现。

    以 query（新作品）侧为准：新作品该函数本身就是 ABI shim / 规范映射，命中什么都不算原创借鉴。
    """
    _, path_globs, name_regexes = load_upstream_registry(path)
    return _abi_hit(s.get("query_func") or {}, path_globs, name_regexes)


def is_excluded_file_path(file_path: str, *, path: str | None = None) -> str | None:
    """文件级路径口径：该文件本身属上游基线 vendored 或 ABI 受限 → 返回类别，否则 None。

    用于 file_matches（整文件相同，无 suspect 级标签可判）：文件位于 upstream_root 段下
    → 'upstream_vendored'；命中 ABI path glob → 'abi_constrained'。队伍自研模块在其他队
    无逐字相同副本，不会进 file_matches，故路径口径不会误伤自研代码。
    """
    roots, path_globs, _ = load_upstream_registry(path)
    if _rel_under_root(file_path, roots) or is_upstream_framework_path(file_path, roots=roots):
        return "upstream_vendored"
    fp = (file_path or "").replace("\\", "/").lower()
    if any(_glob_in_path(g, fp) for g in path_globs):
        return "abi_constrained"
    return None


def tag_upstream_baselines(suspects: list[dict], *, path: str | None = None) -> dict[str, int]:
    """就地给 upstream_vendored / abi_constrained 嫌疑对打标签，返回各类计数。幂等。

    库复用与已标 false_positive 的对不重复打标。每对记细分布尔标；主因
    upstream_vendored（root 名）/ abi_constrained 写到对应字段，供报告标注与来源核对。
    """
    roots, _, _ = load_upstream_registry(path)
    uv_n = abi_n = 0
    for s in suspects:
        for k in ("upstream_vendored", "abi_constrained"):
            s.pop(k, None)                       # 清旧标（重算幂等）
        if s.get("reuse_library") or s.get("false_positive"):
            continue
        # 上游基线判据（任一）：① 双侧同相对路径/后缀（vendored 同版本）；
        # ② query 落在上游框架固有模块目录下（如 arceos/modules/axfs/，从 baseline 派生模块集）
        # ——②覆盖候选队改模块名(axfs→axfs-ng)/版本差异拉低向量相似度等漏判。
        root = is_upstream_vendored_pair(s, path=path)
        if not root:
            qfp = (s.get("query_func") or {}).get("file_path", "")
            if is_upstream_framework_path(qfp, roots=roots):
                for seg in qfp.replace("\\", "/").lower().split("/"):
                    if seg in roots:
                        root = seg
                        break
                root = root or "upstream"
        if root:
            s["upstream_vendored"] = root
            uv_n += 1
            continue                              # 上游基线优先于 ABI（更具体）
        if is_abi_constrained(s, path=path):
            s["abi_constrained"] = True
            abi_n += 1
    return {"upstream_vendored": uv_n, "abi_constrained": abi_n}


# ── 报告小节数据 ─────────────────────────────────────────────────────────────


def upstream_baseline_stats(suspects: list[dict]) -> list[dict]:
    """上游基线 + ABI 受限 清单（按 query 函数去重），按成因分组。

    返回 [{name, file, start, module, lang, reason, source:{repo,file,func,start,sim},
           root}, ...]，按 (reason, module, name) 排序。
    """
    seen: dict[tuple, dict] = {}
    for s in suspects:
        if s.get("reuse_library") or s.get("false_positive"):
            continue
        reason = ""
        root = ""
        if s.get("upstream_vendored"):
            reason, root = "upstream_vendored", s["upstream_vendored"]
        elif s.get("abi_constrained"):
            reason = "abi_constrained"
        if not reason:
            continue
        q = s.get("query_func") or {}
        c = s.get("candidate_func") or {}
        key = (q.get("file_path", ""), q.get("func_name", ""), q.get("start_line", 0))
        if key not in seen:
            seen[key] = {
                "name":    q.get("func_name", ""), "file": q.get("file_path", ""),
                "start":   q.get("start_line", 0), "module": q.get("module_tag", "other"),
                "lang":    q.get("lang", ""), "reason": reason, "root": root,
                "source": {
                    "repo": c.get("repo_id", ""), "file": c.get("file_path", ""),
                    "func": c.get("func_name", ""), "start": c.get("start_line", 0),
                    "sim":  round(float(s.get("final_score") or 0.0), 3),
                },
            }
    out = list(seen.values())
    out.sort(key=lambda x: (0 if x["reason"] == "upstream_vendored" else 1, x["module"], x["name"]))
    return out
