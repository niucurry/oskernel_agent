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
from oskernel_agent.report_quality import assert_report_complete

from .models import EvidenceRef, Finding, ModuleDigest, ReportDigest
from .readability import (
    ai_disclaimer_html,
    clip_at_sentence,
    concise_module_summary,
    explain_terms_in_html,
    explain_terms_on_first_use,
)

_LARGE_COMMIT_FLOOR = 1000
_MAX_STAGES = 12
_MAX_KEY_COMMITS = 3
_PRIMARY_STAGE_FILES = 6
_SEVERITIES = {"info", "low", "medium", "high", "critical"}
_UNVERIFIED_RUNTIME_CLAIM_RE = re.compile(
    r"(?:可|能够|能)(?:正常|成功)?(?:编译|启动|运行)"
    r"|(?:测试套件|测试|用例|LTP).{0,12}(?:全部)?(?:通过|跑通|成功)(?!率)"
    r"|(?:通过|跑通)(?!率).{0,12}(?:测试套件|测试|用例|LTP)"
)
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
    return clip_at_sentence(text, limit)


def _humanize_ai_text(value: object, limit: int) -> str:
    """把结构化状态码改成评委可直接阅读的中文，不改变分析判断。"""
    text = " ".join(str(value or "").split())
    if re.search(r"…|(?<!\.)\.{3}(?!\.)", text):
        raise RuntimeError("开发过程 AI 文字包含省略号，必须改写为完整句子")
    text = re.sub(r"(?<![A-Za-z])dismiss(?![A-Za-z])", "排除", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z])report(?![A-Za-z])", "列为问题", text, flags=re.I)
    return text


def _clean_commit_subject(value: object) -> str:
    """提交主题是参赛队的原始 git 数据，只去掉结尾省略号再渲染。

    省略号截断门禁只针对 AI 生成文字；真实提交消息带「…」（如「具体测试
    能不能成功还不知道...」）属于作者原话，不应让整份报告交付失败。
    """
    return re.sub(r"[….]+$", "", " ".join(str(value or "").split())).strip()


def _remove_repeated_stage_facts(value: str) -> str:
    """删除 AI 结论开头会由程序紧接着复算展示的日期和提交次数。"""
    return re.sub(
        r"^(?:阶段覆盖\s*)?\d{4}-\d{2}-\d{2}\s*(?:至|到|-)\s*"
        r"\d{4}-\d{2}-\d{2}\s*[，,；;]?\s*共\s*(?:约\s*)?\d+\s*次提交\s*[。；;]?\s*",
        "",
        value,
    ).strip()


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
    def find(prefix: str) -> list[tuple[str, int]]:
        return [
            (str(commit["sha"]), index)
            for index, commit in enumerate(commits)
            if str(commit["sha"]).lower().startswith(prefix)
        ]

    matches = find(raw)
    # timeline 明确只提供 12 位 SHA。模型偶尔会擅自补齐不存在的后缀；仅当原始
    # 12 位前缀能唯一映射到真实提交时，丢弃补齐部分并使用 Git 中的完整 SHA。
    if not matches and len(raw) > 12:
        matches = find(raw[:12])
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


def _validate_development_claim_scope(value: str, context: str) -> None:
    """Git 历史只证明开发活动，不能替代当前版本的编译或运行验证。"""
    screened = re.sub(
        r"(?:不能|无法|不足以|不代表|不得)[^。！？；]{0,80}",
        "",
        value,
    )
    # 条件/先决口吻（“需逐项修补才能通过测试”）表达的是前置条件或限制，不是
    # “当前版本已通过”的结论；若一并剔除，既避免误报，也不削弱护栏——真正要拦的
    # 是“使 RISC-V 测试通过”“跑通 LTP”这类未限定断言，它们不含才能/方可。
    screened = re.sub(
        r"(?:才能|方可|之后才能|才能保证)[^。！？；]{0,80}",
        "",
        screened,
    )
    if _UNVERIFIED_RUNTIME_CLAIM_RE.search(screened):
        raise RuntimeError(
            f"{context} 把提交历史写成了当前版本的编译、运行或测试通过结论"
        )


