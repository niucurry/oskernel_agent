"""报告源码链接必须在不同 Git remote 形式和目录所有者下仍可点击。"""

from __future__ import annotations

from types import SimpleNamespace

from src.report import gitlab_links as GL
from src.report import semantic_compare as SC


def test_query_repo_info_uses_command_scoped_safe_directory(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        value = ("https://gitlab.example.com/group/project.git\n"
                 if "remote" in command else "a" * 40 + "\n")
        return SimpleNamespace(returncode=0, stdout=value.encode())

    monkeypatch.setattr(GL.subprocess, "run", fake_run)

    url, sha = GL.query_repo_info(repo)

    assert url == "https://gitlab.example.com/group/project.git"
    assert sha == "a" * 40
    assert all(command[:2] == ["git", "-c"] for command in commands)
    assert all(command[2] == f"safe.directory={repo.resolve()}" for command in commands)


def test_ssh_gitlab_remote_becomes_clickable_web_and_blob_url():
    remote = "git@gitlab.example.com:group/subgroup/project.git"

    assert GL.repo_web_url(remote) == "https://gitlab.example.com/group/subgroup/project"
    assert GL.gitlab_blob_url(remote, "b" * 40, r"src\task.rs", 12, 18) == (
        "https://gitlab.example.com/group/subgroup/project/-/blob/"
        + "b" * 40
        + "/src/task.rs#L12-18"
    )


def test_blob_url_preserves_hidden_paths_and_encodes_special_characters():
    path = "./.config/中文 目录/a#b?x%.rs"

    gitlab = GL.gitlab_blob_url(
        "https://gitlab.example.com/group/project.git", "d" * 40, path, 12, 18,
    )
    github = GL.gitlab_blob_url(
        "https://github.com/group/project.git", "e" * 40, path, 7, 0,
    )

    encoded = ".config/%E4%B8%AD%E6%96%87%20%E7%9B%AE%E5%BD%95/a%23b%3Fx%25.rs"
    assert gitlab == (
        "https://gitlab.example.com/group/project/-/blob/"
        + "d" * 40 + f"/{encoded}#L12-18"
    )
    assert github == (
        "https://github.com/group/project/blob/"
        + "e" * 40 + f"/{encoded}#L7"
    )


def test_query_and_reference_labels_use_real_anchors():
    linker = GL.GitLabLinker(
        {"2025/ref": "https://gitlab.example.com/history/ref"}, {},
        query_repo_url="https://gitlab.example.com/current/work.git",
        query_sha="c" * 40,
    )
    linker.mark_query_repo("2026/new")

    query = SC._make_gitlab_anchor(linker, "2026/new", r"os\src\main.rs", 9)
    reference = SC._ref_repo_anchor(linker, "2025/ref")

    assert '<a class="file-jump"' in query
    assert "/-/blob/" + "c" * 40 + "/os/src/main.rs#L9" in query
    assert '<a class="repo-link"' in reference
    assert 'href="https://gitlab.example.com/history/ref"' in reference


def test_repo_url_map_includes_history_and_baseline_configs(tmp_path):
    history = tmp_path / "repos.yaml"
    baseline = tmp_path / "baselines.yaml"
    history.write_text(
        "- repo_url: https://gitlab.example.com/team/work\n"
        "  year: 2025\n  team_name: work\n  award_level: x\n",
        encoding="utf-8",
    )
    baseline.write_text(
        "repos:\n- repo_url: https://github.com/example/base\n"
        "  year: 0\n  team_name: baseline_base\n  award_level: baseline\n",
        encoding="utf-8",
    )

    result = GL.build_repo_url_map(history, baseline)

    assert result["2025/work"] == "https://gitlab.example.com/team/work"
    assert result["0/baseline_base"] == "https://github.com/example/base"
