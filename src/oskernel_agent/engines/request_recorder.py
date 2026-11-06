"""Localhost recorder between OpenCode and an OpenAI-compatible chat API.

Experiments use it to see what actually entered the model input. The API key is
injected here, so OpenCode configs only hold a placeholder; headers are never logged.
"""

from __future__ import annotations

import gzip
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_RETRYABLE = {429, 500, 502, 503, 504}


def _append(path: Path, lock: threading.Lock, record: dict) -> None:
    with lock:
        fresh = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if fresh:
            path.chmod(0o600)


def parse_stream(raw: bytes) -> dict:
    """Rebuild one chat completion from its SSE chunks."""
    content, reasoning, calls, usage, finish = [], [], {}, None, None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        data = line[5:].strip() if line.startswith("data:") else ""
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content.append(delta.get("content") or "")
            reasoning.append(delta.get("reasoning_content") or "")
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(call.get("index", 0), {"id": None, "name": "", "arguments": ""})
                slot["id"] = call.get("id") or slot["id"]
                function = call.get("function") or {}
                slot["name"] += function.get("name") or ""
                slot["arguments"] += function.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    return {"content": "".join(content), "reasoning": "".join(reasoning),
            "tool_calls": [calls[i] for i in sorted(calls)], "usage": usage, "finish_reason": finish}


def completion_summary(response: dict) -> dict:
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return {"content": message.get("content") or "", "reasoning": message.get("reasoning_content") or "",
            "tool_calls": [{"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                            "arguments": (c.get("function") or {}).get("arguments")}
                           for c in message.get("tool_calls") or []],
            "usage": response.get("usage"), "finish_reason": choice.get("finish_reason")}


def complete(upstream: str, api_key: str, body: dict, timeout: int = 900, attempts: int = 8) -> dict:
    """One non-streaming completion with retries for transient failures."""
    data = json.dumps(dict(body, stream=False), ensure_ascii=False).encode()
    error = "not attempted"
    for attempt in range(attempts):
        request = urllib.request.Request(
            upstream.rstrip("/") + "/chat/completions", data=data, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return completion_summary(json.load(response))
        except urllib.error.HTTPError as exc:
            error = f"HTTP {exc.code}: {exc.read()[:500].decode('utf-8', errors='replace')}"
            if exc.code not in _RETRYABLE:
                break
        except (OSError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"
        time.sleep(min(60, 5 * 2 ** attempt))
    return {"error": error}


class RecordingProxy:
    """Forward POSTs to `upstream`, logging bodies to requests.jsonl/responses.jsonl.

    `label` tags subsequent requests (e.g. main task vs. probe) for later analysis.
    """

    def __init__(self, upstream: str, api_key: str, directory: Path, timeout: int = 900):
        self.upstream = upstream.rstrip("/")
        self.label = "unlabeled"
        directory.mkdir(parents=True, exist_ok=True)
        self.requests = directory / "requests.jsonl"
        self.responses = directory / "responses.jsonl"
        self._lock = threading.Lock()
        self._seq = 0
        self.recording_errors = 0
        proxy = self

        def record_log(path, record):
            text = json.dumps(record, ensure_ascii=False)
            if api_key:
                text = text.replace(api_key, '[redacted]')
            try:
                _append(path, proxy._lock, json.loads(text))
            except OSError:
                proxy.recording_errors += 1

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with proxy._lock:
                    proxy._seq += 1
                    seq = proxy._seq
                label = proxy.label
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                record_log(proxy.requests, {"seq": seq, "label": label, "path": self.path,
                                                      "time": time.time(), "body": payload})
                request = urllib.request.Request(
                    proxy.upstream + self.path, data=body, method="POST",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                             "Accept": self.headers.get("Accept", "*/*")})
                record = {"seq": seq, "label": label}
                raw = b""
                try:
                    upstream = urllib.request.urlopen(request, timeout=timeout)
                except urllib.error.HTTPError as exc:
                    raw = exc.read()
                    record.update(status=exc.code, error=raw[:2000].decode("utf-8", errors="replace"))
                    self.send_response(exc.code)
                    self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    self.wfile.write(raw)
                    record_log(proxy.responses, record)
                    return
                except OSError as exc:
                    record.update(status=502, error=f"{type(exc).__name__}: {exc}")
                    record_log(proxy.responses, record)
                    self.send_error(502, "upstream unavailable")
                    return
                with upstream:
                    kind = upstream.headers.get("Content-Type", "application/json")
                    self.send_response(upstream.status)
                    self.send_header("Content-Type", kind)
                    self.end_headers()
                    client_open = True
                    while chunk := upstream.read1(65536):
                        raw += chunk
                        if client_open:
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except OSError:
                                client_open = False
                record["status"] = upstream.status
                try:
                    record.update(parse_stream(raw) if "event-stream" in kind
                                  else completion_summary(json.loads(raw)))
                except ValueError:
                    record["error"] = raw[:2000].decode("utf-8", errors="replace")
                record_log(proxy.responses, record)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> "RecordingProxy":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def compress_logs(directory: Path) -> None:
    """Request bodies repeat the whole context each turn; gzip them once the run ends."""
    for name in ("requests.jsonl", "responses.jsonl"):
        path = directory / name
        if path.exists():
            target = path.with_name(name + ".gz")
            with path.open("rb") as src, gzip.open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            target.chmod(0o600)
            path.unlink()


def load_records(directory: Path) -> list[dict]:
    """Requests joined with their responses, in send order."""
    def read(path: Path) -> list[dict]:
        if not path.exists() and path.with_name(path.name + ".gz").exists():
            with gzip.open(path.with_name(path.name + ".gz"), "rt", encoding="utf-8") as f:
                text = f.read()
        else:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    responses = {r["seq"]: r for r in read(directory / "responses.jsonl")}
    return [dict(r, response=responses.get(r["seq"])) for r in read(directory / "requests.jsonl")]
