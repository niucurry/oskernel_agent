"""repos.yaml 模板/读取 与 克隆工具（URL 解析、断点续传）的单元测试。"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from oskernel_agent.comparison.ingest.cloner import (
    _auth_url,
    _is_windows_unsafe_path,
    base_url_from_url,
    clone_repo,
    is_cloned,
    project_path_from_url,
)
from oskernel_agent.comparison.ingest.config import load_repos, write_template


def test_write_template_has_three_entries(tmp_path):
    path = write_template(tmp_path / "repos.yaml")
    entries = load_repos(path)
    assert len(entries) == 3
    assert {e.year for e in entries} == {2023, 2024}
    assert entries[0].repo_id == "2023/team_alpha"


def test_write_template_no_overwrite(tmp_path):
    path = tmp_path / "repos.yaml"
    write_template(path)
    path.write_text("repos: []\n", encoding="utf-8")
    write_template(path)
    assert load_repos(path) == []


def test_repo_key_produces_scoped_identity(tmp_path):
    path = tmp_path / "repos.yaml"
    write_template(path)
    entries = load_repos(path)
    assert entries[0].repo_id == "2023/team_alpha"


def test_repo_key_rejects_empty_str(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(
        'repos:\n- {repo_url: "https://g.example/x", year: 2023, team_name: "a", repo_key: ""}\n',
        encoding="utf-8",
    )
    entries = load_repos(path)
    assert entries[0].repo_id == "2023/a"


def test_duplicate_repo_id_raises_without_repo_key(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(
        'repos:\n'
        '- {repo_url: "https://g.example/x", year: 2023, team_name: "a"}\n'
        '- {repo_url: "https://g.example/y", year: 2023, team_name: "a"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repo_id 冲突"):
        load_repos(path)


def test_repo_key_case_mismatch_still_collides(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(
        'repos:\n'
        '- {repo_url: "https://g.example/x", year: 2023, team_name: "aB"}\n'
        '- {repo_url: "https://g.example/y", year: 2023, team_name: "ab"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="repo_id 冲突"):
        load_repos(path)


def test_repo_key_allows_internal_space(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(
        'repos:\n'
        '- {repo_url: "https://g.example/x", year: 2023, team_name: "a", repo_key: "key with space"}\n',
        encoding="utf-8",
    )
    entries = load_repos(path)
    assert entries[0].repo_id == "2023/key with space"


def test_invalid_repo_key_character_raises(tmp_path):
    path = tmp_path / "repos.yaml"
    path.write_text(
        'repos:\n'
        '- {repo_url: "https://g.example/x", year: 2023, team_name: "a", repo_key: "key/with/slash"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_repos(path)


def test_project_path_from_url():
    assert project_path_from_url("https://gitlab.com/g/sub/proj") == "g/sub/proj"
    assert project_path_from_url("https://gitlab.com/g/proj.git") == "g/proj"
    assert project_path_from_url("https://gitlab.com/g/proj/") == "g/proj"


def test_base_url_from_url():
    assert base_url_from_url("https://gitlab.example.com:8443/g/p") == "https://gitlab.example.com:8443"
    assert base_url_from_url("https://gitlab.com/g/p") == "https://gitlab.com"


def test_auth_url_injects_token():
    url = _auth_url("https://gitlab.com/g/p.git", "secret-tok")
    assert url == "https://oauth2:secret-tok@gitlab.com/g/p.git"
    assert _auth_url("https://gitlab.com/g/p.git", None) == "https://gitlab.com/g/p.git"


def test_clone_skips_existing_without_force(tmp_path):
    dest = tmp_path / "repo"
    dest.mkdir()
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(dest), "config", "user.name", "tester"], check=True)
    (dest / "kernel.rs").write_text("fn main() {}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(dest), "add", "kernel.rs"], check=True)
    subprocess.run(["git", "-C", str(dest), "commit", "-q", "-m", "init"], check=True)
    assert is_cloned(dest)
    status = clone_repo("https://gitlab.com/g/p", dest, force=False)
    assert status == "skipped"


def test_incomplete_git_directory_is_not_a_clone(tmp_path):
    dest = tmp_path / "broken"
    (dest / ".git" / "objects").mkdir(parents=True)
    assert not is_cloned(dest)


def test_windows_reserved_path_detection():
    assert _is_windows_unsafe_path("os/src/task/aux.rs")
    assert _is_windows_unsafe_path("drivers/COM1.c")
    assert not _is_windows_unsafe_path("os/src/task/processor.rs")
