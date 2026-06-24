"""
Report generation module.

Two classes:

  ReportWriter     -- serialises legacy RepoReport to JSON/HTML.
  ReportGenerator  -- generates rich HTML + JSON from AggregatedReport.

ReportGenerator output layout:
    output_dir/
    ├── report.html     self-contained interactive report
    ├── report.json     full AggregatedReport serialisation
    └── details/
        ├── fn_001.html  per-function detail page
        └── ...

Line-level highlighting (optional):
    If a *perplexity_calculator* is supplied, ReportGenerator calls
    compute_token_perplexities() for each suspicious function and
    highlights lines whose mean log-rank is more than *sigma* standard
    deviations below the function median.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Protocol, runtime_checkable

from jinja2 import Environment, PackageLoader, select_autoescape

from .aggregator import AggregatedReport, SuspiciousFunction
from .models import RepoReport

# ---------------------------------------------------------------------------
# Shared Jinja2 environment
# ---------------------------------------------------------------------------

_JINJA_ENV = Environment(
    loader=PackageLoader("ai_code_detector", "templates"),
    autoescape=select_autoescape(["html"]),
)

# ---------------------------------------------------------------------------
# Token-perplexity provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TokenPerplexityProvider(Protocol):
    """Subset of PerplexityCalculator needed for line-level highlighting."""

    def compute_token_perplexities(self, code: str) -> list[tuple[str, float]]:
        """Return [(token_string, log_rank), ...] for every token in *code*."""
        ...


# ---------------------------------------------------------------------------
# Line highlighter
# ---------------------------------------------------------------------------


class LineHighlighter:
    """Converts per-token log-rank scores into highlighted line indices.

    Algorithm:
      1. Assign each token to its source line (by counting '\\n' characters).
      2. Compute the mean log-rank per line.
      3. A line is flagged when its mean falls below (median - sigma * std).
    """

    def __init__(self, sigma: float = 1.0) -> None:
        self.sigma = sigma

    def compute(
        self,
        token_perplexities: list[tuple[str, float]],
        source: str,
    ) -> list[int]:
        """Return 0-indexed line numbers that are below the highlight threshold."""
        if not token_perplexities:
            return []

        # Build per-line score buckets by counting newlines in processed source
        line_buckets: dict[int, list[float]] = {}
        char_pos = 0
        for token, score in token_perplexities:
            line_idx = source[:char_pos].count("\n")
            line_buckets.setdefault(line_idx, []).append(score)
            char_pos += len(token)

        if not line_buckets:
            return []

        line_means = {ln: sum(s) / len(s) for ln, s in line_buckets.items()}
        vals = list(line_means.values())
        if len(vals) < 2:
            return []

        med = statistics.median(vals)
        std = statistics.stdev(vals)
        threshold = med - self.sigma * std

        return sorted(ln for ln, mean in line_means.items() if mean < threshold)


# ---------------------------------------------------------------------------
# ReportGenerator
# ---------------------------------------------------------------------------

_COLOURS = {
    "llm":      "#ef4444",
    "human":    "#22c55e",
    "uncertain": "#94a3b8",
}

_DETAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{{ fn.qualified_name }}</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;background:#f8fafc;color:#1e293b;margin:0;padding:1.5rem;font-size:14px}
h1{font-size:1.1rem;margin-bottom:.5rem}.meta{font-size:.8rem;color:#64748b;margin-bottom:1rem}
pre{font-family:'JetBrains Mono','Fira Code',monospace;font-size:12px;line-height:1.7;background:#0f172a;color:#e2e8f0;padding:1rem;border-radius:8px;overflow-x:auto;counter-reset:line}
span.line{display:block;padding:0 .4rem}
span.line::before{content:counter(line);counter-increment:line;display:inline-block;width:2.5em;color:#475569;text-align:right;margin-right:1rem;user-select:none}
span.line.hl{background:rgba(239,68,68,.2);border-left:2px solid #ef4444}
a{color:#3b82f6;text-decoration:none}
</style>
</head>
<body>
<p><a href="../report.html">&larr; Back to report</a></p>
<h1>{{ fn.qualified_name }}</h1>
<div class="meta">
  {{ fn.file_path }}:{{ fn.start_line }}--{{ fn.end_line }} &nbsp;|&nbsp;
  Confidence: {{ "%.3f" | format(fn.confidence) }} &nbsp;|&nbsp;
  {% if fn.detect_score is not none %}NPR Score: {{ "%.4f" | format(fn.detect_score) }}{% endif %}
</div>
<pre>{% for i, line in lines %}
<span class="line{{ ' hl' if i in hl_set else '' }}">{{ line }}</span>{% endfor %}</pre>
</body>
</html>
"""


