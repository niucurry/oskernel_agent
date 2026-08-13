from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from oskernel_agent.finals.development import (
    analyze_history,
    build_development_evidence,
    render_development_html,
    run_ai_development_analysis,
    validate_ai_development_result,
)


def test_esc_renders_numeric_zero_not_empty():
    """0 变更的关键提交必须渲染为「0 LOC」，而不是空串（str(0 or '') 曾吞掉 0）。"""
    from oskernel_agent.finals.development import _esc

    assert _esc(0) == "0"
    assert _esc(3) == "3"
    assert _esc(None) == ""
    assert _esc("0 LOC") == "0 LOC"


def test_development_cli_default_keeps_only_html(tmp_path, monkeypatch):
    import oskernel_agent.finals.__main__ as cli
    import oskernel_agent.finals.development as development

    output = tmp_path / "development.html"

    def fake_generate(_repo, target, **_kwargs):
        target = target.resolve()
        target.write_text("<html></html>", encoding="utf-8")
        paths = {
            "digest_path": target.with_suffix(".digest.json"),
            "ai_path": target.with_suffix(".ai.json"),
            "evidence_path": target.with_suffix(".evidence.json"),
        }
        for path in paths.values():
            path.write_text("{}", encoding="utf-8")
        return {"html_path": str(target), **{key: str(path) for key, path in paths.items()}}

    monkeypatch.setattr(development, "generate_development_report", fake_generate)

    result = cli.main([
        "development", "--repo", str(tmp_path), "--output", str(output),
    ])

    assert result == 0
    assert {path.name for path in tmp_path.iterdir()} == {"development.html"}


def _commit(sha: str, when: datetime, subject: str, additions: int, path: str) -> dict:
    return {
        "sha": sha * 40,
        "date": when.isoformat(),
        "author": "开发者",
        "subject": subject,
        "additions": additions,
        "deletions": 0,
        "files": [{"path": path, "additions": additions, "deletions": 0}],
    }


def _history() -> list[dict]:
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    return [
        _commit("a", start, "initial", 50, "kernel/main.c"),
        _commit("b", start + timedelta(days=1), "add filesystem", 1200, "kernel/fs/vfs.c"),
        _commit(
            "c",
            start + timedelta(days=1, hours=3),
            "more filesystem",
            1300,
            "kernel/fs/inode.c",
        ),
    ]


def _ai_result(commits: list[dict], evidence: dict, *, report_large: bool = True) -> dict:
    issues = []
    for candidate in evidence["candidates"]:
        candidate_id = candidate["candidate_id"]
        should_report = candidate.get("must_report") or (
            report_large and candidate_id in {"large-commits", "consecutive-large"}
        )
        issues.append(
            {
                "candidate_id": candidate_id,
                "status": "report" if should_report else "dismiss",
                "title": candidate["kind"],
                "analysis": "提交规模和时间间隔需要评委结合现场说明复核。",
                "severity": "high" if should_report else "low",
                "confidence": 91,
                "commit_shas": [sha[:12] for sha in candidate["commit_shas"][:2]],
            }
        )
    return {
        "conclusion": "开发先完成基础框架，随后集中实现文件系统；两次大提交需复核。",
        "issues": issues,
        "stages": [
            {
                "name": "基础框架",
                "conclusion": "建立内核入口和基础目录。",
                "reason": "首个提交是独立的初始导入，后续主题转向文件系统。",
                "confidence": 94,
                "start_sha": commits[0]["sha"][:12],
                "end_sha": commits[0]["sha"][:12],
                "key_shas": [commits[0]["sha"][:12]],
            },
            {
                "name": "文件系统",
                "conclusion": "连续完成 VFS 和 inode 实现。",
                "reason": "提交主题和主要路径都集中在 kernel/fs。",
                "confidence": 96,
                "start_sha": commits[1]["sha"][:12],
                "end_sha": commits[2]["sha"][:12],
                "key_shas": [commits[1]["sha"][:12], commits[2]["sha"][:12]],
            },
        ],
    }


def test_git_facts_define_candidates_but_ai_defines_stages_and_findings():
    commits = _history()
    evidence = build_development_evidence(commits)
    analysis = analyze_history("demo", commits, _ai_result(commits, evidence))
    digest = analysis["digest"]

    assert digest.metrics["large_commit_threshold"] == 1000
    assert {item["candidate_id"] for item in evidence["candidates"]} == {
        "large-commits",
        "consecutive-large",
    }
    assert any(item.title == "大规模代码提交" for item in digest.findings)
    assert any("短时间连续" in item.title for item in digest.findings)
    assert analysis["stages"][1]["name"] == "文件系统"
    assert analysis["stages"][1]["files"][0] == {
        "path": "kernel/fs/inode.c",
        "loc": 1300,
    }


def test_minimum_commit_rule_is_only_applied_when_configured():
    commits = _history()[:1]
    unconfigured = build_development_evidence(commits)
    assert unconfigured["minimum_commits"] is None
    assert "不能判断" in unconfigured["minimum_rule"]
    assert not any(
        item["candidate_id"] == "commit-count" for item in unconfigured["candidates"]
    )

    configured = build_development_evidence(commits, min_commits=5)
    candidate = next(
        item for item in configured["candidates"] if item["candidate_id"] == "commit-count"
    )
    assert candidate["must_report"] is True
    assert "可见提交 1 次" in candidate["fact"]
    assert "最低要求 5 次" in candidate["fact"]


def test_ai_stage_ranges_must_cover_real_commits_without_gaps():
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["stages"][1]["start_sha"] = commits[0]["sha"][:12]

    with pytest.raises(RuntimeError, match="起点必须严格递增"):
        validate_ai_development_result(result, evidence, commits)


