import json
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from oskernel_agent.analysis.read_canary import prepare_canary
from oskernel_agent.analysis.recall_probe import (batch_prompt, build_probes, locate, parse_answer,
                                                  parse_batch, score, single_prompt)
from oskernel_agent.cli import recall_probe
from oskernel_agent.engines.request_recorder import (RecordingProxy, complete, compress_logs,
                                                     load_records)

SOURCE = """// header
int clamp_value(int v);
void log_value(int v, int n);
static int helper(int x) { return x + 1; }
int work(int n) {
    int total = 0;
    for (int i = 0; i < n; i++) {
        total += helper(i);
    }
    if (total > 100) {
        total = clamp_value(total);
    }
    log_value(total, n);
    return total;
}
int other(void) {
    return work(3) + helper(4);
}
"""


@pytest.fixture
def experiment(tmp_path):
    repo = tmp_path / "original"
    repo.mkdir()
    (repo / "mm.c").write_text(SOURCE)
    (repo / "lib.rs").write_text("fn f(n: u32) -> u32 {\n    let a = n + 1;\n    let b = a * 2;\n"
                                 "    g(b);\n    b\n}\nfn g(x: u32) {}\n")
    targets = [{"path": "mm.c", "symbol": "work", "confirmed": True, "reason": "entry"},
               {"path": "lib.rs", "symbol": "f", "confirmed": True, "reason": "entry"}]
    result = recall_probe.prepare(repo, targets, tmp_path / "trial", ("start", "mid", "end"),
                                  [{"path": "mm.c", "symbol": "helper"}],
                                  [{"path": "mm.c", "symbol": "other"}])
    trial = tmp_path / "trial"
    return repo, trial, json.loads((trial / "private" / "manifest.json").read_text()), \
        json.loads((trial / "private" / "probes.json").read_text())


def test_markers_keep_lines_and_compile_and_decoys_stay_private(experiment):
    repo, trial, manifest, _ = experiment
    marked = (trial / "repo" / "mm.c").read_text()
    assert re.sub(r" /\* READ_AUDIT_CANARY:[0-9a-f]{32} \*/ ", "", marked) == SOURCE
    assert marked.count("\n") == SOURCE.count("\n")
    work = manifest["targets"][0]
    assert [(m["position"], m["line"]) for m in work["markers"]] == [("start", 5), ("mid", 11), ("end", 14)]
    for marker in work["markers"]:
        assert marker["token"] in marked.splitlines()[marker["line"] - 1]
    assert work["token"] == work["markers"][0]["token"]
    rust = manifest["targets"][1]
    assert [m["position"] for m in rust["markers"]] == ["start", "mid", "end"]
    assert [m["line"] for m in manifest["decoys"][0]["markers"]] == [4]  # one-line function
    public = (trial / "targets.json").read_text()
    assert "helper" not in public
    assert all(m["token"] not in public for t in manifest["targets"] + manifest["decoys"]
               for m in t["markers"])
    compiler = shutil.which("cc")
    if compiler:
        subprocess.run([compiler, "-fsyntax-only", str(trial / "repo" / "mm.c")], check=True,
                       capture_output=True)


def test_default_start_only_is_unchanged(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "mm.c").write_text(SOURCE)
    result = prepare_canary(repo, [{"path": "mm.c", "symbol": "work", "confirmed": True,
                                    "reason": "x"}], tmp_path / "t")
    manifest = json.loads(Path(result["manifest"]).read_text())
    assert [m["position"] for m in manifest["targets"][0]["markers"]] == ["start"]
    assert manifest["decoys"] == []
    with pytest.raises(ValueError, match="start"):
        prepare_canary(repo, [{"path": "mm.c", "symbol": "work", "confirmed": True,
                               "reason": "x"}], tmp_path / "u", positions=("mid",))


def test_probes_cover_markers_controls_and_content_without_leaking_answers(experiment):
    _, _, manifest, probes = experiment
    by_id = {p["id"]: p for p in probes}
    canary = [p for p in probes if p["kind"] == "canary"]
    assert {(p["role"], p["symbol"], p["position"]) for p in canary} >= {
        ("target", "work", "start"), ("target", "work", "mid"), ("target", "work", "end"),
        ("decoy", "helper", "start"), ("nomarker", "other", "start"), ("fake", "helper_shadow", "start")}
    assert by_id["callee:mm.c:work:start"]["answer"] == "helper"
    assert by_id["callee:mm.c:work:end"]["answer"] == "log_value"
    assert "callee:mm.c:work:mid" not in by_id  # line 11 carries a marker
    assert by_id["line:mm.c:work:start"]["answer"] == "for (int i = 0; i < n; i++) {"
    calls = by_id["calls:mm.c:work:all"]
    assert calls["answer"] == ["clamp_value", "helper", "log_value"]
    assert sorted(calls["options"]) == ["clamp_value", "helper", "log_value", "other"]
    assert score(calls, ["helper", "log_value", "clamp_value"], True, set())["outcome"] == "correct"
    assert score(calls, ["helper", "other"], True, set())["outcome"] == "wrong"
    tokens = [m["token"] for t in manifest["targets"] + manifest["decoys"] for m in t["markers"]]
    message, _ = batch_prompt(canary)
    for p in probes:
        text = single_prompt(p) + message
        assert not any(token in text for token in tokens)
        if p["kind"] in {"line", "callee"}:
            assert p["answer"] not in single_prompt(p)


