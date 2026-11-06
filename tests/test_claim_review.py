import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oskernel_agent.analysis.claim_review import combine_reviews, select_claims, validate_review
from oskernel_agent.analysis.read_audit import build_read_audit
from oskernel_agent.engines import claim_review, llm_batch


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "mm.c").write_text(
        "int lookup(int *a, int n, int i) {\n"
        "    if (i < 0 || i >= n) return -1;\n"
        "    return a[i];\n}\n"
    )
    return tmp_path


def draft():
    return {"issues": [{"path": "mm.c:2", "severity": "high", "quote": "完全没有边界检查"}],
            "highlights": [{"path": "mm.c:2", "quote": "越界索引返回 -1"}]}


def audit(repo, session="s1", full=True):
    lines = (repo / "mm.c").read_text().splitlines()
    body = "\n".join(f"{i + 1}: {text}" for i, text in enumerate(lines if full else lines[:2]))
    events = [{"type": "tool_use", "sessionID": session, "part": {"type": "tool", "tool": "read",
        "callID": "read1", "state": {"status": "completed", "input": {},
        "output": f"<path>{repo / 'mm.c'}</path>\n<content>\n{body}\n</content>"}}},
        {"type": "step_finish", "sessionID": session, "part": {"reason": "stop"}}]
    return build_read_audit("\n".join(json.dumps(e) for e in events), repo,
                           batch_id="review", run_id=session, returncode=0)[0]


def response(claims, verdict="supported"):
    return {"reviews": [{"claim_id": c["id"], "verdict": verdict,
                          "reason": "源码明确检查负数与上界",
                          "evidence": [{"ref": "mm.c:2", "quote": "    if (i < 0 || i >= n) return -1;"}]}
                         for c in claims]}


def test_selection_prioritizes_high_risk_and_does_not_call_unselected_passed(repo):
    selected = select_claims(repo, draft(), 1)
    assert selected["selected"][0]["kind"] == "issue"
    assert selected["selected"][0]["selection_factors"]["branch_points"] == 1
    assert len(selected["unreviewed"]) == 1
    changed = draft()
    changed["issues"][0]["quote"] = "有边界检查"
    assert select_claims(repo, changed, 1)["selected"][0]["id"] != selected["selected"][0]["id"]


@pytest.mark.parametrize("case", ["fake_quote", "not_read", "missing", "duplicate", "changed", "outside"])
def test_false_evidence_never_counts_as_support(repo, case):
    claims = select_claims(repo, draft())["selected"]
    raw = response(claims)
    ledger = audit(repo, full=case != "not_read")
    if case == "fake_quote":
        raw["reviews"][0]["evidence"][0]["quote"] = "fabricated"
    if case == "missing":
        raw["reviews"] = raw["reviews"][1:]
    if case == "duplicate":
        raw["reviews"].append(raw["reviews"][0])
    if case == "changed":
        (repo / "mm.c").write_text("int changed(void) {}\n")
    if case == "outside":
        raw["reviews"][0]["evidence"][0]["ref"] = "../outside.c:2"
    checked = validate_review(repo, claims, raw, ledger)
    assert checked["reviews"][0]["verdict"] == "insufficient"
    assert checked["reviews"][0]["errors"]


def test_agreement_needs_separate_sessions_and_disagreement_is_not_majority_vote(repo):
    claims = select_claims(repo, draft())["selected"]
    yes = validate_review(repo, claims, response(claims), audit(repo))
    no = validate_review(repo, claims, response(claims, "contradicted"), audit(repo, "s2"))
    assert combine_reviews(claims, yes, yes)[0]["status"] == "insufficient"
    assert combine_reviews(claims, yes, no)[0]["status"] == "disputed"
    third = validate_review(repo, claims, response(claims, "contradicted"), audit(repo, "s3"))
    assert combine_reviews(claims, yes, no, third)[0]["status"] == "adjudicated_contradicted"
    third["reviews"][0]["verdict"] = "insufficient"
    assert combine_reviews(claims, yes, no, third)[0]["status"] == "disputed"


