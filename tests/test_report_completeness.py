from __future__ import annotations

import pytest

from oskernel_agent.pipeline import tree_builder
from oskernel_agent.report_quality import (
    IncompleteReportError,
    assert_report_complete,
    find_ellipsis_omissions,
    find_system_placeholders,
    find_visual_truncation_styles,
)
from oskernel_agent.comparison.report import semantic_compare as SC
from oskernel_agent.comparison.report.__main__ import build_parser as build_report_parser


def test_system_placeholder_text_is_a_hard_report_error():
    rendered = "<section><p>未启用 LLM 语义分析，以下为规则汇总。</p></section>"
    assert find_system_placeholders(rendered) == ["未启用 LLM 语义分析"]
    with pytest.raises(IncompleteReportError):
        assert_report_complete(rendered)


def test_ai_failure_placeholder_text_is_a_hard_report_error():
    rendered = "<section><p>AI 模型检测未完成。</p></section>"
    assert find_system_placeholders(rendered) == ["AI 模型检测未完成"]
    with pytest.raises(IncompleteReportError):
        assert_report_complete(rendered)


def test_visible_ellipsis_is_a_hard_report_error_but_source_code_is_allowed():
    rendered = "<p>开发过程仍有内容未说明…</p><code>fn demo(...) {}</code>"
    assert find_ellipsis_omissions(rendered) == ["开发过程仍有内容未说明…"]
    with pytest.raises(IncompleteReportError, match="省略号截断"):
        assert_report_complete(rendered)
    assert_report_complete("<p>结论已完整说明。</p><code>fn demo(...) {}</code>")


@pytest.mark.parametrize(
    "rendered, expected",
    [
        ("<style>.x{text-overflow:ellipsis}</style>", "CSS text-overflow: ellipsis"),
        ("<style>.x{-webkit-line-clamp:2}</style>", "CSS line-clamp"),
        ('<script>{"overflow":"truncate"}</script>', "图表文字 truncate"),
    ],
)
def test_visual_text_truncation_is_a_hard_report_error(rendered, expected):
    assert find_visual_truncation_styles(rendered) == [expected]
    with pytest.raises(IncompleteReportError, match="隐藏文字样式"):
        assert_report_complete(rendered)


def test_structured_analysis_error_is_a_hard_report_error():
    tree = {"tree": {"children": [{"name": "内存管理", "_error": "timeout"}]}}
    with pytest.raises(IncompleteReportError):
        assert_report_complete("<html></html>", structured=tree)


def test_report_cli_enables_real_semantic_analysis_by_default():
    parser = build_report_parser()
    default = parser.parse_args([
        "compare", "--suspects", "demo.json", "--ai-detect-result", "ai.json",
    ])
    skipped = parser.parse_args([
        "compare", "--suspects", "demo.json", "--ai-detect-result", "ai.json",
        "--skip-global-semantic-analysis",
    ])
    assert default.global_semantic_analysis is True
    assert skipped.global_semantic_analysis is False


def test_semantic_batches_preserve_every_function_cluster_within_budget():
    groups = []
    for module in ("fs", "mm"):
        for index in range(20):
            groups.append({
                "module": module,
                "overall_tier": "confirmed",
                "overall_sim": 1 - index / 100,
                "query_file": f"src/{module}/{index}.rs",
                "query_func": f"func_{index}",
                "query_code": "q" * 20_000,
                "candidates": [{
                    "ref_repo": f"history/ref-{index}",
                    "ref_code": "r" * 20_000,
                }],
            })

    clusters = SC.build_similarity_clusters(groups)
    batches = SC._semantic_cluster_batches(groups, 20)
    batched_clusters = [cluster for batch in batches for cluster in batch]

    assert {cluster["module"] for cluster in batched_clusters} == {"fs", "mm"}
    assert {cluster["analysis_id"] for cluster in batched_clusters} == {
        cluster["analysis_id"] for cluster in clusters
    }
    assert len(batches) > 1
    assert all(
        sum(SC._semantic_cluster_cost(cluster, 20) for cluster in batch)
        <= SC._SEMANTIC_PROMPT_CHAR_BUDGET
        for batch in batches
    )
    stats = {
        "fs": {"confirmed": 20, "top_source": "history/ref"},
        "mm": {"confirmed": 20, "top_source": "history/ref"},
    }
    for batch in batches:
        message = SC._build_analysis_message("new/repo", batch, stats, 20)
        assert len(message) <= SC._SEMANTIC_PROMPT_CHAR_BUDGET
        assert all(cluster["analysis_id"] in message for cluster in batch)
        assert "data-cluster" in message


