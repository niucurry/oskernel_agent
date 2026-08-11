from __future__ import annotations


import pytest

from oskernel_agent.finals.digests import comparison_digest
from oskernel_agent.comparison.report import semantic_compare as SC


def _suspect(
    repo: str, name: str, module: str = "fs", sim: float = .98,
    exact_lines: int = 18,
) -> dict:
    return {
        "tier": "confirmed",
        "final_score": sim,
        "query_func": {
            "repo_id": "2026/new", "file_path": f"src/{name}.rs", "start_line": 1,
            "end_line": 20, "func_name": name, "module_tag": module,
            "raw_code": "fn x() {}\n" * 20,
        },
        "candidate_func": {
            "repo_id": repo, "file_path": f"old/{name}.rs", "start_line": 1,
            "end_line": 20, "func_name": name, "module_tag": module,
            "raw_code": "fn x() {}\n" * 20,
        },
        "evidence": {"line_similarity": sim, "exact_match_lines": exact_lines},
    }


def test_closest_repo_uses_unique_target_functions_not_candidate_pair_count():
    suspects = [
        _suspect("2025/A", "open"),
        _suspect("2025/A", "close"),
        _suspect("2024/B", "open"),
    ]
    closest = SC.select_closest_historical_repo(suspects)
    assert closest["repo"] == "2025/A"
    assert closest["functions"] == 2


def test_file_matches_are_filtered_to_the_single_closest_repo():
    matches = [{
        "query_file": "src/main.rs",
        "matches": [{"repo_id": "2025/A"}, {"repo_id": "2024/B"}],
    }]
    result = SC._file_matches_for_source(matches, "2025/A")
    assert [item["repo_id"] for item in result[0]["matches"]] == ["2025/A"]


def test_comparison_digest_sorts_modules_and_states_percentage_basis():
    stats = {
        "fs": {"confirmed": 2, "review": 0, "total": 4},
        "mm": {"confirmed": 1, "review": 1, "total": 5},
    }
    digest = comparison_digest("2026/new", "2025/A", stats)
    assert digest.metrics["overall_similarity_pct"] == 33.3
    assert [module.name for module in digest.modules] == ["文件系统", "内存管理"]
    assert "2/4" in digest.modules[0].summary
    assert "2025 年 A 队作品" in digest.conclusion


def test_finals_comparison_html_shows_dynamic_overview_and_closest_evidence():
    suspect = _suspect("2025/A", "open")
    stats = {module: {
        "confirmed": 0, "review": 0, "review_failed": 0, "review_pending": 0,
        "review_incomplete": 0, "weak": 0, "original": 0, "total": 0,
        "copy_pct": 0, "review_pct": 0, "review_incomplete_pct": 0,
        "original_pct": 0, "top_source": "—",
    } for module in SC.MODULES}
    stats["fs"].update({"confirmed": 1, "original": 1, "total": 2,
                        "copy_pct": .5, "original_pct": .5, "top_source": "2025/A"})
    rendered, _digest = SC.generate_finals_comparison_html(
        query_repo_id="2026/new", closest_source="2025/A", suspects=[suspect],
        submodule_stats=stats, file_pairs=[], analysis_html="", review_pairs=[],
        cleared_review_pairs=[], ai_detect_data={
            "status": "skipped", "reason": "没有可检测函数",
            "scope": {"eligible_functions": 0, "analyzed_functions": 0,
                      "extracted_functions": 0, "borrowed_excluded": 0,
                      "third_party_excluded": 0},
        }, query_repo_path=None,
        linker=None, file_matches=[], file_similar=[], retrieval_contract=None, recall=None,
    )
    assert "经人工智能（AI）分析，与 2025/A 最接近" in rendered
    assert "2025 年 · A 队 · 学校信息未提供" in rendered
    assert "参赛队不得修改" not in rendered
    assert "完全由 AI 工具生成" not in rendered
    assert "实现依据" in rendered
    assert 'href="#sec-clusters">高置信证据</a>' in rendered
    assert 'id="sec-lineage"' in rendered and 'x-data="{open: false}"' in rendered
    assert "Top 8" not in rendered
    assert ">候选创新<" not in rendered
    assert "最接近作品的证据" in rendered
    assert 'id="history-overview"' in rendered
    assert "全历史库匹配概览" in rendered
    assert "Top 5" not in rendered
    assert 'id="ai-signal"' not in rendered
    assert "AI 生成代码辅助信号" not in rendered
    assert 'class="echarts-chart' in rendered
    assert 'data-retrieval-complete="false"' in rendered
    assert "&lt;span id=&quot;retrieval" not in rendered


