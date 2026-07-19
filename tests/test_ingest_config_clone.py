"""repos.yaml 模板/读取 与 克隆工具（URL 解析、断点续传）的单元测试。"""

from __future__ import annotations

import subprocess

from src.ingest.cloner import (
    _auth_url,
    _is_windows_unsafe_path,
    base_url_from_url,
    clone_repo,
    is_cloned,
    project_path_from_url,
)
from src.ingest.config import load_repos, write_template


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
    write_template(path)  # 不应覆盖
    assert load_repos(path) == []


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
    # 无 token 时原样返回
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
    # 已存在且未 --force：直接跳过，不触碰 git
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
