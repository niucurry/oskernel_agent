"""Deterministic report-stage witnesses, not a code-semantics benchmark.

Run with PYTHONPATH=src:. python3 research/description_integrity/replay.py
Use --baseline to replay the frozen pre-repair implementation. No model calls.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load_baseline():
    for module, name in [
        ("oskernel_agent.finals.digests", "digests.py"),
        ("oskernel_agent.reports.html_tree", "html_tree.py"),
        ("oskernel_agent.pipeline.tree_builder", "tree_builder.py"),
    ]:
        spec = importlib.util.spec_from_file_location(module, HERE / "baseline" / name)
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[module] = loaded
        spec.loader.exec_module(loaded)


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.baseline:
        load_baseline()
    from oskernel_agent.finals.digests import normalize_description_claim
    from oskernel_agent.reports.html_tree import (
        _clean_capability_claim, _remove_assigned_issue_sentences, _render_all_subsystems,
    )
    from oskernel_agent.pipeline.tree_builder import _files_for_subsys_prompt

    def tree(summary, highlights=None, issues=None):
        return {"facts": {}, "verdict": {}, "tree": {"children": [{
            "type": "subsystem", "name": "内存", "summary": summary,
            "highlights": highlights or [], "issues": issues or [],
            "file_paths": ["kernel/mm.c"], "children": [],
        }]}}

    def render(summary, highlights=None, issues=None):
        return _render_all_subsystems(tree(summary, highlights, issues), lambda p, line: f"#{p}:{line}")

    witnesses = []

    def record(name, source, actual, holds):
        witnesses.append({"id": name, "input": source, "output": actual, "contract_holds": holds})

    source = "构建了完整的 TCP/IP 网络协议栈。"
    actual = _clean_capability_claim(source, {"facts": {}})
    record("no_invented_dependency", source, actual, "smoltcp" not in actual)
    source = "fork 失败时回收页框；mmap 失败时保留旧映射。"
    actual = _remove_assigned_issue_sentences(source, [{"quote": "fork 失败时未回收页框"}])
    record("no_keyword_based_deletion", source, actual, "mmap 失败时保留旧映射" in actual)
    source = "覆盖 120 个系统调用。未知编号由 dispatch 返回 ENOSYS；失败时保留旧映射。"
    actual = normalize_description_claim(source, "", {"syscall": {"standard_count": 48, "standard_total": 60}})
    record("no_invented_fallback_or_lost_independent_sentence", source, actual,
           "sys_nisyscall" not in actual and "未知编号由 dispatch 返回 ENOSYS" in actual
           and "失败时保留旧映射" in actual)
    source = "模块维护映射与页框引用计数，" * 25 + "但仅在单核配置下启用回收。"
    actual = render(source)
    record("long_statement_available_intact", source, actual, html.escape(source.rstrip("。")) in actual)
    source = "当 fd<0、fd>=NOFILE 或 ofile[fd]==0 时返回 -1。"
    actual = render(source)
    record("plain_comparisons_not_treated_as_html", source, actual, html.escape(source.rstrip("。")) in actual)
    source = "映射成功后更新引用计数；撤销时同步回收空闲页框。"
    actual = render("管理页表。", [{"quote": source, "path": "kernel/mm.c:42"}])
    # A complete highlight and its own citation must occur within one list item.
    paired = any(html.escape(source.rstrip("。")) in li and "kernel/mm.c:42" in li
                 for li in re.findall(r"<li\b[^>]*>(.*?)</li>", actual, re.S))
    record("semicolon_highlight_keeps_own_citation", source, actual, paired)
    source = "资源回收路径需要人工复核，" * 16 + "此问题只影响调试配置。"
    actual = render("管理页表。", issues=[{"quote": source, "path": "kernel/mm.c:50", "severity": "low"}])
    record("long_issue_keeps_qualification", source, actual, html.escape(source.rstrip("。")) in actual)
    files = [{"path": f"{directory}/f{i:03d}.c", "name": f"f{i:03d}.c", "lang": "c"}
             for directory, size in [("a", 100), ("b", 2), ("c", 2)] for i in range(size)]
    os.environ["AGENT_SUBSYS_PROMPT_FILE_LIMIT"] = "20"
    picked = _files_for_subsys_prompt(files)
    groups = sorted({f["path"].split("/")[0] for f in picked})
    record("prompt_samples_all_three_directories", {"a": 100, "b": 2, "c": 2}, groups, groups == ["a", "b", "c"])

    result = {
        "kind": "synthetic_regression_witnesses_not_natural_error_rates",
        "implementation": "baseline" if args.baseline else "working_tree",
        "passed": sum(item["contract_holds"] for item in witnesses),
        "total": len(witnesses), "witnesses": witnesses,
        "module_sha256": {str(Path(sys.modules[name].__file__).relative_to(ROOT)):
                          hashlib.sha256(Path(sys.modules[name].__file__).read_bytes()).hexdigest()
                          for name in ["oskernel_agent.finals.digests", "oskernel_agent.reports.html_tree",
                                       "oskernel_agent.pipeline.tree_builder"]},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": result["passed"], "total": result["total"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
