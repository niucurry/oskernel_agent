"""从既有流水线产物提取四件套共用的短摘要。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import EvidenceRef, Finding, ModuleDigest, ReportDigest
from .readability import concise_module_summary, explain_terms_on_first_use, remove_ai_filler


def _path_ref(value: str) -> EvidenceRef:
    text = str(value or "")
    match = re.match(r"^(.*?)(?::|#L)(\d+)(?:-L?\d+)?$", text)
    if not match:
        return EvidenceRef(path=text)
    return EvidenceRef(path=match.group(1), line=int(match.group(2)))


def _severity(value: str) -> str:
    return value if value in {"info", "low", "medium", "high", "critical"} else "medium"


def _confidence_ratio(value: object, default: float = 0.5) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if confidence > 1:
        confidence /= 100
    return max(0.0, min(1.0, confidence))


def _reviewed_hardcode_findings(verdict: dict, integrity: dict) -> list[Finding]:
    raw_items = ((integrity.get("hardcode") or {}).get("findings") or [])
    raw_by_id = {
        str(item.get("signal_id") or f"{item.get('path')}:{item.get('line')}:{item.get('category')}"): item
        for item in raw_items
    }
    reviews = verdict.get("hardcode_reviews") or []
    candidates: list[dict] = []
    if reviews:
        for review in reviews:
            if not isinstance(review, dict) or review.get("status") == "cleared":
                continue
            raw = raw_by_id.get(str(review.get("signal_id") or ""), {})
            candidates.append({
                "reviewed": True,
                "category": str(review.get("category") or raw.get("category") or "未分类"),
                "status": str(review.get("status") or "suspected"),
                "method": str(review.get("method") or "需要结合源码复核实现方法。"),
                "reason": str(review.get("reason") or raw.get("analysis") or "证据不足。"),
                "confidence": _confidence_ratio(review.get("confidence")),
                "path": str(review.get("path") or raw.get("path") or ""),
                "line": review.get("line") or raw.get("line"),
                "excerpt": str(review.get("excerpt") or raw.get("excerpt") or "")[:500],
            })
    else:
        # 兼容旧 tree.json；新生成流程会强制要求 verdict.hardcode_reviews 覆盖所有线索。
        for raw in raw_items:
            candidates.append({
                "reviewed": False,
                "category": str(raw.get("category") or "未分类"),
                "status": "suspected",
                "method": "自动扫描命中，尚缺少结构化 AI 复核。",
                "reason": str(raw.get("analysis") or "需要结合完整源码复核。"),
                "confidence": min(_confidence_ratio(raw.get("confidence")), 0.59),
                "path": str(raw.get("path") or ""),
                "line": raw.get("line"),
                "excerpt": str(raw.get("excerpt") or "")[:500],
            })

    grouped: dict[str, list[dict]] = {}
    for item in candidates:
        grouped.setdefault(item["category"], []).append(item)

    findings: list[Finding] = []
    for category, items in grouped.items():
        representative = sorted(
            items,
            key=lambda item: (
                item["status"] != "confirmed", -item["confidence"], item["path"],
            ),
        )[0]
        confirmed = representative["status"] == "confirmed"
        if not representative["reviewed"]:
            label = "自动扫描硬编码线索（待 AI 复核）"
        else:
            label = "AI 复核硬编码问题" if confirmed else "AI 复核硬编码线索"
        count_text = f"共发现 {len(items)} 处同类实现。" if len(items) > 1 else ""
        detail = concise_module_summary(
            f"{count_text}实现方法：{representative['method']}。"
            f"AI 分析：{representative['reason']}"
        )
        evidence: list[EvidenceRef] = []
        seen_locations: set[tuple[str, int | None]] = set()
        for item in sorted(items, key=lambda value: (-value["confidence"], value["path"])):
            location = (item["path"], item["line"])
            if not item["path"] or location in seen_locations:
                continue
            seen_locations.add(location)
            evidence.append(EvidenceRef(
                path=item["path"], line=item["line"], excerpt=item["excerpt"],
            ))
        findings.append(Finding(
            title=explain_terms_on_first_use(f"{label}：{category}"),
            detail=detail,
            severity="high" if confirmed else "medium",
            confidence=representative["confidence"],
            source="description",
            evidence=evidence[:6],
        ))
    return findings


def _description_priority_key(item: Finding) -> tuple[int, int, float, str]:
    rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
    if item.title in {"编译失败", "运行失败", "编译日志缺失", "运行日志缺失"}:
        group = 0
    elif "硬编码" in item.title:
        group = 1
    elif item.title.startswith(("编译", "运行")):
        group = 2
    elif item.severity in {"critical", "high"}:
        group = 3
    else:
        group = 4
    return group, -rank[item.severity], -item.confidence, item.title


def description_priority_findings(digest: ReportDigest, limit: int = 8) -> list[Finding]:
    """描述报告专用顺序：构建/运行失败、硬编码、严重设计问题、其他。"""

    return sorted(digest.findings, key=_description_priority_key)[: max(0, limit)]


def description_digest_from_tree(tree: dict) -> ReportDigest:
    meta = tree.get("meta") or {}
    verdict = tree.get("verdict") or {}
    facts = tree.get("facts") or {}
    integrity = facts.get("integrity") or {}
    findings: list[Finding] = []

    for log_key, label in (("build_log", "编译"), ("run_log", "运行")):
        log = integrity.get(log_key) or {}
        if log.get("status") == "failed":
            details = "；".join(log.get("errors") or []) or f"{label}日志出现失败标记。"
            findings.append(Finding(
                title=f"{label}失败",
                detail=concise_module_summary(details),
                severity="high",
                confidence=0.95,
                source="description",
                evidence=[EvidenceRef(path=str(log.get("path") or ""))],
            ))
        elif log.get("status") == "missing":
            findings.append(Finding(
                title=f"{label}日志缺失",
                detail=f"指定的{label}日志不存在，无法核验该项结论。",
                severity="medium",
                confidence=1.0,
                source="description",
            ))
        elif log.get("status") == "not_provided":
            findings.append(Finding(
                title=f"{label}日志未提供",
                detail=f"未提供正式{label}日志，无法核验作品是否能够正常{label}。",
                severity="low",
                confidence=1.0,
                source="description",
            ))
        elif log.get("status") == "unknown":
            findings.append(Finding(
                title=f"{label}结果未能确认",
                detail=f"{label}日志没有可识别的成功或失败标记，不能据此判断结果。",
                severity="medium",
                confidence=1.0,
                source="description",
                evidence=[EvidenceRef(path=str(log.get("path") or ""))],
            ))

    hardcode_findings = _reviewed_hardcode_findings(verdict, integrity)
    findings.extend(hardcode_findings)
    hardcode_locations = {
        (evidence.path, evidence.line)
        for finding in hardcode_findings for evidence in finding.evidence
        if evidence.path
    }

    for item in verdict.get("issues") or []:
        quote = remove_ai_filler(str(item.get("quote") or ""))
        if not quote:
            continue
        issue_path = _path_ref(str(item.get("path") or ""))
        if (issue_path.path, issue_path.line) in hardcode_locations:
            continue
        findings.append(Finding(
            title=explain_terms_on_first_use(concise_module_summary(quote))[:80],
            detail=concise_module_summary(quote),
            severity=_severity(str(item.get("severity") or "medium")),
            confidence=_confidence_ratio(item.get("confidence"), default=0.5),
            source="description",
            evidence=[issue_path],
        ))

    modules: list[ModuleDigest] = []
    for subsystem in ((tree.get("tree") or {}).get("children") or []):
        for module in subsystem.get("children") or []:
            summary = module.get("brief") or module.get("summary") or module.get("content") or "未形成模块摘要。"
            modules.append(ModuleDigest(
                name=str(module.get("name") or "未命名模块"),
                summary=concise_module_summary(str(summary)),
                evidence_count=len(module.get("highlights") or []) + len(module.get("issues") or []),
            ))

    conclusion = explain_terms_on_first_use(remove_ai_filler(
        str(verdict.get("one_line") or "已完成源码结构分析，结论见问题清单。")
    ))
    reviews = verdict.get("hardcode_reviews") or []
    ordered_findings = sorted(findings, key=_description_priority_key)
    return ReportDigest(
        repo_id=str(meta.get("repo") or (facts.get("meta") or {}).get("repo_id") or "unknown"),
        kind="description",
        conclusion=conclusion[:240],
        confidence=0.8 if findings else 0.65,
        findings=ordered_findings[:8],
        modules=modules[:32],
        metrics={
            "indexed_files": int(meta.get("indexed_files") or 0),
            "hardcode_signals": len((integrity.get("hardcode") or {}).get("findings") or []),
            "hardcode_candidates": int(
                (integrity.get("hardcode") or {}).get("candidate_count")
                or len((integrity.get("hardcode") or {}).get("findings") or [])
            ),
            "hardcode_scan_truncated": bool(
                (integrity.get("hardcode") or {}).get("truncated")
            ),
            "hardcode_confirmed": sum(
                1 for item in reviews if isinstance(item, dict) and item.get("status") == "confirmed"
            ),
            "hardcode_suspected": sum(
                1 for item in reviews if isinstance(item, dict) and item.get("status") == "suspected"
            ),
            "hardcode_cleared": sum(
                1 for item in reviews if isinstance(item, dict) and item.get("status") == "cleared"
            ),
            "build_log_status": (integrity.get("build_log") or {}).get("status", "not_provided"),
            "run_log_status": (integrity.get("run_log") or {}).get("status", "not_provided"),
        },
    )


_MODULE_NAMES = {
    "sched": "进程调度", "mm": "内存管理", "fs": "文件系统",
    "trap": "异常处理", "syscall": "系统调用", "signal": "信号",
    "ipc": "进程间通信", "sync": "并发同步", "time": "时钟定时",
    "net": "网络", "driver": "设备驱动", "arch": "硬件架构",
    "security": "安全权限", "runtime": "运行时支持", "macro": "宏",
    "other": "其他",
}


def comparison_digest(
    repo_id: str,
    closest_source: str,
    submodule_stats: dict,
    *,
    exact_file_matches: int = 0,
    ai_detect_data: dict | None = None,
) -> ReportDigest:
    """生成“只对比一个最近历史作品”的短摘要。"""
    modules: list[ModuleDigest] = []
    confirmed = review = total = 0
    for module, stats in submodule_stats.items():
        module_total = int(stats.get("total") or 0)
        module_confirmed = int(stats.get("confirmed") or 0)
        module_review = int(stats.get("review") or 0)
        if not module_total:
            continue
        pct = round(module_confirmed / module_total * 100, 1)
        modules.append(ModuleDigest(
            name=_MODULE_NAMES.get(module, module),
            summary=(f"{module_confirmed}/{module_total} 个函数形成高置信同源证据；"
                     f"另有 {module_review} 个函数需要人工复核。"),
            similarity_pct=pct,
            evidence_count=module_confirmed + module_review,
        ))
        confirmed += module_confirmed
        review += module_review
        total += module_total
    modules.sort(key=lambda item: (-(item.similarity_pct or 0), -item.evidence_count, item.name))
    overall = round(confirmed / total * 100, 1) if total else 0.0
    source_text = closest_source or "未形成可靠的最近历史作品"
    conclusion = (
        f"与 {source_text} 最接近；按可比函数口径，{confirmed}/{total} 个函数形成"
        f"高置信同源证据，整体比例 {overall}%。"
        if closest_source else
        "当前证据不足以确定唯一的最近历史作品。"
    )
    findings: list[Finding] = []
    if closest_source and confirmed:
        findings.append(Finding(
            title="发现跨作品同源代码",
            detail=(f"最近来源为 {closest_source}；高置信函数比例 {overall}%。"
                    "该比例用于安排人工核查，不等同于抄袭认定。"),
            severity="high" if overall >= 30 else "medium",
            confidence=0.95,
            source="comparison",
        ))
    if review:
        findings.append(Finding(
            title="仍有模型复核难例",
            detail=f"{review} 个函数存在相似信号，但证据不足以归入高置信同源代码。",
            severity="medium", confidence=0.75, source="comparison",
        ))
    if exact_file_matches:
        findings.append(Finding(
            title="存在整文件相同证据",
            detail=f"与最近历史作品发现 {exact_file_matches} 个整文件规范化哈希相同。",
            severity="high", confidence=0.99, source="comparison",
        ))

    ai_overall = ((ai_detect_data or {}).get("aggregated") or {}).get("overall") or {}
    llm_count = int(ai_overall.get("llm_count") or 0)
    if llm_count:
        findings.append(Finding(
            title="AI 生成代码检测出现高风险信号",
            detail=(f"实际模型将 {llm_count} 个未归入历史借鉴的函数标为 AI 倾向。"
                    "该结果误报风险较高，只用于人工复核。"),
            severity="medium", confidence=0.7, source="comparison",
        ))

    return ReportDigest(
        repo_id=repo_id,
        kind="comparison",
        conclusion=conclusion[:240],
        confidence=0.95 if closest_source else 0.55,
        findings=findings[:8],
        modules=modules[:32],
        metrics={
            "closest_source": closest_source,
            "overall_similarity_pct": overall,
            "confirmed_functions": confirmed,
            "review_functions": review,
            "comparable_functions": total,
            "exact_file_matches": exact_file_matches,
            "ai_llm_functions": llm_count,
        },
    )


def write_digest(path: str | Path, digest: ReportDigest) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(digest.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return target
