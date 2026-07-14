from src.oskernel_agent.pipeline import lang_guard
from src.oskernel_agent.reports.html_tree import render_tree_html

def test_untranslated_english_is_marked_incomplete(monkeypatch):
    monkeypatch.setattr(lang_guard, "_get_client", lambda: None)
    tree = {"summary": "This module provides task scheduling and context switching primitives."}
    stats = lang_guard.normalize_tree_language(tree)
    assert stats["remaining"] == 1 and stats["complete"] is False

def test_description_html_shows_language_warning():
    tree = {"meta": {"repo":"r", "ts":"t", "indexed_files":1, "language_incomplete":True},
            "verdict": {}, "tree": {"type":"root", "name":"r", "children":[]}}
    assert "中文化未完成" in render_tree_html(tree)