class ReportGenerator:
    """Generates rich HTML + JSON reports from AggregatedReport.

    Args:
        perplexity_calculator:  Optional provider for line-level highlighting.
        line_highlight_sigma:   Threshold in std deviations below median.
    """

    def __init__(
        self,
        perplexity_calculator: TokenPerplexityProvider | None = None,
        line_highlight_sigma: float = 1.0,
    ) -> None:
        self.calculator = perplexity_calculator
        self.highlighter = LineHighlighter(sigma=line_highlight_sigma)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(
        self,
        report: AggregatedReport,
        output_dir: Path,
    ) -> None:
        """Write report.html, report.json and details/*.html to *output_dir*."""
        output_dir.mkdir(parents=True, exist_ok=True)
        details_dir = output_dir / "details"
        details_dir.mkdir(exist_ok=True)

        # 1. JSON
        (output_dir / "report.json").write_text(
            report.to_json(indent=2), encoding="utf-8"
        )

        # 2. Compute line highlights
        hl_lines = self._compute_highlights(report.suspicious_functions)

        # 3. Main HTML
        html = self._render_main(report, hl_lines)
        (output_dir / "report.html").write_text(html, encoding="utf-8")

        # 4. Per-function detail pages
        env = Environment(autoescape=True)
        detail_tmpl = env.from_string(_DETAIL_TEMPLATE)
        for i, fn in enumerate(report.suspicious_functions, 1):
            hl_set = set(hl_lines.get(fn.qualified_name, []))
            lines_enum = list(enumerate(fn.source.splitlines()))
            html_detail = detail_tmpl.render(fn=fn, lines=lines_enum, hl_set=hl_set)
            (details_dir / f"fn_{i:03d}.html").write_text(html_detail, encoding="utf-8")

    # ------------------------------------------------------------------
    # Internal: highlights
    # ------------------------------------------------------------------

    def _compute_highlights(
        self, functions: list[SuspiciousFunction]
    ) -> dict[str, list[int]]:
        """Return {qualified_name: [highlighted_line_indices], ...}."""
        if self.calculator is None:
            return {}
        result: dict[str, list[int]] = {}
        for fn in functions:
            if not fn.source:
                continue
            try:
                tok_perp = self.calculator.compute_token_perplexities(fn.source)
                hl = self.highlighter.compute(tok_perp, fn.source)
                if hl:
                    result[fn.qualified_name] = hl
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # Internal: main HTML
    # ------------------------------------------------------------------

    def _render_main(
        self,
        report: AggregatedReport,
        hl_lines: dict[str, list[int]],
    ) -> str:
        o = report.overall

        # Pie chart data
        pie_data = [
            {"label": "LLM Suspected", "value": o.llm_count,     "color": _COLOURS["llm"]},
            {"label": "Human",         "value": o.human_count,   "color": _COLOURS["human"]},
            {"label": "Uncertain",     "value": o.uncertain_count,"color": _COLOURS["uncertain"]},
        ]

        # Language bar data (sorted by LLM ratio)
        bar_data = sorted(
            [
                {"label": ls.language, "llm": ls.llm_count, "total": ls.total_functions}
                for ls in report.by_language
            ],
            key=lambda x: -(x["llm"] / x["total"] if x["total"] else 0),
        )

        # Treemap: flatten dir tree into leaf directories
        treemap_nodes: list[dict] = []
        self._flatten_tree(report.directory_tree, treemap_nodes, max_depth=3)
        treemap_nodes.sort(key=lambda n: -n["total"])

        # Trend data
        trend_data = [
            {"year_month": t.year_month, "total": t.total_functions, "llm": t.llm_count}
            for t in report.monthly_trend
        ]

        suspicion_class = _colour_class(o.llm_ratio_by_count)

        template = _JINJA_ENV.get_template("report_full.html.jinja2")
        return template.render(
            report=report,
            suspicion_class=suspicion_class,
            hl_lines=hl_lines,
            pie_data_json=json.dumps(pie_data),
            bar_data_json=json.dumps(bar_data),
            treemap_data_json=json.dumps(treemap_nodes),
            trend_data_json=json.dumps(trend_data),
        )

    @staticmethod
    def _flatten_tree(
        node,
        out: list[dict],
        depth: int = 0,
        max_depth: int = 3,
    ) -> None:
        """Flatten DirectoryNode tree into a list for the treemap."""
        if depth > 0 and node.total_functions > 0:
            out.append({
                "path": node.path,
                "name": node.name,
                "total": node.total_functions,
                "llm": node.llm_count,
                "ratio": round(node.llm_ratio, 4),
            })
        if depth < max_depth:
            for child in node.children.values():
                ReportGenerator._flatten_tree(child, out, depth + 1, max_depth)


