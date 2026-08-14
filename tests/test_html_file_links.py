from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from oskernel_agent.reports.html import (
    _BROKEN_PREFIX,
    derive_repo_web_base,
    linkify_html,
    make_file_link_resolver,
    repository_url_to_web_base,
)


def test_windows_reserved_path_map_uses_original_gitlab_path(tmp_path):
    safe = "os/src/task/__win_reserved_aux.rs"
    original = "os/src/task/aux.rs"
    source = tmp_path / safe
    source.parent.mkdir(parents=True)
    source.write_text("\n" * 6 + "pub fn init() {}\n", encoding="utf-8")
    (tmp_path / ".codex_windows_path_map.json").write_text(
        json.dumps({safe: original}), encoding="utf-8"
    )
    resolver = make_file_link_resolver(
        [tmp_path],
        repo_web_bases=["https://gitlab.example.com/group/repo/-/blob/deadbeef"],
    )
    assert resolver is not None

    expected = "https://gitlab.example.com/group/repo/-/blob/deadbeef/os/src/task/aux.rs#L7"
    assert resolver(safe, "7") == expected
    assert resolver(original, "7") == expected

def test_file_extension_match_requires_a_token_boundary(tmp_path):
    broken = set()
    resolver = make_file_link_resolver([tmp_path], broken_paths=broken)
    assert linkify_html(".bss 段与 cifs.spnego 名称", resolver) == ".bss 段与 cifs.spnego 名称"


def test_bare_filename_without_line_is_plain_prose(tmp_path):
    broken = set()
    resolver = make_file_link_resolver([tmp_path], broken_paths=broken)
    rendered = linkify_html("与 hart.rs 中定义重复", resolver)
    assert rendered == "与 hart.rs 中定义重复"
    assert not broken


def test_line_number_outside_source_file_is_marked_broken(tmp_path):
    source = tmp_path / "src" / "main.rs"
    source.parent.mkdir()
    source.write_text("fn main() {}\n", encoding="utf-8")
    broken = set()
    resolver = make_file_link_resolver([tmp_path], broken_paths=broken)
    assert resolver is not None
    assert resolver("src/main.rs", "2").startswith(_BROKEN_PREFIX)
    assert "src/main.rs:2" in broken


def test_suffix_match_recovers_written_middle_segment(tmp_path):
    """AI 多写中间段（os/src/vdso/... → 实际 os/vdso/...）必须段对齐恢复。
    此前后缀兜底只覆盖「少写前缀」方向，T202610006999602-3220 因
    `os/src/vdso/loongarch64.S`（文件实为 os/vdso/loongarch64.S）渲染断链。"""
    source = tmp_path / "os" / "vdso" / "loongarch64.S"
    source.parent.mkdir(parents=True)
    source.write_text("  .globl __vdso_getcpu\n", encoding="utf-8")
    broken = set()
    resolver = make_file_link_resolver([tmp_path], broken_paths=broken)
    assert resolver is not None
    url = resolver("os/src/vdso/loongarch64.S", "1")
    assert not url.startswith(_BROKEN_PREFIX)
    # 文件 URL 对分隔符做百分号编码（Windows 为 %5C），断言先解码、与分隔符无关
    from urllib.parse import unquote
    assert "os" + os.sep + "vdso" + os.sep + "loongarch64.S" in unquote(url)
    assert not broken


def test_suffix_match_recovers_missing_prefix(tmp_path):
    """少写前缀方向（axhal/src/cpu.rs → 实际 arceos/.../axhal/src/cpu.rs）保持可用。"""
    source = tmp_path / "arceos" / "modules" / "axhal" / "src" / "cpu.rs"
    source.parent.mkdir(parents=True)
    source.write_text("fn cpu_id() {}\n", encoding="utf-8")
    broken = set()
    resolver = make_file_link_resolver([tmp_path], broken_paths=broken)
    assert resolver is not None
    url = resolver("axhal/src/cpu.rs", "1")
    assert not url.startswith(_BROKEN_PREFIX)
    assert not broken


def test_suffix_match_stays_broken_on_ambiguous_duplicates(tmp_path):
    """同名 basename 多处命中时保持不解析（不猜测，避免链错文件）。"""
    for sub in ("a", "b"):
        src = tmp_path / sub / "src" / "vdso" / "loongarch64.S"
        src.parent.mkdir(parents=True)
        src.write_text("  .globl __vdso_getcpu\n", encoding="utf-8")
    broken = set()
    resolver = make_file_link_resolver([tmp_path], broken_paths=broken)
    assert resolver is not None
    url = resolver("src/vdso/loongarch64.S", "1")
    assert url.startswith(_BROKEN_PREFIX)
    assert "src/vdso/loongarch64.S" in broken


def test_non_git_archive_does_not_inherit_parent_repository_remote(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git unavailable")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "https://gitlab.example.com/report/generator.git"],
        check=True,
    )
    archive = tmp_path / "downloaded-archive"
    archive.mkdir()
    assert derive_repo_web_base(archive) is None


def test_explicit_target_repository_url_builds_the_evidence_base():
    base = repository_url_to_web_base(
        "https://gitlab.example.com/team/entry.git", "a" * 40,
    )
    assert base == (
        "https://gitlab.example.com/team/entry/-/blob/" + "a" * 40
    )
