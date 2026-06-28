"""通道 3：commit 异常信号。

用 git blame 定位引入嫌疑函数的 commit，结合仓库 _meta.json 检查：
  a. 单次新增 > large_commit_lines；
  b. message 属于模糊模板（init/add files/update…）；
  c. 引入时间距比赛开始 < early_impl_days 天却已是完整实现（函数行数较大）。
结果作为报告附注，不影响 tier。
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

from .config import MetadataSettings

_SHA_LINE = re.compile(r"^([0-9a-f]{40})\s+\d+\s+\d+")


def parse_blame(porcelain: str) -> tuple[list[str], dict[str, int]]:
    """解析 --line-porcelain 输出，返回 (每行 sha 列表, sha->author_time)。"""
    line_shas: list[str] = []
    times: dict[str, int] = {}
    cur: str | None = None
    for line in porcelain.splitlines():
        m = _SHA_LINE.match(line)
        if m:
            cur = m.group(1)
        elif line.startswith("author-time ") and cur:
            times[cur] = int(line.split(" ", 1)[1].strip())
        elif line.startswith("\t") and cur:
            line_shas.append(cur)
    return line_shas, times


def find_introducing_commit(repo_path: str | Path, file_path: str, start_line: int, end_line: int) -> str | None:
    """blame 函数行范围，取最早 author-time 的 commit 作为「引入」commit。"""
    try:
        raw = subprocess.run(
            ["git", "-C", str(repo_path), "blame", "-L", f"{start_line},{end_line}",
             "--line-porcelain", "--", file_path],
            check=True, capture_output=True, timeout=30,
        ).stdout
        out = raw.decode("utf-8", errors="replace") if raw else ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if not out:
        return None
    line_shas, times = parse_blame(out)
    if not line_shas:
        return None
    shas = set(line_shas)
    return min(shas, key=lambda s: times.get(s, 1 << 62))


def match_meta_commit(sha: str, meta_commits: list[dict]) -> dict | None:
    """按 sha 前缀在 _meta.json 的 commits 里找对应记录。"""
    for c in meta_commits:
        csha = str(c.get("sha", ""))
        if csha and (csha == sha or sha.startswith(csha) or csha.startswith(sha)):
            return c
    return None


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.fromisoformat(s[:10])
        except ValueError:
            return None


def detect_commit_signals(
    commit: dict | None,
    func_line_count: int,
    settings: MetadataSettings,
) -> list[str]:
    """根据 commit 记录与函数规模检测异常信号名列表。"""
    if not commit:
        return []
    signals: list[str] = []

    additions = commit.get("additions") or 0
    if additions > settings.large_commit_lines:
        signals.append("large_commit")

    msg = (commit.get("message") or "").strip()
    for pat in settings.vague_message_patterns:
        if re.match(pat, msg, re.IGNORECASE):
            signals.append("vague_message")
            break

    cdate = _parse_date(commit.get("date"))
    cstart = _parse_date(settings.contest_start)
    if cdate is not None and cstart is not None:
        # 去掉时区差异影响，按日期差
        delta_days = (cdate.date() - cstart.date()).days
        if 0 <= delta_days < settings.early_impl_days and func_line_count >= settings.early_impl_min_lines:
            signals.append("early_complete_impl")

    return signals


def analyze_function(
    repo_path: str | Path,
    meta_commits: list[dict],
    func: dict,
    settings: MetadataSettings,
) -> list[str]:
    """对一个嫌疑函数：定位引入 commit 并检测信号。"""
    sha = find_introducing_commit(repo_path, func["file_path"], func["start_line"], func["end_line"])
    if sha is None:
        return []
    commit = match_meta_commit(sha, meta_commits)
    line_count = func["end_line"] - func["start_line"] + 1
    return detect_commit_signals(commit, line_count, settings)
