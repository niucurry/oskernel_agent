import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from oskernel_agent.analysis.read_audit import build_read_audit
from oskernel_agent.analysis.read_canary import evaluate_canary, prepare_canary
from oskernel_agent.cli.read_audit import inventory


@pytest.fixture
def experiment(tmp_path):
    repo = tmp_path / "original"
    repo.mkdir()
    (repo / "mm.c").write_bytes(
        "// 中文注释\r\nint helper(void) { return 7; }\r\nint alloc(void) {\r\n"
        "  return helper();\r\n}\r\n".encode()
    )
    selected = [{"path": "mm.c", "symbol": "alloc", "confirmed": True, "reason": "内存分配入口"}]
    gold = selected + [{"path": "mm.c", "symbol": "helper", "confirmed": True}]
    result = prepare_canary(repo, selected, tmp_path / "trial", gold)
    return repo, Path(result["repo"]), json.loads(Path(result["manifest"]).read_text())


def receipt(repo, low, high):
    source = repo / "mm.c"
    lines = source.read_text().splitlines()
    output = (f"<path>{source}</path>\n<content>\n"
              + "\n".join(f"{i}: {lines[i-1]}" for i in range(low, high + 1)) + "\n</content>")
    events = [{"type": "tool_use", "sessionID": "s", "part": {
        "type": "tool", "tool": "read", "callID": "1", "state": {
            "status": "completed", "input": {"filePath": str(source)}, "output": output}}},
        {"type": "step_finish", "sessionID": "s", "part": {"reason": "stop"}}]
    return build_read_audit("\n".join(json.dumps(e) for e in events), repo,
                           batch_id="trial", run_id="one", returncode=0)[0]


def response(manifest):
    target = manifest["targets"][0]
    return {"canary_observations": [{k: target[k] for k in ("path", "symbol", "token")}]}


def test_copy_only_inline_comment_preserves_bytes_except_inserted_marker(experiment):
    original, copy, manifest = experiment
    source = (original / "mm.c").read_bytes()
    marked = (copy / "mm.c").read_bytes()
    assert b"READ_AUDIT_CANARY" not in source
    assert re.sub(rb" /\* READ_AUDIT_CANARY:[0-9a-f]{32} \*/ ", b"", marked) == source
    assert source.count(b"\n") == marked.count(b"\n")
    target = manifest["targets"][0]
    assert target["marker_line"] == 3
    assert target["original_sha256"] == hashlib.sha256(source).hexdigest()
    assert target["token"] not in (copy.parent / "targets.json").read_text()
    assert not (copy / "private").exists()
    compiler = shutil.which("cc")
    if compiler:
        for path in (original / "mm.c", copy / "mm.c"):
            subprocess.run([compiler, "-fsyntax-only", str(path)], check=True, capture_output=True)


def test_marker_retrieval_and_full_function_and_selection_are_separate(experiment):
    _, copy, manifest = experiment
    result = evaluate_canary(manifest, receipt(copy, 3, 3), response(manifest))
    assert result["returned_markers"] == result["correct_reports"] == result["corroborated_reports"] == 1
    assert result["fully_returned_functions"] == 0
    assert result["selection"]["recall"] == 0.5
    assert result["selection"]["omitted"][0]["symbol"] == "helper"
    assert evaluate_canary(manifest, receipt(copy, 3, 5), response(manifest))["fully_returned_functions"] == 1


def test_guessed_marker_without_tool_evidence_is_not_corroborated(experiment):
    _, copy, manifest = experiment
    result = evaluate_canary(manifest, receipt(copy, 4, 5), response(manifest))
    assert result["correct_reports"] == 1
    assert result["corroborated_reports"] == result["returned_markers"] == 0
    wrong = {"canary_observations": [{"path": "mm.c", "symbol": "alloc", "token": "invented"}]}
    result = evaluate_canary(manifest, receipt(copy, 3, 5), wrong)
    assert result["correct_reports"] == 0
    assert result["incorrect_or_unknown_observations"] == 1
    (copy / "mm.c").write_text("changed\n")
    assert evaluate_canary(manifest, {}, response(manifest))["returned_markers"] == 0


def test_unconfirmed_ambiguous_and_in_place_targets_are_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "x.c").write_text("int f(void) {return 1;}\n")
    target = {"path": "x.c", "symbol": "f", "confirmed": True, "reason": "entry"}
    with pytest.raises(ValueError, match="仓库之外"):
        prepare_canary(repo, [target], repo / "copy")
    with pytest.raises(ValueError, match="confirmed"):
        prepare_canary(repo, [dict(target, confirmed=False)], tmp_path / "one")
    with pytest.raises(ValueError, match="不存在或不唯一"):
        prepare_canary(repo, [dict(target, symbol="missing")], tmp_path / "two")
    with pytest.raises(ValueError, match="重复"):
        prepare_canary(repo, [target, target], tmp_path / "three")
    assert list(tmp_path.iterdir()) == [repo]


def test_multiple_functions_random_tokens_and_rust(experiment, tmp_path):
    repo, _, previous = experiment
    targets = [{"path": "mm.c", "symbol": f, "confirmed": True, "reason": "entry"}
               for f in ("helper", "alloc")]
    result = prepare_canary(repo, targets, tmp_path / "second")
    manifest = json.loads(Path(result["manifest"]).read_text())
    tokens = {t["token"] for t in manifest["targets"]}
    assert len(tokens) == 2 and previous["targets"][0]["token"] not in tokens
    assert [t["marker_line"] for t in manifest["targets"]] == [2, 3]
    (repo / "lib.rs").write_text("fn f() {\n #![allow(unused)]\n let x = 1;\n}\n")
    result = prepare_canary(repo, [{"path": "lib.rs", "symbol": "f", "confirmed": True,
                                   "reason": "entry"}], tmp_path / "rust")
    assert "READ_AUDIT_CANARY" in (Path(result["repo"]) / "lib.rs").read_text()


def test_entry_candidates_are_syntactic_and_expose_unresolved_edges(experiment):
    repo, _, _ = experiment
    result = inventory(repo, ["mm.c"], ["mm.c:3"], 2)
    assert [f["symbol"] for f in result["candidates"]] == ["alloc", "helper"]
    assert all(f["confirmed"] is False for f in result["candidates"])
    limited = inventory(repo, ["mm.c"], ["mm.c:3"], 0)
    assert limited["unresolved_calls"][0]["reason"] == "depth_limit"
