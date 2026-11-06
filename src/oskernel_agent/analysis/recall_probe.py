"""Post-report recall probes scored against the recorded model input.

Presence (is the needle in the request the model received?) is measured from the
request log. Recall is asked without tools. Canary tokens have no prior; content
questions do, so they need the no-context baseline before interpretation.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

from .read_canary import MARKER
from .read_coverage import source_functions, syntax_tree, walk

PROBE_TAG = "【记忆测试】"
POSITION_NAMES = {"start": "开头", "mid": "中部", "end": "结尾"}
_HEX = re.compile(r"[0-9a-fA-F]{32}")
_RULES = ("不要调用任何工具，不要重新读取文件，只凭本会话中你已经看到的内容回答。"
          "如果没见过、不确定或者记不清，答案填 null，不要猜测。")


def _thirds(item: dict) -> list[tuple[str, int, int]]:
    start, end = item["start_line"] + 1, item["end_line"] - 1
    size = max(1, end - start + 1)
    return [(name, start + size * i // 3, start + size * (i + 1) // 3 - 1)
            for i, name in enumerate(POSITION_NAMES)]


def _content_probes(repo: Path, item: dict, role: str, marker_lines: set[int]) -> list[dict]:
    path = repo / item["path"]
    data = path.read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines()
    counts: dict[str, int] = {}
    for text in lines:
        counts[text.strip()] = counts.get(text.strip(), 0) + 1
    calls: dict[int, list] = {}
    for node in walk(syntax_tree(path.suffix.lower(), data).root_node):
        line = node.start_point[0] + 1
        if node.type == "call_expression" and item["start_line"] < line <= item["end_line"]:
            calls.setdefault(line, []).append(node.child_by_field_name("function"))
    # Unique lines only, so presence in the input is unambiguous; ALL_CAPS callees are
    # usually macros, which a careful model rightly refuses to call functions.
    callees = {line: fns[0].text.decode("utf-8", errors="replace") for line, fns in calls.items()
               if len(fns) == 1 and fns[0] is not None and fns[0].type == "identifier"
               and line not in marker_lines and counts[lines[line - 1].strip()] == 1
               and not fns[0].text.decode("utf-8", errors="replace").isupper()}
    texts = {n: lines[n - 1].strip() for n in range(item["start_line"] + 1, item["end_line"])
             if n not in marker_lines and len(lines[n - 1].strip()) >= 20
             and counts[lines[n - 1].strip()] == 1 and MARKER not in lines[n - 1]
             and not lines[n - 1].strip().startswith(("//", "/*", "*"))}
    probes = []
    base = {k: item[k] for k in ("path", "symbol", "start_line", "end_line")}
    # Function-keyed content recall (same addressing as canary questions, no line numbers).
    called = sorted({fn.text.decode("utf-8", errors="replace") for fns in calls.values() for fn in fns
                     if fn is not None and fn.type == "identifier"
                     and not fn.text.decode("utf-8", errors="replace").isupper()})
    defined = sorted({f["symbol"] for f in source_functions(repo, item["path"])["functions"]}
                     - set(called) - {item["symbol"]})
    if called and defined:
        real = sorted(called, key=lambda s: hashlib.sha256(s.encode()).hexdigest())[:4]
        fake = sorted(defined, key=lambda s: hashlib.sha256((item["symbol"] + s).encode()).hexdigest())[:4]
        options = sorted(real + fake, key=lambda s: hashlib.sha256(s.encode()).hexdigest())
        probes.append(dict(base, id=f"calls:{item['path']}:{item['symbol']}:all", kind="calls", role=role,
                           position="all", line=None, options=options, answer=sorted(real),
                           needle=lines[item["start_line"] - 1].strip()))
    for name, low, high in _thirds(item):
        center = (low + high) / 2
        for kind, pool in (("callee", callees), ("line", texts)):
            options = [n for n in pool if low <= n <= high]
            if not options:
                continue
            line = min(options, key=lambda n: (abs(n - center), n))
            probes.append(dict(base, id=f"{kind}:{item['path']}:{item['symbol']}:{name}", kind=kind,
                               role=role, position=name, line=line, answer=pool[line],
                               needle=lines[line - 1].strip()))
    return probes


def build_probes(manifest: dict, controls: list[dict] | None = None) -> list[dict]:
    """Canary probes for every marker, null-answer controls, and source-content probes."""
    repo = Path(manifest["repo"])
    probes = []
    for item in manifest["targets"] + manifest.get("decoys", []):
        role = item.get("role", "target")
        markers = item.get("markers") or [{"position": "start", "line": item["marker_line"],
                                           "token": item["token"]}]
        for marker in markers:
            probes.append({**{k: item[k] for k in ("path", "symbol", "start_line", "end_line")},
                           "id": f"canary:{item['path']}:{item['symbol']}:{marker['position']}",
                           "kind": "canary", "role": role, "position": marker["position"],
                           "line": marker["line"], "answer": marker["token"],
                           "needle": marker["token"]})
        probes += _content_probes(repo, item, role, {m["line"] for m in markers})
    for control in controls or []:
        inv = source_functions(repo, control["path"])
        found = [f for f in inv["functions"] if f["symbol"] == control["symbol"]]
        if len(found) != 1:
            raise ValueError(f"对照函数不存在或不唯一：{control}")
        item = dict(found[0], path=inv["path"])
        probes.append({**{k: item[k] for k in ("path", "symbol", "start_line", "end_line")},
                       "id": f"canary:{item['path']}:{item['symbol']}:start", "kind": "canary",
                       "role": "nomarker", "position": "start", "line": item["start_line"],
                       "answer": None, "needle": None})
        probes += _content_probes(repo, item, "nomarker", set())
    for path in sorted({t["path"] for t in manifest["targets"]}):
        names = {f["symbol"] for f in source_functions(repo, path)["functions"]}
        fake = next(f"{stem}_{suffix}" for stem in sorted(names)
                    for suffix in ("shadow", "unused") if f"{stem}_{suffix}" not in names)
        probes.append({"path": path, "symbol": fake, "start_line": None, "end_line": None,
                       "id": f"canary:{path}:{fake}:start", "kind": "canary", "role": "fake",
                       "position": "start", "line": None, "answer": None, "needle": None})
    return probes


def question(probe: dict) -> str:
    where = f"{probe['path']} 中函数 {probe['symbol']}"
    if probe["start_line"]:
        where += f"（第 {probe['start_line']}-{probe['end_line']} 行）"
    if probe["kind"] == "canary":
        at = f"第 {probe['line']} 行（函数{POSITION_NAMES[probe['position']]}）" if probe["line"] \
            else f"函数{POSITION_NAMES[probe['position']]}"
        return (f"{where}的{at}是否有一条形如 {MARKER}<32位十六进制> 的注释？"
                "如果有，答案填这 32 位十六进制值；否则填 null。")
    if probe["kind"] == "callee":
        return f"{where}的第 {probe['line']} 行调用了哪个函数？答案只填函数名。"
    if probe["kind"] == "calls":
        return (f"{probe['path']} 中函数 {probe['symbol']}的函数体内调用了下列哪些函数：{'、'.join(probe['options'])}？"
                "答案填被调用函数名组成的 JSON 数组；一个都不确定就填 null。")
    return f"{where}的第 {probe['line']} 行源码原文是什么？答案填该行去掉首尾空白后的完整文本。"


def single_prompt(probe: dict) -> str:
    return (f"{PROBE_TAG}这不是新任务。{_RULES}只输出一行 JSON：{{\"answer\": ...}}。"
            f"问题：{question(probe)}")


def batch_prompt(probes: list[dict]) -> tuple[str, dict[str, str]]:
    ids = {f"q{i + 1}": p["id"] for i, p in enumerate(probes)}
    items = " ".join(f"{qid}：{question(p)}" for qid, p in zip(ids, probes))
    return (f"{PROBE_TAG}这不是新任务。{_RULES}只输出一行 JSON："
            f"{{\"answers\": {{\"q1\": ..., \"q2\": ...}}}}，每个问题都要给出。问题：{items}"), ids


def _json_objects(text: str):
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch == "{":
            try:
                value, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def parse_answer(text: str) -> tuple[object, bool]:
    found = [o for o in _json_objects(text) if "answer" in o]
    if found:
        return found[-1]["answer"], True
    # Small models sometimes answer with a bare value; take the last non-empty line.
    lines = [line.strip().strip("`") for line in (text or "").splitlines() if line.strip().strip("`")]
    if not lines:
        return None, False
    try:
        value = json.loads(lines[-1])
        return (value, True) if value is None or isinstance(value, str) else (lines[-1], True)
    except ValueError:
        return lines[-1], True


def parse_batch(text: str, ids: dict[str, str]) -> dict[str, tuple[object, bool]]:
    found = [o["answers"] for o in _json_objects(text) if isinstance(o.get("answers"), dict)]
    answers = found[-1] if found else {}
    return {pid: (answers.get(qid), qid in answers) for qid, pid in ids.items()}


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def score(probe: dict, value: object, parsed: bool, tokens: set[str]) -> dict:
    """Outcome categories; `tokens` holds every marker token of the experiment."""
    if not parsed:
        return {"outcome": "unparsed"}
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "null", "none"}):
        return {"outcome": "correct" if probe["answer"] is None else "unknown"}
    text = str(value)
    if probe["kind"] == "canary":
        match = _HEX.search(text)
        if not match:
            return {"outcome": "unknown" if probe["answer"] else "fabricated", "given": text[:200]}
        got = match[0].lower()
        expected = probe["answer"]
        prefix = 0
        while expected and prefix < 32 and got[prefix] == expected[prefix]:
            prefix += 1
        outcome = ("correct" if got == expected else "swapped" if got in tokens
                   else "partial" if prefix >= 8 else "fabricated")
        return {"outcome": outcome, "given": got, "prefix_chars": prefix}
    if probe["kind"] == "calls":
        items = value if isinstance(value, list) else re.split(r"[,，、\s]+", text)
        got = {re.sub(r"[`\"'()\[\]]", "", str(i)).strip() for i in items} & set(probe["options"])
        truth = set(probe["answer"])
        hits = sum((o in got) == (o in truth) for o in probe["options"])
        return {"outcome": "correct" if got == truth else "partial" if hits >= len(probe["options"]) - 2
                else "wrong", "given": sorted(got), "option_accuracy": round(hits / len(probe["options"]), 3)}
    if probe["kind"] == "callee":
        got = re.sub(r"\(.*$", "", text.strip().strip("`")).strip()
        return {"outcome": "correct" if got == probe["answer"] else "wrong", "given": got[:200]}
    ratio = difflib.SequenceMatcher(None, _normal(text), _normal(probe["answer"])).ratio()
    return {"outcome": "correct" if _normal(text) == _normal(probe["answer"])
            else "partial" if ratio >= 0.8 else "wrong", "given": text[:300], "similarity": round(ratio, 3)}


def _flatten(value) -> str:
    if isinstance(value, dict):
        return "\n".join(_flatten(v) for v in value.values())
    if isinstance(value, list):
        return "\n".join(_flatten(v) for v in value)
    return str(value)


def message_texts(messages: list[dict]) -> list[tuple[str, str]]:
    """(role, text) per message, covering content, reasoning and tool-call arguments."""
    result = []
    for message in messages:
        parts = []
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts += [p.get("text", "") for p in content if isinstance(p, dict)]
        if message.get("reasoning_content"):
            parts.append(message["reasoning_content"])
        for call in message.get("tool_calls") or []:
            raw = (call.get("function") or {}).get("arguments") or ""
            try:
                parts.append(_flatten(json.loads(raw)))
            except ValueError:
                parts.append(raw)
        result.append((message.get("role", ""), "\n".join(parts)))
    return result


def locate(messages: list[dict], needle: str | None) -> dict:
    """Where the needle sits in the input; distances count characters to the end."""
    texts = message_texts(messages)
    total = sum(len(text) for _, text in texts)
    if not needle:
        return {"present": None, "total_chars": total}
    hits, offset = [], 0
    for index, (role, text) in enumerate(texts):
        position = text.rfind(needle)
        if position >= 0:
            hits.append({"index": index, "role": role, "from_end": total - offset - position})
        offset += len(text)
    return {"present": bool(hits), "roles": sorted({h["role"] for h in hits}),
            "occurrences": len(hits), "total_chars": total,
            "chars_from_end": min(h["from_end"] for h in hits) if hits else None,
            "relative_depth": round(min(h["from_end"] for h in hits) / total, 4) if hits and total else None}
