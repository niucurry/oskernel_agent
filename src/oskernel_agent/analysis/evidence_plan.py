"""Evaluator-owned evidence requirements, fixed before report generation.

This measures retrieval of an explicit checklist, not semantic sufficiency or
recall against all relevant repository code. No scores are computed here.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath

from .read_coverage import check_requirements, source_functions


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"评价清单缺少 {label}")
    return value.strip()


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} 必须是正整数")
    return value


def _resolve_target(repo: Path, raw: dict, inventories: dict) -> dict:
    target = {key: _text(raw.get(key), f"evidence.{key}") for key in ("id", "purpose", "path")}
    path = PurePosixPath(target["path"].replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in str(path) or str(path) == ".":
        raise ValueError(f"证据路径必须位于仓库内：{target['path']}")
    try:
        (repo / str(path)).resolve().relative_to(repo)
    except ValueError as exc:
        raise ValueError(f"证据路径越出仓库：{path}") from exc
    target["path"] = str(path)
    symbol = raw.get("symbol")
    if symbol is not None:
        target["symbol"] = _text(symbol, "evidence.symbol")
        symbol = target["symbol"]
        if "end_line" in raw:
            raise ValueError("symbol 定位不能同时指定 end_line；同名函数可用 start_line 区分")
        if "start_line" in raw:
            target["start_line"] = _positive(raw["start_line"], "start_line")
    else:
        target["start_line"] = _positive(raw.get("start_line"), "start_line")
        target["end_line"] = _positive(raw.get("end_line"), "end_line")
        if target["end_line"] < target["start_line"]:
            raise ValueError("end_line 不能小于 start_line")
    if str(path) not in inventories:
        inventories[str(path)] = source_functions(repo, str(path))
    inventory = inventories[str(path)]
    target.update(sha256=inventory.get("sha256"), parser_status=inventory["status"], requirements=[])
    if not target["sha256"]:
        return dict(target, resolution="missing_or_unreadable")
    if symbol is not None:
        if inventory["status"] != "parsed":
            return dict(target, resolution="unreliable_parse")
        matches = [f for f in inventory["functions"] if f["symbol"] == symbol
                   and ("start_line" not in target or f["start_line"] == target["start_line"])]
        if len(matches) != 1:
            return dict(target, resolution="ambiguous_symbol" if matches else "symbol_not_found")
        low, high = matches[0]["start_line"], matches[0]["end_line"]
        scope = "function"
    else:
        low, high = target["start_line"], target["end_line"]
        if high > inventory["total_lines"]:
            return dict(target, resolution="invalid_range")
        scope = "declared_lines"
    target["requirements"] = [{"path": inventory["path"], "sha256": inventory["sha256"],
        "start_line": low, "end_line": high, "symbol": symbol or "", "scope": scope,
        "parser_status": inventory["status"]}]
    return dict(target, resolution="resolved")


def load_evidence_plan(repo: Path, path: Path) -> dict:
    """Read only an explicitly supplied, external checklist; never auto-discover it."""
    repo, path = repo.resolve(), path.expanduser().resolve()
    if path == repo or repo in path.parents:
        raise ValueError("评价清单必须由评测方放在被评价仓库之外")
    data = path.read_bytes()
    if len(data) > 1_000_000:
        raise ValueError("评价清单超过 1 MB，请缩小本轮评价范围")
    raw = json.loads(data)
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("评价清单 version 必须为 1")
    plan_id = _text(raw.get("plan_id"), "plan_id")
    items = raw.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        raise ValueError("评价清单必须包含 1–50 个 items")
    resolved, ids, inventories = [], set(), {}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError("items 必须由对象组成")
        item = {key: _text(raw_item.get(key), f"item.{key}") for key in (
            "id", "dimension", "question", "scope", "decision_rule", "score_effect")}
        if item["id"] in ids:
            raise ValueError(f"重复评价项：{item['id']}")
        ids.add(item["id"])
        if raw_item.get("on_insufficient", "needs_review") != "needs_review":
            raise ValueError("当前仅支持 on_insufficient=needs_review，不能把缺证据当作通过或失败")
        item["on_insufficient"] = "needs_review"
        if "score_impact" in raw_item:
            impact = raw_item["score_impact"]
            if type(impact) not in (int, float) or not 0 <= impact <= 100:
                raise ValueError("score_impact 必须为 0–100 的数值")
            item["score_impact"] = impact
        subsystems = raw_item.get("subsystems", [])
        if not isinstance(subsystems, list) or any(not isinstance(s, str) or not s.strip() for s in subsystems):
            raise ValueError("subsystems 必须是子系统名称列表；空列表表示仅在总评核对")
        item["subsystems"] = sorted(set(s.strip() for s in subsystems))
        evidence = raw_item.get("evidence")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 30:
            raise ValueError("每个评价项必须包含 1–30 条 evidence")
        targets, target_ids = [], set()
        for target in evidence:
            if not isinstance(target, dict):
                raise ValueError("evidence 必须由对象组成")
            target = _resolve_target(repo, target, inventories)
            if target["id"] in target_ids:
                raise ValueError(f"评价项 {item['id']} 存在重复证据 ID")
            target_ids.add(target["id"])
            targets.append(target)
        resolved.append(dict(item, evidence=targets))
    result = {"version": 1, "plan_id": plan_id, "plan_path": str(path),
              "plan_sha256": hashlib.sha256(data).hexdigest(), "repo_path": str(repo), "items": resolved}
    # The fingerprint covers the declared evidence only, not the entire repo.
    result["snapshot_sha256"] = _digest(result)
    result["snapshot_scope"] = "declared_evidence_files_only"
    return result


def scoped_plan(plan: dict | None, subsystem: str | None = None) -> dict | None:
    if not plan:
        return None
    result = copy.deepcopy(plan)
    if subsystem is not None:
        result["items"] = [i for i in result["items"]
                           if subsystem in i["subsystems"] or "*" in i["subsystems"]]
    return result if result["items"] else None


def plan_requirements(plan: dict) -> list[dict]:
    unique = {}
    for item in plan["items"]:
        for target in item["evidence"]:
            for req in target["requirements"]:
                key = (req["path"], req["start_line"], req["end_line"], req["scope"])
                unique[key] = dict(req)
    return list(unique.values())


def check_evidence_plan(repo: Path, plan: dict, audit: dict | None) -> dict:
    items = []
    for item in plan["items"]:
        targets = []
        for target in item["evidence"]:
            checked = check_requirements(repo, audit, target["requirements"])
            if target["resolution"] != "resolved":
                status = "unresolved_location"
            elif not audit or audit.get("stream_status") != "complete":
                status = "unknown"
            elif checked and all(r["status"] == "returned" for r in checked):
                status = "returned"
            else:
                status = "needs_evidence"
            targets.append(dict(target, status=status, checked_ranges=checked))
        count = sum(t["status"] == "returned" for t in targets)
        items.append(dict(item, evidence=targets, returned_count=count, required_count=len(targets),
                          status="returned" if count == len(targets) else "needs_evidence",
                          judgment_status="not_verified", score_status="not_computed"))
    result = copy.deepcopy(plan)
    result.update(items=items,
                  status="returned" if all(i["status"] == "returned" for i in items) else "needs_evidence",
                  run_id=(audit or {}).get("run_id"), artifact=(audit or {}).get("artifact"),
                  relevant_evidence_recall="unknown")
    return result


def plan_prompt(plan: dict) -> str:
    return (
        "\n\n【评测方预先确定的评价项与必要证据】\n"
        "逐项核对 question 和 scope，使用原生 read 或 MCP read_file 完整读取 evidence 中的目标范围；"
        "函数每次最多 200 行，长函数分段读完。必要证据不受原任务的工具调用次数上限限制。"
        "这些是最低读取要求，可以继续检索相关上下文。无法定位的项目必须说明证据不足，不能推断功能不存在。"
        "缺证据只列为待核验，不能据此扣分或按未发现问题给满分。"
        "不得通过省略某个评价项或报告引用来取消读取要求。读齐不代表行为验证通过；"
        "decision_rule 和 score_effect 是评测方提供的待验证规则，本步骤不据此自动生成新的分数。\n"
        + json.dumps({"plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"],
                      "items": plan["items"]}, ensure_ascii=False)
    )
