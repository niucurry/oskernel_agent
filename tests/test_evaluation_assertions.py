import copy
import json
from types import SimpleNamespace

import pytest

from oskernel_agent.analysis.claim_review import report_hash, select_claims, validate_review
from oskernel_agent.analysis.evaluation_assertions import assertion_shape_errors, check_assertions
from oskernel_agent.analysis.evidence_plan import load_evidence_plan
from oskernel_agent.analysis.read_audit import build_read_audit
from oskernel_agent.engines import claim_review, llm_batch
from oskernel_agent.engines.evidence_plan import bind_evidence_plan
from oskernel_agent.tools.tool_handlers import read_file


@pytest.fixture
def example(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "retry.c").write_text("int send(void) {\n    return invoke(RETRIES);\n}\n")
    (repo / "settings.yaml").write_text("retries: 3\n")
    raw = {"version": 1, "plan_id": "retry", "items": [{"id": "count", "dimension": "功能性",
        "question": "请求总共调用几次？", "scope": "默认配置的失败请求", "decision_rule": "核对循环与次数配置",
        "score_effect": "独立测试前不评分", "evidence": [
            {"id": "entry", "purpose": "请求函数", "path": "retry.c", "symbol": "send"},
            {"id": "config", "purpose": "次数配置", "path": "settings.yaml", "start_line": 1, "end_line": 1}]}]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw, ensure_ascii=False))
    plan = load_evidence_plan(repo, path)
    report = {"summary": "请求路径见 retry.c:1。", "evaluation_claims": [{
        "item_id": "count", "statement": "总共调用 4 次", "scope": raw["items"][0]["scope"],
        "conditions": ["每次请求均失败"], "counterevidence": ["调用次数可能由 invoke 的实现进一步限制"],
        "evidence": [{"evidence_id": "entry", "ref": "retry.c:2", "quote": "    return invoke(RETRIES);"},
                     {"evidence_id": "config", "ref": "settings.yaml:1", "quote": "retries: 3"}],
        "missing_evidence": [], "limitations": "", "assessment": "source_supported", "validation_need": "execution"}]}
    return repo, plan, report


def receipt(repo, *, session="a", config=True, partial_function=False):
    lines = (repo / "retry.c").read_text().splitlines()
    if partial_function:
        lines = lines[:2]
    output = f"<path>{repo / 'retry.c'}</path>\n<content>\n" + "\n".join(
        f"{i + 1}: {v}" for i, v in enumerate(lines)) + "\n</content>"
    calls = [("read", {}, output)]
    if config:
        calls.append(("os-kernel-tools_read_file", {"path": "settings.yaml"}, read_file(str(repo), "settings.yaml")))
    events = [{"type": "tool_use", "sessionID": session, "part": {"type": "tool", "tool": tool,
        "callID": str(i), "state": {"status": "completed", "input": args, "output": text}}}
        for i, (tool, args, text) in enumerate(calls)]
    events.append({"type": "step_finish", "sessionID": session, "part": {"reason": "stop"}})
    return build_read_audit("\n".join(json.dumps(e) for e in events), repo,
                            batch_id="claims", run_id=session, returncode=0)[0]


def test_source_checks_do_not_pretend_to_prove_semantics_or_execution(example):
    repo, plan, report = example
    # invoke is not provided. Genuine quotes and a complete checklist alone do
    # not establish the answer. An independent reviewer must spot this gap.
    checked = check_assertions(repo, plan, report, receipt(repo))
    row = checked["items"][0]
    assert row["status"] == "ready_for_review"
    assert row["execution_status"] == "not_run" and row["score_status"] == "not_computed"
    assert row["scope"] == plan["items"][0]["scope"]
    changed = copy.deepcopy(report)
    changed["evaluation_claims"][0]["statement"] = "总共调用 3 次"
    other = check_assertions(repo, plan, changed, receipt(repo))["items"][0]
    assert other["status"] == "ready_for_review" and other["claim_id"] != row["claim_id"]


@pytest.mark.parametrize("fault", ["missing", "duplicate", "unknown", "scope", "conditions", "assessment",
                                  "unhashable_id", "unhashable_evidence_id", "unhashable_assessment",
                                  "unhashable_validation_need", "missing_but_supported"])
