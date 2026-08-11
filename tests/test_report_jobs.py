"""Tests for report_jobs — the unified report orchestration module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from oskernel_agent.report_jobs import run
from oskernel_agent.report_jobs._runner import (
    _load_state,
    _save_state,
    _init_state,
    _state_path,
    _find_files_by_suffix,
    _find_single_by_suffix,
    _is_url,
    _safe_dirname,
)


def _tdir(suffix: str = "") -> Path:
    return Path(tempfile.mkdtemp(suffix=suffix))


# ---- state file ----

def test_state_roundtrip() -> None:
    d = _tdir()
    state = _init_state("repo_x", "https://example.com/repo.git", ["comparison", "description"])
    _save_state(d, state)
    loaded = _load_state(d)
    assert loaded["repo_id"] == "repo_x"
    assert loaded["repo_url"] == "https://example.com/repo.git"
    assert loaded["kinds_requested"] == ["comparison", "description"]


def test_load_state_missing() -> None:
    d = _tdir()
    assert _load_state(d) == {}


def test_load_state_corrupt() -> None:
    d = _tdir()
    sp = _state_path(d)
    sp.write_text("not json", encoding="utf-8")
    assert _load_state(d) == {}


# ---- url detection ----

def test_is_url_http() -> None:
    assert _is_url("https://gitlab.example.com/group/repo.git") is True


def test_is_url_local_path() -> None:
    assert _is_url("/home/user/repo") is False
    assert _is_url("C:\\Users\\repo") is False


# ---- safe_dirname ----

def test_safe_dirname_http_url() -> None:
    name = _safe_dirname("https://gitlab.com/group/my-repo.git", "fallback")
    assert name == "my-repo"


def test_safe_dirname_evil_chars() -> None:
    name = _safe_dirname("https://x.com/a<b>c:d*e?f.git", "fb")
    assert "<" not in name
    assert ">" not in name
    assert ":" not in name


# ---- file discovery ----

def test_find_files_by_suffix() -> None:
    d = _tdir()
    (d / "a.html").write_text("")
    (d / "b.html").write_text("")
    (d / "c.txt").write_text("")
    sub = d / "nested"
    sub.mkdir()
    (sub / "d.html").write_text("")
    results = _find_files_by_suffix(d, ".html")
    paths = {r.relative_to(d).as_posix() for r in results}
    assert paths == {"a.html", "b.html", "nested/d.html"}


def test_find_files_by_suffix_skips_dirs() -> None:
    d = _tdir()
    (d / "a.html").write_text("")
    skip = d / "_repos"
    skip.mkdir()
    (skip / "b.html").write_text("")
    results = _find_files_by_suffix(d, ".html")
    paths = {r.relative_to(d).as_posix() for r in results}
    assert paths == {"a.html"}


def test_find_single_by_suffix_newest() -> None:
    d = _tdir()
    a = d / "first_comparison.html"
    a.write_text("")
    import time

    time.sleep(0.1)
    b = d / "second_comparison.html"
    b.write_text("")
    found = _find_single_by_suffix(d, "_comparison.html")
    assert found is not None
    assert found.name == "second_comparison.html"


def test_find_single_by_suffix_none() -> None:
    d = _tdir()
    assert _find_single_by_suffix(d, ".nope") is None


# ---- run() with monkeypatched steps ----

def test_run_comparison_only(monkeypatch) -> None:
    output_dir = _tdir()
    def fake_main(argv):
        (output_dir / "myrepo_comparison.html").write_text("<html></html>")
        (output_dir / "myrepo_comparison.digest.json").write_text("{}")
        return 0

    monkeypatch.setattr("oskernel_agent.comparison.pipeline.__main__.main", fake_main)
    result = run("https://example.com/repo.git", "repo_x", output_dir, ["comparison"])
    assert result.kinds["comparison"].status == "ok"
    assert result.kinds["comparison"].html_path == "comparison.html"
    assert (output_dir / "comparison.html").exists()


def test_run_description_only(monkeypatch) -> None:
    output_dir = _tdir()
    repo = output_dir / "myrepo"
    repo.mkdir()
    (repo / "main.c").write_text("int main() { return 0; }")

    def fake_run_tree(repo_path, repo_name, output_file, cli_depth, **kwargs):
        Path(output_file).write_text("<html></html>")
        (Path(output_file).with_suffix(".digest.json")).write_text("{}")
        return Path(output_file)

    monkeypatch.setattr("oskernel_agent.cli.agent._run_tree_mode", fake_run_tree)
    result = run(str(repo), "repo_x", output_dir, ["description"])
    assert result.kinds["description"].status == "ok"
    assert result.kinds["description"].html_path == "description.html"
    assert (output_dir / "description.html").exists()


def test_run_checkpoint_resume(monkeypatch) -> None:
    output_dir = _tdir()
    call_count = [0]

    def fake_main(argv):
        call_count[0] += 1
        (output_dir / "myrepo_comparison.html").write_text("<html></html>")
        (output_dir / "myrepo_comparison.digest.json").write_text("{}")
        return 0

    monkeypatch.setattr("oskernel_agent.comparison.pipeline.__main__.main", fake_main)
    result1 = run("https://example.com/repo.git", "repo_x", output_dir, ["comparison"])
    assert result1.kinds["comparison"].status == "ok"
    assert call_count[0] == 1

    result2 = run("https://example.com/repo.git", "repo_x", output_dir, ["comparison"])
    assert result2.kinds["comparison"].status == "skipped"
    assert call_count[0] == 1  # not called again


def test_run_comparison_fails_no_html(monkeypatch) -> None:
    output_dir = _tdir()
    def fake_main(argv):
        return 0

    monkeypatch.setattr("oskernel_agent.comparison.pipeline.__main__.main", fake_main)
    result = run("https://example.com/repo.git", "repo_x", output_dir, ["comparison"])
    assert result.kinds["comparison"].status == "failed"
    assert result.kinds["comparison"].error is not None


def test_run_missing_upstream_digest_blocks_summary(monkeypatch) -> None:
    output_dir = _tdir()
    def fake_main(argv):
        (output_dir / "myrepo_comparison.html").write_text("<html></html>")
        (output_dir / "myrepo_comparison.digest.json").write_text("{}")
        return 0

    monkeypatch.setattr("oskernel_agent.comparison.pipeline.__main__.main", fake_main)
    monkeypatch.setattr("oskernel_agent.report_jobs._runner._ensure_clone",
                        lambda repo, out: output_dir)
    # summary requires comparison + description + development
    # comparison succeeds, but description has a fake clone with no real repo
    result = run("https://example.com/repo.git", "repo_x", output_dir, ["summary"])
    assert result.kinds["comparison"].status == "ok"
    # description will fail (fake clone has no source files), summary never reached
    assert result.kinds.get("summary") is None or result.kinds["summary"].status != "ok"


def test_run_all_four_kinds(monkeypatch) -> None:
    output_dir = _tdir()
    repo = output_dir / "myrepo"
    repo.mkdir()
    (repo / "main.c").write_text("int x;")

    def fake_main(argv):
        (output_dir / "myrepo_comparison.html").write_text("<html></html>")
        (output_dir / "myrepo_comparison.digest.json").write_text("{}")
        return 0

    def fake_run_tree(repo_path, repo_name, output_file, cli_depth, **kwargs):
        Path(output_file).write_text("<html></html>")
        (Path(output_file).with_suffix(".digest.json")).write_text("{}")
        return Path(output_file)

    def fake_dev_report(repo_path, output_path, *, repo_id, min_commits):
        Path(output_path).write_text("<html></html>")
        dp = Path(output_path).with_suffix(".digest.json")
        dp.write_text("{}")
        return {
            "html_path": str(output_path), "digest_path": str(dp),
            "ai_path": "", "evidence_path": "", "commit_count": 5, "stage_count": 2,
        }

    def fake_summary(output_dir, repo_id):
        pdf = output_dir / "summary.pdf"
        pdf.write_text("%PDF-1.4 fake")
        from oskernel_agent.report_jobs._runner import KindResult
        kr = KindResult(kind="summary", status="ok", html_path="summary.pdf",
                        digest_path=None, started_at="", finished_at="")
        kr.started_at = ""
        kr.finished_at = ""
        return kr

    monkeypatch.setattr("oskernel_agent.comparison.pipeline.__main__.main", fake_main)
    monkeypatch.setattr("oskernel_agent.cli.agent._run_tree_mode", fake_run_tree)
    monkeypatch.setattr("oskernel_agent.finals.development.generate_development_report", fake_dev_report)
    monkeypatch.setattr("oskernel_agent.report_jobs._runner._run_summary", fake_summary)

    result = run(str(repo), "repo_x", output_dir,
                 ["comparison", "description", "development", "summary"])
    for kind in ("comparison", "description", "development", "summary"):
        assert result.kinds[kind].status == "ok", \
            f"{kind} should be ok, got {result.kinds[kind].status}: {result.kinds[kind].error}"

    assert (output_dir / "comparison.html").exists()
    assert (output_dir / "description.html").exists()
    assert (output_dir / "development.html").exists()
    assert (output_dir / "summary.pdf").exists()
    assert (output_dir / "comparison.digest.json").exists()
    assert (output_dir / "description.digest.json").exists()


# ---- CLI ----

def test_cli_help() -> None:
    import subprocess, sys, os

    result = subprocess.run(
        [sys.executable, "-m", "oskernel_agent.report_jobs", "--help"],
        capture_output=True, encoding="utf-8", timeout=30,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert result.returncode == 0
    assert "--repo" in (result.stdout or "")
    assert "--kinds" in (result.stdout or "")


# ---- final-files whitelist ----

def test_final_files_includes_all_deliverables_and_digests() -> None:
    from oskernel_agent.report_jobs._runner import _FINAL_FILES

    assert "summary.pdf" in _FINAL_FILES
    assert "description.html" in _FINAL_FILES
    assert "description.digest.json" in _FINAL_FILES
    assert "development.html" in _FINAL_FILES
    assert "development.digest.json" in _FINAL_FILES
    assert "comparison.html" in _FINAL_FILES
    assert "comparison.digest.json" in _FINAL_FILES
    assert ".report_jobs_state.json" in _FINAL_FILES


def test_cleanup_non_final_preserves_whitelist() -> None:
    from oskernel_agent.report_jobs._runner import _cleanup_non_final, _FINAL_FILES

    d = _tdir()
    try:
        for name in _FINAL_FILES:
            (d / name).write_text("")
        (d / "junk.html").write_text("")
        subdir = d / "_repos"
        subdir.mkdir()
        (subdir / "clone").write_text("")

        _cleanup_non_final(d)

        for name in _FINAL_FILES:
            assert (d / name).exists(), f"{name} should survive cleanup"
        assert not (d / "junk.html").exists()
        assert not subdir.exists()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---- remove unused imports from pytest ----
pytest  # noqa: B018 (silence "imported but unused" — pytest is needed for monkeypatch)
