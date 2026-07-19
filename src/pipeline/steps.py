"""流水线步骤辅助：本地 ingest、_meta.json 生成、step 顺序与漏斗统计。"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

from loguru import logger

from src.ingest.cloner import clone_repo, is_cloned

# 流水线步骤顺序（normalize 并入 recall：新作品在召回时在线归一化）
# fastpath：L0 文件指纹层，ingest 后 recall 前检测整文件复制，命中文件在 recall 跳过嵌入
# ai_detect：AI 生成代码检测，独立于查重漏斗，产出 {repo}_ai_detect.json 供 report 章六并入
STEPS = ["ingest", "fastpath", "recall", "exact", "segment", "metadata", "ai_detect", "report"]


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://", "git@", "ssh://"))


def local_ingest(repo_arg: str, work_root: str | Path) -> Path:
    """新作品就位：URL 则 git clone 到 work_root，否则用本地路径。"""
    if is_url(repo_arg):
        work_root = Path(work_root)
        work_root.mkdir(parents=True, exist_ok=True)
        name = repo_arg.rstrip("/").split("/")[-1].removesuffix(".git")
        dest = work_root / name
        if not is_cloned(dest):
            logger.info("克隆新作品 {} → {}", repo_arg, dest)
            clone_repo(repo_arg, dest, depth=200)
        return dest
    return Path(repo_arg)


def build_local_meta(repo_path: str | Path) -> list[dict]:
    """从本地 git log 生成 _meta.json 的 commits（供 commit 信号通道）。"""
    repo_path = Path(repo_path)
    if not (repo_path / ".git").exists():
        return []
    try:
        raw = subprocess.run(
            ["git", "-C", str(repo_path), "log", "--numstat", "--date=iso-strict",
             "--pretty=format:@@%H|%an|%aI|%s"],
            check=True, capture_output=True, timeout=60,
        ).stdout
        out = raw.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits: list[dict] = []
    cur: dict | None = None
    for line in out.splitlines():
        if line.startswith("@@"):
            sha, author, date, msg = (line[2:].split("|", 3) + ["", "", "", ""])[:4]
            cur = {"sha": sha, "author": author, "date": date, "message": msg,
                   "additions": 0, "deletions": 0, "changed_files": 0}
            commits.append(cur)
        elif line.strip() and cur is not None:
            parts = line.split("\t")
            if len(parts) == 3:
                add, dele, _ = parts
                cur["additions"] += int(add) if add.isdigit() else 0
                cur["deletions"] += int(dele) if dele.isdigit() else 0
                cur["changed_files"] += 1
    meta = {"commits": commits, "commit_count": len(commits)}
    (repo_path / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return commits


def tier_counts(suspects: list[dict]) -> dict[str, int]:
    return dict(Counter(s.get("tier", "") for s in suspects))
