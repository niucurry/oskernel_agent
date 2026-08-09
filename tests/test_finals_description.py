from __future__ import annotations

import re

import pytest

from finals.digests import description_digest_from_tree
from finals.integrity import analyze_log, scan_hardcode_signals
from oskernel_agent.parsers.code_parser import classify_files_by_content
from oskernel_agent.pipeline.tree_builder import (
    _validate_hardcode_reviews,
    _validate_verdict_integrity_conclusion,
)
from oskernel_agent.report_quality import IncompleteReportError
from oskernel_agent.reports.html_tree import render_tree_html, write_tree_html


def _tree() -> dict:
    return {
        "meta": {"repo": "demo", "ts": "20260808", "indexed_files": 2},
        "facts": {
            "integrity": {
                "build_log": {"status": "failed", "path": "build.log",
                              "errors": ["error: missing symbol"]},
                "run_log": {"status": "not_provided"},
                "hardcode": {"findings": [{
                    "signal_id": "src/main.c:7:按测试名或 ELF 名称分支",
                    "category": "按测试名或 ELF 名称分支",
                    "path": "src/main.c", "line": 7, "excerpt": "if (strstr(name, test))",
                    "confidence": .72, "analysis": "需核对是否按测试名称选择输出。",
                }]},
            },
        },
        "verdict": {
            "score_total": 60,
            "dimensions": [],
            "highlights": [],
            "issues": [{"path": "src/mm.c:9", "severity": "medium",
                        "quote": "页表回收路径不完整。", "confidence": 78}],
            "hardcode_reviews": [{
                "signal_id": "src/main.c:7:按测试名或 ELF 名称分支",
                "category": "按测试名或 ELF 名称分支",
                "path": "src/main.c", "line": 7,
                "status": "confirmed",
                "method": "根据被加载的测试 ELF 名称选择固定返回路径",
                "reason": "该分支绕过正常功能路径，会对指定测试产生确定性结果。",
                "confidence": 91,
                "excerpt": "if (strstr(name, test))",
            }],
            "one_line": "编译失败，并存在测试名分支。",
            "content": "<p>这里是详细分析。</p>",
        },
        "tree": {
            "type": "root", "path": "", "name": "demo", "children": [{
                "type": "subsystem", "name": "内存管理", "path": "<subsys>/内存管理",
                "summary": "负责地址空间。", "content": "<p>子系统详细证据。</p>",
                "children": [{
                    "type": "module", "name": "页表", "path": "<subsys>/内存管理/m001",
                    "summary": "VFS 与本模块无关，页表负责映射。",
                    "content": "<p>模块详细证据。</p>",
                }],
            }],
        },
    }


def test_log_failure_is_extracted(tmp_path):
    path = tmp_path / "build.log"
    path.write_text("compile\nerror: missing symbol\n", encoding="utf-8")
    result = analyze_log(path, kind="build")
    assert result["status"] == "failed"
    assert result["errors"] == ["error: missing symbol"]


def test_log_uses_last_explicit_outcome_instead_of_any_old_error(tmp_path):
    path = tmp_path / "build.log"
    path.write_text(
        "error: first build failed\nfixing\nbuild succeeded\n",
        encoding="utf-8",
    )
    result = analyze_log(path, kind="build")
    assert result["status"] == "passed"
    assert result["errors"] == ["error: first build failed"]


def test_hardcode_signal_keeps_real_path_and_line(tmp_path):
    src = tmp_path / "main.c"
    src.write_text('int main(){\n if (strstr(name, "case.elf")) return 0;\n}\n', encoding="utf-8")
    result = scan_hardcode_signals(tmp_path)
    assert result["findings"][0]["path"] == "main.c"
    assert result["findings"][0]["line"] == 2
    assert result["findings"][0]["signal_id"].startswith("main.c:2:")
    assert result["findings"][0]["confidence"] < 1


def test_all_four_required_hardcode_methods_are_scanned(tmp_path):
    (tmp_path / "loader.c").write_text(
        'if (strstr(name, "case.elf")) return 0;\n', encoding="utf-8"
    )
    (tmp_path / "cache.c").write_text(
        'if (benchmark_case) { cache_victim = 3; }\n', encoding="utf-8"
    )
    (tmp_path / "output.c").write_text(
        'printf("expected output: 42\\n");\n', encoding="utf-8"
    )
    (tmp_path / "run.sh").write_text("make test || true\n", encoding="utf-8")
    categories = {item["category"] for item in scan_hardcode_signals(tmp_path)["findings"]}
    assert {
        "按测试名或 ELF 名称分支",
        "测试专用缓存策略",
        "疑似写死测试结果",
        "脚本强制忽略失败",
    } <= categories


def test_hardcode_limit_does_not_let_first_category_hide_other_methods(tmp_path):
    for index in range(8):
        (tmp_path / f"loader{index}.c").write_text(
            f'if (strstr(name, "case{index}.elf")) return 0;\n', encoding="utf-8"
        )
    (tmp_path / "cache.c").write_text(
        'if (benchmark_case) { cache_victim = 3; }\n', encoding="utf-8"
    )
    (tmp_path / "output.c").write_text(
        'printf("expected output: 42\\n");\n', encoding="utf-8"
    )
    (tmp_path / "run.sh").write_text("make test || true\n", encoding="utf-8")

    result = scan_hardcode_signals(tmp_path, limit=4)
    assert result["truncated"] is True
    assert {item["category"] for item in result["findings"]} == {
        "按测试名或 ELF 名称分支",
        "测试专用缓存策略",
        "疑似写死测试结果",
        "脚本强制忽略失败",
    }
    assert all(item["scanned"] for item in result["category_coverage"].values())