def test_scoring_separates_recall_failure_modes(experiment):
    _, _, manifest, probes = experiment
    tokens = {m["token"] for t in manifest["targets"] + manifest["decoys"] for m in t["markers"]}
    probe = next(p for p in probes if p["id"] == "canary:mm.c:work:mid")
    other = next(t for t in tokens if t != probe["answer"])
    assert score(probe, probe["answer"].upper(), True, tokens)["outcome"] == "correct"
    assert score(probe, other, True, tokens)["outcome"] == "swapped"
    assert score(probe, probe["answer"][:10] + "0" * 22, True, tokens)["outcome"] == "partial"
    assert score(probe, "f" * 32, True, tokens)["outcome"] in {"fabricated", "partial"}
    assert score(probe, None, True, tokens)["outcome"] == "unknown"
    assert score(probe, None, False, tokens)["outcome"] == "unparsed"
    null = next(p for p in probes if p["role"] == "nomarker" and p["kind"] == "canary")
    assert score(null, None, True, tokens)["outcome"] == "correct"
    assert score(null, other, True, tokens)["outcome"] == "swapped"
    callee = next(p for p in probes if p["id"] == "callee:mm.c:work:end")
    assert score(callee, "log_value()", True, tokens)["outcome"] == "correct"
    line = next(p for p in probes if p["id"] == "line:mm.c:work:start")
    assert score(line, "for (int i = 0;  i < n; i++) {", True, tokens)["outcome"] == "correct"
    assert score(line, "for (int i = 0; i <= n; i++) {", True, tokens)["outcome"] == "partial"
    assert parse_answer('思考…{"answer": "abc"}') == ("abc", True)
    assert parse_answer('"8d07"') == ("8d07", True) and parse_answer("null") == (None, True)
    assert parse_answer("```\nlog_value\n```") == ("log_value", True)
    assert parse_answer("") == (None, False)
    assert parse_batch('{"answers": {"q1": null}}', {"q1": "a", "q2": "b"}) == {
        "a": (None, True), "b": (None, False)}


def test_locate_reports_roles_and_distance():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
                {"role": "assistant", "content": "", "tool_calls": [{"function": {
                    "name": "write_report", "arguments": json.dumps({"content": "saw NEEDLE"})}}]},
                {"role": "tool", "content": "1: x NEEDLE y"}, {"role": "assistant", "content": "done"}]
    found = locate(messages, "NEEDLE")
    assert found["present"] and found["roles"] == ["assistant", "tool"] and found["occurrences"] == 2
    assert found["chars_from_end"] == len("NEEDLE y") + len("done")
    assert locate(messages, "missing")["present"] is False
    assert locate(messages, None)["present"] is None


