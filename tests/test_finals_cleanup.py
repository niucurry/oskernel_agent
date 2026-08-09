from __future__ import annotations

from pathlib import Path
import stat

import pytest

from finals.cleanup import (
    cleanup_report_directory,
    cleanup_report_sidecars,
    purge_report_directory,
)


NAMES = ("summary.pdf", "description.html", "development.html", "comparison.html")


def test_cleanup_report_directory_keeps_only_deliverables(tmp_path):
    report_dir = tmp_path / "team"
    report_dir.mkdir()
    for name in NAMES:
        (report_dir / name).write_bytes(b"report")
    (report_dir / "description.digest.json").write_text("{}", encoding="utf-8")
    work = report_dir / "description_tree_work"
    work.mkdir()
    (work / "verdict.json").write_text("{}", encoding="utf-8")
    (work / "verdict.json").chmod(stat.S_IREAD)

    removed = cleanup_report_directory(report_dir, NAMES, output_root=tmp_path)

    assert set(removed) == {"description.digest.json", "description_tree_work"}
    assert {path.name for path in report_dir.iterdir()} == set(NAMES)


def test_cleanup_report_directory_requires_four_complete_reports(tmp_path):
    report_dir = tmp_path / "team"
    report_dir.mkdir()
    with pytest.raises(RuntimeError, match="缺少"):
        cleanup_report_directory(report_dir, NAMES, output_root=tmp_path)
    with pytest.raises(ValueError, match="四个"):
        cleanup_report_directory(report_dir, NAMES[:3], output_root=tmp_path)


def test_cleanup_report_directory_refuses_path_outside_output_root(tmp_path):
    report_dir = tmp_path / "team"
    report_dir.mkdir()
    outside_root = tmp_path / "other"
    outside_root.mkdir()
    with pytest.raises(ValueError, match="输出根目录之外"):
        cleanup_report_directory(report_dir, NAMES, output_root=outside_root)


def test_purge_report_directory_removes_incomplete_outputs(tmp_path):
    report_dir = tmp_path / "team"
    report_dir.mkdir()
    (report_dir / "description.html").write_text("partial", encoding="utf-8")
    (report_dir / "description.digest.json").write_text("{}", encoding="utf-8")
    (report_dir / "work").mkdir()

    removed = purge_report_directory(report_dir, output_root=tmp_path)

    assert set(removed) == {
        "description.html",
        "description.digest.json",
        "work",
    }
    assert list(report_dir.iterdir()) == []


def test_cleanup_report_sidecars_preserves_incomplete_deliverables(tmp_path):
    report_dir = tmp_path / "team"
    report_dir.mkdir()
    (report_dir / "description.html").write_text("report", encoding="utf-8")
    (report_dir / "description.digest.json").write_text("{}", encoding="utf-8")
    (report_dir / "description_tree_work").mkdir()

    removed = cleanup_report_sidecars(report_dir, NAMES, output_root=tmp_path)

    assert set(removed) == {"description.digest.json", "description_tree_work"}
    assert {path.name for path in report_dir.iterdir()} == {"description.html"}


def test_purge_report_directory_refuses_path_outside_output_root(tmp_path):
    report_dir = tmp_path / "team"
    report_dir.mkdir()
    outside_root = tmp_path / "other"
    outside_root.mkdir()

    with pytest.raises(ValueError, match="输出根目录之外"):
        purge_report_directory(report_dir, output_root=outside_root)
