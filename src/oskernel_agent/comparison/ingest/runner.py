"""ingest 编排：克隆历史作品到 repos_root。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from .cloner import clone_repo
from .config import RepoEntry, load_repos


def ingest_one(
    entry: RepoEntry,
    repos_root: Path,
    *,
    token: str | None,
    force: bool,
) -> dict[str, Any]:
    """处理单个仓库：克隆到 repos_root/{year}/{team}/。"""
    dest = repos_root / entry.rel_dir
    clone_status = clone_repo(entry.repo_url, dest, token=token, force=force)
    logger.info("[{}] 克隆: {}", entry.repo_id, clone_status)
    return {"repo_id": entry.repo_id, "clone": clone_status}


def ingest(
    config_path: str | Path,
    repos_root: str | Path,
    *,
    token: str | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    """批量处理 repos.yaml 中的全部仓库。单个失败不阻断其余。"""
    repos_root = Path(repos_root)
    entries = load_repos(config_path)
    logger.info("待处理仓库 {} 个，输出根目录 {}", len(entries), repos_root)

    results: list[dict[str, Any]] = []
    for i, entry in enumerate(entries, 1):
        logger.info("=== ({}/{}) {} ===", i, len(entries), entry.repo_id)
        try:
            results.append(
                ingest_one(
                    entry,
                    repos_root,
                    token=token,
                    force=force,
                )
            )
        except Exception as exc:  # noqa: BLE001 — 记录并继续处理下一个
            logger.error("[{}] 处理失败: {}", entry.repo_id, exc)
            results.append({"repo_id": entry.repo_id, "error": str(exc)})

    ok = sum(1 for r in results if "error" not in r)
    logger.info("完成：成功 {} / 共 {}", ok, len(results))
    return results
