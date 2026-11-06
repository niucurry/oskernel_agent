"""Opt-in real CLI/MCP integration with a localhost scripted model, no paid API.

Run with OSKERNEL_TEST_OPENCODE=1 and TMPDIR on a volume with room for OpenCode.
The model is deterministic; this tests transport and measurements, not reasoning.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import sys
import threading

import pytest

from oskernel_agent.analysis.read_audit import report_read_audit
from oskernel_agent.engines import llm_batch


@pytest.mark.skipif(os.environ.get("OSKERNEL_TEST_OPENCODE") != "1",
                    reason="real OpenCode transport test is opt-in")
@pytest.mark.parametrize("repair_mode", [False, True])
def test_real_native_and_project_mcp_tools_share_audit(tmp_path, monkeypatch, repair_mode):
    pytest.importorskip("mcp")
    executable = shutil.which("opencode")
    if not executable:
        pytest.skip("OpenCode is not installed")
    monkeypatch.setenv("AGENT_READ_AUDIT_DIR", str(tmp_path / "receipts"))
    source = tmp_path / "kernel.c"
    source.write_text("int sample(void) {\n" + "".join(
        f"int value_{i} = {i};\n" for i in range(2, 500)) + "}\n")
    output_path = tmp_path / "result.json"
    received = []
    failures = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            names = [t["function"]["name"] for t in request.get("tools", [])]
            received.append(names)
            completed = sum(m.get("role") == "tool" for m in request.get("messages", []))
            message = {"role": "assistant", "content": "audit complete"}
            reason = "stop"
            repair_turn = "源码读取补证" in json.dumps(request.get("messages", []), ensure_ascii=False)
            plan = [
                    ("read", {"filePath": str(source), "offset": 81, "limit": 20}),
                    ("_read_file", {"path": "kernel.c"}),
                    ("grep", {"pattern": "value_300 =", "path": str(tmp_path)}),
                    ("bash", {"command": "printf 'opaque shell result'", "description": "audit transport"}),
                    ("_write_report", {"output_path": str(output_path),
                                       "content": json.dumps({"summary": "kernel.c:90 kernel.c:300"})}),
                ]
            if repair_turn:
                plan = [
                    ("read", {"filePath": str(source), "offset": 1, "limit": 200}),
                    ("_read_file", {"path": "kernel.c", "start_line": 201, "end_line": 400}),
                    ("read", {"filePath": str(source), "offset": 401, "limit": 100}),
                    ("_write_report", {"output_path": str(output_path),
                                       "content": json.dumps({"summary": "kernel.c:90 kernel.c:300"})}),
                ]
            if names and completed < len(plan):
                suffix, args = plan[completed]
                name = next((n for n in names if n == suffix or n.endswith(suffix)), None)
                if name is None:
                    failures.append({"missing": suffix, "available": names})
                else:
                    reason = "tool_calls"
                    message = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": f"call_{completed}", "type": "function",
                         "function": {"name": name, "arguments": json.dumps(args)}}]}
            self.send_response(200)
            if request.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"][0]["index"] = 0
                for payload in [
                    {"id": "chatcmpl-audit", "object": "chat.completion.chunk", "created": 1,
                     "model": "mock", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"id": "chatcmpl-audit", "object": "chat.completion.chunk", "created": 1,
                     "model": "mock", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                ]:
                    self.wfile.write(("data: " + json.dumps(payload) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": "chatcmpl-audit", "object": "chat.completion",
                    "created": 1, "model": "mock", "choices": [{"index": 0, "message": message,
                    "finish_reason": reason}], "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                    "total_tokens": 2}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = {
        "model": "audit/mock", "small_model": "audit/mock", "share": "disabled",
        "provider": {"audit": {"npm": "@ai-sdk/openai-compatible", "name": "Local audit test",
            "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "local-test"},
            "models": {"mock": {"name": "mock", "limit": {"context": 65536, "output": 4096}}}}},
        "agent": {"audit": {"mode": "primary", "prompt": "Run the transport audit.",
            "permission": {"*": "deny", "read": "allow", "grep": "allow", "bash": "allow",
                           "external_directory": {str(tmp_path): "allow", str(tmp_path / "*"): "allow"},
                           "os-kernel-tools_*": "allow"}}},
        "mcp": {"os-kernel-tools": {"type": "local", "command": [sys.executable, "-m",
            "oskernel_agent.cli.mcp_server"], "enabled": True}},
    }
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("OPENCODE_") or key.startswith("AGENT_OPENCODE_"):
            env.pop(key)
    for key, name in [("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")]:
        env[key] = str(tmp_path / name)
    src = str(Path(__file__).resolve().parents[1] / "src")
    env.update({"OPENCODE_CONFIG_CONTENT": json.dumps(config),
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true", "OPENCODE_DISABLE_AUTOUPDATE": "true",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true", "OPENCODE_DISABLE_EXTERNAL_SKILLS": "true",
                "OPENCODE_DISABLE_MODELS_FETCH": "true", "OPENCODE_DISABLE_LSP_DOWNLOAD": "true",
                "PYTHONPATH": src + os.pathsep + env.get("PYTHONPATH", ""),
                "OSKERNEL_AGENT_REPO_PATH": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm_batch, "_OPENCODE", executable)
    monkeypatch.setattr(llm_batch, "_opencode_env", lambda task: env)
    task = llm_batch.BatchTask(batch_id="native-and-mcp", agent_name="audit", user_request="Run audit",
                               repo_path=tmp_path, output_path=output_path, cache_dir=tmp_path,
                               cache_key="", cache_enabled=False, audit_source_reads=True,
                               read_repair_rounds=2 if repair_mode else 0)
    try:
        if repair_mode:
            result = llm_batch.run_batch_task(task, timeout=90)
            assert result["summary"] == "kernel.c:90 kernel.c:300"
            ok, text = True, ""
        else:
            ok, text, _ = llm_batch._run_opencode_once(task, 90)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not failures, failures
    assert received, "OpenCode did not reach the localhost model"
    assert ok, text
    report = task.read_audit_runs[0]
    assert report["stream_status"] == "complete", report
    assert {c["tool"] for c in report["calls"]} >= {"read", "grep", "bash", "os-kernel-tools_read_file"}
    source_record = next(f for f in report["files"] if f["path"] == "kernel.c")
    assert source_record["returned_ranges"] == [[1, 100], [300, 300], [461, 500]]
    assert source_record["not_observed_ranges"] == [[101, 299], [301, 460]]
    summary = report_read_audit(report, json.loads(output_path.read_text()), tmp_path)
    assert all(r["status"] == "returned" for r in summary["references"])
    assert report["unresolved_tool_calls"] == 1  # bash output is deliberately opaque.
    if repair_mode:
        assert len(task.read_audit_runs) == 2
        assert task.result_read_audit["files"][0]["returned_ranges"] == [[1, 500]]
        assert task.read_evidence["status"] == "returned"
        assert task.read_evidence["repair_rounds"] == 1