def test_each_item_requires_one_scoped_assertion(example, fault):
    repo, plan, report = example
    row = report["evaluation_claims"][0]
    if fault == "missing":
        report["evaluation_claims"] = []
    elif fault == "duplicate":
        report["evaluation_claims"].append(copy.deepcopy(row))
    elif fault == "unknown":
        row["item_id"] = "invented"
    elif fault == "scope":
        row["scope"] = "所有配置的所有入口"
    elif fault == "conditions":
        row["conditions"] = []
    elif fault == "assessment":
        row["assessment"] = "tests_passed"
    elif fault == "unhashable_id":
        row["item_id"] = []
    elif fault == "unhashable_evidence_id":
        row["evidence"][0]["evidence_id"] = []
    elif fault == "unhashable_assessment":
        row["assessment"] = []
    elif fault == "unhashable_validation_need":
        row["validation_need"] = []
    else:
        row["missing_evidence"] = ["config"]
    assert assertion_shape_errors(plan, report)
    checked = check_assertions(repo, plan, report, receipt(repo))
    assert checked["items"][0]["status"] == "insufficient"


@pytest.mark.parametrize("fault,error", [("fake_quote", "unverified_evidence"), ("missing_quote", "missing_target_quotes"),
    ("wrong_target", "quote_not_in_declared_target"), ("not_read", "required_evidence_not_returned"),
    ("partial_function", "required_evidence_not_returned"), ("changed", "required_evidence_not_returned"),
    ("incomplete", "incomplete_author_session")])
def test_assertion_evidence_must_match_its_target_version_and_real_reads(example, fault, error):
    repo, plan, report = example
    ledger = receipt(repo, config=fault != "not_read", partial_function=fault == "partial_function")
    if fault == "fake_quote":
        report["evaluation_claims"][0]["evidence"][1]["quote"] = "retries: 99"
    elif fault == "missing_quote":
        report["evaluation_claims"][0]["evidence"].pop()
    elif fault == "wrong_target":
        report["evaluation_claims"][0]["evidence"][1]["evidence_id"] = "entry"
    elif fault == "changed":
        (repo / "settings.yaml").write_text("retries: 7\n")
    elif fault == "incomplete":
        ledger["stream_status"] = "partial"
    row = check_assertions(repo, plan, report, ledger)["items"][0]
    assert row["status"] == "insufficient" and error in row["errors"]


def test_insufficient_is_a_valid_response_and_never_a_support_vote(example):
    repo, plan, report = example
    report["evaluation_claims"][0].update(assessment="insufficient", missing_evidence=["config"],
                                         limitations="未获取次数配置，不能确认", evidence=[])
    assert not assertion_shape_errors(plan, report)
    checked = check_assertions(repo, plan, report, receipt(repo, config=False))
    assert checked["items"][0]["status"] == "insufficient"
    selected = select_claims(repo, report, assertions=checked)
    assert all(c["kind"] != "evaluation_assertion" for c in selected["selected"])


def test_review_binds_statement_conditions_and_whole_report(example):
    repo, plan, report = example
    ledger = receipt(repo)
    checked = check_assertions(repo, plan, report, ledger)
    review = {"report_sha256": report_hash(report), "claims": [{"id": checked["items"][0]["claim_id"], "status": "supported"}]}
    assert check_assertions(repo, plan, report, ledger, review)["items"][0]["status"] == "supported"
    for field, value in [("statement", "总共调用 3 次"), ("conditions", ["每次请求均成功"])]:
        changed = copy.deepcopy(report)
        changed["evaluation_claims"][0][field] = value
        row = check_assertions(repo, plan, changed, ledger, review)["items"][0]
        assert row["claim_id"] != checked["items"][0]["claim_id"]
        assert row["status"] == "not_reviewed"
    report["summary"] = "正文发生变化"
    assert check_assertions(repo, plan, report, ledger, review)["items"][0]["status"] == "not_reviewed"


