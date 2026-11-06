"""Opt-in: real OpenCode + MCP + recorder against a localhost scripted model (no paid API).

Run with OSKERNEL_TEST_OPENCODE=1 using a Python that has `mcp` installed. The model
answers probes only from tool outputs in the request it receives, so this checks the
plumbing (fork keeps history, probe agent has no tools, compaction drops code).
"""

import json
import os
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oskernel_agent.cli import recall_probe

SOURCE = ("int clamp_value(int v);\nvoid log_value(int v, int n);\n"
          "static int helper(int x) { return x + 1; }\nint work(int n) {\n    int total = 0;\n"
          + "".join(f"    total += helper({i});\n" for i in range(40))
          + "    if (total > 100) {\n        total = clamp_value(total);\n    }\n"
          "    log_value(total, n);\n    return total;\n}\nint other(void) {\n    return work(3);\n}\n")


def _answer(question: str, messages: list[dict]):
    tools = "\n".join(m["content"] for m in messages
                      if m.get("role") == "tool" and isinstance(m.get("content"), str))
    line = re.search(r"第 (\d+) 行（函数", question)
    hit = line and re.search(rf"^\s*{line[1]}(?::|\s\|)\s.*READ_AUDIT_CANARY:([0-9a-f]{{32}})", tools, re.M)
    return hit[1] if hit else None


def _server(repo, state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = request.get("messages", [])
            names = [t["function"]["name"] for t in request.get("tools", [])]
            last = messages[-1].get("content") if messages else ""
            if isinstance(last, list):
                last = "".join(p.get("text", "") for p in last if isinstance(p, dict))
            last = last or ""
            usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
            message = {"role": "assistant", "content": "done"}
            state.setdefault("requests", []).append({"tools": len(names), "last": last[:80]})
            if "anchored summary" in last:
                state["compacted"] = True
                message["content"] = "## Goal\n- describe work"
            elif recall_probe.PROBE_TAG in last:
                text = last
                parts = re.split(r"(q\d+)：", text)
                if len(parts) > 1:
                    answers = {parts[i]: _answer(parts[i + 1], messages) for i in range(1, len(parts), 2)}
                    message["content"] = json.dumps({"answers": answers})
                else:
                    message["content"] = json.dumps({"answer": _answer(text, messages)})
            elif names:
                done = sum(m.get("role") == "tool" for m in messages)
                if not done and not state["compacted"]:
                    call = ("read", {"filePath": str(repo / "mm.c")})
                    if state["force_compaction"]:
                        usage = {"prompt_tokens": 62000, "completion_tokens": 10, "total_tokens": 62010}
                elif not any("write_report" in json.dumps(m, ensure_ascii=False) for m in messages
                             if m.get("role") == "assistant"):
                    call = ("_write_report", {"output_path": state["output"], "content": json.dumps(
                        {"summary": "mm.c:4-49", "functions": [{"path": "mm.c", "symbol": "work"}]})})
                else:
                    call = None
                if call:
                    name = next(n for n in names if n == call[0] or n.endswith(call[0]))
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "index": 0, "id": f"call_{len(messages)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps(call[1])}}]}
            reason = "tool_calls" if message.get("tool_calls") else "stop"
            self.send_response(200)
            if not request.get("stream"):
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"index": 0, "message": message,
                                 "finish_reason": reason}], "usage": usage}).encode())
                return
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for payload in ({"choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                            {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                             "usage": usage}):
                self.wfile.write(("data: " + json.dumps(dict(payload, id="x", object="chat.completion.chunk",
                                  created=1, model="mock")) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.skipif(os.environ.get("OSKERNEL_TEST_OPENCODE") != "1",
                    reason="real OpenCode transport test is opt-in")
@pytest.mark.parametrize("force_compaction", [False, True])
def test_post_report_probes_see_exact_session_context(tmp_path, monkeypatch, force_compaction):
    pytest.importorskip("mcp")
    if not shutil.which("opencode"):
        pytest.skip("OpenCode is not installed")
    original = tmp_path / "original"
    original.mkdir()
    (original / "mm.c").write_text(SOURCE)
    recall_probe.prepare(original, [{"path": "mm.c", "symbol": "work", "confirmed": True, "reason": "entry"}],
                         tmp_path / "trial", ("start", "mid", "end"), [{"path": "mm.c", "symbol": "helper"}],
                         [{"path": "mm.c", "symbol": "other"}])
    trial = tmp_path / "trial"
    state = {"compacted": False, "force_compaction": force_compaction, "output": None}
    real_run = recall_probe.run_live

    server = _server(trial / "repo", state)
    upstream = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(recall_probe, "_task_prompt",
                        lambda repo, public, output, informed, extra: state.update(output=str(output))
                        or f"describe work, write {output}")
    try:
        run = real_run(trial, "mock", upstream, "fake-key", context=65536, output=4096,
                       compaction="auto" if force_compaction else "off", informed=False, extra=[],
                       repair_rounds=0, timeout=120, live_single="canary", tag="test")
        recall_probe.replay(run, upstream, "fake-key", methods=["context", "substituted", "fresh",
                            "report_time"], repeats=1, pads=[], pad_where="after", pad_sources=[],
                            kinds=["canary"], workers=2, max_tokens=100)
    finally:
        server.shutdown()
        server.server_close()
    result = recall_probe.evaluate(run)
    rows = [json.loads(line) for line in (run / "rows.jsonl").read_text().splitlines()]
    work = {r["position"]: r for r in rows if r["method"] == "live_batch" and r["symbol"] == "work"}
    assert result["probe_requests"] >= 1 and result["probe_requests_with_tools"] == 0
    assert all(r["tool_calls"] == 0 for r in rows if r["method"].startswith("live"))
    singles = [r for r in rows if r["method"] == "live_single"]
    assert len(singles) == 6
    if not force_compaction:
        assert not result["compacted_during_main"]
        assert all(r["present"] and r["outcome"] == "correct" for r in work.values())
        assert {r["outcome"] for r in singles} == {"correct"}
        replay = {r["method"]: r for r in rows if r["probe_id"] == "canary:mm.c:work:mid"}
        assert replay["context"]["outcome"] == replay["substituted"]["outcome"] == "correct"
        assert replay["fresh"]["outcome"] == "unknown" and replay["report_time"]["present"]
    else:
        assert result["compacted_during_main"] and state["compacted"]
        assert all(r["present"] is False and r["outcome"] == "unknown" for r in work.values())
