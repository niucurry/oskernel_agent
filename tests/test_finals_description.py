from __future__ import annotations

import re

import pytest

from finals.digests import description_digest_from_tree
from finals.integrity import analyze_log, scan_hardcode_signals
from oskernel_agent.pipeline.tree_builder import _validate_hardcode_reviews
from oskernel_agent.reports.html_tree import render_tree_html


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
                        "quote": "页表回收路径不完整。"}],
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
