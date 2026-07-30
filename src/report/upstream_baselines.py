"""上游基线 / ABI 受限代码 的识别（报告层，与 libraries.py / false_positives.py 同口径）。

回应评审实测暴露的判别缺陷：报告把「ArceOS 上游 vendored 代码」「受 Linux/POSIX ABI
限制的唯一性实现」当成跨队借鉴，虚高了外部重复率。两类均**降级 / 归类**不丢弃——从借鉴
KPI/清单剔除，另单列小节供人工核。配置见 config/upstream_baselines.yaml。

1. upstream_vendored：双方 file_path 在同一 upstream_root（如 arceos）段下、且该段之后
   的相对路径相同 → 双方都 vendored 了同一份上游文件，非跨队抄袭。安全性：队伍自研的新
   模块（如 arceos/modules/asynctask/）在其他队无同名相对路径，不触发本规则。
2. abi_constrained：无条件排除明确的构建/兼容层路径；ABI 上下文目录和泛化名称还必须通过
   薄适配器代码形态检查。复杂系统调用、单个裸数字或名称本身不构成排除理由。

详见 config/upstream_baselines.yaml 注释。注意 file_path 用反斜杠，匹配前归一为正斜杠+小写
（见 [[libraries]]）。
"""

from __future__ import annotations

import re
import unicodedata
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
)
_DEFAULT_ABI_CONTEXT_PATH_GLOBS = ("api/src/file/", "api/src/syscall/")
_DEFAULT_ABI_NAME_PATTERNS = (
    r"_to_kstat$", r"from_kstat$", r"_to_stat$", r"from_stat$", r"^dummy_stat_",
    r"^metadata_to_", r"_to_errno$", r"errno_to_", r"_syscall$",
)
_DEFAULT_ABI_ADAPTER_NAME_PATTERNS = (r"^sys_", r"robust", r"futex")


@lru_cache(maxsize=4)
def load_upstream_registry(path: str | None = None) -> tuple[
    tuple[str, ...], tuple[str, ...], tuple[re.Pattern, ...],
    tuple[str, ...], tuple[re.Pattern, ...],
]:
    """加载配置。

    返回 ``(upstream_roots, unconditional_paths, strong_names, context_paths,
    adapter_names)``。后两类只是 ABI 提示，必须再由函数体证明它是薄适配器，不能单独排除。
    """
    roots = _DEFAULT_UPSTREAM_ROOTS
    path_globs = _DEFAULT_ABI_PATH_GLOBS
    name_pats = _DEFAULT_ABI_NAME_PATTERNS
    context_path_globs = _DEFAULT_ABI_CONTEXT_PATH_GLOBS
    adapter_name_pats = _DEFAULT_ABI_ADAPTER_NAME_PATTERNS
    p = Path(path or DEFAULT_UPSTREAM_PATH)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if data.get("upstream_roots"):
            roots = tuple(data["upstream_roots"])
        ac = data.get("abi_constrained") or {}
        if ac.get("unconditional_path_globs"):
            path_globs = tuple(ac["unconditional_path_globs"])
        elif ac.get("path_globs"):  # 兼容旧配置，但把已知的上下文目录从无条件规则中拆出
            legacy_paths = tuple(ac["path_globs"])
            context_norm = {x.strip("/").lower() for x in _DEFAULT_ABI_CONTEXT_PATH_GLOBS}
            path_globs = tuple(
                item for item in legacy_paths
                if str(item).strip("/").lower() not in context_norm
            )
        if ac.get("name_patterns"):
            name_pats = tuple(ac["name_patterns"])
        if ac.get("adapter_context_path_globs"):
            context_path_globs = tuple(ac["adapter_context_path_globs"])
        if ac.get("adapter_name_patterns"):
            adapter_name_pats = tuple(ac["adapter_name_patterns"])
    return (
        tuple(r.lower() for r in roots),
        tuple(g.lower() for g in path_globs),
        tuple(re.compile(p) for p in name_pats),
        tuple(g.lower() for g in context_path_globs),
        tuple(re.compile(p) for p in adapter_name_pats),
    )


def _root_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return tuple(token for token in re.split(r"[\W_]+", normalized) if token)


def _root_key(value: str) -> str:
    return "".join(_root_tokens(value))


