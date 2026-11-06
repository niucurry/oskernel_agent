import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oskernel_agent.analysis.evidence_plan import (
    check_evidence_plan, load_evidence_plan, plan_requirements, scoped_plan,
)
from oskernel_agent.analysis.read_audit import build_read_audit
from oskernel_agent.engines import llm_batch
from oskernel_agent.engines.evidence_plan import bind_evidence_plan
from oskernel_agent.tools.tool_handlers import read_file


@pytest.fixture
def example(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "request.c").write_text('int request(void) {\n    return retries();\n}\n')
    (repo / "config.h").write_text('#define DEFAULT_RETRIES 3\n')
    raw = {"version": 1, "plan_id": "retry-v1", "items": [{
        "id": "retry", "dimension": "功能性", "question": "默认请求的重试次数是多少？",
        "scope": "默认配置的请求入口", "decision_rule": "核对入口及配置后验证调用次数",
        "score_effect": "先收集证据，行为验证完成前不据此评分", "subsystems": ["网络"],
        "evidence": [{"id": "entry", "purpose": "请求入口", "path": "request.c", "symbol": "request"},
                     {"id": "config", "purpose": "次数配置", "path": "config.h", "start_line": 1, "end_line": 1}],
    }]}
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(raw, ensure_ascii=False))
    return repo, path, raw


def events(repo, include_config=True, session="s1"):
    lines = (repo / "request.c").read_text().splitlines()
    source = f"<path>{repo / 'request.c'}</path>\n<content>\n" + "\n".join(
        f"{i + 1}: {line}" for i, line in enumerate(lines)) + "\n</content>"
    calls = [("read", {}, source)]
    if include_config:
        calls.append(("os-kernel-tools_read_file", {"path": "config.h"}, read_file(str(repo), "config.h")))
    rows = [{"type": "tool_use", "sessionID": session, "part": {"type": "tool", "tool": tool,
        "callID": str(i), "state": {"status": "completed", "input": args, "output": output}}}
        for i, (tool, args, output) in enumerate(calls)]
    rows.append({"type": "step_finish", "sessionID": session, "part": {"reason": "stop"}})
    return "\n".join(json.dumps(row) for row in rows)


def audit(repo, include_config=True):
    return build_read_audit(events(repo, include_config), repo, batch_id="test", run_id="r1", returncode=0)[0]


def test_fixed_targets_measure_native_and_mcp_without_report_references(example):
    repo, path, _ = example
    plan = load_evidence_plan(repo, path)
    assert len(plan_requirements(plan)) == 2
    first = check_evidence_plan(repo, plan, audit(repo, False))
    assert first["items"][0]["returned_count"] == 1
    assert first["items"][0]["evidence"][1]["status"] == "needs_evidence"
    final = check_evidence_plan(repo, plan, audit(repo))
    assert final["status"] == "returned"
    assert final["items"][0]["judgment_status"] == "not_verified"
    assert final["items"][0]["score_status"] == "not_computed"
    assert final["relevant_evidence_recall"] == "unknown"


def test_plan_frozen_before_model_and_bound_to_source_version(example):
    repo, path, raw = example
    plan = load_evidence_plan(repo, path)
    ledger = audit(repo)
    raw["items"][0]["evidence"].pop()
    path.write_text(json.dumps(raw))
    assert len(plan["items"][0]["evidence"]) == 2
    assert load_evidence_plan(repo, path)["plan_sha256"] != plan["plan_sha256"]
    (repo / "config.h").write_text('#define DEFAULT_RETRIES 9\n')
    result = check_evidence_plan(repo, plan, ledger)
    assert result["status"] == "needs_evidence"
    assert result["items"][0]["evidence"][1]["checked_ranges"][0]["status"] == "source_changed_or_unavailable"


@pytest.mark.parametrize("fault", ["inside", "plan_symlink", "escape", "source_symlink", "duplicate_item",
                                  "duplicate_evidence", "empty", "no_rule", "auto_fail", "boolean_line"])
