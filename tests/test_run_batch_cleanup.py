from __future__ import annotations

import json
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


def test_report_digests_must_match_team_id(tmp_path):
    final_dir = tmp_path / "team-1"
    final_dir.mkdir()
    for kind in ("comparison", "description", "development"):
        (final_dir / f"{kind}.digest.json").write_text(
            '{"repo_id":"team-1"}', encoding="utf-8",
        )

    assert run_batch._report_digests_match_team("team-1", final_dir)
    (final_dir / "development.digest.json").write_text(
        '{"repo_id":"team-2"}', encoding="utf-8",
    )
    assert not run_batch._report_digests_match_team("team-1", final_dir)


def test_identity_check_uses_staging_digests_before_sidecar_cleanup(tmp_path):
    staging = tmp_path / "staging"
    final_dir = tmp_path / "team-1"
    staging.mkdir()
    for kind in ("comparison", "description", "development"):
        (staging / f"{kind}.digest.json").write_text(
            '{"repo_id":"team-1"}', encoding="utf-8",
        )

    assert run_batch._report_digests_match_team("team-1", staging)
    final_dir.mkdir()
    assert not run_batch._report_digests_match_team("team-1", final_dir)


def test_comparison_identity_hides_collision_safe_storage_key(tmp_path):
    storage_key = "team-1-deadbeef"
    html = tmp_path / "comparison.html"
    digest = tmp_path / "comparison.digest.json"
    html.write_text(
        f"<title>{storage_key} 对比分析报告</title><h1>{storage_key}</h1>",
        encoding="utf-8",
    )
    digest.write_text(
        json.dumps({"repo_id": storage_key, "closest": "2025/source"}),
        encoding="utf-8",
    )

    run_batch.normalize_comparison_identity(
        html, digest, "T202600000000001", storage_key,
    )

    assert storage_key not in html.read_text(encoding="utf-8")
    assert "T202600000000001" in html.read_text(encoding="utf-8")
    payload = json.loads(digest.read_text(encoding="utf-8"))
    assert payload == {"repo_id": "T202600000000001", "closest": "2025/source"}


def test_do_comparison_does_not_archive_old_output_after_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(run_batch, "OUT", tmp_path / "output")
    repo_name = run_batch.fork_to_repo_name("https://gitlab.example.test/group/repo")
    source = run_batch.OUT / repo_name
    source.mkdir(parents=True)
    (source / f"{repo_name}_comparison.html").write_text(
        "<html>old comparison</html>", encoding="utf-8"
    )
    (source / f"{repo_name}_comparison.digest.json").write_text(
        "{\"kind\":\"comparison\"}", encoding="utf-8"
    )
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    monkeypatch.setattr(run_batch, "run_step", lambda *args, **kwargs: (False, "failed"))

    ok, body = run_batch.do_comparison(
        "team-1", "https://gitlab.example.test/group/repo", final_dir, tmp_path / "cmp.log"
    )

    assert not ok
    assert "failed" in body
    assert not (final_dir / "comparison.html").exists()
    assert not (final_dir / "comparison.digest.json").exists()


def test_do_comparison_rejects_success_without_fresh_output(tmp_path, monkeypatch):
    monkeypatch.setattr(run_batch, "OUT", tmp_path / "output")
    repo_name = run_batch.fork_to_repo_name("https://gitlab.example.test/group/repo")
    source = run_batch.OUT / repo_name
    source.mkdir(parents=True)
    (source / f"{repo_name}_comparison.html").write_text(
        "<html>old comparison</html>", encoding="utf-8"
    )
    (source / f"{repo_name}_comparison.digest.json").write_text(
        "{\"kind\":\"comparison\"}", encoding="utf-8"
    )
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    monkeypatch.setattr(run_batch, "run_step", lambda *args, **kwargs: (True, "ok"))

    ok, body = run_batch.do_comparison(
        "team-1", "https://gitlab.example.test/group/repo", final_dir, tmp_path / "cmp.log"
    )

    assert not ok
    assert "未产生本轮新的" in body
    assert not (final_dir / "comparison.html").exists()


