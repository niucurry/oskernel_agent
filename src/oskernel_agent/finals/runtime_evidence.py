"""核对外部运行日志的判定一致性；日志本身不证明内核能力或来源真实性。"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_UTEST_START = re.compile(r"\[\s*=+\s*\]\s*\[\s*utest\s*\]\s*started\b")
_UTEST_END = re.compile(r"\[\s*=+\s*\]\s*\[\s*utest\s*\]\s*finished\b")
_FAIL = re.compile(r"\[\s*ASSERT\s*\]\s*\[\s*unit\s*\]|\[\s*FAILED\s*\]\s*\[\s*result\s*\]")
_CASE_RESULT = re.compile(r"\[\s*(PASSED|FAILED)\s*\]\s*\[\s*result\s*\]\s*testcase(?: init| cleanup)?\s*\(([^)]+)\)")
_RAN = re.compile(r"\[\s*=+\s*\]\s*\[\s*utest\s*\]\s*(\d+) tests from (\d+) testcase ran\.")
_UNIT = re.compile(r"utest unit name:\s*\(([^)]+)\)")


def parse_utest_log(text: str) -> dict | None:
    """按最后一轮明确的 started/finished 边界核对 RT-Thread utest。

    保留原汇总判定和矛盾证据。先前独立轮次失败不会覆盖后续完整轮次；
    最后一轮未结束或零测例执行不能作为通过。未识别协议时返回 None。
    """
    lines = [_ANSI.sub("", line).strip() for line in text.splitlines()]
    starts = [i for i, line in enumerate(lines) if _UTEST_START.search(line)]
    if not starts:
        return None
    start = starts[-1]
    ends = [i for i in range(start + 1, len(lines)) if _UTEST_END.search(lines[i])]
    end = ends[0] if ends else len(lines) - 1
    scoped = list(enumerate(lines[start:end + 1], start + 1))
    failures = [{"line": i, "text": line} for i, line in scoped if _FAIL.search(line)]
    cases = []
    for i, line in scoped:
        match = _CASE_RESULT.search(line)
        if match:
            cases.append({"line": i, "name": match[2], "reported_status": match[1].lower()})
    counts = [_RAN.search(line) for _, line in scoped if _RAN.search(line)]
    ran = int(counts[-1][1]) if counts else None
    available = int(counts[-1][2]) if counts else None
    reported = "failed" if any(c["reported_status"] == "failed" for c in cases) else "passed" if cases else "unknown"
    if failures and reported == "passed":
        status = "inconsistent"
    elif (not ends or ran is None or ran == 0 or ran > available
          or len(cases) != ran or len({c["name"] for c in cases}) != ran):
        status = "unknown"
    elif failures or reported == "failed":
        status = "failed"
    else:
        status = reported
    return {
        "format": "rtthread_utest", "status": status, "reported_status": reported,
        "complete": bool(ends), "run_start_line": start + 1,
        "run_end_line": end + 1 if ends else None,
        "earlier_runs": len(starts) - 1, "cases_ran": ran, "cases_available": available,
        "cases": cases, "units_observed": [m[1] for _, line in scoped if (m := _UNIT.search(line))],
        "failure_evidence": failures,
        "claim_scope": "仅核对这一轮日志中的测试判定一致性；通过不证明 API 可用、测试充分或日志真实。",
    }


def audit_runtime_log(path: str | Path) -> dict:
    """读取完整本地日志，不执行其内容。"""
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    data = source.read_bytes()
    text = data.decode("utf-8", errors="replace")
    result = parse_utest_log(text) or {
        "format": "unrecognized", "status": "unknown", "reported_status": "unknown",
        "failure_evidence": [], "claim_scope": "未识别测试轮次协议，不能由文本推断运行成功。",
    }
    import hashlib
    return {"path": str(source), "sha256": hashlib.sha256(data).hexdigest(), **result}


def write_runtime_audit(log_path: str | Path, output: str | Path) -> dict:
    source, dest = Path(log_path), Path(output)
    if source.resolve() == dest.resolve() or (dest.exists() and source.samefile(dest)):
        raise ValueError("核对结果不能覆盖原始运行日志")
    result = audit_runtime_log(log_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() == ".html":
        evidence = "".join(
            f'<li>第 {item["line"]} 行：<code>{html.escape(item["text"])}</code></li>'
            for item in result["failure_evidence"]
        )
        body = (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>运行判定核对</title><style>body{max-width:900px;margin:3em auto;'
            'font-family:system-ui;line-height:1.6}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'
            f'<h1>运行判定核对：{html.escape(result["status"])}</h1>'
            f'<p>{html.escape(result["claim_scope"])}</p>'
            f'<p>原日志：{html.escape(result["path"])}</p>'
            f'<ul>{evidence}</ul><details><summary>完整核对记录</summary>'
            f'<pre>{html.escape(json.dumps(result,ensure_ascii=False,indent=2))}</pre></details></html>'
        )
        dest.write_text(body, encoding="utf-8")
    else:
        dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
