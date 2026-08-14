"""oskernel_agent.works_list 的作品仓库列表 xlsx 读取测试。"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import openpyxl
import pytest

from oskernel_agent.works_list import clone_target, read_works_xlsx, team_id_from_url

URL_A = "https://gitlab.eduxiji.net/educg-group-48535-3229427/T2026100069910651-2494"
URL_B = "https://gitlab.eduxiji.net/educg-group-48535-3229427/T202610006999602-3220"


def _write_xlsx(tmp_path: Path, rows: list[list[str]]) -> Path:
    path = tmp_path / "works.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def test_team_id_from_url():
    assert team_id_from_url(URL_A) == "T2026100069910651-2494"
    assert team_id_from_url(URL_A + ".git") == "T2026100069910651-2494"
    assert team_id_from_url(URL_A.rstrip("/") + "/") == "T2026100069910651-2494"


def test_team_id_from_url_rejects_missing_slug():
    with pytest.raises(ValueError):
        team_id_from_url("https://gitlab.eduxiji.net")


def test_read_works_xlsx_single_fork_column(tmp_path):
    path = _write_xlsx(tmp_path, [
        ["fork地址"],
        [URL_A],
        [URL_B],
        [""],
        [URL_A],  # 重复项应去重
        ["不是 URL 的行"],
    ])
    entries = read_works_xlsx(path)
    assert [e.team_id for e in entries] == ["T2026100069910651-2494", "T202610006999602-3220"]
    assert all(not e.url.endswith(".git") for e in entries)
    assert [e.row for e in entries] == [2, 3]


def test_read_works_xlsx_repo_url_column(tmp_path):
    path = _write_xlsx(tmp_path, [
        ["队伍名称", "仓库地址"],
        ["A队", URL_A],
        ["B队", URL_B + ".git"],
    ])
    entries = read_works_xlsx(path)
    assert [e.team_id for e in entries] == ["T2026100069910651-2494", "T202610006999602-3220"]


def test_read_works_xlsx_without_header_uses_first_column(tmp_path):
    path = _write_xlsx(tmp_path, [
        [URL_A],
        ["ignored"],
    ])
    entries = read_works_xlsx(path)
    assert [e.team_id for e in entries] == ["T2026100069910651-2494"]


def test_read_works_xlsx_missing_file():
    with pytest.raises(FileNotFoundError):
        read_works_xlsx("no-such-works.xlsx")


def test_read_works_xlsx_empty_sheet(tmp_path):
    path = _write_xlsx(tmp_path, [])
    assert read_works_xlsx(path) == []


def test_clone_target_github_tree_url_maps_to_repo_and_branch():
    assert clone_target(
        "https://github.com/oscomp/testsuits-for-oskernel/tree/final-2026"
    ) == ("https://github.com/oscomp/testsuits-for-oskernel", "final-2026")


def test_clone_target_github_blob_url_maps_to_repo_and_branch():
    assert clone_target(
        "https://github.com/oscomp/testsuits-for-oskernel/blob/main/README.md"
    ) == ("https://github.com/oscomp/testsuits-for-oskernel", "main")


def test_clone_target_github_repo_root_is_clonable():
    assert clone_target("https://github.com/oscomp/testsuits-for-oskernel") == (
        "https://github.com/oscomp/testsuits-for-oskernel", None)


def test_clone_target_github_browser_page_is_not_clonable():
    assert clone_target("https://github.com/oscomp/testsuits-for-oskernel/commit/abc123") is None
    assert clone_target("https://github.com/oscomp/testsuits-for-oskernel/pull/42") is None


def test_clone_target_non_github_url_unchanged():
    assert clone_target(URL_A) == (URL_A, None)


def test_works_cli_reports_missing_xlsx_without_traceback(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "run_works_xlsx.py", "--xlsx", str(tmp_path / "missing.xlsx")],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 2
    assert "作品仓库列表不存在" in result.stderr
    assert "Traceback" not in result.stderr
