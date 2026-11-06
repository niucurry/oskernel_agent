"""Scoped, evaluator-linked assertions with host-checked source provenance.

Exact quotation and checklist coverage qualify an assertion for review. Neither
qualifies it as true, executed, or eligible for an automatically computed score.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .evidence_plan import check_evidence_plan
from .source_evidence import check_source_quote


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2000


def _strings(value: object, *, required: bool = False) -> bool:
    return isinstance(value, list) and (not required or bool(value)) and len(value) <= 30 and all(_text(v) for v in value)


def _known(value: object, keys: dict | set) -> bool:
    return isinstance(value, str) and value in keys


def assertion_shape_errors(plan: dict, result: dict) -> list[str]:
    rows = result.get("evaluation_claims")
    if not isinstance(rows, list):
        return ["evaluation_claims 必须为列表，每个评价项恰好一条"]
    items = {i["id"]: i for i in plan["items"]}
    errors = []
    if any(not isinstance(r, dict) or not _known(r.get("item_id"), items) for r in rows):
        errors.append("包含未知评价项或无效条目")
    for key, item in items.items():
        matches = [r for r in rows if isinstance(r, dict) and r.get("item_id") == key]
        if len(matches) != 1:
            errors.append(f"{key}：缺失或重复")
            continue
        row = matches[0]
        if not _text(row.get("statement")) or row.get("scope") != item["scope"]:
            errors.append(f"{key}：缺少断言或 scope 与预定范围不同")
        if not _strings(row.get("conditions"), required=True) or not _strings(row.get("counterevidence")):
            errors.append(f"{key}：conditions 须为非空字符串列表，counterevidence 须为字符串列表")
        if row.get("assessment") not in ("source_supported", "insufficient"):
            errors.append(f"{key}：assessment 仅允许 source_supported 或 insufficient")
        if row.get("validation_need") not in ("execution", "static_review"):
            errors.append(f"{key}：须声明 validation_need 为 execution 或 static_review")
        evidence_ids = {e["id"] for e in item["evidence"]}
        missing = row.get("missing_evidence")
        if not _strings(missing) or any(m not in evidence_ids for m in (missing if isinstance(missing, list) else [])):
            errors.append(f"{key}：missing_evidence 必须是本项证据 ID 列表")
        if row.get("assessment") == "source_supported" and missing:
            errors.append(f"{key}：声明缺证据时不能同时声明 source_supported")
        if not isinstance(row.get("limitations"), str) or (row.get("assessment") == "insufficient" and not _text(row["limitations"])):
            errors.append(f"{key}：limitations 须为字符串，证据不足时须说明原因")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 60 or any(
            not isinstance(e, dict) or not _known(e.get("evidence_id"), evidence_ids)
            or not _text(e.get("ref")) or not isinstance(e.get("quote"), str)
            or not e["quote"].strip() or len(e["quote"]) > 80_000 for e in evidence):
            errors.append(f"{key}：evidence 须包含本项 evidence_id、ref 和完整原文 quote")
    return errors


def check_assertions(repo: Path, plan: dict, result: dict, audit: dict | None,
                     review: dict | None = None) -> dict:
    """Recompute all states from this exact report, frozen plan and host receipt."""
    shape_errors = assertion_shape_errors(plan, result)
    rows = result.get("evaluation_claims")
    rows = rows if isinstance(rows, list) else []
    acquisition = check_evidence_plan(repo, plan, audit)
    item_checks = {i["id"]: i for i in acquisition["items"]}
    reviews = {c["id"]: c for c in (review or {}).get("claims", [])}
    review_matches = bool(review and review.get("report_sha256") == _hash(result))
    output = []
    for item in plan["items"]:
        matches = [r for r in rows if isinstance(r, dict) and r.get("item_id") == item["id"]]
        row = matches[0] if len(matches) == 1 else {}
        errors = ["invalid_assertion_structure"] if shape_errors else []
        if not audit or audit.get("stream_status") != "complete" or not audit.get("session_ids"):
            errors.append("incomplete_author_session")
        targets = {e["id"]: e for e in item["evidence"]}
        if item_checks[item["id"]]["status"] != "returned":
            errors.append("required_evidence_not_returned")
        verified, supported_ids, requirements = [], set(), []
        for target in targets.values():
            requirements.extend(target["requirements"])
        raw_evidence = row.get("evidence")
        for quote in raw_evidence if isinstance(raw_evidence, list) else []:
            checked, error = check_source_quote(repo, audit, quote)
            if error:
                errors.append(error)
                continue
            target = targets.get(quote["evidence_id"]) if _known(quote.get("evidence_id"), targets) else None
            if not target or not any(r["path"] == checked["path"]
                                      and r["start_line"] <= checked["start_line"] <= checked["end_line"] <= r["end_line"]
                                      for r in target["requirements"]):
                errors.append("quote_not_in_declared_target")
                continue
            supported_ids.add(target["id"])
            requirements.extend(checked["requirements"])
            verified.append({"evidence_id": target["id"], "ref": checked["ref"], "quote": checked["quote"]})
        unquoted = sorted(set(targets) - supported_ids)
        if unquoted:
            errors.append("missing_target_quotes")
        claim_id = _hash({"snapshot_sha256": plan["snapshot_sha256"], "item": item, "claim": row})[:24]
        status = "insufficient"
        if not errors and row.get("assessment") == "source_supported":
            status = "ready_for_review"
            if review is not None:
                checked_review = reviews.get(claim_id) if review_matches else None
                status = (checked_review or {}).get("status", "not_reviewed")
        unique = {(r["path"], r["start_line"], r["end_line"], r["scope"]): r for r in requirements}
        output.append({"item_id": item["id"], "claim_id": claim_id,
            "dimension": item["dimension"], "question": item["question"], "scope": item["scope"],
            "decision_rule": item["decision_rule"], "score_effect": item["score_effect"],
            "statement": row.get("statement", "未形成有效断言"), "conditions": row.get("conditions", []),
            "counterevidence": row.get("counterevidence", []), "limitations": row.get("limitations", ""),
            "validation_need": row.get("validation_need"), "model_assessment": row.get("assessment"),
            "missing_evidence": row.get("missing_evidence", []), "unquoted_targets": unquoted,
            "evidence": verified, "requirements": list(unique.values()), "status": status,
            "errors": sorted(set(errors)), "execution_status": "not_run", "score_status": "not_computed"})
    if (review or {}).get("recall_check"):
        from ..engines.recall_check import dependencies
        for item in output:
            item["recall_dependencies"] = dependencies(review["recall_check"], item["requirements"])
    return {"version": 1, "plan_sha256": plan["plan_sha256"], "snapshot_sha256": plan["snapshot_sha256"],
            "report_sha256": _hash(result), "run_id": (audit or {}).get("run_id"),
            "read_artifact": (audit or {}).get("artifact"), "shape_errors": shape_errors,
            "items": output, "review_artifact": (review or {}).get("artifact"),
            "limitations": ["source_support_not_execution", "conditions_need_semantic_review",
                            "not_all_report_prose_derived_from_assertions", "no_verified_score_mapping"]}


ASSERTION_PROMPT = (
    "\n\n【按评价项先形成事实断言】\n"
    "先逐项形成可核验断言，再据此组织报告正文。在原有 JSON 中增加 evaluation_claims 数组，"
    "清单每个 item 恰好一条；不要省略证据不足的项目，不改变原有输出文件和字段。"
    "item_id 对应清单 id，scope 逐字复制该项 scope；conditions 列出成立条件，可进一步限定范围。"
    "statement 写清一个主要结论，区分总调用次数与额外重试次数、默认配置与覆盖配置。"
    "每项必要证据至少提供一段带 evidence_id 的完整源码行，保留缩进，不含工具行号前缀，"
    "每段最多 40 行。ref 使用相对路径:起始行-结束行；quote 不受正文的 200 字符摘要限制。"
    "counterevidence 列出仍需检查的配置覆盖、其他入口或边界路径，不把猜测当成已发现的问题。"
    "不能确认时 assessment=insufficient，说明 limitations 和缺失的证据 ID；不能编造引用。"
    "source_supported 只是作者认为源码支持，后续还需独立复核；具体运行次数、输出和边界行为的"
    "validation_need=execution，纯静态事实为 static_review。本轮没有执行验证，禁止填写测试通过或分数。\n"
    '单条格式：{"item_id":"清单id","statement":"限定条件下的明确结论",'
    '"scope":"逐字复制清单范围","conditions":["成立条件"],'
    '"evidence":[{"evidence_id":"清单证据id","ref":"file.c:1-3","quote":"完整源码行"}],'
    '"counterevidence":["待核查的反例方向"],"missing_evidence":[],"limitations":"",'
    '"assessment":"source_supported|insufficient","validation_need":"execution|static_review"}。'
)