def test_early_success_exit_in_test_script_is_a_review_candidate(tmp_path):
    script = tmp_path / "tests" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\nmake test\n", encoding="utf-8")
    findings = scan_hardcode_signals(tmp_path)["findings"]
    assert any(
        item["category"] == "脚本强制忽略失败"
        and item["path"] == "tests/run.sh"
        and item["line"] == 2
        for item in findings
    )


def test_startup_code_is_classified_as_required_startup_module(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "boot.S").write_text(
        ".globl _start\n_start:\n  la sp, boot_stack\n  call rust_main\n",
        encoding="utf-8",
    )
    result = classify_files_by_content(str(tmp_path), ["src"])
    assert result["启动模块"][0]["file"].replace("\\", "/") == "src/boot.S"
    assert result["启动模块"][0]["is_primary"] is True


def test_description_digest_puts_runtime_and_hardcode_first():
    digest = description_digest_from_tree(_tree())
    assert digest.kind == "description"
    assert digest.findings[0].title == "编译失败"
    hardcode = next(finding for finding in digest.findings if "硬编码" in finding.title)
    assert "ELF" in hardcode.title and hardcode.severity == "high"
    assert "根据被加载的测试" in hardcode.detail
    assert len(digest.modules[0].summary) <= 300


def test_description_html_is_problem_first_and_module_text_is_bounded():
    rendered = render_tree_html(_tree())
    assert rendered.index("结论与问题") < rendered.index("模块摘要与证据")
    assert "置信度 95%" in rendered
    assert rendered.index("编译失败") < rendered.index("AI 复核硬编码问题")
    assert "根据被加载的测试" in rendered
    assert "模块详细证据" not in rendered and "子系统详细证据" not in rendered
    assert all(int(value) <= 300 for value in re.findall(r'data-analysis-chars="(\d+)"', rendered))
    assert "编译日志：失败；运行日志：未提供" in rendered
    assert "修改测试脚本绕过失败用例" in rendered
    assert "展开逐条硬编码复核（1 条）" in rendered
    assert "src/main.c:7" in rendered and "确认问题" in rendered
    assert "生成流程不包含人工编辑步骤" in rendered
    assert "虚拟文件系统（VFS）" in rendered
    assert '<section id="evaluation"' in rendered


def test_every_scanner_signal_requires_structured_ai_review():
    tree = _tree()
    facts = tree["facts"]
    with pytest.raises(RuntimeError, match="未经 AI 复核"):
        _validate_hardcode_reviews({"hardcode_reviews": []}, facts)
    _validate_hardcode_reviews(tree["verdict"], facts)


def test_ai_discovered_hardcode_requires_a_real_in_range_source_line(tmp_path):
    source = tmp_path / "src" / "main.c"
    source.parent.mkdir()
    source.write_text('printf("expected output: 42\\n");\n', encoding="utf-8")
    parsed = {"hardcode_reviews": [{
        "signal_id": "ai-new-1",
        "category": "疑似写死测试结果",
        "path": "src/main.c", "line": 1,
        "status": "confirmed",
        "method": "直接打印测试预期结果",
        "reason": "输出绕过正常功能路径。",
        "confidence": .95,
        "excerpt": "模型改写过但不可信的摘录",
    }]}
    facts = {"integrity": {"hardcode": {"findings": [], "truncated": False}}}
    _validate_hardcode_reviews(parsed, facts, tmp_path)
    assert parsed["hardcode_reviews"][0]["confidence"] == 95
    assert parsed["hardcode_reviews"][0]["excerpt"] == 'printf("expected output: 42\\n");'
    parsed["hardcode_reviews"][0]["line"] = 9
    with pytest.raises(RuntimeError, match="超出文件范围"):
        _validate_hardcode_reviews(parsed, facts, tmp_path)


def test_one_line_conclusion_must_match_each_integrity_status():
    facts = {"integrity": {
        "build_log": {"status": "failed"},
        "run_log": {"status": "passed"},
    }}
    parsed = {
        "one_line": "编译失败，运行通过；未发现硬编码，页表回收不完整。",
        "hardcode_reviews": [],
    }
    _validate_verdict_integrity_conclusion(parsed, facts)
    parsed["one_line"] = "编译通过，运行失败；未发现硬编码，页表回收不完整。"
    with pytest.raises(RuntimeError, match="事实不一致"):
        _validate_verdict_integrity_conclusion(parsed, facts)


def test_description_delivery_rejects_broken_source_links(tmp_path):
    tree = _tree()
    tree["facts"]["integrity"]["build_log"] = {"status": "not_provided"}
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.c").write_text("\n" * 8, encoding="utf-8")
    # src/mm.c 故意不存在：问题证据不得被渲染成看似可点击的伪链接。
    with pytest.raises(IncompleteReportError, match="无法回溯到源码"):
        write_tree_html(tmp_path / "description.html", tree, repo_roots=[repo])