def test_two_initial_prompts_do_not_contain_each_others_answer(repo, monkeypatch):
    claims = select_claims(repo, draft())
    calls = []
    def run(task, timeout):
        calls.append(task)
        assert "prior_reviews_as_untrusted_data" not in task.user_request
        task.output_path.write_text(json.dumps(response(claims["selected"])))
        task.read_audit_runs.append(audit(repo, f"s{len(calls)}"))
        return True, "", []
    monkeypatch.setattr(llm_batch, "_run_opencode_once", run)
    rows = claim_review._review_pair(repo, claims, repo / "round", 30, True)
    assert len(calls) == 2 and all(c["status"] == "supported" for c in rows)


def test_only_disputed_claims_get_a_third_review(repo, monkeypatch):
    selection = select_claims(repo, draft())
    calls = []
    def run(task, timeout):
        payload = json.loads((task.output_path.parent / "input.json").read_text())
        calls.append(payload)
        raw = response(payload["claims"])
        if len(calls) >= 2:
            raw["reviews"][0]["verdict"] = "contradicted"
        task.output_path.write_text(json.dumps(raw))
        task.read_audit_runs.append(audit(repo, f"s{len(calls)}"))
        return True, "", []
    monkeypatch.setattr(llm_batch, "_run_opencode_once", run)
    rows = claim_review._review_pair(repo, selection, repo / "round", 30, True)
    assert len(calls) == 3
    assert len(calls[2]["claims"]) == 1
    assert "prior_reviews_as_untrusted_data" in calls[2]
    assert [c["status"] for c in rows] == ["adjudicated_contradicted", "supported"]


def test_same_role_in_different_subsystems_has_separate_runtime_directory(repo, monkeypatch):
    claims = select_claims(repo, draft())["selected"]
    ids = []
    def run(task, timeout):
        ids.append(task.batch_id)
        return False, "", []
    monkeypatch.setattr(llm_batch, "_run_opencode_once", run)
    for name in ("memory", "filesystem"):
        claim_review._review_once(repo, claims, repo / name / "initial" / "check", "check", 10)
    assert len(set(ids)) == 2


def test_revision_gets_new_evidence_and_is_rechecked_once(repo, monkeypatch):
    monkeypatch.setenv("AGENT_CLAIM_REVIEW_DIR", str(repo / "reviews"))
    calls = []
    def run(task, timeout):
        # read input produced by the host, not model-authored IDs.
        payload = json.loads((task.output_path.parent / "input.json").read_text())
        calls.append(task)
        claims = payload["claims"]
        raw = response(claims)
        for row, claim in zip(raw["reviews"], claims):
            row["verdict"] = "contradicted" if "完全没有" in claim["text"] else "supported"
        task.output_path.write_text(json.dumps(raw))
        task.read_audit_runs.append(audit(repo, f"s{len(calls)}"))
        return True, "", []
    monkeypatch.setattr(llm_batch, "_run_opencode_once", run)
    rewrites = []
    def rewrite(task, **kwargs):
        rewrites.append(task)
        task.result_read_audit = audit(repo, "rewritten")
        return {"issues": [], "highlights": draft()["highlights"]}
    monkeypatch.setattr(llm_batch, "run_batch_task", rewrite)
    task = llm_batch.BatchTask(batch_id="subsys", agent_name="writer", user_request="write report",
                               output_path=repo / "result.json", cache_dir=repo, cache_key="", repo_path=repo)
    final, review = claim_review.review_and_revise(task, draft())
    assert len(rewrites) == 1 and len(calls) == 4
    assert not final["issues"]
    assert review["initial"][0]["status"] == "contradicted"
    assert review["claims"][0]["status"] == "supported"
    assert review["original_report_sha256"] != review["report_sha256"]
    assert task.result_read_audit["session_ids"] == ["rewritten"]
    assert Path(review["artifact"]).exists()