def validate_ai_development_result(
    result: dict,
    evidence: dict,
    commits: list[dict],
) -> dict:
    """拒绝缺证据、虚构提交或时间范围不连续的 AI 输出。"""
    if not isinstance(result, dict) or result.get("_error"):
        raise RuntimeError("开发过程 AI 分析失败，拒绝生成确定性模板报告")
    conclusion = _humanize_ai_text(result.get("conclusion"), 240)
    if not conclusion:
        raise RuntimeError("开发过程 AI 分析缺少总体结论")
    _validate_development_claim_scope(conclusion, "开发过程总体结论")

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
        title = _humanize_ai_text(issue.get("title"), 80)
        analysis = _humanize_ai_text(issue.get("analysis"), 180)
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

    prepared_stages: list[dict] = []
    previous_start = -1
    for number, stage in enumerate(raw_stages, start=1):
        if not isinstance(stage, dict):
            raise RuntimeError(f"第 {number} 个开发阶段不是对象")
        name = _humanize_ai_text(stage.get("name"), 60)
        name = re.sub(r"^阶段\s*[一二三四五六七八九十0-9]+\s*[：:、.\-]?\s*", "", name)
        stage_conclusion = _humanize_ai_text(stage.get("conclusion"), 180)
        stage_conclusion = _remove_repeated_stage_facts(stage_conclusion)
        reason = _humanize_ai_text(stage.get("reason"), 180)
        if not name or not stage_conclusion or not reason:
            raise RuntimeError(f"第 {number} 个开发阶段缺少名称、结论或划分依据")
        _validate_development_claim_scope(stage_conclusion, f"第 {number} 个开发阶段结论")
        start_sha, start_index = _resolve_sha(stage.get("start_sha"), commits)
        if number == 1 and start_index != 0:
            raise RuntimeError("第 1 个开发阶段必须从首个可见提交开始")
        if start_index <= previous_start:
            raise RuntimeError("AI 给出的开发阶段起点必须严格递增")
        confidence = _confidence(stage.get("confidence"), f"第 {number} 个开发阶段")
        raw_keys = stage.get("key_shas") or []
        if not isinstance(raw_keys, list) or not raw_keys:
            raise RuntimeError(
                f"第 {number} 个开发阶段必须提供 1 至 {_MAX_KEY_COMMITS} 个关键提交"
            )
        key_candidates: list[tuple[str, int]] = []
        for raw_sha in raw_keys:
            sha, commit_index = _resolve_sha(raw_sha, commits)
            if not any(item[0] == sha for item in key_candidates):
                key_candidates.append((sha, commit_index))
        prepared_stages.append(
            {
                "name": name,
                "conclusion": stage_conclusion,
                "reason": reason,
                "confidence": confidence,
                "start_sha": start_sha,
                "start_index": start_index,
                "key_candidates": key_candidates,
            }
        )
        previous_start = start_index

    stages: list[dict] = []
    for index, stage in enumerate(prepared_stages):
        end_index = (
            prepared_stages[index + 1]["start_index"] - 1
            if index + 1 < len(prepared_stages)
            else len(commits) - 1
        )
        key_shas = [
            sha
            for sha, commit_index in stage.pop("key_candidates")
            if stage["start_index"] <= commit_index <= end_index
        ][:_MAX_KEY_COMMITS]
        if not key_shas:
            raise RuntimeError(f"第 {index + 1} 个开发阶段没有范围内的有效关键提交")
        stage["end_index"] = end_index
        stage["end_sha"] = str(commits[end_index]["sha"])
        stage["key_shas"] = key_shas
        stages.append(stage)

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
    # 全量保留，避免“精简”变成静默丢证据。渲染器把前几项放在主阅读路径，
    # 其余项折叠展示，因此评委可以速读，也能完整复核阶段涉及的文件。
    return [
        {"path": path, "loc": loc}
        for path, loc in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    ]


