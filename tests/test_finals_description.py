from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from oskernel_agent.finals import integrity as integrity_module
from oskernel_agent.finals.digests import description_digest_from_tree, _reviewed_hardcode_findings
from oskernel_agent.analysis.repo_facts import _probe_syscall_dispatch
from oskernel_agent.finals.integrity import (
    analyze_log,
    scan_build_interface,
    scan_hardcode_signals,
    scan_reproducibility,
    verify_contest_build,
)
from oskernel_agent.parsers.code_parser import classify_files_by_content
from oskernel_agent.engines.path_c import TreeSitterEngine
from oskernel_agent.pipeline.tree_builder import (
    _deterministic_verdict_one_line,
    _fallback_subsystem_for_path,
    _verdict_one_line_requires_repair,
    _normalize_verdict_content_paths,
    _normalize_adjacent_module_paths,
    _normalize_similarity_evidence,
    _validate_subsys_result,
    _check_hardcode_capacity,
    _validate_hardcode_reviews,
    _hardcode_reviews_needing_repair,
    _normalize_hardcode_repair_items,
    _validate_verdict_integrity_conclusion,
    _normalize_verdict,
    _validate_verdict_result,
    _verdict_repair_shape_ok,
    _read_latest_matching,
)
from oskernel_agent.report_quality import IncompleteReportError
from oskernel_agent.reports.html_tree import (
    _clean_capability_claim,
    _remove_assigned_issue_sentences,
    render_tree_html,
    write_tree_html,
)
from oskernel_agent.cli.agent import _cleanup_tree_intermediates


