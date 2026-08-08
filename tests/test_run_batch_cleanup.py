from __future__ import annotations

from pathlib import Path

import pytest

import run_batch


def _write_reports(root: Path, team_id: str) -> Path:
    final_dir = root / team_id
    final_dir.mkdir(parents=True)
    for path in run_batch.deliverable_paths(team_id, final_dir):
        path.write_bytes(b"report")
    return final_dir


def test_cleanup_final_dir_keeps_exactly_four_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(run_batch, "OUT", tmp_path)
    final_dir = _write_reports(tmp_path, "team-1")
    (final_dir / "description.digest.json").write_text("{}", encoding="utf-8")
    (final_dir / "development.ai.json").write_text("{}", encoding="utf-8")
    work = final_dir / "description_tree_work"
    work.mkdir()
    (work / "verdict.json").write_text("{}", encoding="utf-8")

    run_batch.cleanup_final_dir("team-1", final_dir)

    assert {path.name for path in final_dir.iterdir()} == {
        "summary.pdf",
        "description.html",
        "development.html",
        "comparison.html",
    }


def test_cleanup_final_dir_refuses_incomplete_or_unscoped_target(tmp_path, monkeypatch):
    monkeypatch.setattr(run_batch, "OUT", tmp_path)
    incomplete = tmp_path / "team-1"
    incomplete.mkdir()
    (incomplete / "summary.pdf").write_bytes(b"report")
    with pytest.raises(RuntimeError, match="尚未齐全"):
        run_batch.cleanup_final_dir("team-1", incomplete)

    outside = tmp_path.parent / "outside-team"
    outside.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="拒绝清理"):
        run_batch.cleanup_final_dir("outside-team", outside)


def test_cleanup_team_never_deletes_final_dir_when_repo_name_matches_team(
    tmp_path, monkeypatch
):
    output = tmp_path / "output"
    repos = tmp_path / "repos"
    output.mkdir()
    repos.mkdir()
    monkeypatch.setattr(run_batch, "OUT", output)
    monkeypatch.setattr(run_batch, "REPOS", repos)
    final_dir = _write_reports(output, "same-name")
    clone = repos / "same-name"
    clone.mkdir()
    (clone / "source.rs").write_text("fn main() {}", encoding="utf-8")

    run_batch.cleanup_team("same-name", final_dir=final_dir)

    assert final_dir.is_dir()
    assert {path.name for path in final_dir.iterdir()} == {
        path.name for path in run_batch.deliverable_paths("same-name", final_dir)
    }
    assert not clone.exists()
