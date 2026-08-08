from __future__ import annotations

from datetime import datetime, timedelta, timezone

from finals.development import analyze_history, render_development_html


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


def test_development_analysis_defines_large_commit_threshold_and_stages():
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)
    commits = [
        _commit("a", start, "initial", 50, "kernel/main.c"),
        _commit("b", start + timedelta(days=1), "add filesystem", 1200, "kernel/fs/vfs.c"),
        _commit("c", start + timedelta(days=1, hours=3), "more filesystem", 1300, "kernel/fs/inode.c"),
    ]
    analysis = analyze_history("demo", commits)
    digest = analysis["digest"]
    assert digest.metrics["large_commit_threshold"] == 1000
    assert any(item.title == "单次大规模代码提交" for item in digest.findings)
    assert any("24 小时" in item.title for item in digest.findings)
    assert analysis["stages"][1]["name"] == "文件系统"


def test_low_commit_count_is_clearly_a_heuristic_not_a_violation():
    commits = [_commit("a", datetime.now(timezone.utc), "initial", 10, "main.c")]
    digest = analyze_history("demo", commits)["digest"]
    finding = next(item for item in digest.findings if "提交次数" in item.title)
    assert "需按章程确认" in finding.title
    assert "不等同于比赛违规认定" in finding.detail


def test_development_html_puts_findings_before_stages():
    commits = [_commit("a", datetime.now(timezone.utc), "initial", 10, "main.c")]
    rendered = render_development_html(analyze_history("demo", commits))
    assert rendered.index("结论与问题") < rendered.index("提交阶段")
    assert "大规模提交阈值（LOC）" in rendered
    assert "关键提交与文件" in rendered