def _tree() -> dict:
    return {
        "meta": {"repo": "demo", "ts": "20260808", "indexed_files": 2},
        "facts": {
            "integrity": {
                "build_log": {"status": "failed", "path": "build.log",
                              "errors": ["error: missing symbol"]},
                "run_log": {"status": "not_provided"},
                "build_interface": {
                    "status": "complete",
                    "summary": (
                        "根目录 Makefile 静态识别到 kernel-rv 与 kernel-la 双架构入口；"
                        "本报告未执行 make。"
                    ),
                    "missing_targets": [],
                    "evidence": [{
                        "path": "Makefile", "line": 1,
                        "excerpt": ".PHONY: kernel-rv kernel-la",
                    }],
                    "container": {
                        "status": "not_provided",
                        "summary": (
                            "未提供 Dockerfile；比赛构建接口以根目录 Makefile 为准，"
                            "因此不据此判定风险。"
                        ),
                        "evidence": [],
                    },
                },
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
            "one_line": "确认存在测试名分支硬编码；主要问题：页表回收路径不完整。",
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


def test_description_cli_cleanup_keeps_only_html(tmp_path):
    output = tmp_path / "description.html"
    output.write_text("<html></html>", encoding="utf-8")
    output.with_suffix(".tree.json").write_text("{}", encoding="utf-8")
    output.with_suffix(".digest.json").write_text("{}", encoding="utf-8")
    work = tmp_path / "description_tree_work"
    work.mkdir()
    (work / "verdict.json").write_text("{}", encoding="utf-8")

    _cleanup_tree_intermediates(str(output))

    assert {path.name for path in tmp_path.iterdir()} == {"description.html"}


def test_similarity_string_evidence_is_normalized_without_inventing_analysis():
    similarity = {
        "borrowed": [
            "src/mm.c:20 — 页表入口与参考实现具有同构控制流",
            "src/bare_path.c:9",
            '{"path":"src/fs.c:3","quote":"目录遍历接口形成可定位对应"}',
        ],
        "original": [42, "未包含源码位置的主观判断"],
    }

    _normalize_similarity_evidence(similarity)

    assert similarity["borrowed"] == [
        {"path": "src/mm.c:20", "quote": "页表入口与参考实现具有同构控制流"},
        {"path": "src/fs.c:3", "quote": "目录遍历接口形成可定位对应"},
    ]
    assert similarity["original"] == []


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


def test_multiline_busybox_exec_fallback_is_review_candidate(tmp_path):
    source = tmp_path / "process.rs"
    source.write_text(
        'let mut app_inode = open_inode(path, flags, true);\n'
        'if app_inode.is_err() {\n'
        '    let is_path_search = path.starts_with("/musl/");\n'
        '    if is_path_search {\n'
        '        app_inode = open_inode("/musl/busybox", flags, true);\n'
        '    }\n'
        '}\n',
        encoding="utf-8",
    )

    findings = scan_hardcode_signals(tmp_path)["findings"]

    assert any(
        item["category"] == "按测试名或 ELF 名称分支"
        and item["path"] == "process.rs"
        and item["line"] == 2
        for item in findings
    )


def test_busybox_success_fallback_cannot_be_cleared_without_targeted_review(tmp_path):
    source = tmp_path / "fs.rs"
    source.write_text(
        'match open_file(&path) {\n'
        'Err(e) => {\n'
        '    if path.starts_with("/musl/") {\n'
        '        if open_inode("/musl/busybox", flags, true).is_ok() { return 0; }\n'
        '    }\n'
        '    e\n'
        '}\n'
        '}\n',
        encoding="utf-8",
    )
    hardcode = scan_hardcode_signals(tmp_path)
    signal = next(
        item for item in hardcode["findings"]
        if item["category"] == "按测试名或 ELF 名称分支"
    )
    review = {
        "signal_id": signal["signal_id"],
        "category": signal["category"],
        "path": signal["path"],
        "line": signal["line"],
        "status": "cleared",
        "method": "兼容性路径",
        "reason": "用于兼容 busybox。",
        "confidence": 80,
        "excerpt": signal["excerpt"],
    }
    parsed = {"hardcode_reviews": [review]}
    facts = {"integrity": {"hardcode": hardcode}}

    assert _hardcode_reviews_needing_repair(parsed, facts, tmp_path)
    with pytest.raises(RuntimeError, match="不能直接标为 cleared"):
        _validate_hardcode_reviews(parsed, facts, tmp_path)


def test_hardcode_repair_normalizes_ai_field_drift():
    target = {
        "signal_id": "src/fs.rs:9:按测试名或 ELF 名称分支",
        "category": "按测试名或 ELF 名称分支",
        "path": "src/fs.rs",
        "line": 9,
        "excerpt": "return 0;",
        "confidence": 0.72,
    }
    candidate = {"hardcode_reviews": [{
        "signal_id": target["signal_id"],
        "status": "suspected",
        "method": "失败后返回成功",
        "impact": "调用者误认为路径存在。",
        "semantic_risk": "改变 faccessat 失败语义。",
    }]}

    item = _normalize_hardcode_repair_items(candidate, [target])[0]

    assert item["reason"] == "改变 faccessat 失败语义。；调用者误认为路径存在。"
    assert item["confidence"] == 72
    assert item["excerpt"] == "return 0;"
    assert item["path"] == "src/fs.rs" and item["line"] == 9


def test_hardcode_repair_normalizes_trigger_method_and_reason_drift():
    """模型把 method 写成 trigger_method、理由拆到多个长字段时，归一化必须兜底，
    否则整批复核因缺 method/reason 被交付校验拒绝（T202610006999602-3220 实测）。"""
    target = {
        "signal_id": "Makefile:108:按测试名或 ELF 名称分支",
        "category": "按测试名或 ELF 名称分支",
        "path": "Makefile",
        "line": 108,
        "excerpt": "ifneq ($(wildcard $(CONFIG_FILE)),)",
        "confidence": 90,
    }
    candidate = {"hardcode_reviews": [{
        "signal_id": target["signal_id"],
        "status": "confirmed",
        "trigger_method": "构建前置条件检查",
        "actual_output_or_return_value_change": "镜像缺失时 exit 1",
        "impact_on_kernel_semantics": "不影响内核执行路径",
        "semantic_risk": "",
    }]}

    item = _normalize_hardcode_repair_items(candidate, [target])[0]

    assert item["method"] == "构建前置条件检查"
    assert item["reason"] == "不影响内核执行路径；镜像缺失时 exit 1"
    assert item["path"] == "Makefile" and item["line"] == 108
    assert item["status"] == "confirmed"


def test_hardcode_repair_normalizes_explanation_drift():
    """模型只写 explanation 一个自由字段时，reason 取 explanation 兜底。"""
    target = {
        "signal_id": "src/fs.rs:9:按测试名或 ELF 名称分支",
        "category": "按测试名或 ELF 名称分支",
        "path": "src/fs.rs",
        "line": 9,
        "excerpt": "return 0;",
        "confidence": 90,
    }
    candidate = {"hardcode_reviews": [{
        "signal_id": target["signal_id"],
        "status": "suspected",
        "method": "失败后返回成功",
        "explanation": "仅当镜像缺失时触发，属构建期行为。",
    }]}

    item = _normalize_hardcode_repair_items(candidate, [target])[0]

    assert item["reason"] == "仅当镜像缺失时触发，属构建期行为。"


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

def test_default_hardcode_review_capacity_handles_large_kernel():
    assert integrity_module.DEFAULT_HARDCODE_SIGNAL_LIMIT >= 150


def test_hardcode_capacity_check_fails_before_llm_work():
    """候选超限必须即刻拒绝（T202610006999602-3220 实测 119 条候选撞 100 上限，
    此前要等 verdict+repair 跑完约 16 分钟才在末次校验报错）。"""
    facts = {"integrity": {"hardcode": {
        "truncated": True, "candidate_count": 119, "findings": list(range(100)),
    }}}
    with pytest.raises(RuntimeError, match="超过复核上限"):
        _check_hardcode_capacity(facts)

    ok_facts = {"integrity": {"hardcode": {
        "truncated": False, "candidate_count": 100, "findings": list(range(100)),
    }}}
    _check_hardcode_capacity(ok_facts)  # 不抛



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


def test_mygo_style_makefile_is_complete_without_dockerfile(tmp_path):
    (tmp_path / "Makefile").write_text(
        ".PHONY: all kernel-rv kernel-la\n"
        "all: kernel-rv kernel-la\n"
        "kernel-la:\n\tcargo build --target loongarch64-unknown-none\n"
        "\tcp target/loongarch64-unknown-none/release/kernel kernel-la\n"
        "kernel-rv:\n\tcargo build --target riscv64gc-unknown-none-elf\n"
        "\tcp target/riscv64gc-unknown-none-elf/release/kernel kernel-rv\n",
        encoding="utf-8",
    )

    result = scan_build_interface(tmp_path)

    assert result["status"] == "complete"
    assert result["missing_targets"] == []
    assert all(item["declared"] for item in result["required_targets"].values())
    assert result["verification"]["status"] == "not_run"
    assert result["container"]["status"] == "not_provided"
    assert "不据此判定风险" in result["container"]["summary"]
    assert "不能据此判定编译通过" in result["summary"]


def test_make_targets_declared_through_simple_variables_are_detected(tmp_path):
    (tmp_path / "Makefile").write_text(
        "KERNEL_RV := kernel-rv\n"
        "KERNEL_LA := kernel-la\n"
        "all: $(KERNEL_RV) $(KERNEL_LA)\n"
        "$(KERNEL_RV):\n\t@echo rv\n"
        "$(KERNEL_LA):\n\t@echo la\n",
        encoding="utf-8",
    )

    result = scan_build_interface(tmp_path)

    assert result["status"] == "complete"
    assert result["required_targets"]["kernel-rv"]["line"] == 4
    assert result["required_targets"]["kernel-la"]["line"] == 6


def test_literal_included_makefile_targets_are_detected_without_running_make(tmp_path):
    rules = tmp_path / "mk" / "targets.mk"
    rules.parent.mkdir()
    rules.write_text(
        "kernel-rv:\n\t@echo rv\n"
        "kernel-la:\n\t@echo la\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "include mk/targets.mk\nall: kernel-rv kernel-la\n", encoding="utf-8",
    )

    result = scan_build_interface(tmp_path)

    assert result["status"] == "complete"
    assert result["required_targets"]["kernel-rv"]["path"] == "mk/targets.mk"


def test_artifacts_copied_in_all_chain_recipes_count_as_declared(tmp_path):
    """官方只要求 make all 产出两个文件：目标名无需叫 kernel-rv/kernel-la。"""
    (tmp_path / "Makefile").write_text(
        "KERNEL_RV := kernel-rv\n"
        "KERNEL_LA := kernel-la\n"
        "all: env_bootstrap\n"
        "\t$(MAKE) build_riscv\n"
        "\t$(MAKE) build_loongarch\n"
        "env_bootstrap:\n\t@echo ready\n"
        "build_riscv:\n\t@cp arceos/*_riscv64-qemu-virt.bin $(KERNEL_RV)\n"
        "build_loongarch:\n\t@cp arceos/*_loongarch64-qemu-virt.bin $(KERNEL_LA)\n",
        encoding="utf-8",
    )

    result = scan_build_interface(tmp_path)

    assert result["status"] == "complete"
    assert result["missing_targets"] == []
    assert result["required_targets"]["kernel-rv"]["declared"] is True
    assert result["required_targets"]["kernel-rv"]["via"] == "recipe"
    assert result["required_targets"]["kernel-la"]["via"] == "recipe"
    assert result["aggregate_targets"] == ["all"]


def test_artifact_rules_without_all_entry_are_still_incomplete(tmp_path):
    (tmp_path / "Makefile").write_text(
        "kernel-rv:\n\t@echo rv\n"
        "kernel-la:\n\t@echo la\n",
        encoding="utf-8",
    )

    result = scan_build_interface(tmp_path)

    assert result["status"] == "partial"
    assert result["missing_targets"] == []
    assert "未识别到 all" in result["summary"]
    assert "双架构比赛构建入口不完整" in result["summary"]


def test_missing_one_architecture_is_reported_as_partial_interface(tmp_path):
    (tmp_path / "Makefile").write_text(
        "kernel-rv:\n\t@echo rv\n", encoding="utf-8"
    )

    result = scan_build_interface(tmp_path)

    assert result["status"] == "partial"
    assert result["missing_targets"] == ["kernel-la"]
    assert "双架构比赛构建入口不完整" in result["summary"]


def test_phony_names_without_real_rules_do_not_count_as_kernel_targets(tmp_path):
    (tmp_path / "Makefile").write_text(
        ".PHONY: kernel-rv kernel-la\nall: kernel-rv kernel-la\n",
        encoding="utf-8",
    )

    result = scan_build_interface(tmp_path)

    assert result["status"] == "partial"
    assert result["missing_targets"] == ["kernel-rv", "kernel-la"]


def test_root_docker_build_mismatch_is_only_an_auxiliary_warning(tmp_path):
    (tmp_path / "Makefile").write_text(
        "all: kernel-rv kernel-la\n"
        "kernel-rv:\n\t@echo rv\n"
        "kernel-la:\n\t@echo la\n"
        "build_docker:\n\tdocker build -t demo .\n",
        encoding="utf-8",
    )
    nested = tmp_path / "ci" / "Dockerfile"
    nested.parent.mkdir()
    nested.write_text("FROM scratch\n", encoding="utf-8")

    result = scan_reproducibility(tmp_path)

    assert result["status"] == "complete"
    assert result["container"]["status"] == "inconsistent"
    assert "根目录没有 Dockerfile" in result["container"]["summary"]
    assert {(item["path"], item["line"]) for item in result["container"]["evidence"]} == {
        ("Makefile", 7), ("ci/Dockerfile", 1),
    }


def test_missing_root_makefile_is_a_build_interface_problem_not_a_docker_problem(tmp_path):
    result = scan_build_interface(tmp_path)

    assert result["status"] == "missing"
    assert result["missing_targets"] == ["kernel-rv", "kernel-la"]
    assert "根目录未发现" in result["summary"]
    assert result["container"]["status"] == "not_provided"


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


@pytest.mark.parametrize(("path", "expected"), [
    ("os/src/mm/address.rs", "内存管理"),
    ("os/src/task/scheduler.rs", "进程管理"),
    ("fs/src/ext4/layout.rs", "文件系统"),
    ("os/src/net/socket.rs", "\u7f51\u7edc"),
    ("os/src/drivers/block.rs", "设备管理"),
    ("os/src/syscall/mod.rs", "系统调用"),
    ("os/src/arch/riscv/address.rs", "硬件抽象"),
])
def test_unclassified_files_use_unambiguous_path_fallback(path, expected):
    assert _fallback_subsystem_for_path(path) == expected


def test_path_fallback_does_not_guess_generic_source_files():
    assert _fallback_subsystem_for_path("os/src/utils.rs") is None


def test_tree_sitter_uses_byte_offsets_after_chinese_comments(tmp_path):
    (tmp_path / "main.rs").write_text(
        "// 中文注释会让 UTF-8 字节偏移大于字符偏移\nfn real_name() {}\n",
        encoding="utf-8",
    )
    engine = TreeSitterEngine(str(tmp_path), "rust")
    assert "real_name" in engine._func_index
    assert engine._func_index["real_name"]["body"] == "fn real_name() {}"


def test_description_digest_puts_hardcode_and_source_issues_first():
    digest = description_digest_from_tree(_tree())
    assert digest.kind == "description"
    titles = [item.title for item in digest.findings]
    assert not any(("编译" in t) or ("构建" in t) or ("运行" in t) for t in titles)
    hardcode = next(finding for finding in digest.findings if "硬编码" in finding.title)
    assert "ELF" in hardcode.title and hardcode.severity == "high"
    assert "根据被加载的测试" in hardcode.detail
    assert len(digest.modules[0].summary) <= 300


def test_description_digest_ignores_build_facts_entirely():
    tree = _tree()
    tree["facts"]["integrity"]["build_log"] = {"status": "not_provided"}
    tree["facts"]["integrity"]["build_interface"] = {
        "status": "partial",
        "summary": "根目录 Makefile 未识别到 kernel-la，双架构比赛构建入口不完整。",
        "missing_targets": ["kernel-la"],
        "evidence": [{"path": "Makefile", "line": 1, "excerpt": "kernel-rv:"}],
        "container": {
            "status": "not_provided",
            "summary": "未提供 Dockerfile；不据此判定风险。",
            "evidence": [],
        },
    }

    digest = description_digest_from_tree(tree)

    assert not any(
        ("构建" in item.title) or ("编译" in item.title) for item in digest.findings
    )
    assert not any(
        key.startswith(("build_", "run_", "kernel_", "container_", "reproducibility_"))
        for key in digest.metrics
    )


def test_description_html_is_problem_first_and_module_text_is_bounded():
    rendered = render_tree_html(_tree())
    assert rendered.index('<section id="verdict"') < rendered.index('<section id="modules"')
    assert "最多 5" not in rendered and "不设数量上限" in rendered
    assert rendered.index('<section id="hardcode"') < rendered.index('<section id="modules"')
    assert "真实可用性" not in rendered
    assert "构建接口</strong>" not in rendered
    assert "根据被加载的测试" in rendered
    assert "模块详细证据" not in rendered and "子系统详细证据" not in rendered
    assert rendered.count('data-subsystem="') == 1
    assert all(int(value) <= 300 for value in re.findall(r'data-analysis-chars="(\d+)"', rendered))
    assert "修改测试脚本旁路失败" in rendered
    assert "src/main.c:7" in rendered and "确认问题" in rendered
    assert "参赛队伍不得修改" not in rendered
    assert "分析依据：源码结构分析与硬编码线索复核" in rendered
    assert '<section id="evaluation"' not in rendered
    assert "tree-node" not in rendered


def test_module_issue_dedup_keeps_term_parentheses_intact():
    value = (
        "网络层包含操作系统（Operating System，OS）Socket 抽象；"
        "其他实现说明。"
    )
    issues = [{"quote": "OS Socket 层无法连接真实网卡"}]

    result = _remove_assigned_issue_sentences(value, issues)

    assert result == "其他实现说明"
    assert "Operating System，" not in result

def test_contest_build_verification_uses_temporary_copy_and_records_artifacts(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text(
        "kernel-rv:\n\t@true\nkernel-la:\n\t@true\n", encoding="utf-8",
    )

    monkeypatch.setattr(integrity_module.shutil, "which", lambda _name: "docker")

    def fake_docker(command, *, timeout):
        if command[1:3] == ["version", "--format"]:
            return subprocess.CompletedProcess(command, 0, '{"Version":"1"}\n')
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0,
                '{"Id":"sha256:image","RepoDigests":["contest@sha256:digest"],"Size":123}\n',
            )
        assert command[1] == "run"
        assert command[-1] == "make all"
        mount = command[command.index("--mount") + 1]
        source = mount.removeprefix("type=bind,source=").removesuffix(",target=/work")
        (Path(source) / "kernel-rv").write_bytes(b"artifact-rv")
        (Path(source) / "kernel-la").write_bytes(b"artifact-la")
        return subprocess.CompletedProcess(
            command, 0,
            "IOCTL_HEX2STR_ERROR configuration enabled\nFinished make all\n",
        )

    monkeypatch.setattr(integrity_module, "_docker_result", fake_docker)
    result = verify_contest_build(repo, image="contest:test", timeout_seconds=60)

    assert result["status"] == "passed"
    assert result["image_id"] == "sha256:image"
    assert result["image_digest"] == "contest@sha256:digest"
    assert result["limits"] == {"cpus": "8", "memory": "12g", "pids": 2048}
    assert set(result["targets"]) == {"kernel-rv", "kernel-la"}
    assert all(item["command"] == "make all" for item in result["targets"].values())
    assert all(item["artifact"]["sha256"] for item in result["targets"].values())
    assert all(not item["errors"] for item in result["targets"].values())
    assert not (repo / "kernel-rv").exists() and not (repo / "kernel-la").exists()


def test_git_build_snapshot_uses_committed_lf_bytes_on_windows(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text(
        "kernel-rv:\n\t@true\nkernel-la:\n\t@true\n", encoding="utf-8",
    )
    script = repo / "build.sh"
    script.write_bytes(b"#!/bin/sh\nexit 0\n")
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "test@example.invalid"],
        ["git", "config", "user.name", "Test"],
        ["git", "add", "."],
        ["git", "commit", "-qm", "fixture"],
    ):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    committed_script = subprocess.run(
        ["git", "show", "HEAD:build.sh"], cwd=repo, check=True, capture_output=True,
    ).stdout
    assert committed_script == b"#!/bin/sh\nexit 0\n"
    script.write_bytes(b"#!/bin/sh\r\nexit 0\r\n")
    real_which = integrity_module.shutil.which
    monkeypatch.setattr(
        integrity_module.shutil, "which",
        lambda name: "docker" if name == "docker" else real_which(name),
    )
    observed_scripts = []

    def fake_docker(command, *, timeout):
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, '{"Version":"1"}\n')
        if command[1] == "image":
            return subprocess.CompletedProcess(
                command, 0, '{"Id":"id","RepoDigests":[],"Size":1}\n',
            )
        mount = command[command.index("--mount") + 1]
        source = mount.removeprefix("type=bind,source=").removesuffix(",target=/work")
        observed_scripts.append((Path(source) / "build.sh").read_bytes())
        (Path(source) / "kernel-rv").write_bytes(b"rv")
        (Path(source) / "kernel-la").write_bytes(b"la")
        return subprocess.CompletedProcess(command, 0, "Finished make all\n")

    monkeypatch.setattr(integrity_module, "_docker_result", fake_docker)
    result = verify_contest_build(repo, image="contest:test", timeout_seconds=60)

    assert result["status"] == "passed"
    assert result["source"] == {
        "kind": "git_head_snapshot",
        "commit": commit,
        "working_tree_dirty": True,
    }
    assert observed_scripts == [b"#!/bin/sh\nexit 0\n"] * 1


def test_contest_build_verification_separates_compile_failure_from_environment_error(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    monkeypatch.setattr(integrity_module.shutil, "which", lambda _name: "docker")

    def fake_docker(command, *, timeout):
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, '{"Version":"1"}\n')
        if command[1] == "image":
            return subprocess.CompletedProcess(
                command, 0, '{"Id":"id","RepoDigests":[],"Size":1}\n',
            )
        return subprocess.CompletedProcess(command, 2, "error: linker failed\n")

    monkeypatch.setattr(integrity_module, "_docker_result", fake_docker)
    result = verify_contest_build(repo, image="contest:test", timeout_seconds=60)

    assert result["status"] == "failed"
    assert result["targets"]["kernel-rv"]["status"] == "failed"
    assert result["targets"]["kernel-la"]["status"] == "failed"
    assert "linker failed" in result["targets"]["kernel-rv"]["errors"][0]


def test_contest_build_verification_treats_offline_toolchain_download_as_environment_error(
    tmp_path, monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    monkeypatch.setattr(integrity_module.shutil, "which", lambda _name: "docker")

    def fake_docker(command, *, timeout):
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, '{"Version":"1"}\n')
        if command[1] == "image":
            return subprocess.CompletedProcess(
                command, 0, '{"Id":"id","RepoDigests":[],"Size":1}\n',
            )
        return subprocess.CompletedProcess(
            command,
            2,
            "error: could not download file from https://static.rust-lang.org: "
            "failed to lookup address\n",
        )

    monkeypatch.setattr(integrity_module, "_docker_result", fake_docker)
    result = verify_contest_build(repo, image="contest:test", timeout_seconds=60)

    assert result["status"] == "environment_error"
    assert {
        item["status"] for item in result["targets"].values()
    } == {"environment_error"}
    assert "\u4e0d\u80fd\u636e\u6b64\u5224\u65ad\u4f5c\u54c1\u5931\u8d25" in result["summary"]

def test_contest_build_verification_reports_missing_docker_as_environment_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(integrity_module.shutil, "which", lambda _name: None)

    result = verify_contest_build(tmp_path, image="contest:test", timeout_seconds=60)

    assert result["status"] == "environment_error"
    assert result["targets"] == {}
    assert "Docker CLI" in result["summary"]


def test_make_all_must_produce_both_root_artifacts_fresh(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    (repo / "kernel-la").write_bytes(b"stale-la")  # 构建前残留产物不应被当成结果
    monkeypatch.setattr(integrity_module.shutil, "which", lambda _name: "docker")

    def fake_docker(command, *, timeout):
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, '{"Version":"1"}\n')
        if command[1] == "image":
            return subprocess.CompletedProcess(
                command, 0, '{"Id":"id","RepoDigests":[],"Size":1}\n',
            )
        mount = command[command.index("--mount") + 1]
        source = mount.removeprefix("type=bind,source=").removesuffix(",target=/work")
        (Path(source) / "kernel-rv").write_bytes(b"rv")
        return subprocess.CompletedProcess(command, 0, "Finished make all\n")

    monkeypatch.setattr(integrity_module, "_docker_result", fake_docker)
    result = verify_contest_build(repo, image="contest:test", timeout_seconds=60)

    assert result["status"] == "partial"
    assert result["targets"]["kernel-rv"]["status"] == "passed"
    assert result["targets"]["kernel-la"]["status"] == "failed"
    assert "未生成非空 kernel-la" in result["targets"]["kernel-la"]["errors"][0]


def test_description_renders_no_usability_section_even_with_build_facts():
    tree = _tree()
    verification = {
        "requested": True,
        "status": "passed",
        "summary": "比赛统一镜像中双架构编译成功。",
        "image": "contest:test",
        "image_digest": "contest@sha256:digest",
        "targets": {
            "kernel-rv": {
                "status": "passed", "command": "make all", "duration_seconds": 12,
                "artifact": {"path": "kernel-rv", "size_bytes": 1048576, "sha256": "a" * 64},
            },
            "kernel-la": {
                "status": "passed", "command": "make all", "duration_seconds": 14,
                "artifact": {"path": "kernel-la", "size_bytes": 2097152, "sha256": "b" * 64},
            },
        },
    }
    tree["facts"]["integrity"]["build_verification"] = verification
    tree["facts"]["integrity"]["build_interface"]["verification"] = verification

    rendered = render_tree_html(tree)
    digest = description_digest_from_tree(tree)

    assert "真实可用性" not in rendered
    assert "比赛编译环境" not in rendered
    assert "RISC-V 编译" not in rendered
    assert "kernel_rv_build_status" not in digest.metrics
    assert "build_verification_requested" not in digest.metrics


def test_description_has_no_problem_count_cap_and_keeps_every_serious_issue():
    tree = _tree()
    tree["verdict"]["issues"] = [
        {
            "path": f"src/issue_{index}.c:{index}",
            "severity": "medium",
            "quote": f"第 {index} 个会影响正确性的独立问题。",
            "confidence": 90,
        }
        for index in range(1, 10)
    ]
    rendered = render_tree_html(tree)
    assert rendered.count('data-main-finding="') == 9
    assert "第 9 个会影响正确性的独立问题" in rendered


def test_description_renders_every_top_level_subsystem_not_only_five():
    tree = _tree()
    names = ["启动模块", "内存管理", "进程管理", "文件系统", "设备管理", "系统调用", "硬件抽象", "其他"]
    tree["tree"]["children"] = [
        {"type": "subsystem", "name": name, "summary": f"{name}静态摘要。", "children": []}
        for name in names
    ]
    rendered = render_tree_html(tree)
    assert rendered.count('data-subsystem="') == len(names)
    assert all(f'data-subsystem="{name}"' in rendered for name in names)


def test_description_promotes_generic_children_to_peer_sections():
    tree = _tree()
    tree["tree"]["children"].append({
        "type": "subsystem",
        "name": "其他",
        "summary": "公共基础设施。",
        "children": [
            {"type": "module", "name": "定时器与时钟", "summary": "维护单调时钟。",
             "file_paths": ["src/timer.c"]},
            {"type": "module", "name": "进程间通信", "summary": "提供管道和消息队列。",
             "file_paths": ["src/ipc.c"]},
        ],
    })

    rendered = render_tree_html(tree)
    digest = description_digest_from_tree(tree)

    assert 'data-subsystem="其他"' not in rendered
    assert 'data-subsystem="定时器与时钟"' in rendered
    assert 'data-subsystem="进程间通信"' in rendered
    assert {module.name for module in digest.modules} >= {"定时器与时钟", "进程间通信"}


def test_important_issues_are_severity_sorted_and_not_repeated_in_modules():
    tree = _tree()
    subsystem = tree["tree"]["children"][0]
    subsystem["summary"] = (
        "该模块构造用户与内核地址空间，管理页表映射、页框生命周期和缺页处理。"
        "地址空间复制支持写时复制，回收路径按映射关系释放资源。"
    )
    subsystem["highlights"] = [
        {"path": "src/mm.c:2", "quote": "页表接口统一封装映射、查询与撤销流程。"},
        {"path": "src/alloc.c:3", "quote": "页框分配器支持回收并维护空闲集合。"},
    ]
    subsystem["children"] = [
        {"type": "module", "name": "页表与地址空间",
         "summary": "处理多级页表映射、权限转换和用户地址空间构造。",
         "content": "<p>入口位于 src/mm.c:2。</p>", "file_paths": ["src/mm.c"]},
        {"type": "module", "name": "物理页分配",
         "summary": "维护空闲页框并为缺页和内核对象提供物理内存。",
         "content": "<p>分配逻辑位于 src/alloc.c:3。</p>", "file_paths": ["src/alloc.c"]},
    ]
    subsystem["issues"] = [
        {"path": "src/mm.c:20", "severity": "medium", "quote": "撤销映射时缺少跨核同步。"},
        {"path": "src/mm.c:30", "severity": "high", "quote": "页表权限检查错误会允许越权写入。"},
        {"path": "src/alloc.c:40", "severity": "low", "quote": "分配器统计信息命名不统一。"},
    ]
    tree["verdict"]["issues"] = list(subsystem["issues"])

    rendered = render_tree_html(tree)

    assert rendered.index("页表权限检查错误") < rendered.index("撤销映射时缺少跨核同步")
    assert rendered.count("页表权限检查错误") == 1
    assert rendered.count("撤销映射时缺少跨核同步") == 1
    assert "分配器统计信息命名不统一" in rendered
    assert "data-other-finding" not in rendered
    assert "src/mm.c:2" in rendered and "src/alloc.c:3" in rendered
    assert 'data-evidence-count="2"' in rendered
    analysis_lengths = [int(value) for value in re.findall(
        r'data-analysis-chars="(\d+)"', rendered
    )]
    assert 150 <= analysis_lengths[0] <= 300


def test_low_severity_incomplete_feature_stays_in_its_module():
    tree = _tree()
    subsystem = tree["tree"]["children"][0]
    subsystem["issues"] = [{
        "path": "src/mm.c:50", "severity": "low",
        "quote": "调试接口尚未实现，当前返回 ENOSYS。",
    }]
    tree["verdict"]["issues"] = list(subsystem["issues"])

    rendered = render_tree_html(tree)

    assert "data-main-finding" not in rendered
    assert "调试接口尚未实现，当前返回 ENOSYS" in rendered


def test_module_summary_does_not_paraphrase_an_important_issue_again():
    tree = _tree()
    subsystem = tree["tree"]["children"][0]
    subsystem["summary"] = (
        "调度策略框架支持多种策略；SCHED_DEADLINE 字段存在但未实现实际调度逻辑。"
    )
    subsystem["issues"] = [{
        "path": "src/mm.c:60", "severity": "medium",
        "quote": "SCHED_DEADLINE 字段已定义但未实现实际调度逻辑。",
    }]
    tree["verdict"]["issues"] = list(subsystem["issues"])

    rendered = render_tree_html(tree)
    modules = re.search(r'<section id="modules"[\s\S]*?</section>', rendered).group(0)

    assert "SCHED_DEADLINE" in rendered
    assert "SCHED_DEADLINE" not in modules


def test_syscall_count_is_labeled_as_a_static_signal_and_never_overclaims():
    tree = _tree()
    tree["facts"]["syscall"] = {"standard_count": 48, "standard_total": 60}
    tree["verdict"]["issues"] = [{
        "path": "src/syscall/mod.rs:20",
        "severity": "low",
        "quote": "当前覆盖 49/60 项标准系统调用，尚有 11 项未实现。",
        "confidence": 90,
    }]
    tree["tree"]["children"][0]["summary"] = "实现 Linux 标准系统调用约 49/60 项。"
    rendered = render_tree_html(tree)
    assert "49/60" not in rendered
    assert "48/60 个标准系统调用名称" in rendered
    assert "不代表语义可用或测试通过" in rendered


def test_plus_syscall_number_claim_is_replaced_by_static_scan_wording():
    tree = _tree()
    tree["facts"]["syscall"] = {
        "standard_count": 47, "standard_total": 60, "dispatch_count": 0,
    }
    tree["tree"]["children"][0]["summary"] = (
        "该系统调用子系统支持 120+ 个调用号，并通过分发表路由 120+ 系统调用。"
    )

    rendered = render_tree_html(tree)
    digest = description_digest_from_tree(tree)

    assert "120+" not in rendered
    assert "47/60 个标准系统调用名称" in rendered
    assert "120+" not in digest.modules[0].summary
    assert "47/60 个标准系统调用名称" in digest.modules[0].summary
    assert "0 个不同分支" not in rendered


def test_plain_syscall_count_is_also_replaced_by_static_scan_wording():
    tree = _tree()
    tree["facts"]["syscall"] = {
        "standard_count": 44, "standard_total": 60, "dispatch_count": 230,
    }
    tree["tree"]["children"][0]["summary"] = "模块实现了44个标准 Linux 系统调用。"

    rendered = render_tree_html(tree)

    assert "实现了44个" not in rendered
    assert "44/60" in rendered and "230 个不同分支" in rendered


def test_syscall_dispatch_probe_counts_unique_rust_and_c_arms(tmp_path):
    rust = tmp_path / "src" / "syscall.rs"
    rust.parent.mkdir()
    rust.write_text(
        "match id {\n SYS_OPEN => open(),\n SYS_CLOSE => close(),\n SYS_OPEN => open2(),\n}\n",
        encoding="utf-8",
    )
    c = tmp_path / "kernel.c"
    c.write_text("switch (id) {\ncase SYS_READ: return read();\n}\n", encoding="utf-8")

    count, evidence = _probe_syscall_dispatch(tmp_path)

    assert count == 3
    assert evidence == ["src/syscall.rs:2", "kernel.c:2"] or evidence == [
        "kernel.c:2", "src/syscall.rs:2",
    ]


def test_hardcode_digest_separates_confirmed_and_suspected_same_category():
    tree = _tree()
    confirmed = tree["verdict"]["hardcode_reviews"][0]
    suspected = {
        **confirmed,
        "signal_id": "src/main.c:9:按测试名或 ELF 名称分支",
        "line": 9,
        "status": "suspected",
        "method": "缺失路径回退为固定程序",
        "reason": "仍需运行确认获益范围。",
    }
    tree["verdict"]["hardcode_reviews"] = [confirmed, suspected]
    tree["facts"]["integrity"]["hardcode"]["findings"].append({
        "signal_id": suspected["signal_id"],
        "category": suspected["category"],
        "path": suspected["path"],
        "line": suspected["line"],
        "excerpt": suspected["excerpt"],
    })

    findings = _reviewed_hardcode_findings(
        tree["verdict"], tree["facts"]["integrity"]
    )

    assert len(findings) == 2
    assert {item.severity for item in findings} == {"high", "medium"}
    assert any("硬编码问题" in item.title for item in findings)
    assert any("硬编码线索" in item.title for item in findings)


def test_hardcode_count_in_conclusion_comes_from_structured_reviews():
    tree = _tree()
    tree["verdict"]["one_line"] = "构建未提供；硬编码存在6处嫌疑。"
    tree["verdict"]["hardcode_reviews"] = [
        {**tree["verdict"]["hardcode_reviews"][0], "signal_id": f"s{i}", "status": "suspected"}
        for i in range(7)
    ]
    tree["facts"]["integrity"]["hardcode"]["findings"] = [
        {"signal_id": f"s{i}"} for i in range(7)
    ]

    rendered = render_tree_html(tree)

    assert "硬编码存在6处嫌疑" not in rendered
    assert "硬编码复核发现 7 条疑似线索、无确认项" in rendered


def test_hardcode_brief_hides_raw_scanner_candidate_counts():
    tree = _tree()
    tree["facts"]["integrity"]["hardcode"].update({
        "candidate_count": 46,
        "scanned_files": 220,
        "category_coverage": {
            name: {"scanned": True}
            for name in ("elf", "cache", "print", "script")
        },
    })
    tree["verdict"]["hardcode_reviews"] = [
        {**tree["verdict"]["hardcode_reviews"][0], "signal_id": f"s{i}", "status": "suspected"}
        for i in range(7)
    ]
    tree["facts"]["integrity"]["hardcode"]["findings"] = [
        {"signal_id": f"s{i}"} for i in range(7)
    ]

    rendered = render_tree_html(tree)

    assert "AI 复核确认 0 条、疑似 7 条" in rendered
    assert "命中 46 条候选" not in rendered
    assert "扫描 220 个文件" not in rendered


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


def test_one_line_rejects_any_compile_claim():
    facts = {"integrity": {}}
    parsed = {
        "one_line": "编译失败；未发现硬编码，页表回收不完整。",
        "hardcode_reviews": [],
    }
    with pytest.raises(RuntimeError, match="不得出现编译或构建"):
        _validate_verdict_integrity_conclusion(parsed, facts)


def test_one_line_accepts_equivalent_no_cheating_conclusion():
    parsed = {
        "one_line": "硬编码线索均已复核，无作弊。",
        "hardcode_reviews": [{"status": "cleared"}],
    }
    facts = {"integrity": {}}
    _validate_verdict_integrity_conclusion(parsed, facts)


def test_one_line_must_match_hardcode_review_conclusion():
    facts = {"integrity": {}}
    parsed = {
        "one_line": "确认发现1项硬编码，页表回收不完整。",
        "hardcode_reviews": [{"status": "confirmed"}],
    }
    _validate_verdict_integrity_conclusion(parsed, facts)
    parsed["one_line"] = "未发现硬编码，页表回收不完整。"
    with pytest.raises(RuntimeError, match="与硬编码复核结果不一致"):
        _validate_verdict_integrity_conclusion(parsed, facts)


def test_deterministic_one_line_fallback_keeps_verified_statuses_under_limit():
    parsed = {
        "issues": [{"quote": "块设备容量硬编码为 4194304 * 1024 字节（4GB），未从设备读取容量。"}],
        "hardcode_reviews": [
            {"status": "suspected"},
            {"status": "suspected"},
            {"status": "cleared"},
        ],
    }
    facts = {"integrity": {
        "build_verification": {"requested": True, "status": "failed"},
        "run_log": {"status": "not_provided"},
    }}
    one_line = _deterministic_verdict_one_line(parsed, facts)
    parsed["one_line"] = one_line
    assert len(one_line) <= 80
    assert "编译" not in one_line and "构建" not in one_line
    assert "发现2项疑似硬编码" in one_line
    assert "主要问题" in one_line
    _validate_verdict_integrity_conclusion(parsed, facts)


def test_short_one_line_with_compile_claim_requires_repair():
    facts = {"integrity": {}}
    parsed = {
        "one_line": "\u7f16\u8bd1\u5931\u8d25\uff1b\u672a\u53d1\u73b0\u786c\u7f16\u7801\u3002",
        "hardcode_reviews": [{"status": "cleared"}],
    }
    assert _verdict_one_line_requires_repair(parsed, facts) is True
    parsed["one_line"] = "\u672a\u53d1\u73b0\u786c\u7f16\u7801\u3002"
    assert _verdict_one_line_requires_repair(parsed, facts) is False


def test_verdict_content_uses_full_path_from_structured_evidence():
    parsed = {
        "issues": [{"path": "os/src/arch/loongarch64/paging.rs:8"}],
        "content": "<li>分页常量见 paging.rs:8。</li>",
    }
    _normalize_verdict_content_paths(parsed)
    assert "os/src/arch/loongarch64/paging.rs:8" in parsed["content"]


def _verdict_with_dimension_names(names: list[str]) -> dict:
    return {
        "score_total": 82,
        "dimensions": [
            {"name": name, "score": 80, "reason": "理由"} for name in names
        ],
        "highlights": [],
        "issues": [],
        "hardcode_reviews": [],
        "similarity": {},
        "one_line": "未发现硬编码。",
        "content": "<p>详细正文</p>",
    }


def test_verdict_normalizes_abbreviated_dimension_names():
    """LLM 省写维度后缀（架构/文档）时确定性纠偏到规范名，总分按规范权重重算。"""
    parsed = _verdict_with_dimension_names(
        ["原创性", "架构", "代码质量", "文档", "完整性", "功能性"])
    out = _normalize_verdict(parsed)
    assert [d["name"] for d in out["dimensions"]] == [
        "原创性", "架构合理性", "代码质量", "文档质量", "完整性", "功能性",
    ]
    by = {d["name"]: d["score"] for d in out["dimensions"]}
    assert by["架构合理性"] == 80 and by["文档质量"] == 80
    assert out["score_total"] == 80


def test_verdict_validation_accepts_abbreviated_dimension_names():
    """校验入口同样接受别名维度名——交付校验与最终校验共用同一映射。"""
    parsed = _verdict_with_dimension_names(
        ["原创性", "架构", "代码质量", "文档", "完整性", "功能性"])
    _validate_verdict_result(parsed, None)  # 不抛 RuntimeError


def test_verdict_validation_rejects_truly_missing_dimension():
    parsed = _verdict_with_dimension_names(
        ["原创性", "架构", "代码质量", "文档", "完整性"])
    with pytest.raises(RuntimeError, match="缺少评分维度：功能性"):
        _validate_verdict_result(parsed, None)


def _repair_shape_ok_dict(**overrides) -> dict:
    base = {
        "score_total": 82,
        "dimensions": [
            {"name": name, "score": 80, "reason": "理由"}
            for name in ("原创性", "架构合理性", "代码质量", "文档质量",
                         "完整性", "功能性")
        ],
        "highlights": [{"path": "os/src/mm.rs:8", "quote": "亮点"}],
        "issues": [{"path": "os/src/mm.rs:8", "quote": "问题", "confidence": 90}],
        "hardcode_reviews": [
            {"signal_id": "os/src/a.rs:1:疑似写死测试结果", "status": "cleared",
             "path": "os/src/a.rs", "line": 1, "excerpt": "e"}
        ],
        "similarity": {"reference_os": "rcore-tutorial-v3",
                       "overlap_pct": 35, "summary": "摘要"},
        "one_line": "未发现硬编码。",
    }
    base.update(overrides)
    return base


def test_verdict_repair_shape_ok_accepts_proper_structure():
    _verdict_repair_shape_ok(_repair_shape_ok_dict())  # 不抛


def test_verdict_repair_shape_ok_rejects_string_highlights():
    drifted = _repair_shape_ok_dict(highlights=["文本亮点"])
    with pytest.raises(RuntimeError, match="亮点"):
        _verdict_repair_shape_ok(drifted)


def test_verdict_repair_shape_ok_rejects_issues_without_quote():
    drifted = _repair_shape_ok_dict(issues=[{"path": "os/src/mm.rs:8"}])
    with pytest.raises(RuntimeError, match="问题"):
        _verdict_repair_shape_ok(drifted)


def test_verdict_repair_shape_ok_accepts_abbreviated_names_but_rejects_unknown():
    """别名维度名视为可识别（与 _validate_verdict_result 同一映射）。"""
    dims = _repair_shape_ok_dict()["dimensions"]
    dims[1]["name"] = "架构"
    _verdict_repair_shape_ok(_repair_shape_ok_dict(dimensions=dims))  # 不抛
    dims[1]["name"] = "魔法值"
    with pytest.raises(RuntimeError, match="维度"):
        _verdict_repair_shape_ok(_repair_shape_ok_dict(dimensions=dims))


def test_read_latest_matching_prefers_newest_alias(tmp_path):
    """正文写到别名路径（.verdict.html）时不得读回上一轮的陈旧正文。"""
    work = tmp_path / "work"
    work.mkdir()
    stale = work / "verdict.html"
    stale.write_text("旧正文", encoding="utf-8")
    alias = work / ".verdict.html"
    alias.write_text("新正文", encoding="utf-8")
    assert _read_latest_matching(stale) == "新正文"
    alias.unlink()
    assert _read_latest_matching(stale) == "旧正文"


def test_description_delivery_rejects_broken_source_links(tmp_path):
    tree = _tree()
    tree["facts"]["integrity"]["build_log"] = {"status": "not_provided"}
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "main.c").write_text("\n" * 8, encoding="utf-8")
    # src/mm.c 故意不存在：问题证据不得被渲染成看似可点击的伪链接。
    with pytest.raises(IncompleteReportError, match="无法回溯到源码"):
        write_tree_html(tmp_path / "description.html", tree, repo_roots=[repo])


