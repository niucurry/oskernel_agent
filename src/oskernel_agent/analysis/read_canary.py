"""Prepare copy-only canary trials and score retrieval separately from selection."""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from pathlib import Path

from .read_coverage import check_requirements, source_functions, syntax_tree, walk

MARKER = "READ_AUDIT_CANARY:"
POSITIONS = ("start", "mid", "end")
_STATEMENTS = {"expression_statement", "return_statement", "declaration", "break_statement",
               "continue_statement", "goto_statement", "let_declaration"}


def _statement_ends(tree, function: dict) -> list[tuple[int, int]]:
    """(line, byte) just after `;` of body statements; an inline comment there keeps lines."""
    ends = set()
    for node in walk(tree.root_node):
        if (node.type in _STATEMENTS and not node.has_error and node.text.endswith(b";")
                and node.start_byte > function["body_byte"]
                and node.end_point[0] + 1 <= function["end_line"]
                and (node.parent is None or node.parent.type not in {"for_statement", "for_range_loop"})):
            ends.add((node.end_point[0] + 1, node.end_byte))
    return sorted(ends)


def _write_json(path: Path, value: object, *, private: bool = False) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if private:
        path.chmod(0o600)


def prepare_canary(repo: Path, targets: list[dict], destination: Path,
                   gold: list[dict] | None = None, *, positions: tuple[str, ...] = ("start",),
                   decoys: list[dict] | None = None) -> dict:
    """Targets must be independently confirmed; random tokens never enter prompts.

    Inserting an inline comment after the function body's `{` (start) or after a
    body statement's `;` (mid/end) preserves line numbers. This supports parsed
    C/C++/Rust functions, not macros/assembly. Decoys get markers but are not
    listed in the public targets.
    """
    repo, destination = repo.resolve(), destination.resolve()
    if repo == destination or repo in destination.parents or destination in repo.parents:
        raise ValueError("实验目录必须在源仓库之外，且不能是源仓库的父目录")
    if destination.exists():
        raise ValueError("实验目录已存在；请使用新的目录，避免覆盖结果")
    if not targets:
        raise ValueError("目标函数清单不能为空")
    if "start" not in positions or not set(positions) <= set(POSITIONS):
        raise ValueError("标记位置只能取 start/mid/end，且必须包含 start")
    selected = []
    identities = set()
    for role, target in [("target", t) for t in targets] + [("decoy", t) for t in decoys or []]:
        if role == "target" and (target.get("confirmed") is not True
                                 or not str(target.get("reason", "")).strip()):
            raise ValueError("每个目标必须提供 confirmed=true 和相关性 reason")
        raw = str(target.get("path", ""))
        inv = source_functions(repo, raw)
        if inv["status"] != "parsed":
            raise ValueError(f"{raw} 无完整语法树，不能可靠放置标记：{inv['status']}")
        line = target.get("line", target.get("start_line"))
        candidates = [f for f in inv["functions"] if f["symbol"] == target.get("symbol")
                      and (line is None or f["start_line"] == line)]
        if len(candidates) != 1:
            raise ValueError(f"{raw} 中目标函数不存在或不唯一，请提供函数起始 line")
        function = candidates[0]
        identity = (inv["path"], function["start_line"])
        if identity in identities:
            raise ValueError("同一个函数不能重复插入标记")
        identities.add(identity)
        path = repo / inv["path"]
        # Do not edit through symlinks, including symlinked parent directories.
        if (repo / raw).absolute() != path or any(p.is_symlink() for p in [path, *path.parents]):
            raise ValueError("标记目标不能经过符号链接")
        data = path.read_bytes()
        if MARKER.encode() in data:
            raise ValueError("源文件已有审计标记，请使用未插入标记的原始仓库")
        selected.append(dict(function, path=inv["path"], original_sha256=inv["sha256"],
                             reason=target.get("reason", ""), token=secrets.token_hex(16),
                             role=role))
    # Gold labels are validated independently, but do not constrain selection.
    gold_ids = None
    if gold is not None:
        gold_ids = []
        for label in gold:
            inv = source_functions(repo, str(label.get("path", "")))
            line = label.get("line", label.get("start_line"))
            matches = [f for f in inv["functions"] if f["symbol"] == label.get("symbol")
                       and (line is None or f["start_line"] == line)]
            if label.get("confirmed") is not True or len(matches) != 1:
                raise ValueError("人工相关函数清单必须逐项确认，且能唯一定位")
            gold_ids.append({"path": inv["path"], "symbol": matches[0]["symbol"],
                             "start_line": matches[0]["start_line"]})
        if not gold_ids:
            raise ValueError("提供人工相关函数清单时，清单不能为空")
    destination.mkdir(parents=True, mode=0o700)
    copy = destination / "repo"
    shutil.copytree(repo, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    by_file: dict[str, list[dict]] = {}
    for target in selected:
        by_file.setdefault(target["path"], []).append(target)
    for path, group in by_file.items():
        source = copy / path
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != group[0]["original_sha256"]:
            raise ValueError("复制期间源文件变化，请重新创建实验")
        tree = syntax_tree(Path(path).suffix.lower(), data) if set(positions) != {"start"} else None
        inserts = []
        for target in group:
            # Insertions never add newlines, so lines from the original bytes stay valid.
            target["marker_line"] = data[:target["body_byte"] + 1].count(b"\n") + 1
            markers = [{"position": "start", "line": target["marker_line"],
                        "byte": target["body_byte"] + 1, "token": target["token"]}]
            ends = [e for e in (_statement_ends(tree, target) if tree else [])
                    if e[0] != target["marker_line"]]
            chosen = {}
            if "end" in positions and ends:
                chosen["end"] = ends[-1]
            middle = (target["start_line"] + target["end_line"]) / 2
            rest = [e for e in ends if e[0] != chosen.get("end", (0,))[0]]
            if "mid" in positions and rest:
                chosen["mid"] = min(rest, key=lambda e: abs(e[0] - middle))
            for position, (line, byte) in chosen.items():
                markers.append({"position": position, "line": line, "byte": byte,
                                "token": secrets.token_hex(16)})
            target["markers"] = sorted(markers, key=lambda m: m["line"])
            inserts += target["markers"]
        for marker in sorted(inserts, key=lambda m: m["byte"], reverse=True):
            token = f" /* {MARKER}{marker['token']} */ ".encode()
            data = data[:marker["byte"]] + token + data[marker["byte"]:]
        source.write_bytes(data)
        inv = source_functions(copy, path)
        if inv["status"] != "parsed" or any(
                not any(f["symbol"] == t["symbol"] and f["start_line"] == t["start_line"]
                        and f["end_line"] == t["end_line"] for f in inv["functions"]) for t in group):
            raise ValueError("插入注释后语法检查失败，实验不可用")
        for target in group:
            target["sha256"] = hashlib.sha256(data).hexdigest()
            target["scope"] = "function"
    decoy_items = [t for t in selected if t["role"] == "decoy"]
    selected = [t for t in selected if t["role"] == "target"]
    manifest = {"schema_version": 1, "repo": str(copy), "original_repo": str(repo),
                "targets": selected, "decoys": decoy_items, "positions": list(positions),
                "gold_functions": gold_ids,
                "limitations": ["not_proof_of_understanding", "not_an_adversarial_access_boundary"]}
    private = destination / "private"
    private.mkdir(mode=0o700)
    _write_json(private / "manifest.json", manifest, private=True)
    public = [{k: t[k] for k in ("path", "symbol", "start_line", "end_line", "reason")}
              for t in selected]
    _write_json(destination / "targets.json", public)
    return {"repo": str(copy), "targets": len(selected),
            "manifest": str(private / "manifest.json")}


def evaluate_canary(manifest: dict, audit: dict, response: dict) -> dict:
    repo = Path(manifest["repo"])
    targets = manifest["targets"]
    functions = check_requirements(repo, audit, targets)
    marker_reqs = [dict(t, start_line=t["marker_line"], end_line=t["marker_line"])
                   for t in targets]
    markers = check_requirements(repo, audit, marker_reqs)
    observations = response.get("canary_observations", [])
    if not isinstance(observations, list):
        observations = []
    observations = [o for o in observations if isinstance(o, dict)]
    rows = []
    accepted = set()
    for target, function, marker in zip(targets, functions, markers):
        indices = {i for i, obs in enumerate(observations)
                   if obs.get("path") == target["path"] and obs.get("symbol") == target["symbol"]
                   and obs.get("token") == target["token"]}
        accepted.update(indices)
        reported = bool(indices)
        returned = marker["status"] == "returned"
        rows.append({"path": target["path"], "symbol": target["symbol"],
                     "start_line": target["start_line"], "marker_returned": returned,
                     "marker_status": marker["status"], "reported_correctly": reported,
                     "corroborated": reported and returned,
                     "function_returned": function["status"] == "returned",
                     "missing_ranges": function["missing_ranges"]})
    gold = manifest.get("gold_functions")
    selection = {"status": "unknown", "reason": "no_independent_gold_functions"}
    if gold is not None:
        selected = {(t["path"], t["start_line"]) for t in targets}
        expected = {(t["path"], t["start_line"]) for t in gold}
        selection = {"status": "measured_against_supplied_labels", "expected": len(expected),
                     "selected_relevant": len(selected & expected),
                     "recall": len(selected & expected) / len(expected),
                     "omitted": [g for g in gold if (g["path"], g["start_line"]) not in selected]}
    return {"selected_functions": len(rows),
            "fully_returned_functions": sum(r["function_returned"] for r in rows),
            "returned_markers": sum(r["marker_returned"] for r in rows),
            "correct_reports": sum(r["reported_correctly"] for r in rows),
            "corroborated_reports": sum(r["corroborated"] for r in rows),
            "incorrect_or_unknown_observations": len(observations) - len(accepted),
            "selection": selection, "targets": rows,
            "run_id": audit.get("run_id"), "stream_status": audit.get("stream_status", "unknown")}