# ---------------------------------------------------------------------------
# Colour helper
# ---------------------------------------------------------------------------

def _colour_class(rate: float) -> str:
    if rate >= 0.5:
        return "danger"
    if rate >= 0.2:
        return "warning"
    return "safe"


# ---------------------------------------------------------------------------
# Legacy ReportWriter (RepoReport -> JSON / simple HTML)
# ---------------------------------------------------------------------------


class ReportWriter:
    """Serialises a RepoReport (legacy pipeline) to JSON or HTML."""

    def write(self, report: RepoReport, output: Path, format: str = "html") -> None:
        """Write *report* to *output* in the given *format* ('html' or 'json')."""
        if format == "json":
            self._write_json(report, output)
        elif format == "html":
            self._write_html(report, output)
        else:
            raise ValueError(f"Unknown format: {format!r}. Use 'html' or 'json'.")

    def _write_json(self, report: RepoReport, output: Path) -> None:
        data = self._to_dict(report)
        output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _to_dict(self, report: RepoReport) -> dict:
        repo_root = report.repo

        def _path(p: Path) -> str:
            try:
                return p.relative_to(repo_root).as_posix()
            except ValueError:
                return p.as_posix()

        files_out = []
        for f in report.files:
            fns_out = []
            for fn in f.functions:
                fns_out.append({
                    "name": fn.chunk.name,
                    "qualified_name": fn.chunk.qualified_name,
                    "start_line": fn.chunk.start_line,
                    "end_line": fn.chunk.end_line,
                    "loc": fn.chunk.loc,
                    "log_rank_score": fn.log_rank_score,
                    "detect_score": fn.detect_score,
                    "label": fn.label.value,
                    "confidence": fn.confidence,
                })
            files_out.append({
                "path": _path(f.path),
                "language": f.language.value,
                "total_functions": f.total_functions,
                "analysed_functions": f.analysed_functions,
                "ai_suspected": f.ai_suspected,
                "suspicion_rate": f.suspicion_rate,
                "functions": fns_out,
            })

        return {
            "repo": report.repo.as_posix(),
            "scanned_at": report.scanned_at,
            "model_id": report.model_id,
            "config": report.config,
            "summary": report.summary.model_dump(),
            "files": files_out,
        }

    def _write_html(self, report: RepoReport, output: Path) -> None:
        ctx = self._prepare_template_context(report)
        template = _JINJA_ENV.get_template("report.html.jinja2")
        html = template.render(**ctx)
        output.write_text(html, encoding="utf-8")

    def _prepare_template_context(self, report: RepoReport) -> dict:
        lang_stats: dict[str, dict] = {}
        for f in report.files:
            lang = f.language.value
            if lang not in lang_stats:
                lang_stats[lang] = {"total": 0, "ai": 0}
            lang_stats[lang]["total"] += f.total_functions
            lang_stats[lang]["ai"] += f.ai_suspected

        lang_breakdown = sorted(
            [
                {
                    "language": lang,
                    "total": s["total"],
                    "ai": s["ai"],
                    "ratio": s["ai"] / s["total"] if s["total"] else 0.0,
                }
                for lang, s in lang_stats.items()
            ],
            key=lambda x: -x["ratio"],
        )

        return {
            "report": report,
            "repo_posix": report.repo.as_posix(),
            "lang_breakdown": lang_breakdown,
            "suspicion_class": _colour_class(report.summary.suspicion_rate),
        }