def test_description_links_use_target_repository_metadata(tmp_path):
    tree = _tree()
    tree["meta"]["repository_url"] = "https://gitlab.example.com/contest/cosmos"
    tree["meta"]["repository_ref"] = "deadbeef"
    tree["facts"]["integrity"]["build_log"] = {"status": "not_provided"}
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "Makefile").write_text(
        "kernel-rv:\n\t@echo rv\nkernel-la:\n\t@echo la\n", encoding="utf-8"
    )
    (repo / "src" / "main.c").write_text("\n" * 7, encoding="utf-8")
    (repo / "src" / "mm.c").write_text("\n" * 9, encoding="utf-8")

    output, broken = write_tree_html(
        tmp_path / "description.html", tree, repo_roots=[repo]
    )

    rendered = output.read_text(encoding="utf-8")
    assert not broken
    assert "https://gitlab.example.com/contest/cosmos/-/blob/deadbeef/" in rendered
    assert "report/generator" not in rendered


def test_description_file_only_evidence_gets_a_line_anchor(tmp_path):
    tree = _tree()
    tree["meta"]["repository_url"] = "https://gitlab.example.com/contest/cosmos"
    tree["meta"]["repository_ref"] = "deadbeef"
    tree["facts"]["integrity"]["build_log"] = {"status": "not_provided"}
    tree["tree"]["children"][0]["highlights"] = [
        {"path": "src/main.c", "quote": "entry point"},
    ]
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "Makefile").write_text(
        "kernel-rv:\n\t@echo rv\nkernel-la:\n\t@echo la\n", encoding="utf-8"
    )
    (repo / "src" / "main.c").write_text("\n" * 7 + "int main(void) {}\n", encoding="utf-8")
    (repo / "src" / "mm.c").write_text("\n" * 9, encoding="utf-8")

    output, broken = write_tree_html(
        tmp_path / "description.html", tree, repo_roots=[repo]
    )

    rendered = output.read_text(encoding="utf-8")
    assert not broken
    assert "/src/main.c#L1" in rendered
    assert ">src/main.c:1</a>" in rendered


