"""开发过程分析：以 Git 事实约束 AI 的问题判断和阶段划分。"""

from __future__ import annotations

import html
import json
import math
import re
import statistics
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from oskernel_agent.engines.llm_batch import BatchTask, run_batch_task

from .models import EvidenceRef, Finding, ModuleDigest, ReportDigest
from .readability import (
    clip_at_sentence,
    concise_module_summary,
    explain_terms_in_html,
    explain_terms_on_first_use,
)

_LARGE_COMMIT_FLOOR = 1000
_MAX_STAGES = 12
_MAX_KEY_COMMITS = 3
_MAX_STAGE_FILES = 8
_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_THRESHOLD_DEFINITION = (
    "有变更的提交少于 8 次时，阈值固定为 1000 LOC；否则取 1000 与"
    "提交变更量中位数的 5 倍向上取整后的较大值。"
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git 命令失败")
    return result.stdout


def collect_commits(repo_path: str | Path) -> tuple[list[dict], bool]:
    """按时间正序读取全部可见提交及 numstat。"""
    repo = Path(repo_path).resolve()
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").strip().lower() == "true"
    raw = _git(
        repo,
        "log",
        "--reverse",
        "--date=iso-strict",
        "--numstat",
        "--format=@@@%H%x1f%aI%x1f%an%x1f%s",
    )
    commits: list[dict] = []
    current: dict | None = None
    for line in raw.splitlines():
        if line.startswith("@@@"):
            if current:
                commits.append(current)
            parts = line[3:].split("\x1f", 3)
            if len(parts) != 4:
                current = None
                continue
            sha, date, author, subject = parts
            current = {
                "sha": sha,
                "date": date,
                "author": author,
                "subject": subject,
                "additions": 0,
                "deletions": 0,
                "files": [],
            }
            continue
        if current is None or not line.strip():
            continue
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        additions = int(fields[0]) if fields[0].isdigit() else 0
        deletions = int(fields[1]) if fields[1].isdigit() else 0
        path = fields[2]
        current["additions"] += additions
        current["deletions"] += deletions
        current["files"].append(
            {"path": path, "additions": additions, "deletions": deletions}
        )
    if current:
        commits.append(current)
    return commits, shallow


def _changes(commit: dict) -> int:
    return int(commit.get("additions") or 0) + int(commit.get("deletions") or 0)


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _large_threshold(commits: list[dict]) -> int:
    changes = [_changes(commit) for commit in commits if _changes(commit) > 0]
    if len(changes) < 8:
        return _LARGE_COMMIT_FLOOR
    return max(_LARGE_COMMIT_FLOOR, math.ceil(statistics.median(changes) * 5))


def _short_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _top_commit_files(commit: dict, limit: int = 4) -> list[dict]:
    rows = []
    for item in commit.get("files") or []:
        loc = int(item.get("additions") or 0) + int(item.get("deletions") or 0)
        rows.append({"path": str(item.get("path") or ""), "loc": loc})
    return sorted(rows, key=lambda item: (-item["loc"], item["path"]))[:limit]


def build_development_evidence(
    commits: list[dict],
    *,
    shallow: bool = False,
    min_commits: int | None = None,
) -> dict:
    """生成给 AI 的只读证据；所有数值均可由 Git 复算。"""
    if min_commits is not None and min_commits < 0:
        raise ValueError("最低提交次数不能为负数")

    threshold = _large_threshold(commits)
    large = [commit for commit in commits if _changes(commit) >= threshold]
    candidates: list[dict] = []

    if min_commits is not None and len(commits) < min_commits:
        candidates.append(
            {
                "candidate_id": "commit-count",
                "kind": "提交次数不足",
                "fact": f"可见提交 {len(commits)} 次，章程最低要求 {min_commits} 次。",
                "commit_shas": [],
                "must_report": True,
            }
        )
    if shallow:
        candidates.append(
            {
                "candidate_id": "shallow-history",
                "kind": "历史不完整",
                "fact": "当前仓库是浅克隆，只包含部分 Git 历史。",
                "commit_shas": [],
                "must_report": True,
            }
        )
    if large:
        largest = sorted(large, key=lambda item: (-_changes(item), item["sha"]))[:12]
        detail = "、".join(
            f"{item['sha'][:12]}（{_changes(item)} LOC）" for item in largest
        )
        suffix = "" if len(large) <= len(largest) else f"；另有 {len(large) - len(largest)} 次"
        candidates.append(
            {
                "candidate_id": "large-commits",
                "kind": "大规模代码提交",
                "fact": (
                    f"按 {threshold} LOC 阈值识别出 {len(large)} 次：{detail}{suffix}。"
                ),
                "commit_shas": [item["sha"] for item in large],
                "must_report": False,
            }
        )

    close_pairs = []
    for left, right in zip(large, large[1:]):
        hours = (_date(str(right["date"])) - _date(str(left["date"]))).total_seconds() / 3600
        if 0 <= hours <= 24:
            close_pairs.append(
                {
                    "left": left["sha"],
                    "right": right["sha"],
                    "hours": round(hours, 1),
                    "loc": _changes(left) + _changes(right),
                }
            )
    if close_pairs:
        shown = close_pairs[:8]
        detail = "、".join(
            f"{item['left'][:12]}→{item['right'][:12]}（{item['hours']} 小时，"
            f"合计 {item['loc']} LOC）"
            for item in shown
        )
        suffix = "" if len(close_pairs) <= len(shown) else f"；另有 {len(close_pairs) - len(shown)} 组"
        candidates.append(
            {
                "candidate_id": "consecutive-large",
                "kind": "短时间连续大规模提交",
                "fact": f"发现 {len(close_pairs)} 组 24 小时内的大规模提交：{detail}{suffix}。",
                "commit_shas": sorted(
                    {sha for item in close_pairs for sha in (item["left"], item["right"])}
                ),
                "must_report": False,
            }
        )

    timeline = []
    for index, commit in enumerate(commits, start=1):
        timeline.append(
            {
                "index": index,
                "sha": str(commit["sha"])[:12],
                "date": str(commit.get("date") or "")[:10],
                "subject": _short_text(commit.get("subject"), 120),
                "loc": _changes(commit),
                "files": _top_commit_files(commit),
            }
        )

    return {
        "commit_count": len(commits),
        "shallow": shallow,
        "minimum_commits": min_commits,
        "minimum_rule": (
            f"章程最低提交次数为 {min_commits} 次。"
            if min_commits is not None
            else "章程最低提交次数未配置，不能判断是否提交缺失。"
        ),
        "large_commit_threshold": threshold,
        "large_commit_threshold_definition": _THRESHOLD_DEFINITION,
        "candidates": candidates,
        "timeline": timeline,
    }


def _resolve_sha(value: object, commits: list[dict]) -> tuple[str, int]:
    raw = str(value or "").strip().lower()
    if len(raw) < 7 or not re.fullmatch(r"[0-9a-f]+", raw):
        raise RuntimeError(f"AI 返回了无效提交标识：{value!r}")
    matches = [
        (str(commit["sha"]), index)
        for index, commit in enumerate(commits)
        if str(commit["sha"]).lower().startswith(raw)
    ]
    if len(matches) != 1:
        raise RuntimeError(f"AI 返回的提交标识不存在或不唯一：{raw}")
    return matches[0]


def _confidence(value: object, context: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{context} 缺少有效置信度") from exc
    if not 0 <= result <= 100:
        raise RuntimeError(f"{context} 置信度必须在 0 至 100 之间")
    return result / 100


def validate_ai_development_result(
    result: dict,
    evidence: dict,
    commits: list[dict],
) -> dict:
    """拒绝缺证据、虚构提交或时间范围不连续的 AI 输出。"""
    if not isinstance(result, dict) or result.get("_error"):
        raise RuntimeError("开发过程 AI 分析失败，拒绝生成确定性模板报告")
    conclusion = _short_text(result.get("conclusion"), 240)
    if not conclusion:
        raise RuntimeError("开发过程 AI 分析缺少总体结论")

    candidates = {
        str(item["candidate_id"]): item for item in evidence.get("candidates") or []
    }
    raw_issues = result.get("issues") or []
    if not isinstance(raw_issues, list):
        raise RuntimeError("开发过程 AI 的 issues 必须是列表")
    reviews: list[dict] = []
    seen: set[str] = set()
    for index, issue in enumerate(raw_issues, start=1):
        if not isinstance(issue, dict):
            raise RuntimeError(f"第 {index} 条问题复核不是对象")
        candidate_id = str(issue.get("candidate_id") or "")
        if candidate_id not in candidates:
            raise RuntimeError(f"AI 返回了没有程序证据的问题：{candidate_id or '未标识'}")
        if candidate_id in seen:
            raise RuntimeError(f"AI 重复复核问题候选：{candidate_id}")
        seen.add(candidate_id)
        candidate = candidates[candidate_id]
        status = str(issue.get("status") or "")
        if status not in {"report", "dismiss"}:
            raise RuntimeError(f"问题候选 {candidate_id} 的 status 无效")
        if candidate.get("must_report") and status != "report":
            raise RuntimeError(f"客观问题 {candidate_id} 不允许被 AI 忽略")
        title = _short_text(issue.get("title"), 80)
        analysis = _short_text(issue.get("analysis"), 180)
        if not title or not analysis:
            raise RuntimeError(f"问题候选 {candidate_id} 缺少标题或分析")
        severity = str(issue.get("severity") or "")
        if severity not in _SEVERITIES:
            raise RuntimeError(f"问题候选 {candidate_id} 的严重度无效")
        confidence = _confidence(issue.get("confidence"), f"问题候选 {candidate_id}")

        allowed = set(candidate.get("commit_shas") or [])
        resolved: list[str] = []
        for raw_sha in issue.get("commit_shas") or []:
            sha, _ = _resolve_sha(raw_sha, commits)
            if allowed and sha not in allowed:
                raise RuntimeError(f"问题候选 {candidate_id} 引用了无关提交 {sha[:12]}")
            if sha not in resolved:
                resolved.append(sha)
        if allowed and not resolved:
            raise RuntimeError(f"问题候选 {candidate_id} 未引用任何相关提交")
        reviews.append(
            {
                "candidate_id": candidate_id,
                "status": status,
                "title": title,
                "analysis": analysis,
                "severity": severity,
                "confidence": confidence,
                "commit_shas": resolved,
                "fact": candidate["fact"],
            }
        )
    missing = set(candidates) - seen
    if missing:
        raise RuntimeError(f"AI 未复核问题候选：{'、'.join(sorted(missing))}")

    raw_stages = result.get("stages") or []
    if not isinstance(raw_stages, list):
        raise RuntimeError("开发过程 AI 的 stages 必须是列表")
    if commits and not 1 <= len(raw_stages) <= _MAX_STAGES:
        raise RuntimeError(f"开发阶段必须为 1 至 {_MAX_STAGES} 个")
    if not commits and raw_stages:
        raise RuntimeError("没有提交时 AI 不得生成开发阶段")

    stages: list[dict] = []
    expected_start = 0
    for number, stage in enumerate(raw_stages, start=1):
        if not isinstance(stage, dict):
            raise RuntimeError(f"第 {number} 个开发阶段不是对象")
        name = _short_text(stage.get("name"), 60)
        stage_conclusion = _short_text(stage.get("conclusion"), 180)
        reason = _short_text(stage.get("reason"), 180)
        if not name or not stage_conclusion or not reason:
            raise RuntimeError(f"第 {number} 个开发阶段缺少名称、结论或划分依据")
        start_sha, start_index = _resolve_sha(stage.get("start_sha"), commits)
        end_sha, end_index = _resolve_sha(stage.get("end_sha"), commits)
        if start_index != expected_start or end_index < start_index:
            raise RuntimeError(f"第 {number} 个开发阶段与前一阶段存在空缺、重叠或倒序")
        confidence = _confidence(stage.get("confidence"), f"第 {number} 个开发阶段")
        key_shas: list[str] = []
        raw_keys = stage.get("key_shas") or []
        if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= _MAX_KEY_COMMITS:
            raise RuntimeError(
                f"第 {number} 个开发阶段必须提供 1 至 {_MAX_KEY_COMMITS} 个关键提交"
            )
        for raw_sha in raw_keys:
            sha, commit_index = _resolve_sha(raw_sha, commits)
            if not start_index <= commit_index <= end_index:
                raise RuntimeError(f"第 {number} 个开发阶段的关键提交超出阶段范围")
            if sha not in key_shas:
                key_shas.append(sha)
        if not key_shas:
            raise RuntimeError(f"第 {number} 个开发阶段没有有效关键提交")
        stages.append(
            {
                "name": name,
                "conclusion": stage_conclusion,
                "reason": reason,
                "confidence": confidence,
                "start_sha": start_sha,
                "end_sha": end_sha,
                "start_index": start_index,
                "end_index": end_index,
                "key_shas": key_shas,
            }
        )
        expected_start = end_index + 1
    if commits and expected_start != len(commits):
        raise RuntimeError("AI 的开发阶段没有覆盖全部可见提交")

    return {"conclusion": conclusion, "issues": reviews, "stages": stages}


def _stage_file_stats(stage_commits: list[dict]) -> list[dict]:
    totals: Counter[str] = Counter()
    for commit in stage_commits:
        for item in commit.get("files") or []:
            path = str(item.get("path") or "")
            if path:
                totals[path] += int(item.get("additions") or 0) + int(
                    item.get("deletions") or 0
                )
    return [
        {"path": path, "loc": loc}
        for path, loc in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[
            :_MAX_STAGE_FILES
        ]
    ]


def analyze_history(
    repo_id: str,
    commits: list[dict],
    ai_result: dict,
    *,
    shallow: bool = False,
    min_commits: int | None = None,
) -> dict:
    """用经校验的 AI 判断组织报告，所有明细数字由提交历史复算。"""
    evidence = build_development_evidence(
        commits, shallow=shallow, min_commits=min_commits
    )
    validated = validate_ai_development_result(ai_result, evidence, commits)
    commit_by_sha = {str(commit["sha"]): commit for commit in commits}

    findings = []
    for review in validated["issues"]:
        if review["status"] != "report":
            continue
        detail = clip_at_sentence(f"{review['fact']} AI 分析：{review['analysis']}", 360)
        findings.append(
            Finding(
                title=review["title"],
                detail=detail,
                severity=review["severity"],
                confidence=review["confidence"],
                source="development",
                evidence=[
                    EvidenceRef(
                        path=f"commit:{sha}",
                        excerpt=str(commit_by_sha[sha].get("subject") or ""),
                    )
                    for sha in review["commit_shas"][:6]
                ],
            )
        )

    stages = []
    for number, ai_stage in enumerate(validated["stages"], start=1):
        stage_commits = commits[ai_stage["start_index"] : ai_stage["end_index"] + 1]
        key_commits = [commit_by_sha[sha] for sha in ai_stage["key_shas"]]
        stages.append(
            {
                "number": number,
                "name": ai_stage["name"],
                "conclusion": ai_stage["conclusion"],
                "reason": ai_stage["reason"],
                "confidence": ai_stage["confidence"],
                "start": str(stage_commits[0]["date"])[:10],
                "end": str(stage_commits[-1]["date"])[:10],
                "commit_count": len(stage_commits),
                "loc": sum(_changes(commit) for commit in stage_commits),
                "key_commits": [
                    {
                        "sha": commit["sha"],
                        "subject": commit.get("subject", ""),
                        "date": str(commit["date"])[:10],
                        "loc": _changes(commit),
                    }
                    for commit in key_commits
                ],
                "files": _stage_file_stats(stage_commits),
            }
        )

    authors = Counter(str(commit.get("author") or "未知") for commit in commits)
    dates = [str(commit.get("date") or "")[:10] for commit in commits if commit.get("date")]
    reported = sum(review["status"] == "report" for review in validated["issues"])
    dismissed = sum(review["status"] == "dismiss" for review in validated["issues"])
    digest = ReportDigest(
        repo_id=repo_id,
        kind="development",
        conclusion=validated["conclusion"],
        confidence=(
            min((stage["confidence"] for stage in stages), default=1.0)
            if not shallow
            else min(0.65, min((stage["confidence"] for stage in stages), default=0.65))
        ),
        findings=findings[:8],
        modules=[
            ModuleDigest(
                name=f"阶段 {stage['number']}：{stage['name']}",
                summary=concise_module_summary(
                    f"{stage['conclusion']} {stage['start']} 至 {stage['end']}，"
                    f"{stage['commit_count']} 次提交，变更 {stage['loc']} LOC。"
                ),
                evidence_count=len(stage["key_commits"]),
            )
            for stage in stages
        ],
        metrics={
            "commit_count": len(commits),
            "minimum_commits": min_commits,
            "minimum_rule_configured": min_commits is not None,
            "start_date": dates[0] if dates else "",
            "end_date": dates[-1] if dates else "",
            "author_count": len(authors),
            "large_commit_threshold": evidence["large_commit_threshold"],
            "large_commit_count": sum(
                _changes(commit) >= evidence["large_commit_threshold"] for commit in commits
            ),
            "ai_reviewed_candidates": len(validated["issues"]),
            "ai_reported_issues": reported,
            "ai_dismissed_candidates": dismissed,
            "shallow": shallow,
        },
    )
    return {
        "repo_id": repo_id,
        "commits": commits,
        "stages": stages,
        "authors": authors.most_common(8),
        "reviews": validated["issues"],
        "evidence": evidence,
        "digest": digest,
    }


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def render_development_html(analysis: dict) -> str:
    digest: ReportDigest = analysis["digest"]
    metrics = digest.metrics
    findings = digest.decision_findings(8)
    finding_html = "".join(
        '<li class="finding ' + _esc(item.severity) + '">'
        f'<div><strong>{_esc(item.title)}</strong><span>AI 置信度 {round(item.confidence * 100)}%</span></div>'
        f'<p>{_esc(explain_terms_on_first_use(item.detail))}</p>'
        + (
            '<p class="evidence">证据：'
            + "、".join(
                f'<code>{_esc(ref.path.removeprefix("commit:")[:12])}</code>'
                for ref in item.evidence
            )
            + "</p>"
            if item.evidence
            else ""
        )
        + "</li>"
        for item in findings
    ) or (
        '<li class="finding info"><strong>AI 未将候选线索判定为问题</strong>'
        '<p>这只表示当前 Git 证据不足以支持问题结论，不代表作品通过全部审查。</p></li>'
    )

    dismissed = [review for review in analysis.get("reviews") or [] if review["status"] == "dismiss"]
    dismissed_html = ""
    if dismissed:
        dismissed_html = (
            '<details class="panel"><summary>AI 排除的候选线索（'
            f'{len(dismissed)} 项）</summary><ul>'
            + "".join(
                f'<li><strong>{_esc(item["title"])}</strong>：{_esc(item["analysis"])}</li>'
                for item in dismissed
            )
            + "</ul></details>"
        )

    stage_html: list[str] = []
    for stage in analysis.get("stages") or []:
        commits = "".join(
            f'<li><code>{_esc(item["sha"][:12])}</code> · {_esc(item["date"])} · '
            f'{_esc(item["subject"])} · {_esc(item["loc"])} LOC</li>'
            for item in stage["key_commits"]
        )
        files = "、".join(
            f'<code>{_esc(item["path"])}</code>（{_esc(item["loc"])} LOC）'
            for item in stage["files"]
        ) or "无可统计文件"
        stage_html.append(
            f'<article class="stage"><div class="stage-head"><h3>阶段 {stage["number"]}：{_esc(stage["name"])}</h3>'
            f'<span>AI 置信度 {round(stage["confidence"] * 100)}%</span></div>'
            f'<p class="stage-conclusion">{_esc(stage["conclusion"])}</p>'
            f'<p>{_esc(stage["start"])} 至 {_esc(stage["end"])}；'
            f'{stage["commit_count"]} 次提交；变更 {stage["loc"]} LOC。</p>'
            f'<p class="reason"><strong>划分依据：</strong>{_esc(stage["reason"])}</p>'
            f'<details><summary>关键提交与涉及文件</summary><ul>{commits}</ul>'
            f'<p><strong>主要文件：</strong>{files}</p></details></article>'
        )

    authors = "、".join(
        f"{_esc(name)}（{count}）" for name, count in analysis.get("authors") or []
    ) or "未知"
    minimum = (
        f"{metrics.get('minimum_commits')} 次"
        if metrics.get("minimum_rule_configured")
        else "未配置"
    )
    minimum_note = (
        f"章程最低提交次数已配置为 {metrics.get('minimum_commits')} 次。"
        if metrics.get("minimum_rule_configured")
        else "章程最低提交次数未配置，本报告不判断“提交缺失”。"
    )
    title = f"{_esc(digest.repo_id)} 开发过程分析报告"
    rendered = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#f8fafc;color:#172033;font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:980px;margin:auto;padding:32px 22px 70px}}h1{{font-size:28px;margin:0 0 6px}}h2{{margin-top:34px;font-size:21px}}
.ai-mark{{display:inline-block;color:#075985;background:#e0f2fe;border-radius:999px;padding:3px 10px;font-size:13px;font-weight:700}}
.lead{{font-size:18px;margin:10px 0 22px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.metric,.stage,.panel{{background:white;border:1px solid #dbe3ee;border-radius:10px;padding:16px}}.metric b{{display:block;font-size:24px}}
.findings{{display:grid;gap:10px;padding:0;list-style:none}}.finding{{background:white;border-left:4px solid #f59e0b;padding:12px 14px}}
.finding.high,.finding.critical{{border-left-color:#dc2626}}.finding>div,.stage-head{{display:flex;justify-content:space-between;gap:12px}}
.finding span,.stage-head span{{font-size:12px;color:#64748b}}.finding p{{margin:4px 0 0}}.evidence,.reason{{color:#475569;font-size:14px}}
.stages{{display:grid;gap:12px}}.stage h3{{margin:0}}.stage-conclusion{{font-size:16px;font-weight:600;margin-bottom:4px}}
details summary{{cursor:pointer;color:#475569;font-weight:600}}code{{font-family:ui-monospace,Consolas,monospace}}
@media(max-width:640px){{main{{padding:20px 14px}}.finding>div,.stage-head{{display:block}}}}
</style></head><body><main>
<header><span class="ai-mark">完全由 AI 工具生成</span><h1>{title}</h1>
<p class="lead">{_esc(digest.conclusion)}</p>
<p>AI 负责问题判断和阶段归纳；提交次数、日期、LOC 与文件明细均由程序依据 Git 复算，参赛队不得修改本报告。</p></header>
<section id="findings"><h2>AI 结论与问题</h2><ol class="findings">{finding_html}</ol>{dismissed_html}</section>
<section><h2>历史概况</h2><div class="metrics">
<div class="metric"><b>{_esc(metrics.get('commit_count', 0))}</b><span>可见提交</span></div>
<div class="metric"><b>{_esc(minimum)}</b><span>章程最低提交次数</span></div>
<div class="metric"><b>{_esc(metrics.get('start_date') or '-')}</b><span>最早提交</span></div>
<div class="metric"><b>{_esc(metrics.get('end_date') or '-')}</b><span>最近提交</span></div>
<div class="metric"><b>{_esc(metrics.get('large_commit_threshold', 0))}</b><span>大规模提交阈值（LOC）</span></div>
</div><div class="panel" style="margin-top:10px"><p><strong>判定口径：</strong>{_esc(minimum_note)}</p>
<p><strong>大规模提交口径：</strong>{_esc(_THRESHOLD_DEFINITION)}</p>
<p><strong>主要贡献者：</strong>{authors}</p></div></section>
<section><h2>AI 归纳的提交阶段</h2><div class="stages">{"".join(stage_html)}</div></section>
</main></body></html>"""
    return explain_terms_in_html(rendered)


def run_ai_development_analysis(
    repo: Path,
    repo_id: str,
    evidence: dict,
    output_path: Path,
) -> dict:
    """要求专用 AI agent 只基于给定 Git 证据输出结构化判断。"""
    ai_path = output_path.with_suffix(".ai.json")
    schema_hint = (
        '{"conclusion":str,"issues":[{"candidate_id":str,'
        '"status":"report|dismiss","title":str,"analysis":str,'
        '"severity":"info|low|medium|high|critical","confidence":0-100,'
        '"commit_shas":[str]}],"stages":[{"name":str,"conclusion":str,'
        '"reason":str,"confidence":0-100,"start_sha":str,"end_sha":str,'
        '"key_shas":[str]}]}'
    )
    request = (
        "请分析下面的 Git 证据，逐一复核问题候选，并按时间连续区间归纳开发阶段。"
        "只能使用输入事实，不得编造提交、日期、LOC 或文件。所有候选必须恰好复核一次；"
        "阶段必须覆盖全部 timeline，不能重叠或留空。\n"
        f"repo_id: {repo_id}\n"
        f"evidence_json: {json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}\n"
        f"expected_schema: {schema_hint}\n"
        f"output_path: {ai_path.resolve()}\n"
        "只调用 write_report；content 为合法 JSON 字符串，output_path 必须使用上面的绝对路径。"
    )
    task = BatchTask(
        batch_id=f"development-{re.sub(r'[^A-Za-z0-9_.-]+', '-', repo_id)[:80]}",
        agent_name="os-kernel-development",
        user_request=request,
        output_path=ai_path,
        cache_dir=output_path.parent,
        cache_key="",
        cache_enabled=False,
        fallback={},
        repo_path=repo,
    )
    return run_batch_task(task, schema_hint=schema_hint, timeout=600)


def generate_development_report(
    repo_path: str | Path,
    output_path: str | Path,
    *,
    repo_id: str | None = None,
    min_commits: int | None = None,
) -> dict:
    repo = Path(repo_path).resolve()
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    commits, shallow = collect_commits(repo)
    evidence = build_development_evidence(
        commits, shallow=shallow, min_commits=min_commits
    )
    ai_result = run_ai_development_analysis(
        repo, repo_id or repo.name, evidence, output
    )
    analysis = analyze_history(
        repo_id or repo.name,
        commits,
        ai_result,
        shallow=shallow,
        min_commits=min_commits,
    )
    output.write_text(render_development_html(analysis), encoding="utf-8")
    digest_path = output.with_suffix(".digest.json")
    digest_path.write_text(
        json.dumps(analysis["digest"].model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "html_path": str(output),
        "digest_path": str(digest_path),
        "ai_path": str(output.with_suffix(".ai.json")),
        "commit_count": len(commits),
        "stage_count": len(analysis["stages"]),
    }