def test_do_comparison_archives_pair_created_by_current_run(tmp_path, monkeypatch):
    monkeypatch.setattr(run_batch, "OUT", tmp_path / "output")
    repo_name = run_batch.fork_to_repo_name("https://gitlab.example.test/group/repo")
    final_dir = tmp_path / "final"
    final_dir.mkdir()

    def successful_step(*_args, **_kwargs):
        source = run_batch.OUT / repo_name
        source.mkdir(parents=True, exist_ok=True)
        (source / f"{repo_name}_comparison.html").write_text(
            "<html>fresh comparison</html>", encoding="utf-8"
        )
        (source / f"{repo_name}_comparison.digest.json").write_text(
            json.dumps({"kind": "comparison", "fresh": True, "repo_id": repo_name}),
            encoding="utf-8",
        )
        return True, "ok"

    monkeypatch.setattr(run_batch, "run_step", successful_step)

    ok, _body = run_batch.do_comparison(
        "team-1", "https://gitlab.example.test/group/repo", final_dir, tmp_path / "cmp.log"
    )

    assert ok
    assert "fresh comparison" in (final_dir / "comparison.html").read_text(encoding="utf-8")


def test_do_comparison_skips_ai_detect_when_batch_flag_off(tmp_path, monkeypatch):
    """BATCH_ENABLE_AI_DETECT 关闭时命令必须显式带 --skip-ai-detect（流水线默认开启）。"""
    monkeypatch.delenv("BATCH_ENABLE_AI_DETECT", raising=False)
    monkeypatch.setattr(run_batch, "OUT", tmp_path / "output")
    repo_name = run_batch.fork_to_repo_name("https://gitlab.example.test/group/repo")
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    captured = {}

    def successful_step(_name, command, _logfile, **_kwargs):
        captured["command"] = command
        source = run_batch.OUT / repo_name
        source.mkdir(parents=True, exist_ok=True)
        (source / f"{repo_name}_comparison.html").write_text(
            "<html>fresh</html>", encoding="utf-8"
        )
        (source / f"{repo_name}_comparison.digest.json").write_text(
            json.dumps({"repo_id": repo_name}), encoding="utf-8"
        )
        return True, "ok"

    monkeypatch.setattr(run_batch, "run_step", successful_step)

    ok, _body = run_batch.do_comparison(
        "team-1", "https://gitlab.example.test/group/repo", final_dir, tmp_path / "cmp.log"
    )
    assert ok
    assert "--skip-ai-detect" in captured["command"]
    assert "--ai-detect" not in captured["command"]

    monkeypatch.setenv("BATCH_ENABLE_AI_DETECT", "1")
    ok, _body = run_batch.do_comparison(
        "team-1", "https://gitlab.example.test/group/repo", final_dir, tmp_path / "cmp.log"
    )
    assert ok
    assert "--ai-detect" in captured["command"]
    assert "--skip-ai-detect" not in captured["command"]


def test_batch_description_runs_without_build_verification(tmp_path, monkeypatch):
    url = "https://gitlab.example.test/group/repo"
    repo_name = run_batch.fork_to_repo_name(url)
    repos = tmp_path / "repos"
    (repos / repo_name).mkdir(parents=True)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(run_batch, "REPOS", repos)
    captured = {}

    def successful_step(name, command, logfile, timeout):
        captured.update(name=name, command=command, timeout=timeout)
        (work_dir / "description.html").write_text("report", encoding="utf-8")
        (work_dir / "description.digest.json").write_text("{}", encoding="utf-8")
        return True, "ok"

    monkeypatch.setattr(run_batch, "run_step", successful_step)
    ok, _body = run_batch.do_description(
        "team-1", url, work_dir, tmp_path / "description.log",
    )

    assert ok
    assert captured["name"] == "描述报告"
    assert "--verify-build" not in captured["command"]
    assert "--pull-build-image" not in captured["command"]
    assert captured["timeout"] == 7200
