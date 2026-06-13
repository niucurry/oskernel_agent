"""验证 build_repo_meta 产出的 _meta.json 字段完整性（用 mock GitLab API）。"""

from __future__ import annotations

import json

from src.ingest.meta import COMMIT_FIELDS, build_repo_meta
from src.ingest.runner import write_meta

REQUIRED_TOP_FIELDS = {
    "schema_version",
    "repo_id",
    "repo_url",
    "year",
    "team_name",
    "award_level",
    "gitlab_project_id",
    "path_with_namespace",
    "created_at",
    "forked_from_project",
    "contributors",
    "commit_count",
    "commits",
    "fetched_at",
}


def test_top_level_fields_complete(mock_project, repo_entry):
    meta = build_repo_meta(mock_project, repo_entry)
    assert REQUIRED_TOP_FIELDS <= set(meta)
    assert meta["repo_id"] == "2023/team_alpha"
    assert meta["gitlab_project_id"] == 12345
    assert meta["created_at"] == "2023-01-15T08:00:00Z"


def test_commit_history_fields(mock_project, repo_entry):
    meta = build_repo_meta(mock_project, repo_entry)
    assert meta["commit_count"] == 2
    assert len(meta["commits"]) == 2
    for c in meta["commits"]:
        assert set(COMMIT_FIELDS) <= set(c)
    first = meta["commits"][0]
    assert first["sha"] == "a1a1a1"
    assert first["author"] == "Alice"
    assert first["date"] == "2023-02-01T10:00:00Z"
    assert first["message"] == "init kernel skeleton"
    assert first["changed_files"] == 3
    assert first["additions"] == 100
    assert first["deletions"] == 0


def test_fork_relationship_captured(mock_project, repo_entry):
    meta = build_repo_meta(mock_project, repo_entry)
    assert meta["forked_from_project"]["path_with_namespace"] == "upstream/os-template"


def test_contributors_captured(mock_project, repo_entry):
    meta = build_repo_meta(mock_project, repo_entry)
    names = {c["name"] for c in meta["contributors"]}
    assert names == {"Alice", "Bob"}
    assert all({"name", "email", "commits"} <= set(c) for c in meta["contributors"])


def test_no_commit_stats_skips_get(mock_project, repo_entry):
    meta = build_repo_meta(mock_project, repo_entry, fetch_commit_stats=False)
    mock_project.commits.get.assert_not_called()
    for c in meta["commits"]:
        assert c["changed_files"] is None
        assert c["additions"] is None
        assert c["deletions"] is None


def test_write_meta_roundtrip(mock_project, repo_entry, tmp_path):
    meta = build_repo_meta(mock_project, repo_entry)
    out = write_meta(tmp_path / "2023" / "team_alpha", meta)
    assert out.name == "_meta.json"
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["repo_id"] == "2023/team_alpha"
    assert loaded["commit_count"] == 2