def test_description_softens_unverified_absolute_capability_claims():
    claim = (
        "覆盖完整，构建了完整的 TCP/IP 网络协议栈，完整定义接口，"
        "与 Linux 主线 UAPI 头文件保持一致，确保用户程序二进制兼容。"
    )

    cleaned = _clean_capability_claim(claim, {"facts": {}})

    assert "覆盖主要路径" in cleaned
    assert "基于 smoltcp 的 TCP/IP 网络能力" in cleaned
    assert "集中定义接口" in cleaned
    assert "以兼容 Linux UAPI 为目标" in cleaned
    assert "为用户程序二进制兼容提供接口基础" in cleaned


def test_module_path_normalization_recovers_unique_omitted_repo_root(tmp_path):
    repo = tmp_path / "repo"
    target = repo / "r-core" / "virtio-drivers" / "src" / "hal.rs"
    target.parent.mkdir(parents=True)
    target.write_text("pub struct Hal;\n", encoding="utf-8")

    assert _normalize_adjacent_module_paths(
        ["virtio-drivers/src/hal.rs"], repo
    ) == ["r-core/virtio-drivers/src/hal.rs"]


def test_module_path_normalization_refuses_ambiguous_suffix(tmp_path):
    repo = tmp_path / "repo"
    for root in ("a", "b"):
        target = repo / root / "virtio-drivers" / "src" / "hal.rs"
        target.parent.mkdir(parents=True)
        target.write_text("pub struct Hal;\n", encoding="utf-8")
    assert _normalize_adjacent_module_paths(["virtio-drivers/src/hal.rs"], repo) == ["virtio-drivers/src/hal.rs"]


