"""开发过程分析：从 Git 历史生成事实、异常和阶段化 HTML。"""

from __future__ import annotations

import html
import json
import math
import statistics
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from .models import EvidenceRef, Finding, ModuleDigest, ReportDigest
from .readability import concise_module_summary, explain_terms_on_first_use

_SOURCE_SUFFIXES = {".c", ".h", ".cc", ".cpp", ".rs", ".s", ".S", ".py", ".sh", ".ld"}
_MIN_COMMIT_HEURISTIC = 5
_LARGE_COMMIT_FLOOR = 1000

_STAGE_RULES = (
    ("文件系统", ("/fs/", "filesystem", "vfs", "inode", "fat", "ext4")),
    ("内存管理", ("/mm/", "memory", "page", "alloc", "cow", "vm")),
    ("进程与调度", ("sched", "task", "process", "thread", "fork", "signal")),
    ("设备与驱动", ("driver", "device", "virtio", "pci", "uart", "block")),
    ("网络与进程间通信", ("network", "socket", "net/", "ipc", "pipe")),
    ("启动与架构", ("boot", "arch/", "riscv", "loongarch", "x86", "trap")),
    ("测试与构建", ("test", "ci", "build", "makefile", "cargo", "script")),
    ("文档", ("readme", "docs/", ".md")),
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
        repo, "log", "--reverse", "--date=iso-strict", "--numstat",
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
                "sha": sha, "date": date, "author": author, "subject": subject,
                "additions": 0, "deletions": 0, "files": [],
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
        current["files"].append({"path": path, "additions": additions, "deletions": deletions})
    if current:
        commits.append(current)
    return commits, shallow


def _changes(commit: dict) -> int:
    return int(commit.get("additions") or 0) + int(commit.get("deletions") or 0)


def _stage_of(commit: dict) -> str:
    haystack = " ".join([
        str(commit.get("subject") or ""),
        *(str(item.get("path") or "") for item in commit.get("files") or []),
    ]).lower().replace("\\", "/")
    scores = [(label, sum(haystack.count(token) for token in tokens))
              for label, tokens in _STAGE_RULES]
    label, score = max(scores, key=lambda item: item[1])
    return label if score else "综合开发"


def _date(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _large_threshold(commits: list[dict]) -> int:
    changes = [_changes(commit) for commit in commits if _changes(commit) > 0]
    if not changes:
        return _LARGE_COMMIT_FLOOR
    if len(changes) < 8:
        # 样本太少时用固定、可解释的 1000 LOC 门槛；否则中位数会被单个导入提交
        # 拉高到“永远没有异常”的水平。
        return _LARGE_COMMIT_FLOOR
    median = statistics.median(changes)
    return max(_LARGE_COMMIT_FLOOR, math.ceil(median * 5))


def _build_stages(commits: list[dict]) -> list[dict]:
    stages: list[dict] = []
    for commit in commits:
        label = _stage_of(commit)
        when = _date(str(commit["date"]))
        new_stage = not stages
        if stages:
            previous_date = _date(str(stages[-1]["commits"][-1]["date"]))
            new_stage = stages[-1]["name"] != label or (when - previous_date).days > 14
        if new_stage:
            stages.append({"name": label, "commits": []})
        stages[-1]["commits"].append(commit)

    # 过度碎片化时按时间连续区间压到 12 段；每段用占比最高的主题命名，
    # 多主题势均力敌时写“综合迭代”，避免生成“及后续综合开发”式机械长标题。
    if len(stages) <= 12:
        return stages
    chunk_size = math.ceil(len(stages) / 12)
    compact: list[dict] = []
    for offset in range(0, len(stages), chunk_size):
        chunk = stages[offset: offset + chunk_size]
        counts = Counter()
        for stage in chunk:
            counts[stage["name"]] += len(stage["commits"])
        name, count = counts.most_common(1)[0]
        total_commits = sum(counts.values())
        if len(counts) > 1 and count * 2 < total_commits:
            name = "综合迭代"
        compact.append({
            "name": name,
            "commits": [commit for stage in chunk for commit in stage["commits"]],
        })
    return compact[:12]


def analyze_history(repo_id: str, commits: list[dict], *, shallow: bool = False) -> dict:
    """把可见 Git 历史转成可解释的异常与阶段。"""
    threshold = _large_threshold(commits)
    findings: list[Finding] = []
    if len(commits) < _MIN_COMMIT_HEURISTIC:
        findings.append(Finding(
            title="提交次数较少（需按章程确认）",
            detail=(f"当前可见历史只有 {len(commits)} 次提交；工具采用少于 "
                    f"{_MIN_COMMIT_HEURISTIC} 次的启发式提醒，不等同于比赛违规认定。"),
            severity="medium", confidence=1.0, source="development",
        ))
    if shallow:
        findings.append(Finding(
            title="Git 历史为浅克隆",
            detail="当前仓库只包含部分提交，提交次数、时间跨度和阶段划分均可能不完整。",
            severity="high", confidence=1.0, source="development",
        ))

    large = [commit for commit in commits if _changes(commit) >= threshold]
    for index, commit in enumerate(large[:5]):
        initial = bool(commits and commit["sha"] == commits[0]["sha"])
        findings.append(Finding(
            title="初始代码大规模导入" if initial else "单次大规模代码提交",
            detail=(f"提交 {commit['sha'][:8]} 一次变更 {_changes(commit)} 代码行（LOC），"
                    f"达到本报告阈值 {threshold} LOC；"
                    + ("初始导入通常可解释，仍需核对来源。" if initial else "需结合开发说明核对来源与完成过程。")),
            severity="low" if initial else "high",
            confidence=0.95,
            source="development",
            evidence=[EvidenceRef(path=f"commit:{commit['sha']}", excerpt=commit.get("subject", ""))],
        ))

    for left, right in zip(large, large[1:]):
        delta_hours = (_date(str(right["date"])) - _date(str(left["date"]))).total_seconds() / 3600
        if 0 <= delta_hours <= 24:
            findings.append(Finding(
                title="24 小时内连续大规模提交",
                detail=(f"提交 {left['sha'][:8]} 与 {right['sha'][:8]} 相隔 "
                        f"{delta_hours:.1f} 小时，合计变更 {_changes(left) + _changes(right)} LOC。"),
                severity="high", confidence=0.95, source="development",
                evidence=[EvidenceRef(path=f"commit:{left['sha']}"),
                          EvidenceRef(path=f"commit:{right['sha']}")],
            ))
            break

    stages = []
    for number, stage in enumerate(_build_stages(commits), start=1):
        stage_commits = stage["commits"]
        files = Counter(
            item["path"]
            for commit in stage_commits
            for item in commit.get("files") or []
            if Path(item["path"]).suffix in _SOURCE_SUFFIXES
        )
        key_commits = sorted(stage_commits, key=lambda item: (-_changes(item), item["sha"]))[:3]
        stages.append({
            "number": number,
            "name": stage["name"],
            "start": str(stage_commits[0]["date"])[:10],
            "end": str(stage_commits[-1]["date"])[:10],
            "commit_count": len(stage_commits),
            "loc": sum(_changes(commit) for commit in stage_commits),
            "key_commits": [{
                "sha": commit["sha"], "subject": commit.get("subject", ""),
                "date": str(commit["date"])[:10], "loc": _changes(commit),
            } for commit in key_commits],
            "files": [path for path, _ in files.most_common(8)],
        })

    authors = Counter(str(commit.get("author") or "未知") for commit in commits)
    dates = [str(commit.get("date") or "")[:10] for commit in commits if commit.get("date")]
    conclusion = (
        f"可见历史包含 {len(commits)} 次提交，识别 {len(stages)} 个开发阶段；"
        f"有 {sum(1 for finding in findings if finding.severity in ('high', 'critical'))} 项高风险过程线索需复核。"
    )
    digest = ReportDigest(
        repo_id=repo_id,
        kind="development",
        conclusion=conclusion,
        confidence=0.65 if shallow else 0.9,
        findings=findings[:8],
        modules=[ModuleDigest(
            name=f"阶段 {stage['number']}：{stage['name']}",
            summary=concise_module_summary(
                f"{stage['start']} 至 {stage['end']}，{stage['commit_count']} 次提交，"
                f"变更 {stage['loc']} LOC。"
            ),
            evidence_count=len(stage["key_commits"]),
        ) for stage in stages],
        metrics={
            "commit_count": len(commits),
            "start_date": dates[0] if dates else "",
            "end_date": dates[-1] if dates else "",
            "author_count": len(authors),
            "large_commit_threshold": threshold,
            "large_commit_count": len(large),
            "shallow": shallow,
        },
    )
    return {
        "repo_id": repo_id,
        "commits": commits,
        "stages": stages,
        "authors": authors.most_common(8),
        "digest": digest,
    }


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def render_development_html(analysis: dict) -> str:
    digest: ReportDigest = analysis["digest"]
    metrics = digest.metrics
    findings = digest.decision_findings(6)
    finding_html = "".join(
        '<li class="finding ' + _esc(item.severity) + '">'
        f'<div><strong>{_esc(item.title)}</strong><span>置信度 {round(item.confidence * 100)}%</span></div>'
        f'<p>{_esc(explain_terms_on_first_use(item.detail))}</p></li>'
        for item in findings
    ) or '<li class="finding info"><strong>未发现高风险提交异常</strong><p>仍需结合比赛章程和现场说明核对。</p></li>'

    stage_html: list[str] = []
    for stage in analysis.get("stages") or []:
        commits = "".join(
            f'<li><code>{_esc(item["sha"][:8])}</code> · {_esc(item["date"])} · '
            f'{_esc(item["subject"])} · {_esc(item["loc"])} LOC</li>'
            for item in stage["key_commits"]
        ) or "<li>无可列出的关键提交</li>"
        files = "、".join(f'<code>{_esc(path)}</code>' for path in stage["files"]) or "无"
        stage_html.append(
            f'<article class="stage"><h3>阶段 {stage["number"]}：{_esc(stage["name"])}</h3>'
            f'<p>{_esc(stage["start"])} 至 {_esc(stage["end"])}；'
            f'{stage["commit_count"]} 次提交；变更 {stage["loc"]} LOC。</p>'
            f'<details><summary>关键提交与文件</summary><ul>{commits}</ul>'
            f'<p><strong>主要文件：</strong>{files}</p></details></article>'
        )

    authors = "、".join(f"{_esc(name)}（{count}）" for name, count in analysis.get("authors") or []) or "未知"
    title = f"{_esc(digest.repo_id)} 开发过程分析报告"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#f8fafc;color:#172033;font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:980px;margin:auto;padding:32px 22px 70px}}h1{{font-size:28px;margin:0 0 6px}}h2{{margin-top:34px;font-size:21px}}
.lead{{font-size:18px;margin:10px 0 22px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.metric,.stage,.panel{{background:white;border:1px solid #dbe3ee;border-radius:10px;padding:16px}}.metric b{{display:block;font-size:24px}}
.findings{{display:grid;gap:10px;padding:0;list-style:none}}.finding{{background:white;border-left:4px solid #f59e0b;padding:12px 14px}}
.finding.high,.finding.critical{{border-left-color:#dc2626}}.finding>div{{display:flex;justify-content:space-between;gap:12px}}.finding span{{font-size:12px;color:#64748b}}
.finding p{{margin:4px 0 0}}.stages{{display:grid;gap:12px}}.stage h3{{margin:0}}details summary{{cursor:pointer;color:#475569;font-weight:600}}code{{font-family:ui-monospace,Consolas,monospace}}
@media(max-width:640px){{main{{padding:20px 14px}}.finding>div{{display:block}}}}
</style></head><body><main>
<header><h1>{title}</h1><p class="lead">{_esc(digest.conclusion)}</p></header>
<section id="findings"><h2>结论与问题</h2><ol class="findings">{finding_html}</ol></section>
<section><h2>历史概况</h2><div class="metrics">
<div class="metric"><b>{_esc(metrics.get('commit_count', 0))}</b><span>可见提交</span></div>
<div class="metric"><b>{_esc(metrics.get('start_date') or '-')}</b><span>最早提交</span></div>
<div class="metric"><b>{_esc(metrics.get('end_date') or '-')}</b><span>最近提交</span></div>
<div class="metric"><b>{_esc(metrics.get('large_commit_threshold', 0))}</b><span>大规模提交阈值（LOC）</span></div>
</div><div class="panel" style="margin-top:10px"><strong>主要贡献者：</strong>{authors}</div></section>
<section><h2>提交阶段</h2><div class="stages">{"".join(stage_html)}</div></section>
</main></body></html>"""


def generate_development_report(
    repo_path: str | Path,
    output_path: str | Path,
    *,
    repo_id: str | None = None,
) -> dict:
    repo = Path(repo_path).resolve()
    commits, shallow = collect_commits(repo)
    analysis = analyze_history(repo_id or repo.name, commits, shallow=shallow)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_development_html(analysis), encoding="utf-8")
    digest_path = output.with_suffix(".digest.json")
    digest_path.write_text(
        json.dumps(analysis["digest"].model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {"html_path": str(output), "digest_path": str(digest_path),
            "commit_count": len(commits), "stage_count": len(analysis["stages"])}
