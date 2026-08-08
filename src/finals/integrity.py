"""编译/运行日志与硬编码线索的轻量事实采集。

扫描结果只表示“值得复核的线索”，不会把关键词命中直接写成作弊结论。
"""

from __future__ import annotations

import re
from pathlib import Path

_SKIP_DIRS = {
    ".git", ".venv", "node_modules", "target", "build", "dist",
    "vendor", "third_party", "external",
}
_SOURCE_SUFFIXES = {".c", ".h", ".cc", ".cpp", ".rs", ".s", ".S", ".py", ".sh"}
_MAX_FILE_BYTES = 2 * 1024 * 1024

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
            r"(?:pytest|ctest|make\s+test)[^\n]{0,160}(?:--deselect|--exclude|grep\s+-v))",
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


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def analyze_log(path: str | Path | None, *, kind: str) -> dict:
    """读取一份外部日志，提取状态与最多 6 行错误证据。"""
    if not path:
        return {"kind": kind, "provided": False, "status": "not_provided", "errors": []}
    log_path = Path(path)
    if not log_path.is_file():
        return {
            "kind": kind, "provided": True, "status": "missing", "path": str(log_path),
            "errors": ["指定的日志文件不存在"],
        }
    text = log_path.read_text(encoding="utf-8", errors="replace")[-1_000_000:]
    error_lines = [
        " ".join(line.split())[:360]
        for line in text.splitlines()
        if _ERROR_RE.search(line)
    ][:6]
    if error_lines:
        status = "failed"
    elif _SUCCESS_RE.search(text):
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
    """扫描可疑源码/脚本局部；每条结果都带真实文件和行号。"""
    root = Path(repo_path).resolve()
    findings: list[dict] = []
    scanned_files = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        if path.suffix not in _SOURCE_SUFFIXES and path.name not in {"Makefile", "Kbuild"}:
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
                findings.append({
                    "signal_id": f"{_relative(path, root)}:{line_no}:{title}",
                    "category": title,
                    "path": _relative(path, root),
                    "line": line_no,
                    "excerpt": excerpt,
                    "confidence": confidence,
                    "analysis": explanation,
                })
                if len(findings) >= limit:
                    return {"scanned_files": scanned_files, "truncated": True,
                            "findings": findings}
    return {"scanned_files": scanned_files, "truncated": False, "findings": findings}


def collect_integrity_facts(
    repo_path: str | Path,
    *,
    build_log: str | Path | None = None,
    run_log: str | Path | None = None,
) -> dict:
    return {
        "build_log": analyze_log(build_log, kind="build"),
        "run_log": analyze_log(run_log, kind="run"),
        "hardcode": scan_hardcode_signals(repo_path),
        "interpretation": (
            "硬编码扫描仅提供待复核线索；只有结合完整源码、正式日志和评测环境后，"
            "才能判断是否构成针对测试的作弊实现。"
        ),
    }