def _upstream(seen):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"auth": self.headers.get("Authorization"), "body": body})
            if body.get("model") == "bad":
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "bad model"}')
                return
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for delta in ({"reasoning_content": "think"}, {"content": "he"}, {"content": "llo"},
                              {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read",
                                                                                    "arguments": "{}"}}]}):
                    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(("data: " + json.dumps({"choices": [{"index": 0, "delta": {},
                                  "finish_reason": "stop"}], "usage": {"prompt_tokens": 7}})
                                  + "\n\ndata: [DONE]\n\n").encode())
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": "plain"},
                                 "finish_reason": "stop"}], "usage": {"prompt_tokens": 3}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_recorder_injects_key_and_never_logs_it(tmp_path):
    import urllib.error
    import urllib.request

    seen = []
    server = _upstream(seen)
    upstream = f"http://127.0.0.1:{server.server_port}"
    try:
        with RecordingProxy(upstream, "secret-key", tmp_path / "llm") as proxy:
            proxy.label = "main"
            request = urllib.request.Request(proxy.base_url + "/chat/completions", method="POST",
                data=json.dumps({"model": "m", "stream": True, "messages": [{"role": "user",
                                 "content": "hi"}]}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer placeholder"})
            streamed = urllib.request.urlopen(request).read().decode()
            assert "data: [DONE]" in streamed
            proxy.label = "probe:x"
            bad = urllib.request.Request(proxy.base_url + "/chat/completions", method="POST",
                                         data=json.dumps({"model": "bad"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(bad)
        assert complete(upstream, "secret-key", {"model": "m", "messages": []})["content"] == "plain"
    finally:
        server.shutdown()
        server.server_close()
    assert {s["auth"] for s in seen} == {"Bearer secret-key"}
    records = load_records(tmp_path / "llm")
    assert [r["label"] for r in records] == ["main", "probe:x"]
    assert records[0]["response"]["content"] == "hello"
    assert records[0]["response"]["reasoning"] == "think"
    assert records[0]["response"]["tool_calls"][0]["name"] == "read"
    assert records[0]["response"]["usage"] == {"prompt_tokens": 7}
    assert records[1]["response"]["status"] == 400
    for path in (tmp_path / "llm").iterdir():
        assert "secret-key" not in path.read_text() and "placeholder" not in path.read_text()
        assert path.stat().st_mode & 0o777 == 0o600
    compress_logs(tmp_path / "llm")
    assert sorted(p.name for p in (tmp_path / "llm").iterdir()) == ["requests.jsonl.gz",
                                                                    "responses.jsonl.gz"]
    assert load_records(tmp_path / "llm") == records


def test_evaluate_joins_presence_with_live_and_replay_answers(experiment, tmp_path):
    _, trial, manifest, probes = experiment
    work = manifest["targets"][0]
    start, mid, end = (m["token"] for m in work["markers"])
    source = (trial / "repo" / "mm.c").read_text().splitlines()
    tool_output = "<content>\n" + "\n".join(f"{n}: {t}" for n, t in enumerate(source[:12], 1))
    main = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function",
             "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": tool_output}]
    canary = [p for p in probes if p["kind"] == "canary"]
    message, ids = batch_prompt(canary)
    run = trial / "runs" / "r1"
    (run / "llm").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({
        "model": "m", "compaction": "off", "informed": False, "context_limit": 1, "extra_files": [],
        "audit_files": [{"path": "mm.c", "returned_ranges": [[1, 12]]}]}))
    requests = [{"seq": 1, "label": "main", "body": {"messages": main}},
                {"seq": 2, "label": "probe:batch", "body": {"messages": main + [
                    {"role": "assistant", "content": "report written"},
                    {"role": "user", "content": message}]}}]
    responses = [{"seq": 1, "tool_calls": [{"name": "os-kernel-tools_write_report"}]},
                 {"seq": 2, "usage": {"prompt_tokens": 99}}]
    for name, items in (("requests", requests), ("responses", responses)):
        (run / "llm" / f"{name}.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
    qid = {pid: q for q, pid in ids.items()}
    answers = {qid["canary:mm.c:work:start"]: start, qid["canary:mm.c:work:mid"]: mid,
               qid["canary:mm.c:work:end"]: None}
    (run / "live.jsonl").write_text(json.dumps({"method": "live_batch", "ids": ids, "tool_calls": [],
                                                "text": json.dumps({"answers": answers})}) + "\n")
    replays = [{"method": "fresh", "probe_id": "canary:mm.c:work:start", "repeat": 0,
                "response": {"content": '{"answer": null}'}},
               {"method": "substituted", "probe_id": "canary:mm.c:work:start", "repeat": 0,
                "substitute": "a" * 32, "response": {"content": '{"answer": "' + "a" * 32 + '"}'}},
               {"method": "context", "probe_id": "line:mm.c:work:start", "repeat": 0,
                "response": {"content": '{"answer": "for (int i = 0; i < n; i++) {"}',
                             "usage": {"prompt_tokens": 50}}},
               {"method": "batch", "probe_id": "batch", "repeat": 0, "ids": ids, "pad": 1000,
                "pad_where": "after", "response": {"content": json.dumps({"answers": answers})}}]
    (run / "replay.jsonl").write_text("".join(json.dumps(r) + "\n" for r in replays))
    result = recall_probe.evaluate(run)
    rows = [json.loads(line) for line in (run / "rows.jsonl").read_text().splitlines()]
    live = {r["probe_id"]: r for r in rows if r["method"] == "live_batch"}
    assert live["canary:mm.c:work:start"]["outcome"] == "correct"
    assert live["canary:mm.c:work:start"]["present"] and live["canary:mm.c:work:start"]["line_returned"]
    assert live["canary:mm.c:work:mid"]["outcome"] == "correct"
    end_row = live["canary:mm.c:work:end"]
    assert end_row["present"] is False and end_row["outcome"] == "unknown"  # line 14 never shown
    assert live["canary:mm.c:other:start"]["outcome"] == "unparsed"
    batch = {r["probe_id"]: r for r in rows if r["method"] == "batch_pad_after_1000"}
    assert batch["canary:mm.c:work:mid"]["outcome"] == "correct" and len(batch) == len(ids)
    by_method = {r["method"]: r for r in rows if not r["method"].startswith(("live_batch", "batch"))}
    assert by_method["fresh"]["present"] is False and by_method["fresh"]["outcome"] == "unknown"
    assert by_method["substituted"]["outcome"] == "correct"
    assert by_method["substituted"]["gave_original"] is False
    assert by_method["context"]["outcome"] == "correct" and by_method["context"]["prompt_tokens"] == 50
    assert result["report_request_found"] and result["base_prompt_tokens"] == 99
    summary = recall_probe.analyze([run], ["method", "kind", "present"])
    assert summary["rows"] == len(rows)