def test_invalid_or_repository_owned_plans_fail_before_model(example, fault):
    repo, path, raw = example
    if fault in {"inside", "plan_symlink"}:
        inside = repo / "rules.json"
        inside.write_text(json.dumps(raw))
        if fault == "plan_symlink":
            path.unlink()
            path.symlink_to(inside)
        else:
            path = inside
    else:
        if fault == "escape":
            raw["items"][0]["evidence"][0]["path"] = "../elsewhere.c"
        elif fault == "source_symlink":
            (repo / "request.c").unlink()
            (repo / "request.c").symlink_to(path)
        elif fault == "duplicate_item":
            raw["items"].append(copy.deepcopy(raw["items"][0]))
        elif fault == "duplicate_evidence":
            raw["items"][0]["evidence"][1]["id"] = "entry"
        elif fault == "empty":
            raw["items"][0]["evidence"] = []
        elif fault == "no_rule":
            raw["items"][0].pop("decision_rule")
        elif fault == "auto_fail":
            raw["items"][0]["on_insufficient"] = "fail"
        elif fault == "boolean_line":
            raw["items"][0]["evidence"][1]["start_line"] = True
        path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_evidence_plan(repo, path)


@pytest.mark.parametrize("fault,reason", [("missing", "missing_or_unreadable"),
    ("ambiguous", "ambiguous_symbol"), ("symbol", "symbol_not_found"),
    ("range", "invalid_range"), ("parser", "unreliable_parse")])
def test_unlocated_evidence_is_preserved_not_counted_as_absent_function(example, fault, reason):
    repo, path, raw = example
    if fault == "missing":
        (repo / "config.h").unlink()
    elif fault == "ambiguous":
        (repo / "request.c").write_text("int request(void) {}\nint request(int a) {}\n")
    elif fault == "symbol":
        raw["items"][0]["evidence"][0]["symbol"] = "nonexistent"
    elif fault == "range":
        raw["items"][0]["evidence"][1]["end_line"] = 99
    else:
        raw["items"][0]["evidence"][0].update(path="config.h", symbol="macro")
        (repo / "config.h").write_text("int broken(\n")
    path.write_text(json.dumps(raw))
    plan = load_evidence_plan(repo, path)
    result = check_evidence_plan(repo, plan, None)
    targets = result["items"][0]["evidence"]
    assert any(t["resolution"] == reason and t["status"] == "unresolved_location" for t in targets)
    assert result["status"] == "needs_evidence"


def test_ambiguous_function_can_be_selected_explicitly_and_subsystems_are_scoped(example):
    repo, path, raw = example
    (repo / "request.c").write_text("int request(void) {}\nint request(int a) {}\n")
    raw["items"][0]["evidence"][0]["start_line"] = 2
    path.write_text(json.dumps(raw))
    plan = load_evidence_plan(repo, path)
    assert plan_requirements(plan)[0]["start_line"] == 2
    assert scoped_plan(plan, "内存管理") is None
    assert len(scoped_plan(plan, "网络")["items"]) == 1
    assert len(scoped_plan(plan)["items"]) == 1


@pytest.mark.parametrize("mode,rounds,status", [("repair", 1, "returned"), ("exhausted", 2, "needs_evidence"),
                                              ("changed", 0, "needs_evidence")])
def test_unmentioned_configuration_is_pinned_and_repaired_with_bounded_budget(example, monkeypatch, mode, rounds, status):
    repo, path, _ = example
    plan = load_evidence_plan(repo, path)
    monkeypatch.setenv("AGENT_READ_AUDIT_DIR", str(repo.parent / "receipts"))
    monkeypatch.setattr(llm_batch, "_opencode_env", lambda task: {})
    task = llm_batch.BatchTask(batch_id="evidence", agent_name="test", user_request="分析实现",
        output_path=repo.parent / "report.json", cache_dir=repo.parent, cache_key="unused",
        repo_path=repo, read_repair_rounds=2)
    bind_evidence_plan(task, plan)
    assert not task.cache_enabled
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) > 1:
            assert "源码读取补证" in " ".join(cmd)
        # The writer neither cites nor admits the missing configuration.
        task.output_path.write_text(json.dumps({"summary": "仅描述入口 request.c:1",
                                                "evidence_plan": {"status": "returned"}}))
        if mode == "changed":
            (repo / "config.h").write_text("#define DEFAULT_RETRIES 9\n")
        text = events(repo, include_config=mode == "repair" and len(calls) > 1, session=f"s{len(calls)}")
        return SimpleNamespace(returncode=0, stdout=text, stderr="")
    monkeypatch.setattr(llm_batch.subprocess, "run", run)
    result = llm_batch.run_batch_task(task)
    assert "config.h" not in result["summary"]
    checked = check_evidence_plan(repo, task.evidence_plan, task.result_read_audit)
    assert checked["status"] == status
    assert len(calls) == rounds + 1
    assert len(task.read_evidence["attempts"]) == rounds + 1
    persisted = json.loads(Path(task.read_evidence["artifact"]).read_text())
    assert persisted["attempts"][0]["evidence_plan"]["status"] == "needs_evidence"
    if mode == "repair":
        assert persisted["attempts"][1]["evidence_plan"]["status"] == "returned"
        assert task.read_audit_runs[0]["session_ids"] != task.result_read_audit["session_ids"]


