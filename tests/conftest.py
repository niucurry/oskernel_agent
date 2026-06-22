"""ingest 测试的共享夹具：用 tests/fixtures/sample_gitlab.json 构造 mock GitLab Project。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.ingest.config import RepoEntry

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_data() -> dict:
    return json.loads((FIXTURES / "sample_gitlab.json").read_text(encoding="utf-8"))


@pytest.fixture
def mock_project(sample_data: dict) -> MagicMock:
    """构造一个行为接近 python-gitlab Project 的 mock 对象。

    - project.commits.list(...) 返回 commit 摘要对象
    - project.commits.get(sha) 返回带 .stats 与 .diff() 的完整 commit
    - project.repository_contributors(...) 返回贡献者 dict 列表
    """
    proj_attrs = sample_data["project"]
    commits = sample_data["commits"]

    summaries = [
        SimpleNamespace(
            id=c["id"],
            author_name=c["author_name"],
            created_at=c["created_at"],
            message=c["message"],
        )
        for c in commits
    ]
    by_sha = {c["id"]: c for c in commits}

    def commits_get(sha: str):
        c = by_sha[sha]
        full = MagicMock()
        full.stats = c["stats"]
        # diff(get_all=True) 返回长度等于变更文件数的列表
        full.diff.return_value = [{"old_path": f"f{i}"} for i in range(c["changed_files"])]
        return full

    project = MagicMock()
    project.id = proj_attrs["id"]
    project.path_with_namespace = proj_attrs["path_with_namespace"]
    project.created_at = proj_attrs["created_at"]
    project.forked_from_project = proj_attrs["forked_from_project"]
    project.commits.list.return_value = summaries
    project.commits.get.side_effect = commits_get
    project.repository_contributors.return_value = sample_data["contributors"]
    return project


@pytest.fixture
def repo_entry() -> RepoEntry:
    return RepoEntry(
        repo_url="https://gitlab.com/group-2023/team-alpha-os",
        year=2023,
        team_name="team_alpha",
        award_level="一等奖",
    )