def test_reviewer_gets_full_required_context_even_when_only_statement_line_is_cited(example, monkeypatch):
    repo, plan, report = example
    checked = check_assertions(repo, plan, report, receipt(repo))
    selection = select_claims(repo, report, 1, assertions=checked)
    claim = selection["selected"][0]
    assert claim["kind"] == "evaluation_assertion"
    assert {r["path"] for r in claim["requirements"]} == {"retry.c", "settings.yaml"}
    def run(task, timeout):
        payload = json.loads((task.output_path.parent / "input.json").read_text())
        context = payload["claims"][0]["assertion_context"]
        assert context["conditions"] == ["每次请求均失败"]
        assert context["scope"] == plan["items"][0]["scope"]
        response = {"reviews": [{"claim_id": claim["id"], "verdict": "supported", "reason": "配置是3",
                                 "evidence": [{"ref": "retry.c:2", "quote": "    return invoke(RETRIES);"}]}]}
        task.output_path.write_text(json.dumps(response))
        task.read_audit_runs.append(receipt(repo, session="review", config=False))
        return True, "", []
    monkeypatch.setattr(llm_batch, "_run_opencode_once", run)
    review = claim_review._review_once(repo, [claim], repo.parent / "review", "check", 10)
    assert review["reviews"][0]["verdict"] == "insufficient"
    assert "claim_source_not_fully_returned" in review["reviews"][0]["errors"]


def test_yaml_quote_is_accepted_in_independent_review(example):
    repo, plan, report = example
    checked = check_assertions(repo, plan, report, receipt(repo))
    claim = select_claims(repo, report, 1, assertions=checked)["selected"][0]
    response = {"reviews": [{"claim_id": claim["id"], "verdict": "insufficient", "reason": "缺少 invoke 的实现",
                            "evidence": [{"ref": "settings.yaml:1", "quote": "retries: 3"}]}]}
    review = validate_review(repo, [claim], response, receipt(repo))
    assert review["reviews"][0]["errors"] == []


def test_shape_retry_does_not_accept_a_missing_evaluation_item(example, monkeypatch):
    repo, plan, report = example
    task = llm_batch.BatchTask(batch_id="assertion", agent_name="writer", user_request="生成报告",
        output_path=repo.parent / "report.json", cache_dir=repo.parent, cache_key="", repo_path=repo,
        cache_validator=lambda r: True if r.get("summary") else "缺摘要")
    bind_evidence_plan(task, plan, with_assertions=True)
    calls = []
    def run(current, timeout):
        calls.append(current.user_request)
        value = {"summary": "第一轮漏掉断言"} if len(calls) == 1 else report
        current.output_path.write_text(json.dumps(value))
        current.read_audit_runs.append(receipt(repo, session=f"author-{len(calls)}"))
        return True, "", []
    monkeypatch.setattr(llm_batch, "_run_opencode_once", run)
    final = llm_batch.run_batch_task(task)
    assert len(calls) == 2 and "评价断言格式无效" in calls[1]
    assert not assertion_shape_errors(plan, final)
    assert task.result_read_audit["session_ids"] == ["author-2"]
    assert task.cache_validator({"evaluation_claims": []}) == "缺摘要"


def test_invalid_assertions_stop_production_after_bounded_shape_retry(example):
    from oskernel_agent.pipeline.tree_builder import _review_description_result
    repo, plan, _ = example
    task = SimpleNamespace(evaluation_assertions_enabled=True, evidence_plan=plan, result_read_audit=None)
    with pytest.raises(RuntimeError, match="评价断言未形成"):
        _review_description_result(task, {"summary": "漏答"})


def test_html_retains_scope_conditions_and_pending_execution(example):
    from oskernel_agent.reports.html_tree import _render_description_scope
    repo, plan, report = example
    checked = check_assertions(repo, plan, report, receipt(repo))
    html = _render_description_scope({"verdict": {"evaluation_assertions": checked}})
    assert "默认配置的失败请求" in html and "每次请求均失败" in html
    assert "来源核对通过，待语义复核" in html and "执行验证：未运行" in html
    assert "settings.yaml:1-1" in html


