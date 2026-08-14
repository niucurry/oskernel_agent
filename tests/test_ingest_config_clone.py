"""repos.yaml 模板/读取 与 克隆工具（URL 解析、断点续传）的单元测试。"""

from __future__ import annotations

import os
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


def test_load_repos_warns_on_placeholder_team_names(tmp_path):
    """undefined / undefinedType 这类占位队名必须在加载时就显形。"""
    import loguru

    from oskernel_agent.finals.digests import team_display_label

    path = tmp_path / "repos.yaml"
    path.write_text(
        'repos:\n'
        '- {repo_url: "https://g.example/x", year: 2023, team_name: "undefined"}\n'
        '- {repo_url: "https://g.example/y", year: 2023, team_name: "undefinedType"}\n',
        encoding="utf-8",
    )
    records: list[str] = []
    sink = loguru.logger.add(records.append, level="WARNING")
    try:
        entries = load_repos(path)
    finally:
        loguru.logger.remove(sink)
    assert len(entries) == 2
    assert any("占位符" in message for message in records)
    assert team_display_label("undefined") == "未知队伍"
    assert team_display_label("undefinedType") == "未知队伍"


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


@pytest.mark.skipif(os.name != "nt", reason="core.longpaths 仅 Windows 需要")
def test_clone_sets_longpaths_on_windows(tmp_path):
    """深路径仓库在 Windows 上 checkout 会触发 "Filename too long"，克隆须启用 longpaths。"""
    source = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "tester"], check=True)
    (source / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "init"], check=True)

    dest = tmp_path / "dest"
    assert clone_repo(str(source), dest) == "cloned"
    value = subprocess.run(
        ["git", "-C", str(dest), "config", "core.longpaths"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert value == "true"


def test_clone_with_branch_checks_out_requested_branch(tmp_path):
    """默认分支无源码的作品可指定真正含代码的分支克隆。"""
    source = tmp_path / "src"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "tester"], check=True)
    (source / "README.md").write_text("docs only", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "docs"], check=True)
    subprocess.run(["git", "-C", str(source), "checkout", "-q", "-b", "os2026-2"], check=True)
    (source / "Makefile").write_text("all:", encoding="utf-8")
    (source / "kernel.rs").write_text("fn main() {}", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "Makefile", "kernel.rs"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-q", "-m", "code"], check=True)

    dest = tmp_path / "dest"
    assert clone_repo(str(source), dest, branch="os2026-2") == "cloned"
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert head == "os2026-2"
    assert (dest / "kernel.rs").is_file()
