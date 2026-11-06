import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from oskernel_agent.analysis.read_coverage import (
    check_requirements, read_requirements, source_functions,
)
from oskernel_agent.engines import llm_batch


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "mm.c").write_text(
        "int alloc(void) {\n"
        + "".join(f"  int x{i} = {i};\n" for i in range(2, 301)) + "  return x2;\n}\n"
    )
    return tmp_path


def test_reference_expands_to_full_function_and_merged_ranges(repo):
    requirements = read_requirements(repo, {"summary": "mm.c:250"})
    req = requirements[0]
    assert (req["symbol"], req["start_line"], req["end_line"]) == ("alloc", 1, 302)
    audit = {"stream_status": "complete", "files": [dict(
        path="mm.c", sha256=req["sha256"], returned_ranges=[[1, 80], [263, 302]])]}
    result = check_requirements(repo, audit, requirements)[0]
    assert result["status"] == "not_observed"
    assert result["missing_ranges"] == [[81, 262]]
    audit["files"][0]["returned_ranges"] = [[1, 200], [201, 302]]
    assert check_requirements(repo, audit, requirements)[0]["status"] == "returned"


def test_range_references_declarations_rust_and_unsupported(tmp_path):
    (tmp_path / "lib.rs").write_text("const X: i32 = 1;\nfn f() {\n g();\n}\nfn g() {}\n")
    inv = source_functions(tmp_path, "lib.rs")
    assert inv["status"] == "parsed"
    assert inv["functions"][0]["calls"] == ["g"]
    reqs = read_requirements(tmp_path, {"summary": "lib.rs:1-5"})
    assert {r["symbol"] for r in reqs} == {"f", "g", ""}
    (tmp_path / "boot.S").write_text("entry:\n ret\n")
    assert read_requirements(tmp_path, {"summary": "boot.S:1-2"})[0]["scope"] == "cited_lines"


def test_changed_source_unknown_stream_and_untrusted_metadata(repo):
    reqs = read_requirements(repo, {"summary": "mm.c:2", "read_audit": {"fake": "bad.c:5"}})
    assert len(reqs) == 1
    assert check_requirements(repo, None, reqs)[0]["status"] == "unknown"
    (repo / "mm.c").write_text("int other(void) {}\n")
    assert check_requirements(repo, None, reqs)[0]["status"] == "source_changed_or_unavailable"
    assert source_functions(repo, "../outside.c")["status"] == "unavailable"


def test_invalid_range_cannot_vacuously_pass(repo):
    for ref in ("mm.c:20-0", "mm.c:0", "mm.c:9999"):
        reqs = read_requirements(repo, {"summary": ref})
        assert check_requirements(repo, {"stream_status": "complete"}, reqs)[0]["status"] == "unresolved_reference"


def test_reread_prompt_does_not_leak_previous_canary_answer():
    from oskernel_agent.engines.read_repair import _draft_for_reread
    token = "0a" * 16
    draft = _draft_for_reread({"summary": f"mm.c:3 标记 {token}",
                               "canary_observations": [{"token": token}],
                               "read_audit": {"untrusted": "metadata"}})
    assert token not in draft and "canary_observations" not in draft
    assert "mm.c:3" in draft and "read_audit" not in draft


def test_html_keeps_unresolved_functions_even_after_citation_is_removed():
    from oskernel_agent.reports.html_tree import _render_description_scope
    html = _render_description_scope({"verdict": {"read_audit": {"coverage": {
        "repair_rounds": 2, "requirements": [{"path": "mm.c", "symbol": "alloc", "scope": "function",
        "start_line": 1, "end_line": 302, "status": "unknown", "missing_ranges": [[81, 262]]}]}}}})
    assert "仍缺可核对的返回证据" in html
    assert "补读 2 轮" in html and "mm.c:1-302" in html


