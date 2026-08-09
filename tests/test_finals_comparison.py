from __future__ import annotations

from finals.digests import comparison_digest
from src.report import semantic_compare as SC


def _suspect(repo: str, name: str, module: str = "fs", sim: float = .98) -> dict:
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
        "evidence": {"line_similarity": sim, "exact_match_lines": 18},
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


def test_finals_comparison_html_has_one_source_and_no_top_five_noise():
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
    assert "完全由人工智能（AI）工具生成 · 参赛队不得修改" in rendered
    assert "实现依据" in rendered
    assert 'href="#sec-clusters">高置信证据</a>' in rendered
    assert 'id="sec-lineage"' in rendered and 'x-data="{open: false}"' in rendered
    assert "Top 8" not in rendered
    assert ">候选创新<" not in rendered
    assert "其他候选不进入评委正文" in rendered
