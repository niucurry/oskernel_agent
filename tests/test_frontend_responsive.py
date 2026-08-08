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
    assert 'path.join(reportDir(repo.id), "summary.pdf")' in pipeline
    assert '"-m",\n        "finals",\n        "development"' in pipeline
    assert '"--description-digest"' in pipeline
