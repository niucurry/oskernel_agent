"""编译/运行日志与硬编码线索的轻量事实采集。

扫描结果只表示“值得复核的线索”，不会把关键词命中直接写成作弊结论。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
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
DEFAULT_CONTEST_BUILD_IMAGE = "zhouzhouyi/os-contest:20260510"
DEFAULT_HARDCODE_SIGNAL_LIMIT = 100
_BUILD_COPY_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "target", "build", "dist", "__pycache__",
}
_BUILD_ENV_ERROR_RE = re.compile(
    r"cannot connect to the docker daemon|docker desktop.*not running|"
    r"error during connect|no space left on device|mounts denied|drive is not shared|"
    r"manifest unknown|pull access denied|no matching manifest|oci runtime|"
    r"failed to create task for container|"
    r"could not download file|error sending request for url|failed to lookup address|"
    r"network is unreachable|failed to download|failed to fetch|download of .+ failed",
    re.I,
)

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


def _build_log_details(text: str) -> tuple[list[str], list[str]]:
    lines = [" ".join(line.split())[:360] for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if _ERROR_RE.search(line)][:6]
    return errors, lines[-12:]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_copy_ignore(root: Path):
    def ignore(source: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name.casefold() in _BUILD_COPY_SKIP_DIRS}
        if Path(source).resolve() == root:
            ignored.update(name for name in names if name in _REQUIRED_KERNEL_TARGETS)
        return ignored

    return ignore


def _prepare_build_workspace(root: Path, workspace: Path, temp_root: Path) -> dict:
    """构造可复现编译输入；Git 仓库使用 HEAD 原始 blob，避开宿主换行转换。"""
    git = shutil.which("git")
    if git:
        top = _local_result(
            [git, "-C", str(root), "rev-parse", "--show-toplevel"], timeout=30,
        )
        if top.returncode == 0:
            # Windows 的 Git 输出路径可能受控制台编码影响；仓库根的 .git（目录或
            # worktree 指针文件）比字符串往返比较更可靠。
            is_root = (root / ".git").exists()
            if is_root:
                revision = _local_result(
                    [git, "-C", str(root), "rev-parse", "HEAD"], timeout=30,
                )
                status = _local_result(
                    [git, "-C", str(root), "status", "--porcelain=v1"], timeout=60,
                )
                archive_path = temp_root / "source-head.tar"
                archive = _local_result(
                    [
                        git, "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                        "-C", str(root), "archive", "--format=tar",
                        f"--output={archive_path}", "HEAD",
                    ],
                    timeout=120,
                )
                if revision.returncode == 0 and archive.returncode == 0 and archive_path.is_file():
                    workspace.mkdir()
                    with tarfile.open(archive_path, mode="r:") as source_tar:
                        source_tar.extractall(workspace, filter="data")
                    return {
                        "kind": "git_head_snapshot",
                        "commit": revision.stdout.strip(),
                        "working_tree_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
                    }

    shutil.copytree(
        root,
        workspace,
        symlinks=True,
        ignore=_build_copy_ignore(root),
    )
    return {
        "kind": "filesystem_copy",
        "commit": "",
        "working_tree_dirty": None,
    }


def _local_result(
    command: list[str], *, timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _docker_result(
    command: list[str], *, timeout: int,
) -> subprocess.CompletedProcess[str]:
    return _local_result(command, timeout=timeout)


def verify_contest_build(
    repo_path: str | Path,
    *,
    image: str = DEFAULT_CONTEST_BUILD_IMAGE,
    timeout_seconds: int = 1800,
    pull_image: bool = False,
) -> dict:
    """在比赛统一镜像的临时副本中依次执行双架构 Make 目标。

    参赛仓库按不可信输入处理：构建发生在一次性目录和一次性容器中，不挂载原仓库、
    Docker socket 或其他主机目录，且关闭容器网络。这里只验证编译和根目录产物，不启动
    内核，也不把宿主环境异常归因于参赛作品。
    """
    root = Path(repo_path).resolve()
    image = str(image or DEFAULT_CONTEST_BUILD_IMAGE).strip()
    timeout_seconds = max(60, int(timeout_seconds or 1800))
    started = time.monotonic()
    base = {
        "requested": True,
        "image": image,
        "image_id": "",
        "image_digest": "",
        "image_size_bytes": 0,
        "workspace": "temporary_copy",
        "network": "disabled",
        "limits": {
            "cpus": os.environ.get("OSKERNEL_BUILD_CPUS", "8"),
            "memory": os.environ.get("OSKERNEL_BUILD_MEMORY", "12g"),
            "pids": 2048,
        },
        "source": {
            "kind": "not_prepared",
            "commit": "",
            "working_tree_dirty": None,
        },
        "targets": {},
    }

    docker = shutil.which("docker")
    if not docker:
        return {
            **base,
            "status": "environment_error",
            "summary": "本机未安装 Docker CLI，未执行比赛镜像编译。",
            "errors": ["docker command not found"],
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    try:
        server = _docker_result(
            [docker, "version", "--format", "{{json .Server}}"], timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            **base,
            "status": "environment_error",
            "summary": "无法连接 Docker Linux 引擎，未执行比赛镜像编译。",
            "errors": [str(exc)[:360]],
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    if server.returncode != 0 or not server.stdout.strip() or server.stdout.strip() == "null":
        errors, tail = _build_log_details(server.stdout)
        return {
            **base,
            "status": "environment_error",
            "summary": "Docker Linux 引擎未运行，未执行比赛镜像编译。",
            "errors": errors or tail[-2:] or ["Docker server unavailable"],
            "duration_seconds": round(time.monotonic() - started, 2),
        }

    inspect = _docker_result(
        [docker, "image", "inspect", image, "--format", "{{json .}}"], timeout=30,
    )
    pull_output = ""
    if inspect.returncode != 0 and pull_image:
        try:
            pull = _docker_result(
                [docker, "pull", image], timeout=max(timeout_seconds, 3600),
            )
            pull_output = pull.stdout or ""
        except subprocess.TimeoutExpired as exc:
            return {
                **base,
                "status": "environment_error",
                "summary": "拉取比赛镜像超时，未执行编译。",
                "errors": [str(exc)[:360]],
                "duration_seconds": round(time.monotonic() - started, 2),
            }
        if pull.returncode == 0:
            inspect = _docker_result(
                [docker, "image", "inspect", image, "--format", "{{json .}}"], timeout=30,
            )
    if inspect.returncode != 0:
        errors, tail = _build_log_details(pull_output or inspect.stdout)
        return {
            **base,
            "status": "environment_error",
            "summary": f"本机没有可用的比赛镜像 {image}，未执行编译。",
            "errors": errors or tail[-2:],
            "duration_seconds": round(time.monotonic() - started, 2),
        }
    try:
        image_meta = json.loads(inspect.stdout)
    except json.JSONDecodeError:
        image_meta = {}
    repo_digests = image_meta.get("RepoDigests") or []
    base.update({
        "image_id": str(image_meta.get("Id") or ""),
        "image_digest": str(repo_digests[0] if repo_digests else ""),
        "image_size_bytes": int(image_meta.get("Size") or 0),
    })
    runtime_image = base["image_id"] or image

    temp_parent = root.parent if os.access(root.parent, os.W_OK) else None
    try:
        with tempfile.TemporaryDirectory(
            prefix=".oskernel-build-", dir=str(temp_parent) if temp_parent else None,
        ) as temp_dir:
            workspace = Path(temp_dir) / "repo"
            base["source"] = _prepare_build_workspace(root, workspace, Path(temp_dir))
            for target in _REQUIRED_KERNEL_TARGETS:
                target_started = time.monotonic()
                artifact_path = workspace / target
                if artifact_path.is_symlink() or artifact_path.is_file():
                    artifact_path.unlink()
                elif artifact_path.is_dir():
                    shutil.rmtree(artifact_path)
                command = [
                    docker,
                    "run",
                    "--rm",
                    "--network", "none",
                    "--security-opt", "no-new-privileges",
                    "--cap-drop", "ALL",
                    "--cpus", base["limits"]["cpus"],
                    "--memory", base["limits"]["memory"],
                    "--pids-limit", "2048",
                    "--mount", f"type=bind,source={workspace},target=/work",
                    "-w", "/work",
                    runtime_image,
                    "/bin/bash", "-lc", f"make {target}",
                ]
                try:
                    process = _docker_result(command, timeout=timeout_seconds)
                    output = process.stdout or ""
                    exit_code = process.returncode
                    status = "passed" if exit_code == 0 else "failed"
                except subprocess.TimeoutExpired as exc:
                    raw_output = exc.stdout or ""
                    output = (
                        raw_output.decode("utf-8", errors="replace")
                        if isinstance(raw_output, bytes) else str(raw_output)
                    )
                    exit_code = None
                    status = "timeout"
                errors, tail = _build_log_details(output)
                artifact = None
                if artifact_path.is_file() and artifact_path.stat().st_size > 0:
                    artifact = {
                        "path": target,
                        "size_bytes": artifact_path.stat().st_size,
                        "sha256": _sha256_file(artifact_path),
                    }
                if status == "passed" and artifact is None:
                    status = "failed"
                    errors.insert(0, f"命令退出码为 0，但仓库根目录未生成非空 {target}")
                elif status == "passed":
                    # 配置项名称可能含 ERROR（如 IOCTL_HEX2STR_ERROR），但成功退出且
                    # 生成目标产物时不能在结构化结果中同时保留“错误”列表。
                    errors = []
                if status == "failed" and (
                    exit_code == 125 or _BUILD_ENV_ERROR_RE.search(output)
                ):
                    status = "environment_error"
                base["targets"][target] = {
                    "status": status,
                    "command": f"make {target}",
                    "exit_code": exit_code,
                    "duration_seconds": round(time.monotonic() - target_started, 2),
                    "artifact": artifact,
                    "errors": errors[:6],
                    "log_tail": tail,
                }
    except (OSError, shutil.Error) as exc:
        return {
            **base,
            "status": "environment_error",
            "summary": "创建一次性编译副本失败，未能完成比赛镜像编译。",
            "errors": [str(exc)[:360]],
            "duration_seconds": round(time.monotonic() - started, 2),
        }

    statuses = [
        str((base["targets"].get(target) or {}).get("status") or "unknown")
        for target in _REQUIRED_KERNEL_TARGETS
    ]
    if statuses and all(status == "passed" for status in statuses):
        status = "passed"
        summary = "比赛统一镜像中 kernel-rv 与 kernel-la 均编译成功，并生成非空根目录产物。"
    elif "passed" in statuses:
        status = "partial"
        summary = "比赛统一镜像中仅有一个架构完成编译，双架构编译未全部通过。"
    elif any(item == "environment_error" for item in statuses):
        status = "environment_error"
        summary = "比赛镜像编译受到环境或构建前置依赖影响，未进入可归因于源码的编译阶段；不能据此判断作品失败。"
    elif any(item == "timeout" for item in statuses):
        status = "timeout"
        summary = "比赛镜像编译超时，未形成双架构编译结论。"
    else:
        status = "failed"
        summary = "比赛统一镜像中 kernel-rv 与 kernel-la 均未编译成功。"
    return {
        **base,
        "status": status,
        "summary": summary,
        "errors": [],
        "duration_seconds": round(time.monotonic() - started, 2),
    }


def scan_build_interface(repo_path: str | Path) -> dict:
    """静态检查比赛约定的双架构 Make 入口，本函数本身不执行编译。

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
                "kernel-rv 与 kernel-la 构建入口。"
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
                "summary": "未请求比赛镜像编译验证；实际编译状态未核验。",
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
            "约定产物分别为仓库根目录同名文件；静态目标存在不能据此判定编译通过。"
        )
    else:
        status = "partial"
        present = [target for target in _REQUIRED_KERNEL_TARGETS if target in found]
        present_text = "、".join(present) if present else "两个规定目标均未识别到"
        missing_text = "、".join(missing_targets)
        summary = (
            f"根目录 {makefile_rel} 存在，但静态检查仅确认 {present_text}；"
            f"未识别到 {missing_text}，双架构比赛构建入口不完整。"
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
            "summary": "未请求比赛镜像编译验证；目标存在只表示接口声明，实际编译状态未核验。",
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
    verify_build: bool = False,
    build_image: str = DEFAULT_CONTEST_BUILD_IMAGE,
    build_timeout: int = 1800,
    pull_build_image: bool = False,
) -> dict:
    try:
        signal_limit = max(4, int(os.environ.get("AGENT_HARDCODE_SIGNAL_LIMIT", str(DEFAULT_HARDCODE_SIGNAL_LIMIT))))
    except ValueError:
        signal_limit = DEFAULT_HARDCODE_SIGNAL_LIMIT
    build_interface = scan_build_interface(repo_path)
    build_verification = (
        verify_contest_build(
            repo_path,
            image=build_image,
            timeout_seconds=build_timeout,
            pull_image=pull_build_image,
        )
        if verify_build else build_interface["verification"]
    )
    build_interface["verification"] = build_verification
    return {
        "build_log": analyze_log(build_log, kind="build"),
        "run_log": analyze_log(run_log, kind="run"),
        "hardcode": scan_hardcode_signals(repo_path, limit=signal_limit),
        "build_interface": build_interface,
        "build_verification": build_verification,
        "interpretation": (
            "构建接口来自 Makefile 静态检查；若请求比赛镜像验证，则双架构编译在一次性副本中执行。"
            "目标存在不等于编译通过，宿主或 Docker 环境错误也不等于作品失败。"
            "硬编码扫描仅提供待复核线索；只有结合完整源码、正式日志和评测环境后，"
            "才能判断是否构成针对测试的作弊实现。未实测项不等于作品失败。"
        ),
    }
