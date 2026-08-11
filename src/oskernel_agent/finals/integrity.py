"""编译/运行日志与硬编码线索的轻量事实采集。

扫描结果只表示“值得复核的线索”，不会把关键词命中直接写成作弊结论。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ALWAYS_SKIP_DIRS = {".git", ".venv", "node_modules"}
_GENERATED_DIRS = {"target", "build", "dist"}
_DEPENDENCY_DIRS = {"vendor", "third_party", "thirdparty", "external"}
_SOURCE_SUFFIXES = {
    ".c", ".h", ".cc", ".cpp", ".rs", ".s", ".py", ".sh", ".bash",
    ".ps1", ".bat", ".cmd", ".cmake",
}
_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".cmake"}
_SCRIPT_NAMES = {"makefile", "gnumakefile", "kbuild", "kconfig"}
_ROOT_MAKEFILE_NAMES = ("GNUmakefile", "makefile", "Makefile")
_REQUIRED_KERNEL_TARGETS = ("kernel-rv", "kernel-la")
_MAX_FILE_BYTES = 2 * 1024 * 1024

REQUIRED_HARDCODE_CATEGORIES = (
    "按测试名或 ELF 名称分支",
    "测试专用缓存策略",
    "疑似写死测试结果",
    "脚本强制忽略失败",
)

_SIGNALS = (
    (
        "按测试名或 ELF 名称分支",
        re.compile(
            r"(?:strcmp|strstr)\s*\([^\n]{0,160}[\"'][^\"'\n]*"
            r"(?:test(?:case)?|ltp|benchmark|busybox|\.elf)[^\"'\n]*[\"']|"
            r"(?:contains|starts_with|ends_with)\s*\(\s*[\"'][^\"'\n]*"
            r"(?:test(?:case)?|ltp|benchmark|busybox|\.elf)[^\"'\n]*[\"']|"
            r"(?:if|match)[^\n]{0,200}[\"'][^\"'\n]*"
            r"(?:test(?:case)?|ltp|benchmark|busybox|\.elf)[^\"'\n]*[\"']",
            re.I,
        ),
        0.72,
        "代码根据测试名称、测试程序或 ELF 文件名选择路径，可能产生针对性输出。",
    ),
    (
        "脚本强制忽略失败",
        re.compile(
            r"(?:(?:pytest|ctest|cargo\s+test|make\s+test|\./[^\n ]*test[^\n ]*)"
            r"[^\n]{0,160}\|\|\s*(?:true\b|:\s*(?:#.*)?$|exit\s+0\b)|"
            r"(?:pytest|ctest|cargo\s+test|make\s+test)[^\n]{0,160}"
            r"(?:--deselect|--exclude|--ignore|--skip|\s-E\s|grep\s+-v)|"
            r"(?:sed|perl)[^\n]{0,120}(?:test|case)[^\n]{0,120}(?:delete|remove|skip|#))",
            re.I | re.M,
        ),
        0.58,
        "脚本显式吞掉命令失败；需核对它是否会旁路正式测试。",
    ),
    (
        "疑似写死测试结果",
        re.compile(
            r"(?:printf|puts|print|println!|panic!)\s*\([^\n]{0,180}"
            r"(?:all tests passed|test passed|\bsuccess\b|\bscore\s*[:=]|\bbenchmark\b|"
            r"\bexpected\b(?:\s+output)?|golden\s+output)",
            re.I,
        ),
        0.62,
        "输出语句包含测试通过、分数或基准结果字样；需核对是否直接伪造评测输出。",
    ),
    (
        "测试专用缓存策略",
        re.compile(
            r"(?:cache|replacement|evict|victim)[\s\S]{0,240}(?:test|benchmark|score|case)"
            r"|(?:test|benchmark|score|case)[\s\S]{0,240}(?:cache|replacement|evict|victim)",
            re.I,
        ),
        0.45,
        "缓存或替换策略与测试名称出现在同一局部上下文；证据较弱，需要人工解释设计动机。",
    ),
)

_ERROR_RE = re.compile(
    r"(?:^|\s)(?:error(?:\[[^\]]+\])?|fatal|failed|panic|undefined reference|"
    r"segmentation fault|exception)(?:\s|:)", re.I,
)
_SUCCESS_RE = re.compile(
    r"(?:build|compile|test).{0,40}(?:succeeded|successful|passed)|"
    r"(?:finished|successfully built)", re.I,
)
_TEST_SCRIPT_PATH_RE = re.compile(r"(?:^|[/_.-])(?:test|tests|grade|grader|judge|eval|case)", re.I)
_EARLY_SUCCESS_EXIT_RE = re.compile(r"^\s*exit\s+0\b", re.I | re.M)
_PATH_LOCAL_FAILURE_BYPASS_RE = re.compile(
    r"\|\|\s*(?:true\b|:\s*(?:#.*)?$|exit\s+0\b)|^\s*set\s+\+e\b",
    re.I | re.M,
)
_ROOT_DOCKER_BUILD_RE = re.compile(
    r"\b(?:docker|podman)\s+build\b(?![^\n]*(?:\s-f\s|\s--file(?:=|\s)))"
    r"[^\n]*\s\.\s*(?:#.*)?$",
    re.I,
)
_MAKE_ASSIGNMENT_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|\?=|\+=|=)\s*(.*?)\s*$"
)
_MAKE_INCLUDE_RE = re.compile(r"^\s*(?:-?include|sinclude)\s+(.+?)\s*$")
_MAKE_VAR_RE = re.compile(r"\$\(([^(){}]+)\)|\$\{([^(){}]+)\}")


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def analyze_log(path: str | Path | None, *, kind: str) -> dict:
    """读取一份外部日志，提取状态与最多 6 行错误证据。"""
    if not path:
        return {"kind": kind, "provided": False, "status": "not_provided", "errors": []}
    log_path = Path(path).resolve()
    if not log_path.is_file():
        return {
            "kind": kind, "provided": True, "status": "missing", "path": str(log_path),
            "errors": ["指定的日志文件不存在"],
        }
    text = log_path.read_text(encoding="utf-8", errors="replace")[-1_000_000:]
    error_matches = list(_ERROR_RE.finditer(text))
    success_matches = list(_SUCCESS_RE.finditer(text))
    error_lines = [
        " ".join(line.split())[:360]
        for line in text.splitlines()
        if _ERROR_RE.search(line)
    ][:6]
    # 一份日志可能包含“第一次构建失败、修复后再次构建成功”。不能因为前面出现过
    # error 就断言最终无法编译；以最后一个明确状态标记为准，同时保留错误摘录供复核。
    if error_matches and (
        not success_matches or error_matches[-1].start() > success_matches[-1].start()
    ):
        status = "failed"
    elif success_matches:
        status = "passed"
    else:
        status = "unknown"
    return {
        "kind": kind,
        "provided": True,
        "status": status,
        "path": str(log_path),
        "errors": error_lines,
    }


def scan_hardcode_signals(repo_path: str | Path, *, limit: int = 20) -> dict:
    """扫描可疑源码/脚本局部；每条结果都带真实文件和行号。

    扫描始终遍历完整个仓库并执行四类规则。达到输出上限时按类别轮询取样，避免
    第一类大量命中耗尽全局名额，导致报告错误地声称其余三类已经检查但实际未检查。
    """
    root = Path(repo_path).resolve()
    candidates: dict[str, list[dict]] = {
        category: [] for category in REQUIRED_HARDCODE_CATEGORIES
    }
    candidate_ids: set[str] = set()
    scanned_files = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        folded_parts = {part.casefold() for part in rel_parts}
        if folded_parts & (_ALWAYS_SKIP_DIRS | _DEPENDENCY_DIRS):
            continue
        suffix = path.suffix.casefold()
        script_like = suffix in _SCRIPT_SUFFIXES or path.name.casefold() in _SCRIPT_NAMES
        if folded_parts & _GENERATED_DIRS and not script_like:
            continue
        if suffix not in _SOURCE_SUFFIXES and path.name.casefold() not in _SCRIPT_NAMES:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned_files += 1
        lines = text.splitlines()
        for title, pattern, confidence, explanation in _SIGNALS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                excerpt = " ".join(lines[line_no - 1].split())[:280] if lines else ""
                signal_id = f"{_relative(path, root)}:{line_no}:{title}"
                if signal_id in candidate_ids:
                    continue
                candidate_ids.add(signal_id)
                candidates[title].append({
                    "signal_id": signal_id,
                    "category": title,
                    "path": _relative(path, root),
                    "line": line_no,
                    "excerpt": excerpt,
                    "confidence": confidence,
                    "analysis": explanation,
                })

        # “测试脚本一开始就成功退出”无法仅靠通用 `|| true` 规则发现。只在测试/评测
        # 语义路径下补充低置信候选，交给 AI 结合完整脚本判断是否旁路失败用例。
        if script_like and _TEST_SCRIPT_PATH_RE.search(_relative(path, root)):
            for match in _PATH_LOCAL_FAILURE_BYPASS_RE.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                title = "脚本强制忽略失败"
                signal_id = f"{_relative(path, root)}:{line_no}:{title}"
                if signal_id in candidate_ids:
                    continue
                candidate_ids.add(signal_id)
                candidates[title].append({
                    "signal_id": signal_id,
                    "category": title,
                    "path": _relative(path, root),
                    "line": line_no,
                    "excerpt": " ".join(lines[line_no - 1].split())[:280] if lines else "",
                    "confidence": 0.54,
                    "analysis": (
                        "测试或评测脚本显式吞掉命令失败；需核对是否会旁路正式失败用例。"
                    ),
                })
            for match in _EARLY_SUCCESS_EXIT_RE.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                if line_no > 20:
                    continue
                title = "脚本强制忽略失败"
                signal_id = f"{_relative(path, root)}:{line_no}:{title}"
                if signal_id in candidate_ids:
                    continue
                candidate_ids.add(signal_id)
                candidates[title].append({
                    "signal_id": signal_id,
                    "category": title,
                    "path": _relative(path, root),
                    "line": line_no,
                    "excerpt": " ".join(lines[line_no - 1].split())[:280] if lines else "",
                    "confidence": 0.42,
                    "analysis": (
                        "测试或评测脚本在前 20 行显式成功退出；需核对是否会在执行正式"
                        "用例前旁路失败。"
                    ),
                })

    total_candidates = sum(len(items) for items in candidates.values())
    findings: list[dict] = []
    # 轮询各类别，先保留每个已命中类别的代表证据，再分配剩余名额。
    depth = 0
    bounded_limit = max(0, int(limit))
    while len(findings) < bounded_limit:
        added = False
        for category in REQUIRED_HARDCODE_CATEGORIES:
            items = candidates[category]
            if depth < len(items) and len(findings) < bounded_limit:
                findings.append(items[depth])
                added = True
        if not added:
            break
        depth += 1

    return {
        "scanned_files": scanned_files,
        "truncated": total_candidates > len(findings),
        "candidate_count": total_candidates,
        "category_coverage": {
            category: {"scanned": True, "matches": len(candidates[category])}
            for category in REQUIRED_HARDCODE_CATEGORIES
        },
        "excluded_dependency_dirs": sorted(_DEPENDENCY_DIRS),
        "findings": findings,
    }


def _logical_make_lines(text: str) -> list[tuple[int, str]]:
    """合并 Makefile 续行，同时保留逻辑行的起始行号。"""
    result: list[tuple[int, str]] = []
    parts: list[str] = []
    start = 1
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not parts:
            start = line_no
        stripped = raw.rstrip()
        continued = stripped.endswith("\\")
        parts.append(stripped[:-1] if continued else stripped)
        if not continued:
            result.append((start, " ".join(parts)))
            parts = []
    if parts:
        result.append((start, " ".join(parts)))
    return result


def _expand_simple_make_vars(value: str, variables: dict[str, str]) -> str:
    """只展开静态字符串变量；不执行 ``$(shell ...)`` 或任何 Make 函数。"""
    expanded = value
    for _ in range(6):
        updated = _MAKE_VAR_RE.sub(
            lambda match: variables.get(match.group(1) or match.group(2) or "", match.group(0)),
            expanded,
        )
        if updated == expanded:
            break
        expanded = updated
    return expanded


def _safe_make_include(root: Path, current: Path, token: str) -> Path | None:
    token = token.strip().strip('"\'')
    if not token or any(mark in token for mark in ("$", "%", "*", "?", "[")):
        return None
    candidate = (current.parent / token).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _scan_make_targets(root: Path, entry: Path) -> tuple[dict[str, dict], list[str]]:
    """静态读取根 Makefile 及可安全解析的 include，不调用 make。"""
    variables: dict[str, str] = {}
    visited: set[Path] = set()
    queue = [entry.resolve()]
    records: list[tuple[Path, int, str]] = []

    while queue and len(visited) < 64:
        path = queue.pop(0)
        if path in visited:
            continue
        visited.add(path)
        try:
            logical_lines = _logical_make_lines(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue

        # 先收集简单变量，使 ``$(KERNEL_RV):`` 这类比赛常见写法可静态识别。
        for _, raw in logical_lines:
            line = raw.split("#", 1)[0]
            match = _MAKE_ASSIGNMENT_RE.match(line)
            if match:
                variables[match.group(1)] = _expand_simple_make_vars(
                    match.group(2).strip(), variables
                )

        for line_no, raw in logical_lines:
            line = raw.split("#", 1)[0].rstrip()
            if not line or line.startswith("\t"):
                continue
            include_match = _MAKE_INCLUDE_RE.match(line)
            if include_match:
                include_text = _expand_simple_make_vars(include_match.group(1), variables)
                for token in include_text.split():
                    included = _safe_make_include(root, path, token)
                    if included is not None and included not in visited:
                        queue.append(included)
                continue
            records.append((path, line_no, line))

    found: dict[str, dict] = {}
    aggregate_targets: set[str] = set()
    for path, line_no, line in records:
        # ``:=`` 等变量赋值不是规则；规则配方已在上面按 Tab 排除。
        match = re.match(r"^\s*([^:=]+?)\s*:(?!=)(.*)$", line)
        if not match:
            continue
        lhs = _expand_simple_make_vars(match.group(1), variables)
        names = {token.rstrip("&") for token in lhs.split()}
        aggregate_targets.update(names & {"all", "submit"})
        for target in _REQUIRED_KERNEL_TARGETS:
            if target not in names or target in found:
                continue
            found[target] = {
                "declared": True,
                "path": _relative(path, root),
                "line": line_no,
                "excerpt": " ".join(line.split())[:280],
                "expected_output": target,
            }

    return found, sorted(aggregate_targets)


def _scan_container_entry(root: Path) -> dict:
    """采集可选容器说明；Dockerfile 不是比赛规定构建接口。"""
    dockerfiles = sorted(
        (
            path for path in root.rglob("*")
            if path.is_file()
            and path.name.casefold() in {"dockerfile", "containerfile"}
            and not ({part.casefold() for part in path.relative_to(root).parts}
                     & (_ALWAYS_SKIP_DIRS | _DEPENDENCY_DIRS))
        ),
        key=lambda path: path.as_posix().casefold(),
    )
    root_files = [path for path in dockerfiles if path.parent == root]
    nested_files = [path for path in dockerfiles if path.parent != root]

    root_build_commands: list[dict] = []
    for script in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if not script.is_file() or script.name.casefold() not in _SCRIPT_NAMES:
            continue
        try:
            text = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            if _ROOT_DOCKER_BUILD_RE.search(line):
                root_build_commands.append({
                    "path": _relative(script, root),
                    "line": line_no,
                    "excerpt": " ".join(line.split())[:280],
                })

    evidence = list(root_build_commands[:2])
    if root_build_commands and not root_files:
        if nested_files:
            nested = nested_files[0]
            evidence.append({
                "path": _relative(nested, root),
                "line": 1,
                "excerpt": "Dockerfile 位于子目录，而声明的构建命令使用仓库根上下文。",
            })
        return {
            "status": "inconsistent",
            "summary": (
                "仓库自行声明的容器辅助入口执行 docker build .，但根目录没有 Dockerfile；"
                + (f"现有 {_relative(nested_files[0], root)} 不会被该命令自动采用。"
                   if nested_files else "该入口会在读取 Dockerfile 前失败。")
            ),
            "evidence": evidence,
            "dockerfiles": [_relative(path, root) for path in dockerfiles],
        }

    if root_files:
        evidence.append({
            "path": _relative(root_files[0], root),
            "line": 1,
            "excerpt": "仓库根目录提供容器构建文件。",
        })
        return {
            "status": "provided",
            "summary": "仓库额外提供根目录 Dockerfile；它不是比赛规定的 Make 构建接口，也不证明编译通过。",
            "evidence": evidence[:3],
            "dockerfiles": [_relative(path, root) for path in dockerfiles],
        }

    if nested_files:
        nested = nested_files[0]
        return {
            "status": "nested",
            "summary": (
                f"仅在子目录发现 {_relative(nested, root)}；这是补充环境材料，不影响比赛规定的 Make 构建接口判断。"
            ),
            "evidence": [{
                "path": _relative(nested, root), "line": 1,
                "excerpt": "容器构建文件位于子目录。",
            }],
            "dockerfiles": [_relative(path, root) for path in dockerfiles],
        }

    return {
        "status": "not_provided",
        "summary": "未提供 Dockerfile 或 Containerfile；比赛构建接口以根目录 Makefile 为准，因此不据此判定风险。",
        "evidence": [],
        "dockerfiles": [],
    }


def scan_build_interface(repo_path: str | Path) -> dict:
    """静态检查比赛约定的双架构 Make 入口，不执行编译或 QEMU。

    该事实用于帮助评委确认作品是否声明了规定入口。即使两个目标都存在，也只能写
    “入口完整、未实测”，不能外推为编译、启动或测试通过。
    """
    root = Path(repo_path).resolve()
    root_files = {
        path.name: path
        for path in root.iterdir()
        if path.is_file()
    }
    makefile = next(
        (root_files[name] for name in _ROOT_MAKEFILE_NAMES if name in root_files),
        None,
    )
    container = _scan_container_entry(root)
    if makefile is None:
        return {
            "status": "missing",
            "summary": (
                "仓库根目录未发现 Makefile、makefile 或 GNUmakefile，无法确认比赛规定的 "
                "kernel-rv 与 kernel-la 构建入口；本报告未执行编译。"
            ),
            "makefile": None,
            "required_targets": {
                target: {"declared": False, "expected_output": target}
                for target in _REQUIRED_KERNEL_TARGETS
            },
            "missing_targets": list(_REQUIRED_KERNEL_TARGETS),
            "aggregate_targets": [],
            "evidence": [],
            "verification": {
                "status": "not_run",
                "summary": "本地描述报告不执行 make；实际编译状态未核验。",
            },
            "container": container,
        }

    found, aggregate_targets = _scan_make_targets(root, makefile)
    targets = {
        target: found.get(target, {"declared": False, "expected_output": target})
        for target in _REQUIRED_KERNEL_TARGETS
    }
    missing_targets = [target for target in _REQUIRED_KERNEL_TARGETS if target not in found]
    makefile_rel = _relative(makefile, root)
    try:
        first_line_no, first_line = next(
            (
                (line_no, line.strip())
                for line_no, line in enumerate(
                    makefile.read_text(encoding="utf-8", errors="replace").splitlines(),
                    start=1,
                )
                if line.strip()
            ),
            (1, "根目录 Make 构建入口。"),
        )
        first_line = first_line[:280]
    except OSError:
        first_line_no = 1
        first_line = "根目录 Make 构建入口。"
    evidence = [{"path": makefile_rel, "line": first_line_no, "excerpt": first_line}]
    evidence.extend(
        found[target] for target in _REQUIRED_KERNEL_TARGETS if target in found
    )

    if not missing_targets:
        status = "complete"
        summary = (
            f"根目录 {makefile_rel} 静态识别到 kernel-rv 与 kernel-la 双架构入口，"
            "约定产物分别为仓库根目录同名文件；本报告未执行 make，不能据此判定编译通过。"
        )
    else:
        status = "partial"
        present = [target for target in _REQUIRED_KERNEL_TARGETS if target in found]
        present_text = "、".join(present) if present else "两个规定目标均未识别到"
        missing_text = "、".join(missing_targets)
        summary = (
            f"根目录 {makefile_rel} 存在，但静态检查仅确认 {present_text}；"
            f"未识别到 {missing_text}，双架构比赛构建入口不完整。本报告未执行 make。"
        )

    return {
        "status": status,
        "summary": summary,
        "makefile": makefile_rel,
        "required_targets": targets,
        "missing_targets": missing_targets,
        "aggregate_targets": aggregate_targets,
        "evidence": evidence,
        "verification": {
            "status": "not_run",
            "summary": "本地描述报告不执行 make；目标存在只表示接口声明，实际编译状态未核验。",
        },
        "container": container,
    }


def scan_reproducibility(repo_path: str | Path) -> dict:
    """兼容旧调用名；现按比赛 Make 接口采集静态构建事实。"""
    return scan_build_interface(repo_path)


def collect_integrity_facts(
    repo_path: str | Path,
    *,
    build_log: str | Path | None = None,
    run_log: str | Path | None = None,
) -> dict:
    try:
        signal_limit = max(4, int(os.environ.get("AGENT_HARDCODE_SIGNAL_LIMIT", "40")))
    except ValueError:
        signal_limit = 40
    build_interface = scan_build_interface(repo_path)
    return {
        "build_log": analyze_log(build_log, kind="build"),
        "run_log": analyze_log(run_log, kind="run"),
        "hardcode": scan_hardcode_signals(repo_path, limit=signal_limit),
        "build_interface": build_interface,
        "interpretation": (
            "构建接口仅做 Makefile 静态检查，不执行编译或 QEMU；目标存在不等于编译通过。"
            "硬编码扫描仅提供待复核线索；只有结合完整源码、正式日志和评测环境后，"
            "才能判断是否构成针对测试的作弊实现。未实测项不等于作品失败。"
        ),
    }
