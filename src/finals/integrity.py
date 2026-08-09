"""编译/运行日志与硬编码线索的轻量事实采集。

扫描结果只表示“值得复核的线索”，不会把关键词命中直接写成作弊结论。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ALWAYS_SKIP_DIRS = {".git", ".venv", "node_modules"}
_GENERATED_DIRS = {"target", "build", "dist"}
_SOURCE_SUFFIXES = {
    ".c", ".h", ".cc", ".cpp", ".rs", ".s", ".py", ".sh", ".bash",
    ".ps1", ".bat", ".cmd", ".cmake",
}
_SCRIPT_SUFFIXES = {".py", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".cmake"}
_SCRIPT_NAMES = {"makefile", "gnumakefile", "kbuild", "kconfig"}
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
            r"(?:strcmp|strstr|contains|starts_with|ends_with|match|if)"
            r"[\s\S]{0,280}(?:test(?:case)?|ltp|benchmark|busybox|\.elf)",
            re.I,
        ),
        0.72,
        "代码根据测试名称、测试程序或 ELF 文件名选择路径，可能产生针对性输出。",
    ),
    (
        "脚本强制忽略失败",
        re.compile(
            r"(?:\|\|\s*(?:true\b|:\s*(?:#.*)?$|exit\s+0\b)|set\s+\+e\b|"
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
            r"(?:all tests passed|test passed|success|score\s*[:=]|benchmark|"
            r"expected(?:\s+output)?|golden\s+output)",
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
        if folded_parts & _ALWAYS_SKIP_DIRS:
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
        "findings": findings,
    }


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
    return {
        "build_log": analyze_log(build_log, kind="build"),
        "run_log": analyze_log(run_log, kind="run"),
        "hardcode": scan_hardcode_signals(repo_path, limit=signal_limit),
        "interpretation": (
            "硬编码扫描仅提供待复核线索；只有结合完整源码、正式日志和评测环境后，"
            "才能判断是否构成针对测试的作弊实现。"
        ),
    }
