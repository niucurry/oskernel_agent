from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.pipeline.steps import build_local_meta


def test_build_local_meta_uses_command_scoped_safe_directory(tmp_path, monkeypatch):
    repo = tmp_path / "foreign-owned-repo"
    (repo / ".git").mkdir(parents=True)
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=b"@@abc|author|2026-01-02T03:04:05+08:00|initial\n1\t0\tkernel.rs\n",
            stderr=b"",
        )

    monkeypatch.setattr("src.pipeline.steps.subprocess.run", fake_run)

    commits = build_local_meta(repo)

    assert len(commits) == 1
    assert commands[0][:3] == ["git", "-c", f"safe.directory={repo.resolve()}"]
    assert commands[0][3:5] == ["-C", str(repo.resolve())]
    assert commits[0]["changed_files"] == 1


def test_build_local_meta_returns_empty_on_git_error(tmp_path, monkeypatch):
    repo = tmp_path / "broken-repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        "src.pipeline.steps.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=128, stdout=b"", stderr=b"bad repo"),
    )

    assert build_local_meta(Path(repo)) == []
    assert not (repo / "_meta.json").exists()
