"""Report transformations must not invent facts or sever statements from evidence."""
import html
import re

from oskernel_agent.finals.digests import description_digest_from_tree, normalize_description_claim
from oskernel_agent.finals.readability import concise_module_summary
from oskernel_agent.pipeline.tree_builder import _files_for_subsys_prompt
from oskernel_agent.reports.html_tree import (
    _clean_capability_claim,
    _render_all_subsystems,
    _section_statements,
)


def node(summary, **kwargs):
    return {"name": "内存管理", "type": "subsystem", "summary": summary,
            "children": [], **kwargs}


def render(item):
    tree = {"facts": {}, "verdict": {}, "tree": {"children": [item]}}
    return _render_all_subsystems(tree, lambda path, line: f"#{path}:{line}")


def test_capability_cleanup_does_not_invent_a_library():
    text = _clean_capability_claim("基于 lwIP 构建完整的 TCP/IP 网络协议栈。", {})
    assert "lwIP" in text and "smoltcp" not in text


def test_shared_risk_word_does_not_delete_independent_behavior():
    text = "mmap 失败时保留旧映射。"
    issue = {"quote": "fork 失败时泄漏页框", "path": "kernel/mm.c:42", "severity": "low"}
    output = render(node(text, file_paths=["kernel/mm.c"], issues=[issue]))
    assert html.escape(text.rstrip("。")) in output
    output = render(node(text, file_paths=["kernel/mm.c"], issues=[dict(issue, quote=text)]))
    assert output.count(html.escape(text.rstrip("。"))) == 1


def test_syscall_correction_keeps_other_sentences_and_does_not_invent_handler():
    text = "覆盖 120 个系统调用。未知编号返回 ENOSYS；失败时保留旧映射。"
    output = normalize_description_claim(text, "", {"syscall": {"standard_count": 48, "standard_total": 60}})
    assert "120" not in output and "48/60" in output
    assert "未知编号返回 ENOSYS；失败时保留旧映射" in output
    assert "sys_nisyscall" not in output


def test_full_summary_is_used_instead_of_preclipped_brief():
    text = "读取可等待；写端关闭且缓冲区为空时返回零。"
    output = render(node(text, brief="读取可等待。"))
    assert html.escape(text.rstrip("。")) in output


def test_plain_text_comparisons_are_escaped_instead_of_stripped_as_html():
    text = "当 fd<0、fd>=NOFILE 或 ofile[fd]==0 时返回 -1。"
    output = render(node(text))
    assert html.escape(text.rstrip("。")) in output


def test_overflow_keeps_entire_statement_and_respects_custom_budget():
    text = "维护页框引用并检查映射状态，" * 26 + "但仅适用于单核配置。"
    item = node(text)
    visible, deferred = _section_statements(item, None, {}, [], set(), limit=20)
    assert sum(len(entry["text"]) for entry in visible) <= 20
    assert deferred[0]["text"] == text.rstrip("。")
    output = render(item)
    assert 'data-description-overflow="true"' in output
    assert html.escape(text.rstrip("。")) in output


def test_highlight_with_semicolon_keeps_complete_text_and_its_own_citation():
    text = "映射后递增引用计数；仅在最后一个引用释放时回收。"
    output = render(node("维护映射。", highlights=[{"quote": text, "path": "kernel/mm.c:42"}]))
    rows = re.findall(r"<li\b[^>]*>(.*?)</li>", output, re.S)
    assert any(text.rstrip("。") in row and "kernel/mm.c:42" in row for row in rows)


def test_long_issue_is_not_clipped_and_has_a_separate_character_count():
    text = "释放路径需要检查所有映射，" * 26 + "此问题仅影响调试配置。"
    output = render(node("维护映射。", issues=[{"quote": text, "path": "kernel/mm.c:42", "severity": "low"}]))
    assert text.rstrip("。") in output
    assert int(re.search(r'data-issue-chars="(\d+)"', output)[1]) == len(text.rstrip("。"))
    assert int(re.search(r'data-analysis-chars="(\d+)"', output)[1]) <= 300


def test_prompt_selection_is_deterministic_and_covers_directories(monkeypatch):
    monkeypatch.setenv("AGENT_SUBSYS_PROMPT_FILE_LIMIT", "20")
    files = [{"path": f"{directory}/f{i}.c", "name": f"f{i}.c", "lang": "c"}
             for directory, count in [("kernel/mm", 100), ("kernel/net", 2), ("kernel/fs", 2)]
             for i in range(count)]
    selected = _files_for_subsys_prompt(files)
    assert len(selected) == 20
    assert selected == _files_for_subsys_prompt(list(reversed(files)))
    assert {item["path"].rsplit("/", 1)[0] for item in selected} == {"kernel/mm", "kernel/net", "kernel/fs"}


def test_plain_summary_keeps_comparisons_and_generic_type_parameters():
    text = "当 fd<0、fd>=NOFILE 时返回 -1，结果写入 Vec<T>。"
    assert concise_module_summary(text, input_is_html=False) == text.rstrip("。")


def test_html_summary_is_decoded_once_without_stripping_comparisons_again():
    text = "<p>当 fd&lt;0、fd&gt;=NOFILE 时返回 -1。</p>"
    assert concise_module_summary(text) == "当 fd<0、fd>=NOFILE 时返回 -1"


def test_shared_digest_prefers_full_plain_summary_and_preserves_issue_conditions():
    text = "当 fd<0、fd>=NOFILE 或 ofile[fd]==0 时返回 -1。"
    tree = {
        "tree": {"children": [node(text, brief="检查文件描述符。")]},
        "verdict": {"issues": [{"path": "kernel/fd.c:12", "quote": text, "severity": "medium"}]},
        "facts": {},
    }
    digest = description_digest_from_tree(tree)
    assert digest.modules[0].summary == text.rstrip("。")
    assert digest.findings[0].detail == text.rstrip("。")


def test_shared_digest_html_content_fallback_preserves_encoded_comparisons():
    item = node("", content="<p>仅在 n&lt;3 且 capacity&gt;0 时允许写入。</p>")
    digest = description_digest_from_tree({"tree": {"children": [item]}})
    assert digest.modules[0].summary == "仅在 n<3 且 capacity>0 时允许写入"


def test_tree_heading_keeps_plain_comparisons():
    from oskernel_agent.reports.html_tree import _render_tree_node_static

    output = _render_tree_node_static(node("当 fd<0、fd>=NOFILE 时拒绝操作。"), 1, lambda _: None)
    assert "fd&lt;0、fd&gt;=NOFILE" in output


def test_development_plain_commit_text_keeps_comparisons():
    from oskernel_agent.finals.development import _short_text

    assert _short_text("修复 fd<0、fd>=NOFILE 时的检查。", 160) == "修复 fd<0、fd>=NOFILE 时的检查"
