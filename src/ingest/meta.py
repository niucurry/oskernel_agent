"""从 GitLab API 项目对象构建 _meta.json 内容。

这里的函数只依赖 python-gitlab 的 Project 对象接口（commits / repository_contributors /
属性），因此单元测试可以用 mock 的 Project 直接验证字段完整性，无需联网。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import RepoEntry

# 落盘 schema 版本，便于后续字段演进时识别旧数据
META_SCHEMA_VERSION = 1

# 每条 commit 必须具备的字段（供测试断言）
COMMIT_FIELDS = (
    "sha",
    "author",
    "date",
    "message",
    "changed_files",
    "additions",
    "deletions",
)


def _commit_to_dict(project: Any, commit: Any, fetch_stats: bool) -> dict[str, Any]:
    """把一个 commit 摘要对象转为标准 dict，可选拉取增删行/变更文件数。"""
    sha = getattr(commit, "id", None) or getattr(commit, "sha", None)
    author = getattr(commit, "author_name", None)
    date = getattr(commit, "created_at", None) or getattr(commit, "committed_date", None)
    message = (
        getattr(commit, "message", None) or getattr(commit, "title", "") or ""
    ).strip()

    changed_files: int | None = None
    additions: int | None = None
    deletions: int | None = None
    if fetch_stats and sha is not None:
        full = project.commits.get(sha)
        stats = getattr(full, "stats", None) or {}
        additions = stats.get("additions")
        deletions = stats.get("deletions")
        diffs = full.diff(get_all=True) or []
        changed_files = len(diffs)

    return {
        "sha": sha,
        "author": author,
        "date": date,
        "message": message,
        "changed_files": changed_files,
        "additions": additions,
        "deletions": deletions,
    }


def _contributors(project: Any) -> list[dict[str, Any]]:
    try:
        raw = project.repository_contributors(all=True)
    except Exception:  # noqa: BLE001 — 贡献者接口失败不应阻断整体抓取
        return []
    out: list[dict[str, Any]] = []
    for c in raw or []:
        out.append(
            {
                "name": c.get("name"),
                "email": c.get("email"),
                "commits": c.get("commits"),
                "additions": c.get("additions"),
                "deletions": c.get("deletions"),
            }
        )
    return out


def build_repo_meta(
    project: Any,
    entry: RepoEntry,
    *,
    fetch_commit_stats: bool = True,
) -> dict[str, Any]:
    """构建单仓库的 _meta.json 内容。

    包含：仓库身份、项目创建时间、fork 关系、贡献者列表、完整 commit 历史
    （每条含 sha/author/date/message/变更文件数/增删行数）。
    """
    commits = [
        _commit_to_dict(project, c, fetch_commit_stats)
        for c in project.commits.list(all=True, iterator=True)
    ]

    return {
        "schema_version": META_SCHEMA_VERSION,
        "repo_id": entry.repo_id,
        "repo_url": entry.repo_url,
        "year": entry.year,
        "team_name": entry.team_name,
        "award_level": entry.award_level,
        "gitlab_project_id": getattr(project, "id", None),
        "path_with_namespace": getattr(project, "path_with_namespace", None),
        "created_at": getattr(project, "created_at", None),
        "forked_from_project": getattr(project, "forked_from_project", None),
        "contributors": _contributors(project),
        "commit_count": len(commits),
        "commits": commits,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