def test_budget_limited_unselected_assertion_remains_not_reviewed(example):
    repo, plan, report = example
    for i in range(1, 7):
        item = copy.deepcopy(plan["items"][0])
        item["id"] = f"count-{i}"
        plan["items"].append(item)
        row = copy.deepcopy(report["evaluation_claims"][0])
        row["item_id"] = item["id"]
        report["evaluation_claims"].append(row)
    checked = check_assertions(repo, plan, report, receipt(repo))
    selected = select_claims(repo, report, 6, assertions=checked)
    assert len(selected["selected"]) == 6
    review = {"report_sha256": report_hash(report), "claims": [
        {"id": c["id"], "status": "supported"} for c in selected["selected"]]}
    final = check_assertions(repo, plan, report, receipt(repo), review)
    assert [i["status"] for i in final["items"]].count("supported") == 6
    assert [i["status"] for i in final["items"]].count("not_reviewed") == 1


def test_production_verdict_preserves_verified_assertions_before_score_normalization(example, monkeypatch):
    from oskernel_agent.pipeline import tree_builder
    repo, plan, report = example
    report.update(dimensions=[{"name": name, "score": 7, "reason": "测试原因"}
                             for name in tree_builder.VERDICT_DIMENSIONS], score_total=70)
    for name in ("_ensure_reference_database", "_drop_unresolvable_evidence", "_validate_verdict_result",
                 "_validate_similarity_result", "_validate_hardcode_reviews"):
        monkeypatch.setattr(tree_builder, name, lambda *a, **kw: None)
    monkeypatch.setattr(tree_builder, "_hardcode_reviews_needing_repair", lambda *a: [])
    monkeypatch.setattr(tree_builder, "_verdict_one_line_requires_repair", lambda *a: False)
    def run(task, **kw):
        assert task.evaluation_assertions_enabled
        task.result_read_audit = receipt(repo)
        return report
    def review(task, value):
        checked = check_assertions(repo, plan, value, task.result_read_audit)
        return value, {"report_sha256": report_hash(value), "claims": [
            {"id": checked["items"][0]["claim_id"], "status": "supported"}]}
    monkeypatch.setattr(tree_builder, "run_batch_task", run)
    monkeypatch.setattr(tree_builder, "_review_description_result", review)
    final = tree_builder.run_verdict_stage({"children": [], "_evidence_plan": plan}, {}, repo.parent, repo)
    assert final["dimensions"][0]["score"] == 70
    assert final["evaluation_assertions"]["items"][0]["status"] == "supported"
    assert final["evaluation_assertions"]["items"][0]["execution_status"] == "not_run"
    assert "evaluation_claims" not in final


def test_assertion_revision_retains_plan_and_rechecks_new_statement(example, monkeypatch):
    repo, plan, report = example
    task = llm_batch.BatchTask(batch_id="scoped", agent_name="test", user_request="生成报告",
        output_path=repo.parent / "output.json", cache_dir=repo.parent, cache_key="", repo_path=repo)
    bind_evidence_plan(task, plan, with_assertions=True)
    task.result_read_audit = receipt(repo)
    monkeypatch.setenv("AGENT_CLAIM_REVIEW_DIR", str(repo.parent / "reviews"))
    rounds = []
    def pair(repo, selection, directory, timeout, adjudicate):
        rounds.append(selection)
        return [dict(c, status="contradicted" if len(rounds) == 1 else "supported",
                     reviews=[{"errors": [], "reason": "核对后的意见"}]) for c in selection["selected"]]
    def rewrite(candidate, **kwargs):
        assert candidate.evaluation_assertions_enabled and candidate.evidence_plan == task.evidence_plan
        corrected = copy.deepcopy(report)
        corrected["evaluation_claims"][0]["statement"] = "配置写明重试次数为 3；具体调用次数仍需核对实现"
        assert candidate.cache_validator(corrected) is True
        candidate.result_read_audit = receipt(repo, session="revised")
        return corrected
    monkeypatch.setattr(claim_review, "_review_pair", pair)
    monkeypatch.setattr(llm_batch, "run_batch_task", rewrite)
    final, review = claim_review.review_and_revise(task, report, limit=1)
    assert len(rounds) == 2 and review["revision"]["attempted"]
    assert rounds[0]["selected"][0]["id"] != rounds[1]["selected"][0]["id"]
    state = check_assertions(repo, plan, final, task.result_read_audit, review)
    assert state["items"][0]["status"] == "supported"
    assert state["run_id"] == "revised"
