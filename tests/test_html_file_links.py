from __future__ import annotations

import json

from oskernel_agent.reports.html import make_file_link_resolver


def test_windows_reserved_path_map_uses_original_gitlab_path(tmp_path):
    safe = "os/src/task/__win_reserved_aux.rs"
    original = "os/src/task/aux.rs"
    source = tmp_path / safe
    source.parent.mkdir(parents=True)
    source.write_text("pub fn init() {}", encoding="utf-8")
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