def test_subsystem_analysis_excludes_dependency_scope_only_issues(tmp_path):
    repo = tmp_path / "repo"
    source = repo / "os" / "src" / "net" / "tcp.rs"
    source.parent.mkdir(parents=True)
    source.write_text(
        "//! loopback-oriented compatibility layer\n"
        "pub fn sys_connect() -> isize { -111 }\n",
        encoding="utf-8",
    )
    parsed = {
        "summary": "网络适配层摘要",
        "content": "<p>网络适配层分析。</p>",
        "highlights": [{"path": "os/src/net/tcp.rs:2", "quote": "连接入口"}],
        "issues": [
            {
                "path": "os/src/net/tcp.rs:1",

                "severity": "low",
                "quote": "TCP 实现声明为 loopback-oriented 兼容层，非完整 Linux TCP 栈",
            },
            {
                "path": "os/src/net/tcp.rs:2",
                "severity": "high",
                "quote": "connect 系统调用直接返回错误码 -111，导致外部连接失败",
            },
            {
                "path": "vendor/smoltcp/src/socket/tcp.rs:1",
                "severity": "medium",
                "quote": "第三方协议库内部实现不完整",
            },
            {
                "path": "r-core/lwext4_rust/examples/src/vfs_ops.rs:17",
                "severity": "medium",
                "quote": "示例工程中的 VfsOps 没有实现 format",
            },
        ],
        "modules": [{
            "name": "TCP 适配层",
            "summary": "连接接口",
            "content": "<p>连接接口。</p>",
            "file_paths": ["os/src/net/tcp.rs", "examples/tcp_demo.rs"],
        }],
    }

    _validate_subsys_result(parsed, "设备管理", repo)

    assert parsed["issues"] == [{
        "path": "os/src/net/tcp.rs:2",
        "severity": "high",
        "quote": "connect 系统调用直接返回错误码 -111，导致外部连接失败",
    }]
    assert parsed["modules"][0]["file_paths"] == ["os/src/net/tcp.rs"]



def test_description_renderer_hides_dependency_scope_only_issue():
    tree = _tree()
    tree["verdict"]["issues"] = []
    tree["tree"]["children"][0]["issues"] = [{
        "path": "src/mm.c:9",
        "severity": "low",
        "quote": "TCP 实现声明为 loopback-oriented 兼容层，非完整 Linux TCP 栈",
    }]

    rendered = render_tree_html(tree)

    assert "loopback-oriented" not in rendered