def _root_location(
    file_path: str, roots: tuple[str, ...],
) -> tuple[int, str, str] | None:
    """Return ``(segment index, canonical key, configured label)`` for a root."""
    labels = {
        _root_key(root): str(root).casefold().strip()
        for root in roots if _root_key(root)
    }
    parts = file_path.replace("\\", "/").casefold().split("/")
    for index, segment in enumerate(parts):
        key = _root_key(segment)
        if key in labels:
            return index, key, labels[key]
    return None


def _rel_under_root(file_path: str, roots: tuple[str, ...]) -> str | None:
    """file_path 在某 upstream_root 段下的相对路径；不在任一根下返回 None。

    以路径段全等匹配 root（避免子串误伤），取**首个**命中根。如
    `arceos/modules/axhal/x.rs` under `arceos` → `modules/axhal/x.rs`。
    """
    if not file_path:
        return None
    parts = file_path.replace("\\", "/").casefold().split("/")
    location = _root_location(file_path, roots)
    if location is not None and location[0] + 1 < len(parts):
        return "/".join(parts[location[0] + 1:])
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


def _baseline_root_key(repo_name: str, roots: tuple[str, ...]) -> str | None:
    """Associate a baseline directory with the longest unique root token prefix."""
    repo_tokens = _root_tokens(repo_name)
    if repo_tokens[:1] == ("baseline",):
        repo_tokens = repo_tokens[1:]
    candidates: dict[str, tuple[int, int]] = {}
    for root in roots:
        tokens = _root_tokens(root)
        if not tokens or repo_tokens[:len(tokens)] != tokens:
            continue
        candidates[_root_key(root)] = (len(tokens), sum(map(len, tokens)))
    if not candidates:
        return None
    best_score = max(candidates.values())
    best = [key for key, score in candidates.items() if score == best_score]
    return best[0] if len(best) == 1 else None


@lru_cache(maxsize=4)
def _load_upstream_module_layout(
    baseline_glob: str, roots: tuple[str, ...],
) -> frozenset[tuple[str, str, str]]:
    """按上游根隔离派生 ``(root, container, direct_child)`` 模块布局。

    旧实现把所有 baseline 仓库的 ``src/*``/``crates/*`` 子目录揉成一个平面集合，可能让
    virtio-drivers 的 ``transport`` 等名称污染 ArceOS 判断；同时任意深度命中也会误伤自研模块。
    这里仅从名称对应的基线仓库派生，并要求模块是 modules/crates/ulib/api/src 的直接子项。
    """
    layout: set[tuple[str, str, str]] = set()
    try:
        from glob import glob
        for repo in glob(baseline_glob):
            rp = Path(repo)
            root_key = _baseline_root_key(rp.name, roots)
            if root_key is None:
                continue
            for container in ("modules", "crates", "ulib", "api", "src"):
                cdir = rp / container
                if not cdir.is_dir():
                    continue
                for sub in cdir.iterdir():
                    if not sub.is_dir() or sub.name.startswith("."):
                        continue
                    layout.add((root_key, container, sub.name.casefold()))
    except OSError:
        pass
    # 没有可用基线时仅为内置已知根提供保守兜底；仍保留容器-直接子项约束。
    arceos_key = _root_key("arceos")
    if (arceos_key in {_root_key(root) for root in roots}
            and not any(item[0] == arceos_key for item in layout)):
        for container in ("modules", "crates", "ulib", "api"):
            layout.update((arceos_key, container, seg) for seg in _DEFAULT_FRAMEWORK_SEGS)
    return frozenset(layout)


def is_upstream_framework_path(file_path: str, *, roots: tuple[str, ...] | None = None,
                               baseline_glob: str = _BASELINE_REPOS_GLOB) -> bool:
    """file_path 是否落在上游框架的固有模块目录下（如 arceos/modules/axfs/...）。

    系统化判据（替代「双侧路径全等」的脆弱口径）：query 路径在某 upstream_root 下、且其后
    紧跟的某个目录段 ∈ 上游框架固有模块集（从 baseline 仓库派生）→ 框架自带代码。
    这覆盖「候选队把 axfs 改名 axfs-ng」「上游版本号差异拉低向量相似度」等 vendored 上游
    漏判，且队伍自研模块（asynctask/trampoline 等不在派生集）不受影响。
    """
    if roots is None:
        roots, _, _, _, _ = load_upstream_registry()
    rel = _rel_under_root(file_path, roots)
    if rel is None:
        return False
    location = _root_location(file_path, roots)
    if location is None:
        return False
    root_key = location[1]
    rel_parts = rel.split("/")
    layout = _load_upstream_module_layout(baseline_glob, tuple(roots))
    return any(
        (root_key, rel_parts[index], rel_parts[index + 1]) in layout
        for index in range(len(rel_parts) - 1)
    )