def test_incomplete_audit_is_not_full_plan_success(example):
    repo, path, _ = example
    ledger = audit(repo)
    ledger["stream_status"] = "partial"
    checked = check_evidence_plan(repo, load_evidence_plan(repo, path), ledger)
    assert checked["status"] == "needs_evidence"
    assert all(t["status"] == "unknown" for t in checked["items"][0]["evidence"])


def test_plan_html_shows_unlocated_evidence_even_without_report_citations(example):
    from oskernel_agent.reports.html_tree import _render_description_scope
    repo, path, _ = example
    (repo / "config.h").unlink()
    checked = check_evidence_plan(repo, load_evidence_plan(repo, path), None)
    html = _render_description_scope({"verdict": {"evidence_plan": checked}})
    assert "评价项的必要证据" in html and "config.h" in html
    assert "文件缺失或不可读取" in html and "尚未定位" in html
    assert "本步骤未计算分数" in html


def test_model_or_old_artifact_cannot_claim_plan_completion(example):
    from oskernel_agent.pipeline.tree_builder import _apply_subsys_result, _clean_tree
    repo, _, _ = example
    node = {"name": "网络", "path": "<subsys>/网络", "evidence_plan": {"status": "returned"},
            "_evidence_plan": {"private": "snapshot"}}
    _apply_subsys_result(node, {"summary": "请求入口", "evidence_plan": {"status": "returned"}}, repo)
    assert "evidence_plan" not in node
    assert "_evidence_plan" not in _clean_tree(node)


def test_production_subsystem_bypasses_old_artifact_and_binds_requirements(example, monkeypatch):
    from oskernel_agent.pipeline import tree_builder
    repo, path, _ = example
    plan = load_evidence_plan(repo, path)
    node = {"name": "网络", "files": [], "_evidence_plan": scoped_plan(plan, "网络")}
    monkeypatch.setattr(tree_builder, "_subsys_fingerprint_matches_one", lambda *a: True)
    def restore(*args):
        pytest.fail("a new evidence checklist must not reuse unaudited old artifacts")
    monkeypatch.setattr(tree_builder, "_restore_one_subsys_from_artifacts", restore)
    class Captured(Exception):
        pass
    def run(task, **kwargs):
        assert len(task.required_source_ranges) == 2
        assert task.evidence_plan["snapshot_sha256"] == plan["snapshot_sha256"]
        assert "评测方预先确定" in task.user_request
        raise Captured
    monkeypatch.setattr(tree_builder, "run_batch_task", run)
    with pytest.raises(Captured):
        tree_builder._process_one_subsys(node, repo, repo.parent, {})


def test_production_verdict_uses_same_frozen_plan_even_if_file_changes(example, monkeypatch):
    from oskernel_agent.pipeline import tree_builder
    repo, path, raw = example
    plan = load_evidence_plan(repo, path)
    raw["items"][0]["evidence"].pop()
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("AGENT_EVIDENCE_PLAN", str(path))
    monkeypatch.setattr(tree_builder, "_ensure_reference_database", lambda *a: None)
    class Captured(Exception):
        pass
    def run(task, **kwargs):
        assert len(task.required_source_ranges) == 2
        assert task.evidence_plan["plan_sha256"] == plan["plan_sha256"]
        raise Captured
    monkeypatch.setattr(tree_builder, "run_batch_task", run)
    with pytest.raises(Captured):
        tree_builder.run_verdict_stage({"children": [], "_evidence_plan": plan}, {}, repo.parent, repo)


def test_trial_native_permissions_cover_opencode_relative_and_external_paths(example, monkeypatch):
    from oskernel_agent.cli import read_audit
    repo, _, _ = example
    monkeypatch.setattr(read_audit.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=str(repo.parent) + "\n"))
    permissions = read_audit._trial_permissions(repo)
    assert permissions["read"]["repo/*"] == "allow"
    assert permissions["read"]["*"] == "deny"
    assert permissions["external_directory"][str(repo)] == "allow"
    assert permissions["external_directory"]["*"] == "deny"
    monkeypatch.setattr(read_audit.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=str(repo) + "\n"))
    assert read_audit._trial_permissions(repo)["read"]["*"] == "allow"