def analyze_history(
    repo_id: str,
    commits: list[dict],
    ai_result: dict,
    *,
    shallow: bool = False,
    min_commits: int | None = None,
    repository_url: str = "",
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
        # 大规模提交: 只展示被 AI 标记的具体提交证据，不列出所有超大提交
        if review["candidate_id"] == "large-commits" and review.get("commit_shas"):
            cited = []
            for sha in review["commit_shas"][:6]:
                c = commit_by_sha.get(sha)
                if c:
                    cited.append(
                        f"{sha[:12]}（{_changes(c)} LOC，提交信息："
                        f"{_short_text(c.get('subject'), 60)}）"
                    )
            fact_prefix = f"涉疑提交 {len(review['commit_shas'])} 次：{'；'.join(cited)}。"
            detail = f"{fact_prefix} AI 分析：{review['analysis']}"
        else:
            detail = f"{review['fact']} AI 分析：{review['analysis']}"
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
                        excerpt=str(
                            _short_text(commit_by_sha.get(sha, {}).get("subject", ""), 80)
                        ),
                        url=(
                            f"{repository_url.rstrip('/')}/-/commit/{sha}"
                            if repository_url else ""
                        ),
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
                        "subject": _clean_commit_subject(commit.get("subject", "")),
                        "date": str(commit["date"])[:10],
                        "loc": _changes(commit),
                        "url": (
                            f"{repository_url.rstrip('/')}/-/commit/{commit['sha']}"
                            if repository_url else ""
                        ),
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
        findings=findings,
        modules=[
            ModuleDigest(
                name=f"阶段 {stage['number']}：{stage['name']}",
                summary=concise_module_summary(
                    f"{stage['conclusion']} {stage['start']} 至 {stage['end']}，"
                    f"{stage['commit_count']} 次提交，代码变更行数（LOC）为 {stage['loc']}。"
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
        # 完整列出全部作者：digest.author_count 是全体作者数，HTML 截断会造成
        # 「名单数 ≠ author_count」的口径不一致（审计 E3 曾拦截 8 vs 10 实例）。
        "authors": authors.most_common(),
        "reviews": validated["issues"],
        "evidence": evidence,
        "digest": digest,
        "repository_url": repository_url,
    }


def _esc(value: object) -> str:
    # 仅把 None 归一为空串；数值 0 必须原样渲染（str(0 or "") 会吞掉 0）。
    return html.escape("" if value is None else str(value), quote=True)


def render_development_html(analysis: dict) -> str:
    digest: ReportDigest = analysis["digest"]
    metrics = digest.metrics
    # 问题按严重度前置，但不设置条数上限：精简只压缩表达，不能漏掉严重问题。
    findings = digest.decision_findings(len(digest.findings))
    finding_html = "".join(
        '<li class="finding ' + _esc(item.severity) + '">'
        f'<div><strong>{_esc(item.title)}</strong><span>AI 置信度 {round(item.confidence * 100)}%</span></div>'
        f'<p>{_esc(explain_terms_on_first_use(item.detail))}</p>'
        + (
            '<p class="evidence">证据：'
            + "、".join(
                (
                    f'<a href="{_esc(ref.url)}"><code>'
                    f'{_esc(ref.path.removeprefix("commit:")[:12])}</code></a>'
                    if ref.url else
                    f'<code>{_esc(ref.path.removeprefix("commit:")[:12])}</code>'
                )
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

    # 被排除候选不属于评委需要核查的问题，不进入最终报告。
    dismissed_html = ""

    stage_html: list[str] = []
    for stage in analysis.get("stages") or []:
        commits = "".join(
            '<li>'
            + (
                f'<a href="{_esc(item.get("url"))}"><code>{_esc(item["sha"][:12])}</code></a>'
                if item.get("url") else f'<code>{_esc(item["sha"][:12])}</code>'
            )
            + f' · {_esc(item["date"])} · '
            f'{_esc(item["subject"])} · {_esc(item["loc"])} LOC</li>'
            for item in stage["key_commits"]
        )
        primary_files = stage["files"][:_PRIMARY_STAGE_FILES]
        extra_files = stage["files"][_PRIMARY_STAGE_FILES:]
        files = "、".join(
            f'<code>{_esc(item["path"])}</code>（{_esc(item["loc"])} LOC）'
            for item in primary_files
        ) or "无可统计文件"
        extra_files_html = (
            f'<p class="reason">另有 {len(extra_files)} 个文件已纳入阶段统计；正文仅列变更量前 '
            f'{_PRIMARY_STAGE_FILES} 项。</p>'
            if extra_files else ""
        )
        stage_html.append(
            f'<article class="stage"><div class="stage-head"><h3>阶段 {stage["number"]}：{_esc(stage["name"])}</h3>'
            f'<span>AI 置信度 {round(stage["confidence"] * 100)}%</span></div>'
            f'<p class="stage-conclusion">{_esc(stage["conclusion"])}</p>'
            f'<p>{_esc(stage["start"])} 至 {_esc(stage["end"])}；'
            f'{stage["commit_count"]} 次提交；变更 {stage["loc"]} LOC。</p>'
            f'<p class="reason"><strong>划分依据：</strong>{_esc(stage["reason"])}</p>'
            f'<div class="stage-evidence"><div><strong>关键提交（标题原文）</strong><ul>{commits}</ul></div>'
            f'<div><strong>主要文件</strong><p>{files}</p>{extra_files_html}</div></div></article>'
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
    disclaimer = ai_disclaimer_html("development")
    rendered = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#f8fafc;color:#172033;font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:980px;margin:auto;padding:32px 22px 70px}}h1{{font-size:28px;margin:0 0 6px}}h2{{margin-top:34px;font-size:21px}}
.ai-mark{{display:inline-block;color:#075985;background:#e0f2fe;border-radius:999px;padding:3px 10px;font-size:13px;font-weight:700}}
.ai-disclaimer{{font-size:12px;color:#64748b;margin-top:6px;line-height:1.5}}
.lead{{font-size:18px;margin:10px 0 22px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.metric,.stage,.panel{{background:white;border:1px solid #dbe3ee;border-radius:10px;padding:16px}}.metric b{{display:block;font-size:24px}}
.findings{{display:grid;gap:10px;padding:0;list-style:none}}.finding{{background:white;border-left:4px solid #f59e0b;padding:12px 14px}}
.finding.high,.finding.critical{{border-left-color:#dc2626}}.finding>div,.stage-head{{display:flex;justify-content:space-between;gap:12px}}
.finding span,.stage-head span{{font-size:12px;color:#64748b}}.finding p{{margin:4px 0 0}}.evidence,.reason{{color:#475569;font-size:14px}}
.stages{{display:grid;gap:12px}}.stage h3{{margin:0}}.stage-conclusion{{font-size:16px;font-weight:600;margin-bottom:4px}}
.stage-evidence{{display:grid;grid-template-columns:minmax(15rem,.85fr) minmax(18rem,1.15fr);gap:18px;border-top:1px solid #e2e8f0;padding-top:10px}}
.stage-evidence ul,.stage-evidence p{{margin:.35rem 0;padding-left:1.2rem}}.stage-evidence p{{padding-left:0}}
details summary{{cursor:pointer;color:#475569;font-weight:600}}.more-files{{margin-top:.4rem}}code{{font-family:ui-monospace,Consolas,monospace}}
a{{color:#075985;text-decoration:none}}a:hover{{text-decoration:underline}}
@media(max-width:640px){{main{{padding:20px 14px}}.finding>div,.stage-head{{display:block}}.stage-evidence{{grid-template-columns:1fr}}.metrics{{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}}h1{{font-size:22px}}}}
@media(max-width:414px){{main{{padding:14px 10px}}h1{{font-size:18px}}h2{{font-size:16px}}.lead{{font-size:15px}}.metric b{{font-size:18px}}body{{font-size:14px}}code{{word-break:break-all}}.stage-evidence{{display:block}}.stage{{padding:10px}}}}
</style></head><body><main>
<header><span class="ai-mark">AI 工具生成</span><h1>{title}</h1>
<p class="lead">{_esc(digest.conclusion)}</p>
{disclaimer}
<div class="ai-disclaimer">证据口径：“关键提交”中的文字为 Git 提交标题原文，只用于还原开发意图与阶段，不代表人工智能（AI）已验证编译、启动或测试成功；运行结论须以正式日志为准。</div></header>
<section id="findings"><h2>经 AI 分析，该作品存在以下问题</h2><ol class="findings">{finding_html}</ol>{dismissed_html}</section>
<section><h2>历史概况</h2><div class="metrics">
<div class="metric"><b>{_esc(metrics.get('commit_count', 0))}</b><span>可见提交</span></div>
<div class="metric"><b>{_esc(minimum)}</b><span>章程最低提交次数</span></div>
<div class="metric"><b>{_esc(metrics.get('start_date') or '-')}</b><span>最早提交</span></div>
<div class="metric"><b>{_esc(metrics.get('end_date') or '-')}</b><span>最近提交</span></div>
<div class="metric"><b>{_esc(metrics.get('large_commit_threshold', 0))}</b><span>大规模提交阈值（LOC）</span></div>
</div><div class="panel" style="margin-top:10px"><p><strong>判定口径：</strong>{_esc(minimum_note)}</p>
<p><strong>大规模提交口径：</strong>{_esc(_THRESHOLD_DEFINITION)}</p>
<p><strong>贡献者：</strong>{authors}</p></div></section>
<section><h2>提交历史与开发阶段</h2><div class="stages">{"".join(stage_html)}</div></section>
</main></body></html>"""
    return explain_terms_in_html(rendered)


def run_ai_development_analysis(
    repo: Path,
    repo_id: str,
    evidence: dict,
    output_path: Path,
    commits: list[dict] | None = None,
) -> dict:
    """要求专用 AI agent 只基于给定 Git 证据输出结构化判断。"""
    ai_path = output_path.with_suffix(".ai.json")
    evidence_path = output_path.with_suffix(".evidence.json")
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    schema_hint = (
        '{"conclusion":str,"issues":[{"candidate_id":str,'
        '"status":"report|dismiss","title":str,"analysis":str,'
        '"severity":"info|low|medium|high|critical","confidence":0-100,'
        '"commit_shas":[str]}],"stages":[{"name":str,"conclusion":str,'
        '"reason":str,"confidence":0-100,"start_sha":str,'
        '"key_shas":[str]}]}'
    )
    request = (
        "请站在操作系统内核赛题评委角度，分析随消息附加的 development evidence JSON，"
        "逐一复核问题候选，并按时间连续区间归纳开发阶段。"
        "只能使用输入事实，不得编造提交、日期、LOC 或文件。所有候选必须恰好复核一次；"
        "阶段必须覆盖全部 timeline，不能重叠或留空。只有证据足以影响真实性、过程可信度或"
        "章程符合性时才标为 report；不能仅因提交较大就下负面结论。问题标题直接点明性质，"
        "analysis 用可复算数字说明为何需要评委复核；严重度按对评审结论的影响填写。"
        "阶段按功能目标合并，避免逐提交复述；conclusion 先写提交历史呈现的开发目标或代码变更，"
        "再写主要限制，reason 只说明划分依据。Git 历史不能证明当前版本可编译、可启动、可运行或"
        "测试通过；总体和阶段结论只能写‘提交历史显示/呈现围绕某目标开发’，禁止把提交主题或文件"
        "变更改写为动态验证结论。明确禁止出现以下短语或其变体：‘测试通过’、‘跑通’、"
        "‘通过 LTP/测试/用例’、‘使…测试通过’、‘能编译/能启动/能运行’；描述限制时应写"
        "‘需逐项修补’、‘存在功能缺陷’这类陈述，不得写‘才能通过测试’这类通过口吻。"
        "文字必须精炼，不写套话。所有结论和分析必须写成完整句子，"
        "禁止使用‘…’或‘...’省略未说完的内容。\n"
        f"repo_id: {repo_id}\n"
        f"evidence_file: {evidence_path.resolve()}\n"
        f"expected_schema: {schema_hint}\n"
        f"output_path: {ai_path.resolve()}\n"
        "只调用 write_report；content 为合法 JSON 字符串，output_path 必须使用上面的绝对路径。"
    )

    def _delivery_complete(value: dict) -> bool | str:
        if commits is None:
            return True
        try:
            validate_ai_development_result(value, evidence, commits)
        except RuntimeError as exc:
            return str(exc)
        return True

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
        input_files=(evidence_path,),
        cache_validator=_delivery_complete,
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
    repository_url = ""
    try:
        # 只接受传入仓库本身的 remote，不能让归档子目录继承外层工作树地址。
        git_root = Path(_git(repo, "rev-parse", "--show-toplevel").strip()).resolve()
        if git_root == repo:
            from oskernel_agent.comparison.report.gitlab_links import repo_web_url

            repository_url = repo_web_url(_git(repo, "remote", "get-url", "origin").strip()) or ""
    except (RuntimeError, OSError):
        repository_url = ""
    evidence = build_development_evidence(
        commits, shallow=shallow, min_commits=min_commits
    )
    ai_result = run_ai_development_analysis(
        repo, repo_id or repo.name, evidence, output, commits
    )
    analysis = analyze_history(
        repo_id or repo.name,
        commits,
        ai_result,
        shallow=shallow,
        min_commits=min_commits,
        repository_url=repository_url,
    )
    rendered = render_development_html(analysis)
    assert_report_complete(rendered)
    output.write_text(rendered, encoding="utf-8")
    digest_path = output.with_suffix(".digest.json")
    digest_path.write_text(
        json.dumps(analysis["digest"].model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "html_path": str(output),
        "digest_path": str(digest_path),
        "ai_path": str(output.with_suffix(".ai.json")),
        "evidence_path": str(output.with_suffix(".evidence.json")),
        "commit_count": len(commits),
        "stage_count": len(analysis["stages"]),
    }
