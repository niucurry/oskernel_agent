"""目录(TOC)定位准确性的回归测试。

保证「系统生成的速读报告里，每个目录项点击后都能精确定位」这一性质不被破坏：
  - 目录只保留结论、可用性、硬编码和全部一级模块概览；
  - 子系统/子模块不再生成目录项，避免主阅读路径膨胀；
  - 产出前的硬校验能识别「断链 / 重复 id」这类定位错乱。

无第三方依赖，标准库 unittest，可直接 `python -m unittest discover tests` 运行。
"""
import re
import sys
import unittest
from pathlib import Path

# 让 `oskernel_agent` 可导入（无需安装）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oskernel_agent.reports.html import (  # noqa: E402
    TocIntegrityError,
    assert_toc_resolves,
    find_toc_locate_problems,
)
from oskernel_agent.reports.html_tree import render_tree_html  # noqa: E402
from oskernel_agent.report_quality import IncompleteReportError  # noqa: E402


def _sample_tree() -> dict:
    """root → 2 子系统，其一带 2 个模块子节点。"""
    return {
        "meta": {"repo": "demo-os", "ts": "2026-06-12", "indexed_files": 9},
        "verdict": {
            "score_total": 80, "one_line": "测试",
            "dimensions": [{"name": "设计", "score": 80, "reason": "ok"}],
            "highlights": [], "issues": [],
        },
        "tree": {
            "type": "root", "name": "demo-os", "children": [
                {"type": "subsystem", "name": "内存管理", "path": "mm",
                 "summary": "分页", "children": [
                     {"type": "module", "name": "页表", "path": "mm/page.c"},
                     {"type": "module", "name": "分配器", "path": "mm/alloc.c"},
                 ]},
                {"type": "subsystem", "name": "进程调度", "path": "sched",
                 "summary": "RR", "children": []},
            ],
        },
    }


_HREF_RE = re.compile(r'class="toc-link[^"]*"[^>]*?href="#([^"]+)"')
_ID_RE = re.compile(r'\sid="([^"]+)"')


class TocLocateAccuracy(unittest.TestCase):

    def setUp(self):
        self.html = render_tree_html(_sample_tree(), title="t", resolver=None)

    def test_every_toc_item_resolves_to_unique_anchor(self):
        """每个目录项 href 恰好命中一个 id —— 定位准确的核心性质。"""
        self.assertEqual(find_toc_locate_problems(self.html), [])

    def test_expected_anchors_present(self):
        """速读版四个决策区块都进入目录。"""
        hrefs = set(_HREF_RE.findall(self.html))
        self.assertEqual(hrefs, {"verdict", "usability", "hardcode", "modules"})

    def test_child_modules_are_not_toc_or_tree_nodes(self):
        self.assertNotIn("subsys-", self.html)
        self.assertNotIn("tree-node", self.html)
        self.assertNotIn("页表</span>", self.html)

    def test_no_duplicate_anchor_ids(self):
        ids = _ID_RE.findall(self.html)
        dups = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(dups, set(), f"锚点 id 重复会导致定位错乱: {dups}")

    def test_toc_never_empty(self):
        """真实报告至少含结论和模块入口，不会产出空目录。"""
        hrefs = set(_HREF_RE.findall(self.html))
        self.assertIn("verdict", hrefs)
        self.assertIn("modules", hrefs)


class AggregationFailureRejected(unittest.TestCase):
    """任一子系统聚合失败时必须拒绝生成报告，不能渲染占位模块。"""

    def test_failed_subsystem_rejects_rendering(self):
        tree = _sample_tree()
        tree["tree"]["children"][1]["_error"] = "llm_batch_failed"  # 进程调度失败
        with self.assertRaises(IncompleteReportError):
            render_tree_html(tree, title="t", resolver=None)


class TocIntegrityCheck(unittest.TestCase):
    """硬校验函数本身对各类「定位错乱」的识别。"""

    def test_dangling_link_detected(self):
        bad = '<a class="toc-link" href="#ghost">x</a><div id="real"></div>'
        self.assertTrue(find_toc_locate_problems(bad))
        with self.assertRaises(TocIntegrityError):
            assert_toc_resolves(bad)

    def test_duplicate_target_detected(self):
        bad = ('<a class="toc-link" href="#dup">x</a>'
               '<div id="dup"></div><section id="dup"></section>')
        self.assertTrue(find_toc_locate_problems(bad))
        with self.assertRaises(TocIntegrityError):
            assert_toc_resolves(bad)

    def test_clean_passes(self):
        ok = '<a class="toc-link" href="#a">x</a><div id="a"></div>'
        self.assertEqual(find_toc_locate_problems(ok), [])
        assert_toc_resolves(ok)  # 不抛错


if __name__ == "__main__":
    unittest.main(verbosity=2)