def test_team_name_already_ending_in_team_suffix_is_not_duplicated():
    rendered, digest = SC.generate_finals_comparison_html(
        query_repo_id="2026/new", closest_source="2025/火箭队",
        suspects=[_suspect("2025/火箭队", "open")],
        submodule_stats={"arch": {
            "confirmed": 1, "review": 0, "review_failed": 0,
            "review_pending": 0, "review_incomplete": 0, "weak": 0,
            "original": 1, "total": 2, "copy_pct": .5,
            "review_pct": 0, "review_incomplete_pct": 0,
            "original_pct": .5, "top_source": "2025/火箭队",
        }},
        file_pairs=[], analysis_html="", review_pairs=[], cleared_review_pairs=[],
        ai_detect_data={
            "status": "skipped", "reason": "无可检测函数",
            "scope": {"eligible_functions": 0, "analyzed_functions": 0,
                      "extracted_functions": 0, "borrowed_excluded": 0,
                      "third_party_excluded": 0},
        },
        query_repo_path=None, linker=None, file_matches=[], file_similar=[],
        retrieval_contract=None, recall=None,
    )

    assert "火箭队 队" not in rendered
    assert "2025 年 火箭队作品" in digest.conclusion


def test_history_overview_selects_strong_sources_without_fixed_count():
    suspects = [
        _suspect("2025/A", "alpha"),
        _suspect("2025/A", "beta"),
        _suspect("2025/A", "gamma"),
        _suspect("2024/B", "alpha"),
        _suspect("2024/B", "beta"),
        _suspect("2023/C", "delta", exact_lines=17),
        {**_suspect("2022/D", "epsilon", exact_lines=16), "tier": "weak"},
        {**_suspect("2021/E", "zeta", exact_lines=15), "tier": "weak"},
        {**_suspect("2020/F", "eta", exact_lines=14), "tier": "weak"},
    ]
    sources = SC._historical_source_metrics(suspects)
    assert [item["repo"] for item in sources] == [
        "2025/A", "2024/B", "2023/C", "2020/F", "2021/E", "2022/D",
    ]
    assert SC.select_closest_historical_repo(suspects) == sources[0]
    assert sources[0]["functions"] == 3
    assert sources[0]["multi_repo_functions"] == 2

    global_stats = SC.compute_submodule_stats(suspects)
    primary_suspects = SC._suspects_for_source(suspects, "2025/A")
    primary_stats = SC.compute_submodule_stats(primary_suspects)
    overview = SC._build_history_overview(
        suspects, global_stats, source_metrics=sources,
    )
    assert [item["repo"] for item in overview["similar_sources"]] == [
        "2025/A", "2024/B", "2023/C",
    ]
    rendered, digest = SC.generate_finals_comparison_html(
        query_repo_id="2026/new",
        closest_source="2025/A",
        suspects=primary_suspects,
        submodule_stats=primary_stats,
        file_pairs=[],
        analysis_html="",
        review_pairs=[],
        cleared_review_pairs=[],
        ai_detect_data={
            "status": "skipped", "reason": "没有可检测函数",
            "scope": {"eligible_functions": 0, "analyzed_functions": 0,
                      "extracted_functions": 0, "borrowed_excluded": 0,
                      "third_party_excluded": 0},
        },
        query_repo_path=None,
        linker=None,
        file_matches=[],
        file_similar=[],
        retrieval_contract=None,
        recall=None,
        history_overview=overview,
    )

    assert "2025/A" in rendered
    for repo in ("2024/B", "2023/C"):
        assert repo in rendered
    for repo in ("2022/D", "2021/E", "2020/F"):
        assert repo not in rendered
    assert "筛出 3 个相似仓库" in rendered
    assert "Top 5" not in rendered
    assert 'id="history-overview"' in rendered
    assert "AI 生成代码辅助信号" not in rendered
    assert digest.metrics["closest_source"] == "2025/A"
    assert digest.metrics["history_sources_shown"] == 3
    assert digest.metrics["confirmed_functions"] == 3
    assert digest.metrics["history_confirmed_functions"] == 4


def test_most_similar_sources_falls_back_to_completed_reviews_only():
    metrics = [
        {"repo": "2025/A", "functions": 0, "exact_files": 0,
         "review_functions": 2, "review_incomplete_functions": 0},
        {"repo": "2024/B", "functions": 0, "exact_files": 0,
         "review_functions": 1, "review_incomplete_functions": 3},
        {"repo": "2023/C", "functions": 0, "exact_files": 0,
         "review_functions": 0, "review_incomplete_functions": 8},
    ]

    assert [item["repo"] for item in SC._most_similar_sources(metrics)] == [
        "2025/A", "2024/B",
    ]


def test_finals_history_overview_rejects_primary_or_total_mismatch():
    suspects = [_suspect("2025/A", "alpha"), _suspect("2024/B", "beta")]
    stats = SC.compute_submodule_stats(suspects)
    overview = SC._build_history_overview(suspects, stats)

    wrong_primary = {**overview, "sources": list(reversed(overview["sources"]))}
    expected_primary = str(overview["sources"][0]["repo"])
    assert wrong_primary["sources"][0]["repo"] != expected_primary
    with pytest.raises(RuntimeError, match="排名第一.*主对比作品"):
        SC._validate_finals_history_overview(wrong_primary, expected_primary)

    wrong_total = {
        **overview,
        "distribution_rows": [dict(row) for row in overview["distribution_rows"]],
    }
    wrong_total["distribution_rows"][0]["count"] += 1
    with pytest.raises(RuntimeError, match="分类数量与总函数数"):
        SC._validate_finals_history_overview(wrong_total, expected_primary)