def test_ai_cannot_reference_a_fabricated_commit():
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["stages"][0]["key_shas"] = ["deadbee"]

    with pytest.raises(RuntimeError, match="不存在或不唯一"):
        validate_ai_development_result(result, evidence, commits)


def test_ai_extra_key_commits_are_limited_without_changing_their_order():
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["stages"][1]["name"] = "阶段二：文件系统"
    result["stages"][1]["key_shas"] = [
        commits[1]["sha"][:12],
        commits[2]["sha"][:12],
        commits[1]["sha"][:12],
        commits[2]["sha"][:12],
    ]

    validated = validate_ai_development_result(result, evidence, commits)

    assert validated["stages"][1]["name"] == "文件系统"
    assert validated["stages"][1]["key_shas"] == [
        commits[1]["sha"],
        commits[2]["sha"],
    ]


def test_development_html_puts_ai_findings_first_and_shows_exact_evidence():
    commits = _history()
    evidence = build_development_evidence(commits)
    analysis = analyze_history("demo", commits, _ai_result(commits, evidence))
    rendered = render_development_html(analysis)

    assert rendered.index("经 AI 分析，该作品存在以下问题") < rendered.index("提交历史与开发阶段")
    assert "参赛队伍不得修改" not in rendered
    assert "分析依据：Git 提交历史与代码变更记录" in rendered
    assert "关键提交（标题原文）" in rendered
    assert "不代表人工智能（AI）已验证编译、启动或测试成功" in rendered
    assert "AI 排除的候选线索" not in rendered
    assert "<details" not in rendered

    assert "章程最低提交次数未配置" in rendered
    assert "大规模提交口径" in rendered
    assert "kernel/fs/inode.c</code>（1300 LOC）" in rendered
    assert "虚拟文件系统（VFS）" in rendered
    assert rendered.index("关键提交") < rendered.index("kernel/fs/inode.c")


def test_development_rejects_runtime_claims_derived_only_from_git_history():
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["conclusion"] = "提交历史显示最终跑通LTP全量测试点。"

    with pytest.raises(RuntimeError, match="提交历史写成了当前版本"):
        validate_ai_development_result(result, evidence, commits)


def test_development_allows_explicit_runtime_evidence_limitation():
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["conclusion"] = (
        "提交历史显示持续开发；Git 历史不能证明当前版本可编译、可启动或测试通过。"
    )

    validated = validate_ai_development_result(result, evidence, commits)

    assert "不能证明" in validated["conclusion"]


def test_development_allows_conditional_prerequisite_limitation():
    """“需逐项修补才能通过测试”是先决/限制口吻，不是“当前版本已通过”的断言。"""
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["stages"][0]["conclusion"] = (
        "提交历史呈现初始种子导入后的功能修复；主要限制是初始代码在多个子系统上"
        "存在功能缺陷，需逐项修补才能通过测试。"
    )

    validated = validate_ai_development_result(result, evidence, commits)

    assert "需逐项修补" in validated["stages"][0]["conclusion"]


def test_development_allows_pass_rate_metric_phrasing():
    """“测试通过率”是比率指标（整句在说限制），不是“当前版本已通过”的断言。"""
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["stages"][0]["conclusion"] = (
        "提交历史呈现竞赛冲刺阶段；主要限制是多次提交仅修改测试白名单，"
        "反映测试通过率依赖白名单排除而非全部修复。"
    )

    validated = validate_ai_development_result(result, evidence, commits)

    assert "通过率" in validated["stages"][0]["conclusion"]


def test_development_rejects_ellipsis_in_ai_text():
    commits = _history()
    evidence = build_development_evidence(commits)
    result = _ai_result(commits, evidence)
    result["conclusion"] = "提交历史显示开发覆盖了文件系统到 LoongArch 移…"

    with pytest.raises(RuntimeError, match="包含省略号"):
        validate_ai_development_result(result, evidence, commits)


def test_development_lists_only_primary_stage_files_and_links_commit_evidence():
    commits = _history()
    for index in range(8):
        commits[2]["files"].append({
            "path": f"kernel/extra/{index}.c",
            "additions": 10 + index,
            "deletions": 0,
        })
        commits[2]["additions"] += 10 + index
    evidence = build_development_evidence(commits)
    analysis = analyze_history(
        "demo",
        commits,
        _ai_result(commits, evidence),
        repository_url="https://gitlab.example/team/demo",
    )

    rendered = render_development_html(analysis)

    assert "另有 4 个文件已纳入阶段统计" in rendered
    assert "kernel/extra/0.c" not in rendered
    assert "kernel/extra/7.c" in rendered
    assert f'https://gitlab.example/team/demo/-/commit/{commits[1]["sha"]}' in rendered


def test_large_development_evidence_is_attached_instead_of_put_on_command_line(
    tmp_path, monkeypatch
):
    captured = {}

    def fake_run(task, schema_hint="", timeout=0):
        captured["task"] = task
        return {"conclusion": "完成", "issues": [], "stages": []}

    monkeypatch.setattr("oskernel_agent.finals.development.run_batch_task", fake_run)
    evidence = {"timeline": [{"subject": "x" * 1000}] * 100}
    output = tmp_path / "development.html"

    run_ai_development_analysis(tmp_path, "demo", evidence, output)

    task = captured["task"]
    assert len(task.user_request) < 4000
    assert task.input_files == (output.with_suffix(".evidence.json"),)
    assert task.input_files[0].exists()
    assert "x" * 1000 not in task.user_request
    assert "Git 历史不能证明当前版本可编译" in task.user_request
