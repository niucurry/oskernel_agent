"""ingest 编排：克隆仓库 + 通过 GitLab API 抓取元数据 + 落盘 _meta.json。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import gitlab
from loguru import logger

from .cloner import base_url_from_url, clone_repo, project_path_from_url
from .config import RepoEntry, load_repos
from .meta import build_repo_meta

META_FILENAME = "_meta.json"


def make_gitlab(
    repo_url: str,
    token: str | None,
    gitlab_url: str | None = None,
) -> gitlab.Gitlab:
    """根据配置/环境/URL 推断实例地址并建立 GitLab 连接。"""
    url = gitlab_url or os.getenv("GITLAB_URL") or base_url_from_url(repo_url)
    if token:
        return gitlab.Gitlab(url, private_token=token)
    return gitlab.Gitlab(url)  # 公开仓库可匿名访问（受速率限制）


def write_meta(dest: str | Path, meta: dict[str, Any]) -> Path:
    """把元数据写入仓库目录下的 _meta.json。"""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / META_FILENAME
    out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def ingest_one(
    entry: RepoEntry,
    repos_root: Path,
    *,
    token: str | None,
    gitlab_url: str | None,
    force: bool,
    fetch_commit_stats: bool = True,
) -> dict[str, Any]:
    """处理单个仓库：克隆 + 抓元数据 + 写 _meta.json。"""
    dest = repos_root / entry.rel_dir
    clone_status = clone_repo(entry.repo_url, dest, token=token, force=force)
    logger.info("[{}] 克隆: {}", entry.repo_id, clone_status)

    gl = make_gitlab(entry.repo_url, token, gitlab_url)
    project = gl.projects.get(project_path_from_url(entry.repo_url))
    meta = build_repo_meta(project, entry, fetch_commit_stats=fetch_commit_stats)
    write_meta(dest, meta)
    logger.info(
        "[{}] 元数据: {} 个 commit, {} 位贡献者, fork={}",
        entry.repo_id,
        meta["commit_count"],
        len(meta["contributors"]),
        bool(meta["forked_from_project"]),
    )
    return {"repo_id": entry.repo_id, "clone": clone_status, "meta": str(dest / META_FILENAME)}


def ingest(
    config_path: str | Path,
    repos_root: str | Path,
    *,
    token: str | None = None,
    gitlab_url: str | None = None,
    force: bool = False,
    fetch_commit_stats: bool = True,
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
                    gitlab_url=gitlab_url,
                    force=force,
                    fetch_commit_stats=fetch_commit_stats,
                )
            )
        except Exception as exc:  # noqa: BLE001 — 记录并继续处理下一个
            logger.error("[{}] 处理失败: {}", entry.repo_id, exc)
            results.append({"repo_id": entry.repo_id, "error": str(exc)})

    ok = sum(1 for r in results if "error" not in r)
    logger.info("完成：成功 {} / 共 {}", ok, len(results))
    return results
