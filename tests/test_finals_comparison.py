from __future__ import annotations

import json
import re

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


def test_finals_comparison_html_has_all_history_overview_and_one_detailed_source():
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
    assert "经 AI 分析，与 2025/A 最接近" in rendered
    assert "2025 年 · A 队 · 学校信息未提供" in rendered
    assert "参赛队伍不得修改" in rendered
    assert "实现依据" in rendered
    assert 'href="#sec-clusters">高置信证据</a>' in rendered
    assert 'id="sec-lineage"' in rendered and 'x-data="{open: false}"' in rendered
    assert "Top 8" not in rendered
    assert ">候选创新<" not in rendered
    assert "全历史库匹配概览" in rendered
    assert "历史匹配作品 Top 1" in rendered
    assert "详细证据口径" in rendered
    assert "其他候选不进入评委正文" not in rendered


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


def test_history_source_ranking_drives_top_five_chart_table_and_primary_digest():
    suspects = [
        _suspect("2025/A", "alpha"),
        _suspect("2025/A", "beta"),
        _suspect("2025/A", "gamma"),
        _suspect("2024/B", "alpha"),
        _suspect("2024/B", "beta"),
        _suspect("2023/C", "delta", exact_lines=17),
        _suspect("2022/D", "epsilon", exact_lines=16),
        _suspect("2021/E", "zeta", exact_lines=15),
        _suspect("2020/F", "eta", exact_lines=14),
    ]
    sources = SC._historical_source_metrics(suspects)
    assert [item["repo"] for item in sources] == [
        "2025/A", "2024/B", "2023/C", "2022/D", "2021/E", "2020/F",
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

    assert "历史匹配作品 Top 5" in rendered
    for repo in ("2025/A", "2024/B", "2023/C", "2022/D", "2021/E"):
        assert repo in rendered
    assert "2020/F" not in rendered
    assert "多仓重复函数" in rendered
    assert digest.metrics["closest_source"] == "2025/A"
    assert digest.metrics["confirmed_functions"] == 3
    assert digest.metrics["history_confirmed_functions"] == 7
    assert digest.metrics["history_sources_shown"] == 5

    chart_payloads = [
        json.loads(blob)
        for blob in re.findall(
            r'<script type="application/json">(.*?)</script>', rendered, re.DOTALL,
        )
    ]
    source_chart = next(
        payload for payload in chart_payloads
        if (payload.get("yAxis") or {}).get("data")
        == ["2021/E", "2022/D", "2023/C", "2024/B", "2025/A"]
    )
    confirmed_series = next(
        series for series in source_chart["series"]
        if series["name"] == "高置信同源代码"
    )
    assert confirmed_series["data"] == [1, 1, 1, 2, 3]


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