def test_innovation_parser_is_independent_from_strict_review_parser():
    fenced = '```json\n{"innovations": []}\n```'
    assert SC._parse_innovation_json_object(fenced) == {"innovations": []}
    with pytest.raises(ValueError):
        SC._parse_json_object(fenced)


def test_innovation_prompt_applies_repository_independent_code_budget():
    candidates = []
    for index in range(18):
        candidates.append({
            "key": f"t{index:04d}", "module_display": "内存管理",
            "reference_repo": "history/ref", "file": f"src/{index}.rs",
            "start": 1, "end": 100, "func": f"target_{index}",
            "analysis_code": "q" * 20_000,
            "references": [{
                "key": f"r{index:04d}", "repo": "history/ref",
                "file": f"ref/{index}.rs", "start": 1, "end": 100,
                "func": f"reference_{index}", "analysis_code": "r" * 20_000,
            }],
        })

    message = SC._innovation_message("new/repo", candidates)

    assert len(message) < 120_000
    assert "q" * (SC._INNOVATION_CODE_CHAR_LIMIT + 1) not in message
    assert "r" * (SC._INNOVATION_CODE_CHAR_LIMIT + 1) not in message


def test_normal_report_rejects_unfinished_model_review_candidates():
    suspect = {
        "tier": "review",
        "review_verdict": "复核失败",
        "model_review_selection": "selected",
        "query_func": {
            "file_path": "src/mm.rs", "start_line": 10, "func_name": "map_page",
        },
    }
    with pytest.raises(IncompleteReportError, match="模型复核未完整完成"):
        SC._assert_model_review_complete([suspect])


def test_review_completeness_gate_ignores_deterministic_and_valid_results():
    confirmed = {"tier": "confirmed", "query_func": {"func_name": "copy"}}
    unselected = {
        "tier": "weak",
        "query_func": {"file_path": "src/other.rs", "start_line": 2, "func_name": "helper"},
    }
    reviewed = {
        "tier": "review", "review_verdict": "疑似", "model_review_selection": "selected",
        "query_func": {"file_path": "src/fs.rs", "start_line": 1, "func_name": "open"},
    }
    SC._assert_model_review_complete([confirmed, unselected, reviewed])


def test_subsystem_validation_rejects_missing_module_content():
    parsed = {
        "summary": "负责页表与地址空间管理。",
        "content": "<p>分析地址空间生命周期。</p>",
        "modules": [{
            "name": "页表", "summary": "维护多级页表。",
            "content": "", "file_paths": ["src/mm/page.rs"],
        }],
    }
    with pytest.raises(RuntimeError, match="content"):
        tree_builder._validate_subsys_result(parsed, "内存管理")


def test_bare_module_filename_is_resolved_next_to_previous_real_path(tmp_path):
    module_dir = tmp_path / "os" / "src" / "task"
    module_dir.mkdir(parents=True)
    (module_dir / "processor.rs").write_text("", encoding="utf-8")
    (module_dir / "mod.rs").write_text("", encoding="utf-8")

    result = tree_builder._normalize_adjacent_module_paths(
        ["os/src/task/processor.rs", "mod.rs"], tmp_path
    )

    assert result == ["os/src/task/processor.rs", "os/src/task/mod.rs"]


def test_bare_file_line_in_module_content_uses_real_module_path():
    result = tree_builder._normalize_module_content_paths(
        "调用 suspend_current（mod.rs:65）后切换任务。",
        ["os/src/task/processor.rs", "os/src/task/mod.rs"],
    )

    assert "os/src/task/mod.rs:65" in result
    assert tree_builder._normalize_module_content_paths(
        "通过 mod.rs 统一导出。", ["os/src/drivers/net.rs", "os/src/drivers/mod.rs"]
    ) == "通过 os/src/drivers/mod.rs 统一导出。"


def test_verdict_validation_rejects_fixed_or_missing_dimensions():
    parsed = {
        "content": "<p>总体分析。</p>",
        "one_line": "实现较完整。",
        "dimensions": [{"name": "原创性", "score": 60, "reason": "有代码证据。"}],
    }
    with pytest.raises(RuntimeError, match="缺少评分维度"):
        tree_builder._validate_verdict_result(parsed)
