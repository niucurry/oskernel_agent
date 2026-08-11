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
    runner = (ROOT / "src/oskernel_agent/report_jobs/_runner.py").read_text(encoding="utf-8")

    expected = '["summary", "description", "development", "comparison"]'
    assert expected in report_files
    assert 'activeReportKind = ref("summary")' in app
    assert '"oskernel_agent.report_jobs"' in pipeline
    assert '"--kinds"' in pipeline
    assert "verify_build=True" in runner and "pull_build_image=True" in runner


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
    assert "await cleanupReportDirectory(repo.id);" not in pipeline
    assert '"oskernel_agent.report_jobs"' in pipeline


def test_drain_protected_by_try_finally():
    pipeline = (ROOT / "frontend/server/pipeline.js").read_text(encoding="utf-8")
    start = pipeline.find("async drain()")
    assert start != -1, "drain() not found"
    # next class method at same indentation
    end_marker = pipeline.find("\n  async ", start + 1)
    drain_body = pipeline[start:end_marker] if end_marker != -1 else pipeline[start:]
    assert "try {" in drain_body, "drain() should have try block"
    assert "} finally {" in drain_body, "drain() should have finally block"
    assert "this.running = false" in drain_body


def test_final_report_names_includes_digests():
    report_files = (ROOT / "frontend/server/reportFiles.js").read_text(encoding="utf-8")
    for name in ("description.digest.json", "development.digest.json", "comparison.digest.json"):
        assert f'"{name}"' in report_files, f"{name} should be in FINAL_REPORT_NAMES"
    assert '".report_jobs_state.json"' in report_files


def test_recover_interrupted_jobs_no_longer_calls_cleanup_on_all_repos():
    pipeline = (ROOT / "frontend/server/pipeline.js").read_text(encoding="utf-8")
    assert "for (const repo of repositories)" not in pipeline, \
        "recoverInterruptedJobs should not iterate all repos for cleanup"