def _events(repo, ranges, report, session):
    lines = (repo / "mm.c").read_text().splitlines()
    events = []
    for index, (start, end) in enumerate(ranges):
        output = (f"<path>{repo / 'mm.c'}</path>\n<content>\n"
                  + "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
                  + "\n</content>")
        events.append({"type": "tool_use", "sessionID": session,
                       "part": {"type": "tool", "callID": str(index), "tool": "read",
                                "state": {"status": "completed", "input": {}, "output": output}}})
    events.append({"type": "text", "sessionID": session, "part": {"text": json.dumps(report)}})
    events.append({"type": "step_finish", "sessionID": session, "part": {"reason": "stop"}})
    return "\n".join(json.dumps(e) for e in events)


def task_for(repo, monkeypatch):
    monkeypatch.setenv("AGENT_READ_AUDIT_DIR", str(repo / "receipts"))
    monkeypatch.setattr(llm_batch, "_opencode_env", lambda task: {})
    return llm_batch.BatchTask(batch_id="subsys", agent_name="test", user_request="write report",
                               output_path=repo / "result.json", cache_dir=repo, cache_key="",
                               repo_path=repo, cache_enabled=False, audit_source_reads=True,
                               read_repair_rounds=2)


def test_real_batch_path_repairs_then_rechecks_full_function(repo, monkeypatch):
    task = task_for(repo, monkeypatch)
    invocations = []
    def run(cmd, **kwargs):
        invocations.append(cmd)
        ranges = [[1, 80], [263, 302]] if len(invocations) == 1 else [[1, 200], [201, 302]]
        if len(invocations) == 2:
            assert "源码读取补证" in " ".join(cmd)
            assert '"end_line": 302' in " ".join(cmd)
        report = {"summary": "mm.c:250"}
        task.output_path.write_text(json.dumps(report))
        return SimpleNamespace(stdout=_events(repo, ranges, report, f"s{len(invocations)}"),
                               stderr="", returncode=0)
    monkeypatch.setattr(llm_batch.subprocess, "run", run)
    result = llm_batch.run_batch_task(task)
    assert result["summary"] == "mm.c:250"
    assert len(invocations) == 2
    assert task.read_evidence["status"] == "returned"
    assert task.read_evidence["repair_rounds"] == 1
    assert len({r["run_id"] for r in task.read_audit_runs}) == 2
    assert Path(task.read_evidence["artifact"]).exists()
    assert all(Path(a["result_artifact"]).is_file() for a in task.read_evidence["attempts"])


def test_deleting_citation_and_disjoint_sessions_cannot_pass(repo, monkeypatch):
    task = task_for(repo, monkeypatch)
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        report = {"summary": "mm.c:250" if len(calls) == 1 else "已读完"}
        task.output_path.write_text(json.dumps(report))
        ranges = [[1, 200]] if len(calls) == 1 else [[201, 302]]
        return SimpleNamespace(stdout=_events(repo, ranges, report, f"s{len(calls)}"),
                               stderr="", returncode=0)
    monkeypatch.setattr(llm_batch.subprocess, "run", run)
    llm_batch.run_batch_task(task)
    assert len(calls) == 3
    assert task.read_evidence["status"] == "unresolved"
    assert task.read_evidence["requirements"][0]["missing_ranges"] == [[1, 200]]


def test_failed_delivery_retry_keeps_original_evidence_identity(repo, monkeypatch):
    task = task_for(repo, monkeypatch)
    task.cache_validator = lambda result: False
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        if len(calls) == 1:
            task.output_path.write_text('{"summary":"mm.c:2"}')
        return SimpleNamespace(stdout=_events(repo, [[1, 2]], {}, f"s{len(calls)}"),
                               stderr="", returncode=0)
    monkeypatch.setattr(llm_batch.subprocess, "run", run)
    llm_batch.run_batch_task(task)
    assert len(calls) == 2
    assert task.result_read_audit["session_ids"] == ["s1"]
