"""从既有流水线产物提取四件套共用的短摘要。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import EvidenceRef, Finding, ModuleDigest, ReportDigest
from .readability import (
    clip_at_sentence,
    concise_module_summary,
    explain_terms_on_first_use,
    remove_ai_filler,
)


DESCRIPTION_SUBSYSTEM_ORDER = (
    "启动模块", "内存管理", "进程管理", "文件系统", "设备管理",
    "系统调用", "硬件抽象", "其他",
)

GENERIC_DESCRIPTION_SUBSYSTEMS = {"其他", "其他模块", "未分类", "基础设施"}


def description_review_sections(tree: dict) -> list[tuple[str, dict, dict | None]]:
    """返回评委报告的并列分析维度，并把笼统“其他”拆成真实子模块。"""
    root = tree.get("tree") or {}
    top_nodes = [
        node for node in (root.get("children") or []) if isinstance(node, dict)
    ]
    by_name = {str(node.get("name") or ""): node for node in top_nodes}
    ordered = [by_name[name] for name in DESCRIPTION_SUBSYSTEM_ORDER if name in by_name]
    ordered.extend(
        node for node in top_nodes
        if str(node.get("name") or "") not in DESCRIPTION_SUBSYSTEM_ORDER
    )

    sections: list[tuple[str, dict, dict | None]] = []
    used_names: set[str] = set()
    for node in ordered:
        name = str(node.get("name") or "未命名模块")
        children = [
            child for child in (node.get("children") or []) if isinstance(child, dict)
        ]
        candidates = children if name in GENERIC_DESCRIPTION_SUBSYSTEMS and children else [node]
        for candidate in candidates:
            candidate_name = str(candidate.get("name") or name)
            display_name = candidate_name
            if display_name in used_names:
                display_name = f"{name} · {candidate_name}"
            used_names.add(display_name)
            sections.append((display_name, candidate, node if candidate is not node else None))
    return sections


def _path_ref(value: str) -> EvidenceRef:
    text = str(value or "")
    match = re.match(r"^(.*?)(?::|#L)(\d+)(?:-L?\d+)?$", text)
    if not match:
        return EvidenceRef(path=text)
    return EvidenceRef(path=match.group(1), line=int(match.group(2)))


def _severity(value: str, text: str = "") -> str:
    severity = value if value in {"info", "low", "medium", "high", "critical"} else "medium"
    if severity == "medium" and any(
        term in text for term in ("重复代码", "缺乏复用", "重复定义")
    ):
        return "low"
    return severity


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


def normalize_description_claim(value: str, path: str, facts: dict) -> str:
    """用事实档案约束容易被误写成“已可用”的系统调用数量声明。"""
    text = remove_ai_filler(str(value or ""))
    syscall = (facts.get("syscall") or {}) if isinstance(facts, dict) else {}
    try:
        count = int(syscall.get("standard_count"))
        total = int(syscall.get("standard_total"))
    except (TypeError, ValueError):
        return text
    has_count_claim = bool(re.search(
        r"\b\d+\s*/\s*\d+\b|(?<!\d)\d+\s*个(?:标准\s*)?(?:Linux\s*)?(?:系统调用|syscall)",
        text,
        re.I,
    ))
    if not has_count_claim or not ("系统调用" in text or "syscall" in text.casefold()):
        return text
    if "nisyscall" in text.casefold() or "ENOSYS" in text:
        return (
            f"sys_nisyscall 对未实现编号返回 ENOSYS；函数定义正则扫描识别到 "
            f"{count}/{total} 个标准名称，该数字不代表接口语义可用。"
        )
    dispatch = syscall.get("dispatch_count")
    dispatch_note = (
        f"；SYS_* 分发表静态识别到 {int(dispatch)} 个不同分支"
        if isinstance(dispatch, int) and dispatch >= 0 else ""
    )
    return (
        f"函数定义正则扫描识别到 {count}/{total} 个标准系统调用名称；"
        f"该计数只表示接口线索{dispatch_note}，不代表语义可用或测试通过。"
    )


def normalize_description_conclusion(value: str, verdict: dict) -> str:
    """Make hardcode counts in the headline agree with structured reviews."""
    text = explain_terms_on_first_use(remove_ai_filler(str(value or "")))
    reviews = [item for item in (verdict.get("hardcode_reviews") or []) if isinstance(item, dict)]
    confirmed = sum(item.get("status") == "confirmed" for item in reviews)
    suspected = sum(item.get("status") == "suspected" for item in reviews)
    pattern = re.compile(
        r"硬编码(?:存在|有|复核发现)?\s*\d+\s*(?:处|条)(?:嫌疑|疑似(?:线索|问题)?)"
    )
    if not pattern.search(text):
        return text
    replacement = (
        f"硬编码复核确认 {confirmed} 条、疑似 {suspected} 条"
        if confirmed else f"硬编码复核发现 {suspected} 条疑似线索、无确认项"
    )
    return pattern.sub(replacement, text, count=1)


def _description_priority_key(item: Finding) -> tuple[int, int, float]:
    """先按严重级别，再在同级内按评委决策价值排序。"""
    rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
    text = f"{item.title} {item.detail}"
    if item.title in {"编译失败", "运行失败", "编译日志缺失", "运行日志缺失"}:
        group = 0
    elif "硬编码" in item.title:
        group = 1
    elif "复现" in item.title or "Dockerfile" in text:
        group = 2
    elif any(token in text for token in (
        "仅返回成功", "占位实现", "行为不明确", "溢出丢弃", "旁路",
        "伪造", "错误结果",
    )):
        group = 3
    elif any(token in text for token in ("路由", "竞态", "死锁", "越界", "权限")):
        group = 4
    elif item.severity in {"critical", "high"}:
        group = 5
    elif any(token in text for token in ("性能", "浪费", "热点", "锁竞争")):
        group = 8
    elif item.severity == "medium":
        group = 6
    else:
        group = 7
    return -rank[item.severity], group, -item.confidence


def description_priority_findings(
    digest: ReportDigest, limit: int | None = None,
) -> list[Finding]:
    """描述报告专用顺序：可复现性、诚信、正确性、性能。"""
    ordered = sorted(digest.findings, key=_description_priority_key)
    return ordered if limit is None else ordered[: max(0, limit)]


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

    reproducibility = integrity.get("reproducibility") or {}
    if reproducibility.get("status") in {"warning", "missing"}:
        findings.append(Finding(
            title="自动评测环境复现存在风险",
            detail=concise_module_summary(str(
                reproducibility.get("summary") or "容器构建环境无法核验。"
            )),
            severity="high" if reproducibility.get("status") == "warning" else "medium",
            confidence=1.0,
            source="description",
            evidence=[
                EvidenceRef(
                    path=str(item.get("path") or ""),
                    line=item.get("line"),
                    excerpt=str(item.get("excerpt") or "")[:500],
                )
                for item in (reproducibility.get("evidence") or [])[:2]
                if isinstance(item, dict) and item.get("path")
            ],
        ))

    hardcode_findings = _reviewed_hardcode_findings(verdict, integrity)
    findings.extend(hardcode_findings)
    hardcode_locations = {
        (evidence.path, evidence.line)
        for finding in hardcode_findings for evidence in finding.evidence
        if evidence.path
    }

    reported_issue_locations: set[tuple[str, int | None]] = set()
    for item in verdict.get("issues") or []:
        raw_path = str(item.get("path") or "")
        quote = normalize_description_claim(str(item.get("quote") or ""), raw_path, facts)
        if not quote:
            continue
        issue_path = _path_ref(raw_path)
        if (issue_path.path, issue_path.line) in hardcode_locations:
            continue
        reported_issue_locations.add((issue_path.path, issue_path.line))
        source_name = Path(issue_path.path).name or "未知文件"
        findings.append(Finding(
            title=f"源码实现问题：{source_name}",
            detail=concise_module_summary(quote),
            severity=_severity(str(item.get("severity") or "medium"), quote),
            confidence=_confidence_ratio(item.get("confidence"), default=0.5),
            source="description",
            evidence=[issue_path],
        ))

    # 顶层 verdict 只会挑代表项；描述报告还必须吸收各一级子系统中未被挑中的问题，
    # 以免“精简”演变成静默丢失严重或语义不完整的实现。
    for subsystem in ((tree.get("tree") or {}).get("children") or []):
        if not isinstance(subsystem, dict):
            continue
        for item in subsystem.get("issues") or []:
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or "")
            quote = normalize_description_claim(str(item.get("quote") or ""), raw_path, facts)
            issue_path = _path_ref(raw_path)
            location = (issue_path.path, issue_path.line)
            if (
                not quote or not issue_path.path or location in hardcode_locations
                or location in reported_issue_locations
            ):
                continue
            reported_issue_locations.add(location)
            source_name = Path(issue_path.path).name or "未知文件"
            findings.append(Finding(
                title=f"源码实现问题：{source_name}",
                detail=concise_module_summary(quote),
                severity=_severity(str(item.get("severity") or "medium"), quote),
                confidence=_confidence_ratio(item.get("confidence"), default=0.75),
                source="description",
                evidence=[issue_path],
            ))

    modules: list[ModuleDigest] = []
    for name, subsystem, _parent in description_review_sections(tree):
        summary = normalize_description_claim(str(
            subsystem.get("brief") or subsystem.get("summary")
            or subsystem.get("content") or "未形成模块摘要。"
        ), "", facts)
        children = [
            child for child in (subsystem.get("children") or []) if isinstance(child, dict)
        ]
        modules.append(ModuleDigest(
            name=name,
            summary=clip_at_sentence(explain_terms_on_first_use(str(summary)), 160),
            evidence_count=(
                len(subsystem.get("highlights") or []) + len(subsystem.get("issues") or [])
                + sum(len(child.get("file_paths") or []) for child in children)
            ),
        ))

    conclusion = normalize_description_conclusion(
        str(verdict.get("one_line") or "已完成源码结构分析，结论见问题清单。"),
        verdict,
    )
    reviews = verdict.get("hardcode_reviews") or []
    ordered_findings = sorted(findings, key=_description_priority_key)
    return ReportDigest(
        repo_id=str(meta.get("repo") or (facts.get("meta") or {}).get("repo_id") or "unknown"),
        kind="description",
        conclusion=conclusion[:240],
        confidence=0.8 if findings else 0.65,
        findings=ordered_findings,
        modules=modules,
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
            "hardcode_scanned_files": int(
                (integrity.get("hardcode") or {}).get("scanned_files") or 0
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
            "build_log_note": str((integrity.get("build_log") or {}).get("note") or ""),
            "run_log_note": str((integrity.get("run_log") or {}).get("note") or ""),
            "reproducibility_status": reproducibility.get("status", "unknown"),
            "reproducibility_summary": str(reproducibility.get("summary") or ""),
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
    closest_institution: str = "",
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
    closest_year = ""
    closest_team = ""
    if closest_source and "/" in closest_source:
        closest_year, closest_team = closest_source.split("/", 1)
    identity = source_text
    if closest_year and closest_team:
        closest_team_label = (
            closest_team if closest_team.endswith("队") else f"{closest_team} 队"
        )
        identity = (
            f"{closest_year} 年来自 {closest_institution} 的 {closest_team_label}作品"
            if closest_institution else
            f"{closest_year} 年 {closest_team_label}作品"
        )
    conclusion = (
        f"与 {identity}最接近；按可比函数口径，{confirmed}/{total} 个函数形成"
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
        findings=findings,
        modules=modules,
        metrics={
            "closest_source": closest_source,
            "closest_year": closest_year,
            "closest_team": closest_team,
            "closest_institution": closest_institution,
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
