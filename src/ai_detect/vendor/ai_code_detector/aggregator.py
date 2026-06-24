"""
Repository-level aggregation of PipelineResult objects.

RepoAggregator takes a flat list[PipelineResult] and produces an
AggregatedReport with seven analysis dimensions:

  A. OverallStats      -- counts, ratios, average confidence
  B. LanguageStat list -- per-language breakdown
  C. DirectoryNode     -- recursive directory tree with LLM ratios
  D. HighRiskFile list -- top-N files by LLM-function ratio
  E. SuspiciousFunction list -- high-confidence LLM functions
  F. AuthorStat list   -- per-author LLM ratio (requires git blame)
  G. MonthlyTrend list -- monthly commit trend (requires git blame)

AggregatedReport provides three serialisation methods:
  .to_dict()          -- JSON-serialisable dict
  .to_json()          -- pretty-printed JSON string
  .to_summary_text()  -- Markdown summary suitable for PR comments

Legacy Aggregator (ScoredFunction -> RepoReport) is kept for
backward compatibility with the original pipeline skeleton.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from .models import (
    FileResult,
    FunctionBlock,
    Language,
    RepoReport,
    RepoSummary,
    ScoredFunction,
    Label,
)
from .pipeline import PipelineResult

# ---------------------------------------------------------------------------
# Dimension data models
# ---------------------------------------------------------------------------


class OverallStats(BaseModel):
    """Dimension A: repository-wide statistics."""

    total_functions: int = 0
    total_loc: int = 0
    llm_count: int = 0
    human_count: int = 0
    uncertain_count: int = 0
    # Ratios are over decided functions (LLM + Human); uncertain excluded
    llm_ratio_by_count: float = 0.0
    llm_ratio_by_loc: float = 0.0
    average_confidence: float = 0.0  # mean over non-uncertain results


class LanguageStat(BaseModel):
    """Dimension B: per-language breakdown."""

    language: str
    total_functions: int = 0
    llm_count: int = 0
    human_count: int = 0
    uncertain_count: int = 0
    llm_ratio: float = 0.0
    average_confidence: float = 0.0


class DirectoryNode(BaseModel):
    """Dimension C: one node in the recursive directory tree."""

    path: str                                                # e.g. "src/api"
    name: str                                                # last component
    total_functions: int = 0
    llm_count: int = 0
    llm_ratio: float = 0.0
    children: dict[str, "DirectoryNode"] = Field(default_factory=dict)

    def to_tree_dict(self) -> dict:
        """Recursively convert to a plain nested dict (JSON-serialisable)."""
        return {
            "path": self.path,
            "name": self.name,
            "total_functions": self.total_functions,
            "llm_count": self.llm_count,
            "llm_ratio": round(self.llm_ratio, 4),
            "children": {
                k: v.to_tree_dict()
                for k, v in sorted(self.children.items())
            },
        }


# Required for Pydantic v2 recursive model
DirectoryNode.model_rebuild()


class HighRiskFile(BaseModel):
    """Dimension D: one entry in the top-N high-risk file list."""

    path: str
    total_functions: int = 0
    llm_count: int = 0
    llm_ratio: float = 0.0
    most_suspicious_fn: str = ""  # name of highest-confidence LLM function


class SuspiciousFunction(BaseModel):
    """Dimension E: a single high-confidence LLM-suspected function."""

    file_path: str
    function_name: str
    qualified_name: str
    start_line: int
    end_line: int
    loc: int
    language: str = ""
    confidence: float
    log_rank: float | None
    detect_score: float | None
    stage: str
    source: str = ""  # function source code, populated for HTML reports


class AuthorStat(BaseModel):
    """Dimension F: per-author LLM function statistics (git blame)."""

    author: str
    total_functions: int = 0
    llm_count: int = 0
    llm_ratio: float = 0.0


class MonthlyTrend(BaseModel):
    """Dimension G: monthly LLM function counts (git blame commit timestamps)."""

    year_month: str   # e.g. "2024-03"
    total_functions: int = 0
    llm_count: int = 0
    llm_ratio: float = 0.0


# ---------------------------------------------------------------------------
# Top-level report
# ---------------------------------------------------------------------------


class AggregatedReport(BaseModel):
    """Full aggregated report produced by RepoAggregator."""

    # Metadata
    repo_path: str
    generated_at: str
    total_files_scanned: int = 0

    # Dimensions
    overall: OverallStats = Field(default_factory=OverallStats)
    by_language: list[LanguageStat] = Field(default_factory=list)
    directory_tree: DirectoryNode = Field(
        default_factory=lambda: DirectoryNode(path=".", name=".")
    )
    high_risk_files: list[HighRiskFile] = Field(default_factory=list)
    suspicious_functions: list[SuspiciousFunction] = Field(default_factory=list)
    by_author: list[AuthorStat] = Field(default_factory=list)
    monthly_trend: list[MonthlyTrend] = Field(default_factory=list)

    git_blame_available: bool = False

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict (Path objects converted to str)."""
        return json.loads(self.model_dump_json())

    def to_json(self, indent: int = 2) -> str:
        """Return a pretty-printed JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_summary_text(self) -> str:
        """Return a Markdown summary suitable for PR comments or terminal output."""
        o = self.overall

        decided = o.llm_count + o.human_count
        llm_pct = f"{o.llm_ratio_by_count:.1%}" if decided else "n/a"
        llm_loc_pct = f"{o.llm_ratio_by_loc:.1%}" if o.total_loc else "n/a"

        lines: list[str] = [
            "## AI Code Detection Report",
            "",
            f"**Repository**: `{self.repo_path}`  "
            f"**Scanned**: {self.generated_at}  "
            f"**Files**: {self.total_files_scanned}",
            "",
            "### Overall",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total functions | {o.total_functions} |",
            f"| LLM suspected | **{o.llm_count}** ({llm_pct} by count, "
            f"{llc_loc_pct(o)} by LOC) |",
            f"| Human | {o.human_count} |",
            f"| Uncertain / skipped | {o.uncertain_count} |",
            f"| Average confidence | {o.average_confidence:.2f} |",
            "",
        ]

        # Top risk files (max 10 in summary)
        if self.high_risk_files:
            lines += [
                "### Top Risk Files",
                "| File | LLM | Total | Ratio |",
                "|------|-----|-------|-------|",
            ]
            for f in self.high_risk_files[:10]:
                lines.append(
                    f"| `{f.path}` | {f.llm_count} | {f.total_functions} "
                    f"| {f.llm_ratio:.1%} |"
                )
            lines.append("")

        # By language
        if self.by_language:
            lines += [
                "### By Language",
                "| Language | Functions | LLM | LLM% |",
                "|----------|-----------|-----|------|",
            ]
            for ls in sorted(self.by_language, key=lambda x: -x.llm_count):
                lines.append(
                    f"| {ls.language} | {ls.total_functions} | {ls.llm_count} "
                    f"| {ls.llm_ratio:.1%} |"
                )
            lines.append("")

        # Most suspicious functions (max 5)
        if self.suspicious_functions:
            lines += [
                "### Most Suspicious Functions",
                "| Function | File | Confidence | Score |",
                "|----------|------|------------|-------|",
            ]
            for fn in self.suspicious_functions[:5]:
                score_str = (
                    f"{fn.detect_score:.3f}" if fn.detect_score is not None else "-"
                )
                lines.append(
                    f"| `{fn.qualified_name or fn.function_name}` "
                    f"| `{fn.file_path}:{fn.start_line}` "
                    f"| {fn.confidence:.2f} | {score_str} |"
                )
            lines.append("")

        # Author breakdown (if available)
        if self.by_author:
            lines += [
                "### By Author (git blame)",
                "| Author | LLM Functions | Total | LLM% |",
                "|--------|---------------|-------|------|",
            ]
            for a in self.by_author[:8]:
                lines.append(
                    f"| {a.author} | {a.llm_count} | {a.total_functions} "
                    f"| {a.llm_ratio:.1%} |"
                )
            lines.append("")

        lines += [
            "---",
            "*Generated by ai-code-detector - DetectCodeGPT method - "
            "Results are probabilistic, not definitive.*",
        ]
        return "\n".join(lines)


def llc_loc_pct(o: OverallStats) -> str:
    return f"{o.llm_ratio_by_loc:.1%}" if o.total_loc else "n/a"


# ---------------------------------------------------------------------------
# Git blame helpers
# ---------------------------------------------------------------------------


def _get_blame_info(
    file_path: Path,
    start_line: int,
    end_line: int,
    repo_root: Path,
) -> tuple[str, str] | None:
    """Return (author_name, year_month) for the given line range, or None."""
    try:
        result = subprocess.run(
            [
                "git", "blame", "--porcelain",
                f"-L{start_line},{end_line}",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=8,
        )
        if result.returncode != 0:
            return None
        return _parse_blame_output(result.stdout)
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None


def _parse_blame_output(output: str) -> tuple[str, str] | None:
    """Extract (author, year_month) from porcelain git blame output."""
    author: str | None = None
    year_month: str | None = None
    for line in output.splitlines():
        if line.startswith("author ") and not line.startswith("author-"):
            author = line[7:].strip()
        elif line.startswith("author-time "):
            try:
                ts = int(line[12:].strip())
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                year_month = dt.strftime("%Y-%m")
            except (ValueError, OSError):
                pass
    if author and year_month:
        return author, year_month
    return None


# ---------------------------------------------------------------------------
# Main aggregator
# ---------------------------------------------------------------------------


class RepoAggregator:
    """Aggregates a list[PipelineResult] into a multi-dimensional AggregatedReport.

    Args:
        top_n_files:           Number of files to include in high_risk_files list.
        min_confidence_llm:    Minimum confidence to include in suspicious_functions.
        enable_git_blame:      Whether to run git blame (subprocess). Safe to disable.
        max_blame_calls:       Cap on git blame subprocesses (performance guard).
    """

    def __init__(
        self,
        top_n_files: int = 20,
        min_confidence_llm: float = 0.7,
        enable_git_blame: bool = True,
        max_blame_calls: int = 100,
    ) -> None:
        self.top_n_files = top_n_files
        self.min_confidence_llm = min_confidence_llm
        self.enable_git_blame = enable_git_blame
        self.max_blame_calls = max_blame_calls

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def aggregate(
        self,
        results: list[PipelineResult],
        repo_path: Path,
    ) -> AggregatedReport:
        """Aggregate *results* into a full AggregatedReport.

        Args:
            results:    Output of DetectionPipeline.process_batch().
            repo_path:  Root of the scanned repository (used for relative paths).
        """
        repo_str = str(repo_path)
        generated_at = _now_iso()
        total_files = len({str(r.function_block.file_path) for r in results})

        # Compute each dimension
        overall = self._compute_overall(results)
        by_lang = self._compute_by_language(results)
        dir_tree = self._compute_dir_tree(results, repo_path)
        high_risk = self._compute_high_risk_files(results, repo_path)
        suspicious = self._compute_suspicious_functions(results, repo_path)

        # Git blame (optional; degrades gracefully)
        by_author: list[AuthorStat] = []
        monthly: list[MonthlyTrend] = []
        blame_ok = False
        if self.enable_git_blame and results:
            by_author, monthly, blame_ok = self._compute_git_blame(
                results, repo_path
            )

        return AggregatedReport(
            repo_path=repo_str,
            generated_at=generated_at,
            total_files_scanned=total_files,
            overall=overall,
            by_language=by_lang,
            directory_tree=dir_tree,
            high_risk_files=high_risk,
            suspicious_functions=suspicious,
            by_author=by_author,
            monthly_trend=monthly,
            git_blame_available=blame_ok,
        )

    # ------------------------------------------------------------------
    # Dimension A: overall
    # ------------------------------------------------------------------

    def _compute_overall(self, results: list[PipelineResult]) -> OverallStats:
        if not results:
            return OverallStats()

        llm = [r for r in results if r.label == "LLM"]
        human = [r for r in results if r.label == "Human"]
        uncertain = [r for r in results if r.label == "Uncertain"]

        total = len(results)
        decided = len(llm) + len(human)

        total_loc = sum(r.function_block.loc for r in results)
        llm_loc = sum(r.function_block.loc for r in llm)
        human_loc = sum(r.function_block.loc for r in human)
        decided_loc = llm_loc + human_loc

        llm_ratio_count = len(llm) / decided if decided else 0.0
        llm_ratio_loc = llm_loc / decided_loc if decided_loc else 0.0

        decided_conf = [r.confidence for r in results if r.label != "Uncertain"]
        avg_conf = sum(decided_conf) / len(decided_conf) if decided_conf else 0.0

        return OverallStats(
            total_functions=total,
            total_loc=total_loc,
            llm_count=len(llm),
            human_count=len(human),
            uncertain_count=len(uncertain),
            llm_ratio_by_count=llm_ratio_count,
            llm_ratio_by_loc=llm_ratio_loc,
            average_confidence=avg_conf,
        )

    # ------------------------------------------------------------------
    # Dimension B: by language
    # ------------------------------------------------------------------

    def _compute_by_language(
        self, results: list[PipelineResult]
    ) -> list[LanguageStat]:
        buckets: dict[str, list[PipelineResult]] = defaultdict(list)
        for r in results:
            buckets[r.function_block.language.value].append(r)

        stats: list[LanguageStat] = []
        for lang, items in sorted(buckets.items()):
            llm    = [x for x in items if x.label == "LLM"]
            human  = [x for x in items if x.label == "Human"]
            unc    = [x for x in items if x.label == "Uncertain"]
            decided = len(llm) + len(human)
            decided_items = [x for x in items if x.label != "Uncertain"]
            avg_conf = (
                sum(x.confidence for x in decided_items) / len(decided_items)
                if decided_items else 0.0
            )
            stats.append(
                LanguageStat(
                    language=lang,
                    total_functions=len(items),
                    llm_count=len(llm),
                    human_count=len(human),
                    uncertain_count=len(unc),
                    llm_ratio=len(llm) / decided if decided else 0.0,
                    average_confidence=avg_conf,
                )
            )
        return stats

    # ------------------------------------------------------------------
    # Dimension C: directory tree
    # ------------------------------------------------------------------

    def _compute_dir_tree(
        self, results: list[PipelineResult], repo_root: Path
    ) -> DirectoryNode:
        root = DirectoryNode(path=".", name=".")

        # Aggregate per-file stats first
        file_buckets: dict[str, list[PipelineResult]] = defaultdict(list)
        for r in results:
            file_buckets[str(r.function_block.file_path)].append(r)

        for abs_path_str, file_results in file_buckets.items():
            total = len(file_results)
            llm = sum(1 for r in file_results if r.label == "LLM")

            # Compute relative path for tree keys
            try:
                rel = Path(abs_path_str).relative_to(repo_root)
            except ValueError:
                rel = Path(abs_path_str)

            parts = rel.parts  # e.g. ("src", "api", "handler.py")

            # Add to root
            root.total_functions += total
            root.llm_count += llm

            # Walk / create directory nodes (skip the filename component)
            current = root
            for depth, part in enumerate(parts[:-1]):
                if part not in current.children:
                    dir_path = "/".join(parts[: depth + 1])
                    current.children[part] = DirectoryNode(
                        path=dir_path, name=part
                    )
                current = current.children[part]
                current.total_functions += total
                current.llm_count += llm

        # Compute ratios bottom-up
        def _set_ratios(node: DirectoryNode) -> None:
            if node.total_functions:
                node.llm_ratio = node.llm_count / node.total_functions
            for child in node.children.values():
                _set_ratios(child)

        _set_ratios(root)
        return root

    # ------------------------------------------------------------------
    # Dimension D: high-risk files
    # ------------------------------------------------------------------

    def _compute_high_risk_files(
        self, results: list[PipelineResult], repo_root: Path
    ) -> list[HighRiskFile]:
        file_buckets: dict[str, list[PipelineResult]] = defaultdict(list)
        for r in results:
            file_buckets[str(r.function_block.file_path)].append(r)

        files: list[HighRiskFile] = []
        for abs_path_str, items in file_buckets.items():
            llm_items = [x for x in items if x.label == "LLM"]
            decided = sum(1 for x in items if x.label != "Uncertain")
            if not decided:
                continue
            llm_ratio = len(llm_items) / decided

            # Relative POSIX path for display (forward slashes on all platforms)
            try:
                rel = Path(abs_path_str).relative_to(repo_root).as_posix()
            except ValueError:
                rel = Path(abs_path_str).as_posix()

            # Most suspicious function name
            top_fn = ""
            if llm_items:
                best = max(llm_items, key=lambda x: x.confidence)
                top_fn = best.function_block.qualified_name or best.function_block.name

            files.append(
                HighRiskFile(
                    path=rel,
                    total_functions=len(items),
                    llm_count=len(llm_items),
                    llm_ratio=llm_ratio,
                    most_suspicious_fn=top_fn,
                )
            )

        return sorted(files, key=lambda f: -f.llm_ratio)[: self.top_n_files]

    # ------------------------------------------------------------------
    # Dimension E: suspicious functions
    # ------------------------------------------------------------------

    def _compute_suspicious_functions(
        self, results: list[PipelineResult], repo_root: Path
    ) -> list[SuspiciousFunction]:
        suspicious: list[SuspiciousFunction] = []
        for r in results:
            if r.label != "LLM" or r.confidence < self.min_confidence_llm:
                continue
            b = r.function_block
            try:
                rel = b.file_path.relative_to(repo_root).as_posix()
            except ValueError:
                rel = b.file_path.as_posix()
            suspicious.append(
                SuspiciousFunction(
                    file_path=rel,
                    function_name=b.name,
                    qualified_name=b.qualified_name or b.name,
                    start_line=b.start_line,
                    end_line=b.end_line,
                    loc=b.loc,
                    language=b.language.value,
                    confidence=r.confidence,
                    log_rank=r.log_rank,
                    detect_score=r.detect_score,
                    stage=r.stage,
                    source=b.source,
                )
            )
        return sorted(suspicious, key=lambda x: -x.confidence)

    # ------------------------------------------------------------------
    # Dimensions F & G: git blame
    # ------------------------------------------------------------------

    def _compute_git_blame(
        self,
        results: list[PipelineResult],
        repo_root: Path,
    ) -> tuple[list[AuthorStat], list[MonthlyTrend], bool]:
        """Run git blame on LLM functions and aggregate by author and month."""
        # Check git is available
        try:
            check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                cwd=str(repo_root),
                timeout=5,
            )
            if check.returncode != 0:
                return [], [], False
        except (subprocess.SubprocessError, FileNotFoundError, OSError):
            return [], [], False

        author_buckets: dict[str, list[str]] = defaultdict(list)  # author -> [label]
        month_buckets: dict[str, list[str]] = defaultdict(list)   # year_month -> [label]

        blame_calls = 0
        for r in results:
            if blame_calls >= self.max_blame_calls:
                break
            b = r.function_block
            info = _get_blame_info(b.file_path, b.start_line, b.end_line, repo_root)
            if info is None:
                continue
            author, year_month = info
            label = r.label
            author_buckets[author].append(label)
            month_buckets[year_month].append(label)
            blame_calls += 1

        by_author: list[AuthorStat] = []
        for author, labels in sorted(author_buckets.items()):
            llm = labels.count("LLM")
            decided = sum(1 for lb in labels if lb != "Uncertain")
            by_author.append(
                AuthorStat(
                    author=author,
                    total_functions=len(labels),
                    llm_count=llm,
                    llm_ratio=llm / decided if decided else 0.0,
                )
            )
        by_author.sort(key=lambda a: -a.llm_count)

        monthly: list[MonthlyTrend] = []
        for ym in sorted(month_buckets):
            labels = month_buckets[ym]
            llm = labels.count("LLM")
            decided = sum(1 for lb in labels if lb != "Uncertain")
            monthly.append(
                MonthlyTrend(
                    year_month=ym,
                    total_functions=len(labels),
                    llm_count=llm,
                    llm_ratio=llm / decided if decided else 0.0,
                )
            )

        return by_author, monthly, blame_calls > 0


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Legacy Aggregator (ScoredFunction -> RepoReport)
# Kept for backward compatibility; new code should use RepoAggregator.
# ---------------------------------------------------------------------------


class Aggregator:
    """Aggregates ScoredFunction lists into a RepoReport (legacy pipeline)."""

    def build_report(
        self,
        scored: list[ScoredFunction],
        repo: Path,
        model_id: str,
        config: dict,
    ) -> RepoReport:
        file_groups = self._group_by_file(scored)
        file_results = [
            self._build_file_result(path, fns[0].chunk.language, fns)
            for path, fns in file_groups.items()
        ]
        file_results.sort(key=lambda f: -f.suspicion_rate)
        summary = self._build_summary(file_results)
        return RepoReport(
            repo=repo,
            scanned_at=_now_iso(),
            model_id=model_id,
            config=config,
            summary=summary,
            files=file_results,
        )

    def _group_by_file(
        self, scored: list[ScoredFunction]
    ) -> dict[Path, list[ScoredFunction]]:
        groups: dict[Path, list[ScoredFunction]] = defaultdict(list)
        for fn in scored:
            groups[fn.chunk.file_path].append(fn)
        return dict(groups)

    def _build_file_result(
        self,
        path: Path,
        language: Language,
        functions: list[ScoredFunction],
    ) -> FileResult:
        analysed = [f for f in functions if f.label != Label.SKIPPED]
        ai = [f for f in analysed if f.label == Label.AI_SUSPECTED]
        rate = len(ai) / len(analysed) if analysed else 0.0
        return FileResult(
            path=path,
            language=language,
            total_functions=len(functions),
            analysed_functions=len(analysed),
            ai_suspected=len(ai),
            suspicion_rate=rate,
            functions=functions,
        )

    def _build_summary(self, file_results: list[FileResult]) -> RepoSummary:
        total_fn = sum(f.total_functions for f in file_results)
        analysed = sum(f.analysed_functions for f in file_results)
        ai = sum(f.ai_suspected for f in file_results)
        rate = ai / analysed if analysed else 0.0
        return RepoSummary(
            total_files=len(file_results),
            total_functions=total_fn,
            analysed_functions=analysed,
            ai_suspected=ai,
            suspicion_rate=rate,
        )
