from src.oskernel_agent.pipeline import lang_guard
from src.oskernel_agent.reports.html_tree import render_tree_html
import pytest

def test_untranslated_english_is_marked_incomplete(monkeypatch):
    monkeypatch.setattr(lang_guard, "_get_client", lambda: None)
    tree = {"summary": "This module provides task scheduling and context switching primitives."}
    stats = lang_guard.normalize_tree_language(tree)
    assert stats["remaining"] == 1 and stats["complete"] is False

def test_description_html_refuses_content_that_would_need_manual_editing():
    tree = {"meta": {"repo":"r", "ts":"t", "indexed_files":1, "language_incomplete":True},
            "verdict": {}, "tree": {"type":"root", "name":"r", "children":[]}}
    with pytest.raises(RuntimeError, match="拒绝生成"):
        render_tree_html(tree)


def test_mixed_html_cannot_hide_one_english_section():
    html = (
        "<h3>进程调度</h3><p>本节分析任务创建、切换与退出流程。</p>"
        "<h3>Design Differences</h3>"
        "<p>The implementation uses a different queue for task scheduling.</p>"
    )
    assert lang_guard.needs_translation(html) is True


def test_chinese_sentence_may_keep_technical_terms_in_english():
    html = "<p>该模块通过 page table、buddy system 和 scheduler 完成内存管理与任务调度。</p>"
    assert lang_guard.needs_translation(html) is False


def test_bare_paths_and_identifier_lists_are_not_english_prose():
    html = (
        "<table><tr><td>os/src/fs/vfs.rs, fs_info.rs, mount.rs</td></tr></table>"
        "<p>支持 epoll 系列(epoll_create1/ctl/pwait)、poll(ppoll)、select(pselect6)。</p>"
    )
    assert lang_guard.needs_translation(html) is False


def test_english_generated_module_title_is_not_cache_eligible():
    payload = {
        "summary": "本子系统负责进程生命周期管理。",
        "modules": [{"name": "Task Scheduling", "summary": "实现任务调度。"}],
    }
    assert lang_guard.language_output_complete(payload) is False


def test_chinese_html_skips_translation_client(monkeypatch):
    def fail_if_called():
        raise AssertionError("中文首轮结果不应调用翻译模型")

    monkeypatch.setattr(lang_guard, "_get_client", fail_if_called)
    text, stats = lang_guard.normalize_html_language("<p>本节完整说明了调度流程。</p>")
    assert text == "<p>本节完整说明了调度流程。</p>"
    assert stats["translated"] == 0 and stats["complete"] is True