def is_upstream_vendored_pair(s: dict, *, path: str | None = None) -> str | None:
    """双方都在同一 upstream_root 下、且相对路径相同 → 返回该 root 名，否则 None。

    两条命中路径（任一即可）：① 双方 file_path 在某 root 段下、相对路径相同；
    ② query 在某 root 下、candidate 路径**以 query 的相对路径为后缀**（其他队把上游
    vendored 到不同目录结构下，如 `AstrancE/api/arceos_api/...` 之于 `arceos/api/arceos_api/...`）。
    队伍自研的新模块（其他队无同名相对路径）不命中，故不会被误降。
    """
    roots, _, _, _, _ = load_upstream_registry(path)
    q = (s.get("query_func") or {}).get("file_path", "")
    c = (s.get("candidate_func") or {}).get("file_path", "")
    q_location = _root_location(q, roots)
    rq = _rel_under_root(q, roots)
    if q_location is None or not rq:
        return None
    q_root_key = q_location[1]
    rc = _rel_under_root(c, roots)
    c_location = _root_location(c, roots)
    same_rel = bool(rc) and c_location is not None and (
        q_root_key == c_location[1] and rq == rc
    )
    # 后缀兜底：candidate 归一后以 "/{rq}" 结尾，或就等于 rq（其他队结构不同但同文件）
    c_norm = c.replace("\\", "/").lower()
    suffix_match = (
        c_location is None or c_location[1] == q_root_key
    ) and (c_norm.endswith("/" + rq) or c_norm == rq)
    if same_rel or suffix_match:
        # 返回 query 侧实际命中的 root 段名（供报告标注）
        return q_location[2]
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
_SIGNAL_CONTEXT_RE = re.compile(r"\b(?:sig(?:nal)?|timer|alarm|itimer)\w*\b", re.IGNORECASE)


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
    signal_context = bool(_SIGNAL_CONTEXT_RE.search(code))
    if signal_context and _TIMER_SIGNALS <= nums:    # 14/26/27 同现且确在信号/定时器语境
        return True
    return signal_context and len(nums & _POSIX_SIGNALS) >= 4


def _nonblank_lines(code: str) -> list[str]:
    return [line.strip() for line in (code or "").splitlines() if line.strip()]


def _is_mechanical_adapter(code: str) -> bool:
    """函数体是否主要是参数转换/校验后转发，而不是系统调用的实质实现。

    这是保守的代码形态门：路径或 ``sys_*`` 名称只能触发候选，仍须函数很短、无循环/分支树，
    且至少存在一次调用、转换或结构构造。这样不会把复杂 VFS、调度、网络逻辑当成 ABI 样板。
    """
    lines = _nonblank_lines(code)
    if not lines or len(lines) > 18:
        return False
    low = "\n".join(lines).lower()
    if re.search(r"\b(for|while|loop|match|switch|case|await|yield)\b", low):
        return False
    if len(re.findall(r"\bif\b", low)) > 1:
        return False
    open_brace = low.find("{")
    close_brace = low.rfind("}")
    body = low[open_brace + 1:close_brace] if 0 <= open_brace < close_brace else ""
    calls = re.findall(r"\b[a-zA-Z_]\w*\s*\(", body)
    calls = [call for call in calls if not re.match(r"(?:if|while|for|match)\s*\(", call)]
    has_conversion = bool(re.search(r"\bas\s+[a-zA-Z_]|\b(?:into|from|try_from)\s*\(", body))
    # 结构体字面量必须出现在函数体赋值/返回表达式中，不能把函数签名的返回类型 ``T {`` 算进去。
    has_struct = bool(re.search(r"(?:=|return|=>|\()\s*[a-zA-Z_]\w*\s*\{", body))
    return bool(calls or has_conversion or has_struct)


