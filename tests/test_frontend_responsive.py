from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_mobile_detail_is_visible_and_scrolled():
    app = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    css = (ROOT / "frontend/src/styles.css").read_text(encoding="utf-8")
    assert 'ref="detailPane"' in app and "scrollIntoView" in app
    assert "@media (max-width: 768px)" in css
    assert "100dvh" in css and "order: -1" in css


def test_frontend_exposes_finals_reports_in_judge_order():
    app = (ROOT / "frontend/src/App.vue").read_text(encoding="utf-8")
    report_files = (ROOT / "frontend/server/reportFiles.js").read_text(encoding="utf-8")
    pipeline = (ROOT / "frontend/server/pipeline.js").read_text(encoding="utf-8")

    expected = '["summary", "description", "development", "comparison"]'
    assert expected in report_files
    assert 'activeReportKind = ref("summary")' in app
    assert '"oskernel_agent.report_jobs"' in pipeline
    assert '"--kinds"' in pipeline


def test_frontend_always_cleans_intermediates_and_keeps_digests_internal():
    report_files = (ROOT / "frontend/server/reportFiles.js").read_text(encoding="utf-8")
    pipeline = (ROOT / "frontend/server/pipeline.js").read_text(encoding="utf-8")

    for name in (
        "summary.pdf",
        "description.html",
        "development.html",
        "comparison.html",
    ):
        assert f'"{name}"' in report_files
    assert "await this.runJobImplementation(id, job);" in pipeline
    assert "finally" in pipeline
    assert "await cleanupReportDirectory(job.repo_id);" in pipeline
    assert "await cleanupReportDirectory(repo.id);" in pipeline
    assert '"oskernel_agent.report_jobs"' in pipeline
