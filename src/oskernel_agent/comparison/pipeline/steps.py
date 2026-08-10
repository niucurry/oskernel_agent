"""流水线步骤辅助：本地 ingest、step 顺序与漏斗统计。"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from loguru import logger

from oskernel_agent.comparison.ingest.cloner import clone_repo, is_cloned
from oskernel_agent.repository_identity import repository_storage_key

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
        dest = work_root / repository_storage_key(repo_arg)
        if not is_cloned(dest):
            logger.info("克隆新作品 {} → {}", repo_arg, dest)
            clone_repo(repo_arg, dest, depth=200)
        return dest
    return Path(repo_arg)


def tier_counts(suspects: list[dict]) -> dict[str, int]:
    return dict(Counter(s.get("tier", "") for s in suspects))
