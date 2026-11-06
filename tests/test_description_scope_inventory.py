from oskernel_agent.analysis.description_scope import description_scope
from oskernel_agent.reports.html_tree import _render_description_scope


def test_inventory_does_not_infer_read_coverage_or_omit_extra_sources():
    scope = description_scope(
        [{"path": "a.c"}, {"path": "b.c"}, {"path": "a.c"}],
        [{"path": "a.c"}],
        [{"file_paths": ["b.c", "b.c", "elsewhere.c"]}],
    )
    assert scope == {
        "indexed_files": 2, "prompted_files": 1, "representative_files": 1,
        "unrepresented_files": 1, "additional_representative_files": 1,
        "source_read_coverage": "unknown",
    }
    html = _render_description_scope({"tree": {"children": [
        {"name": "文件系统<", "description_scope": scope},
    ]}})
    assert "文件系统&lt;" in html
    assert "实际源码阅读覆盖尚未测量" in html
    assert "未列为代表文件不等于功能遗漏" in html


def test_legacy_reports_do_not_invent_scope_counts():
    assert _render_description_scope({"tree": {"children": [{"name": "文件系统"}]}}) == ""
    assert description_scope([{"path": "a.c"}], None, [])["prompted_files"] is None


def test_windows_inventory_matches_canonical_module_paths():
    scope = description_scope(
        [{"path": "kernel\\mm.c"}], [{"path": "kernel\\mm.c"}],
        [{"file_paths": ["kernel/mm.c"]}],
    )
    assert scope["representative_files"] == 1
    assert scope["unrepresented_files"] == 0