def _standard_constraint_dominates(code: str) -> bool:
    """标准常量是否支配整个短映射，而不是仅在复杂实现里偶然出现一次。"""
    if not _has_standard_constants(code):
        return False
    lines = _nonblank_lines(code)
    if not lines or len(lines) > 45:
        return False
    low = "\n".join(lines).lower()
    if re.search(r"\b(for|while|loop)\b", low):
        return False
    nums = set(re.findall(r"\b(\d{1,2})\b", code))
    field_initializers = len(re.findall(r"(?m)^\s*[A-Za-z_]\w*\s*:\s*", code))
    signal_context = bool(_SIGNAL_CONTEXT_RE.search(code))
    if signal_context and (_TIMER_SIGNALS <= nums or len(nums & _POSIX_SIGNALS) >= 4):
        return bool(re.search(r"\b(match|switch)\b", low)) or len(lines) <= 20
    # 单一文件系统魔数只能解释短谓词，或字段占主体的 stat/statfs 机械构造；不能给复杂函数免责。
    return len(lines) <= 8 or (field_initializers >= 5 and len(lines) <= 35)


def _abi_basis(
    func: dict,
    path_globs: tuple[str, ...],
    name_regexes: tuple[re.Pattern, ...],
    context_path_globs: tuple[str, ...],
    adapter_name_regexes: tuple[re.Pattern, ...],
) -> str | None:
    fp = (func.get("file_path") or "").replace("\\", "/").lower()
    if any(_glob_in_path(g, fp) for g in path_globs):
        return "non_product_or_compatibility_path"
    name = (func.get("func_name") or "").lower()
    if any(p.search(name) for p in name_regexes):
        return "explicit_abi_conversion"
    code = func.get("raw_code") or ""
    if _standard_constraint_dominates(code):
        return "standard_mapping_dominates"
    context_hit = any(_glob_in_path(g, fp) for g in context_path_globs)
    adapter_name_hit = any(p.search(name) for p in adapter_name_regexes)
    if (context_hit or adapter_name_hit) and _is_mechanical_adapter(code):
        return "thin_abi_adapter"
    return None


def is_abi_constrained(s: dict, *, path: str | None = None) -> bool:
    """query 函数是否由 ABI/标准约束主导。

    路径/泛化名称只提供上下文；复杂业务实现不会仅因位于 syscall 目录或名为 ``sys_*`` 被排除。
    """
    _, path_globs, name_regexes, context_paths, adapter_names = load_upstream_registry(path)
    return bool(_abi_basis(
        s.get("query_func") or {}, path_globs, name_regexes, context_paths, adapter_names,
    ))


def is_excluded_file_path(file_path: str, *, path: str | None = None) -> str | None:
    """文件级路径口径：该文件本身属上游基线 vendored 或 ABI 受限 → 返回类别，否则 None。

    用于 file_matches（整文件相同，无 suspect 级标签可判）：文件位于 upstream_root 段下
    → 'upstream_vendored'；命中 ABI path glob → 'abi_constrained'。队伍自研模块在其他队
    无逐字相同副本，不会进 file_matches，故路径口径不会误伤自研代码。
    """
    roots, path_globs, _, _, _ = load_upstream_registry(path)
    if _rel_under_root(file_path, roots) or is_upstream_framework_path(file_path, roots=roots):
        return "upstream_vendored"
    fp = (file_path or "").replace("\\", "/").lower()
    if any(_glob_in_path(g, fp) for g in path_globs):
        return "abi_constrained"
    return None


def _raw_line_similarity(s: dict) -> float:
    ev = s.get("evidence") or {}
    if ev.get("line_similarity") is not None:
        return max(0.0, min(1.0, float(ev["line_similarity"])))
    matched = int(ev.get("exact_match_lines") or 0) + int(ev.get("renamed_match_lines") or 0)
    if not matched:
        return 0.0
    q_lines = len(_nonblank_lines((s.get("query_func") or {}).get("raw_code") or ""))
    c_lines = len(_nonblank_lines((s.get("candidate_func") or {}).get("raw_code") or ""))
    return min(1.0, matched / max(q_lines, c_lines, 1))


def _pair_has_source_evidence(s: dict) -> bool:
    """候选是否足以作为具体来源展示；向量分和名称/路径提示本身不算代码证据。"""
    ev = s.get("evidence") or {}
    if ev.get("normalized_fingerprint_match"):
        return True
    matched = int(ev.get("exact_match_lines") or 0) + int(ev.get("renamed_match_lines") or 0)
    line_sim = _raw_line_similarity(s)
    if matched >= 5 and line_sim >= 0.35:
        return True
    if matched >= 3 and int(ev.get("unique_string_matches") or 0) > 0:
        return True
    segment = ev.get("segment_hits") or {}
    hits = int(segment.get("hits") or 0)
    q_total = int(segment.get("q_total") or 0)
    c_total = int(segment.get("c_total") or 0)
    return bool(hits and q_total and c_total
                and min(hits / q_total, hits / c_total) >= 0.3)


