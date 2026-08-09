from __future__ import annotations

from pathlib import Path

import pytest

from oskernel_agent.cli import batch as run_batch


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


def test_publish_final_reports_is_transactional_and_keeps_no_sidecars(
    tmp_path, monkeypatch
):
    output = tmp_path / "output"
    staging = tmp_path / "staging"
    output.mkdir()
    staging.mkdir()
    monkeypatch.setattr(run_batch, "OUT", output)
    team_id = "team-1"
    final_dir = output / team_id
    final_dir.mkdir()
    (final_dir / "stale.digest.json").write_text("{}", encoding="utf-8")
    for path in run_batch.deliverable_paths(team_id, staging):
        path.write_bytes(path.name.encode())
    (staging / "description.digest.json").write_text("{}", encoding="utf-8")
    (staging / "description.tree.json").write_text("{}", encoding="utf-8")

    run_batch.publish_final_reports(team_id, staging, final_dir)

    assert {path.name for path in final_dir.iterdir()} == {
        "summary.pdf",
        "description.html",
        "development.html",
        "comparison.html",
    }
    assert (staging / "description.digest.json").is_file()


def test_publish_final_reports_rejects_incomplete_stage_without_touching_final(
    tmp_path, monkeypatch
):
    output = tmp_path / "output"
    staging = tmp_path / "staging"
    output.mkdir()
    staging.mkdir()
    monkeypatch.setattr(run_batch, "OUT", output)
    team_id = "team-1"
    final_dir = _write_reports(output, team_id)
    (staging / "summary.pdf").write_bytes(b"partial")

    with pytest.raises(RuntimeError, match="拒绝发布不完整报告"):
        run_batch.publish_final_reports(team_id, staging, final_dir)

    assert {path.name for path in final_dir.iterdir()} == {
        path.name for path in run_batch.deliverable_paths(team_id, final_dir)
    }


def test_cleanup_batch_runtime_removes_logs_state_and_clones(tmp_path, monkeypatch):
    logdir = tmp_path / "_batch"
    repos = tmp_path / "_repos"
    audit = tmp_path / "recall_completeness_audit.json"
    logdir.mkdir()
    repos.mkdir()
    (logdir / "state.json").write_text("{}", encoding="utf-8")
    clone = repos / "repo"
    clone.mkdir()
    (clone / "source.rs").write_text("fn main() {}", encoding="utf-8")
    audit.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(run_batch, "OUT", tmp_path)
    monkeypatch.setattr(run_batch, "LOGDIR", logdir)
    monkeypatch.setattr(run_batch, "REPOS", repos)

    run_batch.cleanup_batch_runtime()

    assert not logdir.exists()
    assert not repos.exists()
    assert not audit.exists()


def test_remove_empty_final_dir_only_removes_scoped_empty_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(run_batch, "OUT", tmp_path)
    empty = tmp_path / "team-1"
    empty.mkdir()

    run_batch.remove_empty_final_dir("team-1", empty)

    assert not empty.exists()
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    with pytest.raises(ValueError, match="拒绝清理"):
        run_batch.remove_empty_final_dir("outside", outside)
