"""Audit host-emitted OpenCode tool results, never the model's claims of reading.

Ranges mean complete returned lines matching the repository snapshot at audit time.
They do not measure attention, comprehension, prompt attachments, or relevance recall.
Unknown tools (including arbitrary shell commands and child agents) stay explicit.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def merge_ranges(lines: list[int]) -> list[list[int]]:
    result: list[list[int]] = []
    for line in sorted(set(lines)):
        if result and line == result[-1][1] + 1:
            result[-1][1] = line
        else:
            result.append([line, line])
    return result


def missing_ranges(ranges: list[list[int]], total: int) -> list[list[int]]:
    result = []
    start = 1
    for low, high in ranges:
        if start < low:
            result.append([start, low - 1])
        start = max(start, high + 1)
    if start <= total:
        result.append([start, total])
    return result


def parse_events(stdout: str) -> tuple[list[dict], int]:
    """Only decode whole host JSONL records; never recursively decode text/output."""
    events = []
    invalid = 0
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            invalid += 1
            continue
        if not isinstance(event, dict) or not isinstance(event.get("sessionID"), str):
            invalid += 1
            continue
        if event.get("type") not in {
            "tool_use", "step_start", "step_finish", "text", "reasoning", "error",
        }:
            invalid += 1
            continue
        if event["type"] != "error" and not isinstance(event.get("part"), dict):
            invalid += 1
            continue
        events.append(event)
    return events, invalid


def assistant_text(stdout: str) -> str:
    """Legacy report-write recovery must not scan JSON tool outputs as model text."""
    events, _ = parse_events(stdout)
    return "\n".join(
        str(event.get("part", {}).get("text", ""))
        for event in events if event["type"] == "text"
    )


def completed_output_writes(stdout: str, output_root: Path) -> list[Path]:
    """Recover paths of actual successful writes, without replaying their contents."""
    events, _ = parse_events(stdout)
    result = []
    root = output_root.resolve()
    for event in events:
        part = event.get("part")
        if event["type"] != "tool_use" or not isinstance(part, dict):
            continue
        state = part.get("state", {})
        if not isinstance(state, dict):
            continue
        kind = _tool_kind(str(part.get("tool", "")))
        if state.get("status") != "completed" or kind not in {"write", "write_report", "write_to_file"}:
            continue
        if kind != "write" and not str(state.get("output", "")).startswith("[完成] 报告已保存到 "):
            continue
        args = state.get("input", {})
        if not isinstance(args, dict):
            continue
        raw = args.get("output_path") or args.get("filePath") or args.get("path")
        if not isinstance(raw, str) or not Path(raw).is_absolute():
            continue
        try:
            path = Path(raw).resolve()
            path.relative_to(root)
            if path.is_file() and path not in result:
                result.append(path)
        except (OSError, ValueError):
            continue
    return result


def _tool_kind(name: str) -> str:
    return re.sub(r"^os[-_]kernel[-_]tools[_:]", "", name)


def _returned_lines(kind: str, args: dict, output: str) -> tuple[list[tuple], bool]:
    """Return (path, number, text) from supported formats, plus format recognition."""
    if kind in {"read", "read_file"}:
        if kind == "read":
            location = re.search(r"<path>(.*?)</path>", output)
            path = location[1] if location else args.get("filePath")
            # A relative native path is relative to OpenCode's cwd, which may
            # differ from repo_path. Never silently resolve it against the repo.
            if not isinstance(path, str) or not Path(path).is_absolute():
                return [], False
            match = re.search(r"<content>\n?(.*?)\n?</content>", output, re.S)
            # Older OpenCode versions use <file> and 00001| numbered lines.
            if match is None:
                match = re.search(r"<file>\n?(.*?)\n?</file>", output, re.S)
            body = match.group(1) if match else ""
            pattern = r"^\s*(\d+)(?:: |\| ?)(.*)$"
        else:
            path = args.get("path") or args.get("file_path") or args.get("filepath")
            if path:
                path = re.sub(r":\d+(?:-\d+)?$", "", str(path))
            body = output if output.startswith("## ") else ""
            pattern = r"^\s*(\d+) \| (.*)$"
        rows = [(path, int(m[1]), m[2]) for m in re.finditer(pattern, body, re.M)]
        return rows, bool(rows)

    if kind == "search_code":
        rows = [(m[1], int(m[2]), m[3]) for m in re.finditer(
            r"^(.+?):(\d+) \| (.*)$", output, re.M,
        )]
        return rows, bool(rows) or output.startswith("[未找到]")

    if kind == "grep":
        rows = []
        path = None
        for line in output.splitlines():
            match = re.match(r"^  Line (\d+): (.*)$", line)
            if match and path:
                rows.append((path, int(match[1]), match[2]))
            elif line and not line.startswith(" ") and line.endswith(":"):
                path = line[:-1]
        return rows, bool(rows) or output.strip() == "No files found"

    if kind == "find_symbol_definition":
        location = re.search(r"^文件：(.+?)  行：(\d+)-(\d+)$", output, re.M)
        block = re.search(r"^```[^\n]*\n(.*?)\n```", output, re.S | re.M)
        if location and block:
            body = block[1]
            start, end = int(location[2]), int(location[3])
            # The dispatcher truncates definitions to 40 head / 20 tail lines.
            halves = re.split(r"\n\n  // \.\.\. 省略 [^\n]+\n\n", body)
            if len(halves) == 2:
                head, tail = (part.splitlines() for part in halves)
                rows = [(location[1], start + i, text) for i, text in enumerate(head)]
                rows += [(location[1], end - len(tail) + 1 + i, text)
                         for i, text in enumerate(tail)]
                return rows, True
            if len(halves) == 1:
                return [(location[1], start + i, text)
                        for i, text in enumerate(body.splitlines())], True
    return [], False


def build_read_audit(stdout: str, repo_path: Path, *, batch_id: str,
                     run_id: str, returncode: int | None) -> tuple[dict, list[dict]]:
    events, invalid = parse_events(stdout)
    sessions = sorted({e["sessionID"] for e in events})
    finishes = [e for e in events if e["type"] == "step_finish"]
    complete = (returncode == 0 and invalid == 0 and len(sessions) == 1
                and bool(finishes) and finishes[-1].get("part", {}).get("reason") == "stop"
                and not any(e["type"] == "error" for e in events))
    root = repo_path.resolve()
    snapshots: dict[str, dict] = {}
    calls = []
    receipts = []
    seen = set()
    unresolved = 0

    def snapshot(raw_path: object) -> dict | None:
        if not isinstance(raw_path, str) or not raw_path:
            return None
        # Native tools supply absolute paths; relative paths are repo-relative.
        try:
            path = (root / raw_path.replace("\\", "/")).resolve()
            relative = path.relative_to(root).as_posix()
            if relative not in snapshots:
                if not path.is_file() or path.stat().st_size > 10_000_000:
                    return None
                data = path.read_bytes()
                snapshots[relative] = {
                    "path": relative, "sha256": hashlib.sha256(data).hexdigest(),
                    "lines": data.decode("utf-8", errors="replace").splitlines(),
                    "returned": [], "partial": [],
                }
            return snapshots[relative]
        except (OSError, ValueError):
            return None

    for event in events:
        if event["type"] != "tool_use":
            continue
        part = event.get("part")
        if not isinstance(part, dict) or not isinstance(part.get("state"), dict):
            invalid += 1
            complete = False
            continue
        state = part["state"]
        call_id = part.get("callID") or part.get("id")
        if (not call_id or part.get("type") != "tool"
                or part.get("sessionID", event["sessionID"]) != event["sessionID"]):
            invalid += 1
            complete = False
            continue
        key = (event["sessionID"], call_id)
        if key in seen:
            continue
        seen.add(key)
        receipts.append(event)
        kind = _tool_kind(str(part.get("tool", "")))
        output = state.get("output", "")
        args = state.get("input", {})
        call = {"session_id": event["sessionID"], "call_id": call_id,
                "tool": part.get("tool"), "status": state.get("status"),
                "output_sha256": hashlib.sha256(str(output).encode()).hexdigest(),
                "evidence": []}
        calls.append(call)
        if len(sessions) != 1:
            call["interpretation"] = "unknown_mixed_sessions"
            unresolved += 1
            continue
        if state.get("status") != "completed":
            call["interpretation"] = "failed_or_incomplete"
            continue
        if kind in {"glob", "todowrite", "write_report", "write_to_file", "validate_refs"}:
            call["interpretation"] = "not_source_content"
            continue
        rows, recognized = _returned_lines(
            kind, args if isinstance(args, dict) else {}, output if isinstance(output, str) else "",
        )
        call["interpretation"] = "source_lines" if recognized else "unknown"
        if not recognized:
            unresolved += 1
        per_file: dict[str, list[int]] = {}
        rejected = 0
        for raw_path, number, text in rows:
            file = snapshot(raw_path)
            if file is None or not 1 <= number <= len(file["lines"]):
                rejected += 1
                continue
            source = file["lines"][number - 1]
            # Search tools strip end whitespace; that does not prove a full line.
            if source == text:
                file["returned"].append(number)
                per_file.setdefault(file["path"], []).append(number)
            else:
                file["partial"].append(number)
                rejected += 1
        call["unverified_lines"] = rejected
        for path, numbers in per_file.items():
            call["evidence"].append({"path": path, "returned_ranges": merge_ranges(numbers)})

    files = []
    for file in snapshots.values():
        ranges = merge_ranges(file["returned"])
        files.append({"path": file["path"], "sha256": file["sha256"],
                      "total_lines": len(file["lines"]), "returned_ranges": ranges,
                      "unverified_line_ranges": merge_ranges(file["partial"]),
                      "not_observed_ranges": missing_ranges(ranges, len(file["lines"]))})
    report = {
        "schema_version": 1, "batch_id": batch_id, "run_id": run_id,
        "session_ids": sessions, "returncode": returncode,
        "stream_status": "complete" if complete else ("partial" if events else "unknown"),
        "invalid_event_lines": invalid, "unresolved_tool_calls": unresolved,
        "measurement": "tool_output_matching_current_repository",
        "source_read_coverage": "unknown", "calls": calls,
        "files": sorted(files, key=lambda f: f["path"]),
        "limitations": ["not_model_attention_or_comprehension", "not_relevant_code_recall",
                        "inline_prompts_and_attachments_not_measured",
                        "child_agent_tools_not_in_parent_stream",
                        "repository_compared_after_run"],
    }
    return report, receipts


def persist_read_audit(stdout: str, repo_path: Path, directory: Path, *,
                       batch_id: str, run_id: str, returncode: int | None) -> dict:
    report, receipts = build_read_audit(
        stdout, repo_path, batch_id=batch_id, run_id=run_id, returncode=returncode,
    )
    directory.mkdir(parents=True, exist_ok=True)
    # Each invocation gets its own directory, including timeouts and retries.
    (directory / "tool_events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in receipts), encoding="utf-8",
    )
    report["artifact"] = str((directory / "audit.json").resolve())
    Path(report["artifact"]).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return report


def report_read_audit(audit: dict | None, result: dict, repo_path: Path) -> dict:
    """A compact diagnostic, not a delivery gate: absence in tools isn't absence in input."""
    if audit is None:
        return {"stream_status": "unknown", "reason": "no_current_run_audit"}
    files = {f["path"]: f for f in audit.get("files", [])}
    from .read_coverage import source_references
    refs = source_references(result)
    statuses = []
    for path, number, end in refs:
        path = re.sub(r"\\+", "/", path)
        try:
            path = (repo_path / path).resolve().relative_to(repo_path.resolve()).as_posix()
        except (OSError, ValueError):
            pass
        file = files.get(path)
        status = "not_observed_in_tool_outputs"
        if audit.get("stream_status") != "complete" or audit.get("unresolved_tool_calls"):
            status = "unknown"
        if file:
            try:
                current_hash = hashlib.sha256((repo_path / path).read_bytes()).hexdigest()
            except OSError:
                current_hash = None
            if current_hash != file["sha256"]:
                status = "unknown_source_changed"
            elif 1 <= number <= end and any(low <= number and end <= high for low, high in file["returned_ranges"]):
                status = "returned"
            elif any(low <= end and number <= high for low, high in file["unverified_line_ranges"]):
                status = "unverified_line"
        statuses.append({"path": path, "line": number, "end_line": end, "status": status})
    return {
        "stream_status": audit["stream_status"], "run_id": audit["run_id"],
        "artifact": audit.get("artifact"), "tool_calls": len(audit.get("calls", [])),
        "unresolved_tool_calls": audit.get("unresolved_tool_calls", 0),
        "observed_files": sum(bool(f["returned_ranges"]) for f in files.values()),
        "references": statuses,
    }