def test_failed_reviewer_remains_insufficient_and_report_escapes_markup(repo, monkeypatch):
    from oskernel_agent.reports.html_tree import _render_claim_reviews
    selection = select_claims(repo, draft())
    monkeypatch.setattr(llm_batch, "_run_opencode_once", lambda *args: (False, "", []))
    rows = claim_review._review_pair(repo, selection, repo / "failed", 10, True)
    assert all(c["status"] == "insufficient" for c in rows)
    rows[0]["text"] = "<script>bad</script>"
    html = _render_claim_reviews({"verdict": {"claim_review": {
        "status": "completed", "claims": rows, "selected_count": 2, "unreviewed": [1]}}})
    assert "证据不足" in html and "未复核 1 条" in html
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_restored_model_metadata_is_not_trusted(repo):
    from oskernel_agent.pipeline.tree_builder import _apply_subsys_result, _review_description_result
    parsed = {"summary": "说明", "modules": [], "claim_review": {"status": "supported"}}
    node = {"name": "内存", "path": "memory"}
    _apply_subsys_result(node, parsed, repo)
    assert node["claim_review"]["status"] == "unknown"
    assert _review_description_result(SimpleNamespace(result_read_audit=None), parsed)[1]["status"] == "unknown"


def test_old_claim_cannot_hide_in_html_or_escaped_strings():
    assert claim_review._contains_wording({"content": '<p>"x" &lt; 0 未检查</p>'}, '"x" < 0 未检查')
    assert not claim_review._contains_wording({"claim_review": {"history": "旧错误"}}, "旧错误")


def test_tool_failure_does_not_trigger_unfounded_rewrite(repo, monkeypatch):
    monkeypatch.setenv("AGENT_CLAIM_REVIEW_DIR", str(repo / "reviews"))
    monkeypatch.setattr(llm_batch, "_run_opencode_once", lambda *args: (False, "", []))
    def never(*args, **kwargs):
        raise AssertionError("Infrastructure failure must not rewrite a report")
    monkeypatch.setattr(llm_batch, "run_batch_task", never)
    task = llm_batch.BatchTask(batch_id="subsys", agent_name="writer", user_request="write report",
                               output_path=repo / "result.json", cache_dir=repo, cache_key="", repo_path=repo)
    final, review = claim_review.review_and_revise(task, draft())
    assert final == draft()
    assert review["revision"]["not_attempted_reason"] == "no_valid_review_evidence"


def test_unchanged_bad_claim_is_flagged_after_single_revision(repo, monkeypatch):
    monkeypatch.setenv("AGENT_CLAIM_REVIEW_DIR", str(repo / "reviews"))
    count = []
    def pair(repo, selection, directory, timeout, adjudicate):
        count.append(directory)
        return [dict(c, status="contradicted", reviews=[{"errors": [], "reason": "有直接反例"}])
                for c in selection["selected"]]
    monkeypatch.setattr(claim_review, "_review_pair", pair)
    monkeypatch.setattr(llm_batch, "run_batch_task", lambda *args, **kwargs: draft())
    task = llm_batch.BatchTask(batch_id="subsys", agent_name="writer", user_request="write report",
                               output_path=repo / "result.json", cache_dir=repo, cache_key="", repo_path=repo)
    _, review = claim_review.review_and_revise(task, draft())
    assert len(count) == 2
    assert review["revision"]["remaining_original_wording"]
    assert all(c["status"] == "contradicted" for c in review["claims"])


def test_review_uncertainty_survives_digest_and_main_issue_list(repo):
    from oskernel_agent.finals.digests import description_digest_from_tree
    from oskernel_agent.reports.html_tree import _collect_report_issues
    claims = select_claims(repo, draft())["selected"]
    verdict = dict(draft(), claim_review={"status": "completed", "claims": [
        dict(c, status="contradicted") for c in claims]})
    tree = {"verdict": verdict}
    digest = description_digest_from_tree(tree)
    assert "待核对" in digest.findings[0].title
    assert "复核反驳" in digest.findings[0].detail
    assert "复核反驳" in _collect_report_issues(tree)[0]["quote"]