def _query_key(s: dict) -> tuple:
    q = s.get("query_func") or {}
    return (q.get("file_path", ""), q.get("func_name", ""), q.get("start_line", 0))


def tag_upstream_baselines(suspects: list[dict], *, path: str | None = None) -> dict[str, int]:
    """就地给 upstream_vendored / abi_constrained 嫌疑对打标签，返回各类计数。幂等。

    库复用与已标 false_positive 的对不重复打标。每对记细分布尔标；主因
    upstream_vendored（root 名）/ abi_constrained 写到对应字段，供报告标注与来源核对。
    """
    roots, path_globs, name_regexes, context_paths, adapter_names = load_upstream_registry(path)
    uv_keys: set[tuple] = set()
    abi_keys: set[tuple] = set()
    for s in suspects:
        for k in (
            "upstream_vendored", "abi_constrained", "upstream_basis",
            "abi_basis", "upstream_source_valid",
        ):
            s.pop(k, None)                       # 清旧标（重算幂等）
        if s.get("reuse_library") or s.get("false_positive"):
            continue
        # 上游基线判据（任一）：① 双侧同相对路径/后缀（vendored 同版本）；
        # ② query 落在上游框架固有模块目录下（如 arceos/modules/axfs/，从 baseline 派生模块集）
        # ——②覆盖候选队改模块名(axfs→axfs-ng)/版本差异拉低向量相似度等漏判。
        root = is_upstream_vendored_pair(s, path=path)
        upstream_basis = "paired_relative_path" if root else ""
        if not root:
            qfp = (s.get("query_func") or {}).get("file_path", "")
            # 框架目录只是上游提示；候选改名/换目录时仍必须有独立代码证据，不能把任意召回项
            # 当作共同上游来源。
            if is_upstream_framework_path(qfp, roots=roots) and _pair_has_source_evidence(s):
                for seg in qfp.replace("\\", "/").lower().split("/"):
                    if seg in roots:
                        root = seg
                        break
                root = root or "upstream"
                upstream_basis = "framework_path_with_code_evidence"
        if root:
            s["upstream_vendored"] = root
            s["upstream_basis"] = upstream_basis
            s["upstream_source_valid"] = True
            uv_keys.add(_query_key(s))
            continue                              # 上游基线优先于 ABI（更具体）
        basis = _abi_basis(
            s.get("query_func") or {}, path_globs, name_regexes, context_paths, adapter_names,
        )
        if basis:
            s["abi_constrained"] = True
            s["abi_basis"] = basis
            s["upstream_source_valid"] = _pair_has_source_evidence(s)
            abi_keys.add(_query_key(s))
    return {"upstream_vendored": len(uv_keys), "abi_constrained": len(abi_keys)}


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
                "basis": s.get("upstream_basis") or s.get("abi_basis") or "",
                "source": None,
                "_source_rank": None,
            }
        item = seen[key]
        # 同一 query 的标签可能分布在多条候选对上；只从有独立 pair 证据的候选中择优归因。
        if s.get("upstream_source_valid"):
            ev = s.get("evidence") or {}
            rank = (
                int(bool(ev.get("normalized_fingerprint_match"))),
                _raw_line_similarity(s),
                int(ev.get("exact_match_lines") or 0) + int(ev.get("renamed_match_lines") or 0),
                float(ev.get("function_identity_score") or 0.0),
            )
            if item["_source_rank"] is None or rank > item["_source_rank"]:
                item["_source_rank"] = rank
                item["source"] = {
                    "repo": c.get("repo_id", ""), "file": c.get("file_path", ""),
                    "func": c.get("func_name", ""), "start": c.get("start_line", 0),
                    "sim": round(_raw_line_similarity(s), 3),
                }
    out = list(seen.values())
    for item in out:
        item.pop("_source_rank", None)
    out.sort(key=lambda x: (0 if x["reason"] == "upstream_vendored" else 1, x["module"], x["name"]))
    return out
