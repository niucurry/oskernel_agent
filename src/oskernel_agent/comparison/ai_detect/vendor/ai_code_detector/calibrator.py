"""
Calibrator: estimate optimal detection thresholds from labelled code samples.

Input layout:
    samples_dir/
    ├── llm/
    │   ├── python/  (or *.py directly)
    │   └── java/
    └── human/
        ├── python/
        └── java/

Algorithm (per language):
  1. Extract functions from llm/ and human/ sub-trees.
  2. Compute log_rank for every function via the provider (batched).
  3. Optionally compute DetectCodeGPT NPR score (--compute-detect-score).
  4. For each metric compute a ROC curve.
  5. Select:
     - Youden's J threshold   (maximises TPR - FPR)
     - F1-max threshold        (maximises F1 score)
     - Recall-max threshold    (catches all LLM, at cost of FP rate)
  6. Save per-language thresholds to YAML.
  7. Render an interactive HTML calibration report.

No sklearn dependency — ROC + AUC computed from first principles.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import yaml
from jinja2 import Environment, PackageLoader, select_autoescape

from .detector import DetectCodeGPT, LogRankProvider
from .extractor import FunctionExtractor
from .models import Language

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ROCResult:
    """Output of a single binary-classification ROC analysis."""

    # Curve points (parallel lists, length N+1)
    fpr: list[float]
    tpr: list[float]
    thresholds: list[float]  # original-scale score values

    auc: float

    # Selected thresholds
    youden_threshold: float   # Youden's J  (maximises TPR − FPR)
    youden_tpr: float
    youden_fpr: float

    f1_max_threshold: float
    f1_max_value: float

    recall_max_threshold: float  # smallest LLM score seen = catches all positives


@dataclass
class ConfusionMatrix:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def accuracy(self) -> float:
        total = self.tp + self.fp + self.fn + self.tn
        return (self.tp + self.tn) / total if total else 0.0


@dataclass
class LangCalibration:
    """Calibration result for one language."""

    language: str
    n_llm: int
    n_human: int

    # Raw scores
    llm_lr: list[float]    # log_rank scores for LLM functions (lower = more LLM-like)
    human_lr: list[float]  # log_rank scores for human functions

    llm_ds: list[float] = field(default_factory=list)    # NPR detect_scores (if computed)
    human_ds: list[float] = field(default_factory=list)

    # ROC results
    lr_roc: ROCResult | None = None     # log_rank ROC
    ds_roc: ROCResult | None = None     # detect_score ROC

    # Recommended thresholds (populated after ROC computation)
    recommended_lr_low: float = 1.5    # below this → LLM  (Youden)
    recommended_lr_high: float = 3.0   # above this → Human
    recommended_ds: float = 0.1        # NPR threshold (Youden if ds_roc exists)


@dataclass
class CalibrationReport:
    """Full calibration report across all languages."""

    generated_at: str
    samples_dir: str
    model_id: str
    results: list[LangCalibration] = field(default_factory=list)


# ---------------------------------------------------------------------------
# ROC helpers (no sklearn)
# ---------------------------------------------------------------------------


def _compute_roc(
    pos_scores: Sequence[float],   # positive-class scores (higher = more positive)
    neg_scores: Sequence[float],
) -> ROCResult:
    """Compute ROC curve for a binary score.

    pos_scores: scores where higher value = predicted positive.
    neg_scores: scores where lower value = predicted negative.

    For log_rank:  negate before calling  (-log_rank → higher = more LLM-like).
    For NPR score: pass as-is            (higher = more LLM-like).
    """
    n_pos = len(pos_scores)
    n_neg = len(neg_scores)

    if n_pos == 0 or n_neg == 0:
        return ROCResult(
            fpr=[0.0, 1.0], tpr=[0.0, 1.0], thresholds=[math.inf, -math.inf],
            auc=0.5,
            youden_threshold=0.0, youden_tpr=0.5, youden_fpr=0.5,
            f1_max_threshold=0.0, f1_max_value=0.0,
            recall_max_threshold=min(pos_scores, default=0.0),
        )

    # Build labelled pairs, sort by score descending
    pairs = [(s, 1) for s in pos_scores] + [(s, 0) for s in neg_scores]
    pairs.sort(key=lambda x: -x[0])

    fpr_pts: list[float] = [0.0]
    tpr_pts: list[float] = [0.0]
    thresh_pts: list[float] = [math.inf]

    tp = fp = 0
    prev_score: float | None = None

    for score, label in pairs:
        # Emit a point whenever the score changes
        if prev_score is not None and score != prev_score:
            fpr_pts.append(fp / n_neg)
            tpr_pts.append(tp / n_pos)
            thresh_pts.append(score)
        if label == 1:
            tp += 1
        else:
            fp += 1
        prev_score = score

    fpr_pts.append(fp / n_neg)
    tpr_pts.append(tp / n_pos)
    thresh_pts.append(-math.inf)

    # AUC via trapezoid rule
    auc_val = sum(
        abs(fpr_pts[i] - fpr_pts[i - 1]) * (tpr_pts[i] + tpr_pts[i - 1]) / 2
        for i in range(1, len(fpr_pts))
    )

    # Youden's J = TPR − FPR
    j_scores = [t - f for t, f in zip(tpr_pts, fpr_pts)]
    best_j_idx = max(range(len(j_scores)), key=lambda i: j_scores[i])

    # F1-max: iterate candidate thresholds
    pos_set = list(pos_scores)
    neg_set = list(neg_scores)
    best_f1 = 0.0
    best_f1_thresh = thresh_pts[best_j_idx]
    for thresh in thresh_pts[1:]:  # skip +inf
        tp_t = sum(1 for s in pos_set if s >= thresh)
        fp_t = sum(1 for s in neg_set if s >= thresh)
        fn_t = n_pos - tp_t
        p = tp_t / (tp_t + fp_t) if (tp_t + fp_t) else 0.0
        r = tp_t / (tp_t + fn_t) if (tp_t + fn_t) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        if f1 > best_f1:
            best_f1, best_f1_thresh = f1, thresh

    # Recall-max: lowest threshold (catches all positives)
    recall_max_thresh = min(pos_set, default=0.0)

    return ROCResult(
        fpr=fpr_pts,
        tpr=tpr_pts,
        thresholds=thresh_pts,
        auc=auc_val,
        youden_threshold=thresh_pts[best_j_idx],
        youden_tpr=tpr_pts[best_j_idx],
        youden_fpr=fpr_pts[best_j_idx],
        f1_max_threshold=best_f1_thresh,
        f1_max_value=best_f1,
        recall_max_threshold=recall_max_thresh,
    )


def _confusion_at_threshold(
    pos_scores: list[float],
    neg_scores: list[float],
    threshold: float,
) -> ConfusionMatrix:
    """Compute confusion matrix at a given threshold (higher score → positive)."""
    tp = sum(1 for s in pos_scores if s >= threshold)
    fp = sum(1 for s in neg_scores if s >= threshold)
    fn = len(pos_scores) - tp
    tn = len(neg_scores) - fp
    return ConfusionMatrix(tp=tp, fp=fp, fn=fn, tn=tn)


def _compute_lr_roc(llm_lr: list[float], human_lr: list[float]) -> ROCResult:
    """Compute ROC for log_rank.

    Negate scores so that lower log_rank (= more LLM-like) maps to higher
    positive-class score for the standard ROC formulation.
    """
    return _compute_roc(
        pos_scores=[-s for s in llm_lr],
        neg_scores=[-s for s in human_lr],
    )


def _compute_ds_roc(llm_ds: list[float], human_ds: list[float]) -> ROCResult:
    """Compute ROC for NPR detect_score (higher = more LLM-like, no negation needed)."""
    return _compute_roc(pos_scores=llm_ds, neg_scores=human_ds)


def _lr_thresholds_from_roc(roc: ROCResult) -> tuple[float, float]:
    """Convert ROC thresholds (negated log_rank space) back to original scale.

    Returns (lr_low, lr_high):
      lr_low  = Youden's-J threshold: log_rank below this → LLM
      lr_high = F1-max threshold + gap: log_rank above this → Human
    """
    # Negate back: negated_threshold → -negated_threshold = original log_rank
    lr_low = -roc.youden_threshold
    # For the human threshold, use f1_max_threshold with a small upward margin
    lr_high = max(-roc.f1_max_threshold, lr_low + 0.5)
    return round(lr_low, 3), round(lr_high, 3)


# ---------------------------------------------------------------------------
# Calibrator
# ---------------------------------------------------------------------------


class Calibrator:
    """Estimates optimal detection thresholds from labelled sample directories.

    Args:
        provider:            LogRankProvider for computing log_rank scores.
        detector:            Optional DetectCodeGPT for NPR score calibration.
        max_per_class:       Cap on functions per (language, class) to keep
                             computation time manageable.
        compute_detect_score: Whether to run the expensive NPR calibration.
    """

    def __init__(
        self,
        provider: LogRankProvider,
        detector: DetectCodeGPT | None = None,
        max_per_class: int = 200,
        compute_detect_score: bool = False,
    ) -> None:
        self.provider = provider
        self.detector = detector
        self.max_per_class = max_per_class
        self.compute_detect_score = compute_detect_score
        self._extractor = FunctionExtractor()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        samples_dir: Path,
        model_id: str = "unknown",
        languages: list[str] | None = None,
    ) -> CalibrationReport:
        """Extract functions, score them, and compute ROC curves per language."""
        llm_dir   = samples_dir / "llm"
        human_dir = samples_dir / "human"

        if not llm_dir.exists() or not human_dir.exists():
            raise ValueError(
                f"Expected {llm_dir} and {human_dir} subdirectories."
            )

        # Extract functions
        llm_blocks   = list(self._extractor.extract_from_repo(llm_dir))
        human_blocks = list(self._extractor.extract_from_repo(human_dir))

        # Group by language
        def by_lang(blocks):
            d: dict[str, list] = {}
            for b in blocks:
                d.setdefault(b.language.value, []).append(b)
            return d

        llm_by_lang   = by_lang(llm_blocks)
        human_by_lang = by_lang(human_blocks)

        target_langs = set(llm_by_lang) & set(human_by_lang)
        if languages:
            target_langs &= set(languages)

        results: list[LangCalibration] = []

        for lang in sorted(target_langs):
            llm_fns   = llm_by_lang[lang][: self.max_per_class]
            human_fns = human_by_lang[lang][: self.max_per_class]

            # Batch log_rank computation
            all_codes  = [b.source for b in llm_fns] + [b.source for b in human_fns]
            all_scores = self.provider.compute_log_rank_batch(all_codes)

            llm_lr   = all_scores[: len(llm_fns)]
            human_lr = all_scores[len(llm_fns):]

            cal = LangCalibration(
                language=lang,
                n_llm=len(llm_fns),
                n_human=len(human_fns),
                llm_lr=llm_lr,
                human_lr=human_lr,
            )

            # Compute log_rank ROC
            cal.lr_roc = _compute_lr_roc(llm_lr, human_lr)
            lr_low, lr_high = _lr_thresholds_from_roc(cal.lr_roc)
            cal.recommended_lr_low  = lr_low
            cal.recommended_lr_high = lr_high

            # Optional detect_score calibration
            if self.compute_detect_score and self.detector:
                llm_ds, human_ds = [], []
                for b in llm_fns:
                    try:
                        r = self.detector.detect(b.source, language=b.language.value)
                        llm_ds.append(r.score)
                    except Exception:
                        pass
                for b in human_fns:
                    try:
                        r = self.detector.detect(b.source, language=b.language.value)
                        human_ds.append(r.score)
                    except Exception:
                        pass
                if llm_ds and human_ds:
                    cal.llm_ds   = llm_ds
                    cal.human_ds = human_ds
                    cal.ds_roc   = _compute_ds_roc(llm_ds, human_ds)
                    cal.recommended_ds = round(cal.ds_roc.youden_threshold, 4)

            results.append(cal)

        return CalibrationReport(
            generated_at=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            samples_dir=str(samples_dir),
            model_id=model_id,
            results=results,
        )

    def save_thresholds(
        self,
        report: CalibrationReport,
        output: Path,
    ) -> None:
        """Save per-language thresholds to a YAML file."""
        output.parent.mkdir(parents=True, exist_ok=True)

        data: dict = {
            "_generated_at": report.generated_at,
            "_model_id":     report.model_id,
            "_samples_dir":  report.samples_dir,
        }

        # Global thresholds: mean across languages
        if report.results:
            data["global"] = {
                "threshold_log_rank_low":  round(
                    sum(r.recommended_lr_low  for r in report.results) / len(report.results), 3
                ),
                "threshold_log_rank_high": round(
                    sum(r.recommended_lr_high for r in report.results) / len(report.results), 3
                ),
                "threshold_detect_score":  round(
                    sum(r.recommended_ds for r in report.results) / len(report.results), 4
                ),
            }

        # Per-language thresholds
        data["per_language"] = {
            r.language: {
                "threshold_log_rank_low":  r.recommended_lr_low,
                "threshold_log_rank_high": r.recommended_lr_high,
                "threshold_detect_score":  r.recommended_ds,
                "auc_log_rank": round(r.lr_roc.auc, 4) if r.lr_roc else None,
                "auc_detect_score": round(r.ds_roc.auc, 4) if r.ds_roc else None,
                "n_llm":   r.n_llm,
                "n_human": r.n_human,
            }
            for r in report.results
        }

        output.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False), encoding="utf-8")

    def render_html(
        self,
        report: CalibrationReport,
        output: Path,
    ) -> None:
        """Render an interactive HTML calibration report using Plotly."""
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        output.parent.mkdir(parents=True, exist_ok=True)

        # Build one figure per language
        plot_divs: list[dict] = []

        for cal in report.results:
            lang = cal.language

            # ---- ROC subplot ----
            fig = make_subplots(
                rows=1, cols=2,
                subplot_titles=(
                    f"ROC — Log-Rank  (AUC={cal.lr_roc.auc:.3f})" if cal.lr_roc else "ROC — Log-Rank",
                    "ROC — NPR Detect Score" if cal.ds_roc else "Score Distributions",
                ),
            )

            # Log-rank ROC
            if cal.lr_roc:
                fig.add_trace(go.Scatter(
                    x=cal.lr_roc.fpr, y=cal.lr_roc.tpr,
                    name=f"LR ROC (AUC={cal.lr_roc.auc:.3f})",
                    line=dict(color="#3b82f6", width=2),
                    hovertemplate="FPR=%{x:.3f}<br>TPR=%{y:.3f}<extra></extra>",
                ), row=1, col=1)
                # Youden's J point
                fig.add_trace(go.Scatter(
                    x=[cal.lr_roc.youden_fpr], y=[cal.lr_roc.youden_tpr],
                    name=f"Youden T={cal.recommended_lr_low:.3f}",
                    mode="markers",
                    marker=dict(color="#ef4444", size=10, symbol="star"),
                ), row=1, col=1)
                # Diagonal reference
                fig.add_trace(go.Scatter(
                    x=[0, 1], y=[0, 1], name="Random",
                    line=dict(color="gray", dash="dash", width=1),
                    showlegend=False,
                ), row=1, col=1)

            # Score distribution OR detect_score ROC
            if cal.ds_roc and cal.llm_ds and cal.human_ds:
                fig.add_trace(go.Scatter(
                    x=cal.ds_roc.fpr, y=cal.ds_roc.tpr,
                    name=f"DS ROC (AUC={cal.ds_roc.auc:.3f})",
                    line=dict(color="#8b5cf6", width=2),
                ), row=1, col=2)
                fig.add_trace(go.Scatter(
                    x=[0, 1], y=[0, 1], name="Random",
                    line=dict(color="gray", dash="dash", width=1), showlegend=False,
                ), row=1, col=2)
            else:
                # Score distributions (histogram)
                fig.add_trace(go.Histogram(
                    x=cal.llm_lr, name="LLM log_rank",
                    opacity=0.6, marker_color="#ef4444",
                    nbinsx=20, histnorm="probability",
                ), row=1, col=2)
                fig.add_trace(go.Histogram(
                    x=cal.human_lr, name="Human log_rank",
                    opacity=0.6, marker_color="#22c55e",
                    nbinsx=20, histnorm="probability",
                ), row=1, col=2)
                # Threshold lines
                if cal.lr_roc:
                    for t, label, colour in [
                        (cal.recommended_lr_low,  "Youden LLM", "#ef4444"),
                        (cal.recommended_lr_high, "Youden Human", "#22c55e"),
                    ]:
                        fig.add_vline(
                            x=t, line_dash="dash", line_color=colour,
                            annotation_text=label, row=1, col=2,
                        )

            fig.update_layout(
                title_text=f"Calibration: {lang.upper()}  "
                           f"({cal.n_llm} LLM, {cal.n_human} human)",
                height=380, barmode="overlay",
                plot_bgcolor="#f8fafc", paper_bgcolor="#ffffff",
                font=dict(family="system-ui, sans-serif", size=12),
                legend=dict(orientation="h", yanchor="bottom", y=-0.25),
            )
            fig.update_xaxes(title_text="FPR / score", gridcolor="#e2e8f0", row=1, col=1)
            fig.update_yaxes(title_text="TPR", gridcolor="#e2e8f0", row=1, col=1)

            div_html = fig.to_html(
                full_html=False,
                include_plotlyjs=False,   # we embed once via CDN in the template
                config={"responsive": True},
            )

            # Confusion matrix at each threshold
            cm_youden = cm_f1 = cm_recall = None
            if cal.lr_roc and cal.llm_lr and cal.human_lr:
                pos = [-s for s in cal.llm_lr]
                neg = [-s for s in cal.human_lr]
                cm_youden = _confusion_at_threshold(pos, neg, cal.lr_roc.youden_threshold)
                cm_f1     = _confusion_at_threshold(pos, neg, cal.lr_roc.f1_max_threshold)
                cm_recall = _confusion_at_threshold(pos, neg, -cal.lr_roc.recall_max_threshold)

            plot_divs.append({
                "language":    lang,
                "n_llm":       cal.n_llm,
                "n_human":     cal.n_human,
                "auc_lr":      f"{cal.lr_roc.auc:.4f}" if cal.lr_roc else "N/A",
                "auc_ds":      f"{cal.ds_roc.auc:.4f}" if cal.ds_roc else "N/A",
                "lr_low":      cal.recommended_lr_low,
                "lr_high":     cal.recommended_lr_high,
                "ds_thresh":   cal.recommended_ds,
                "div":         div_html,
                "cm_youden":   cm_youden,
                "cm_f1":       cm_f1,
                "cm_recall":   cm_recall,
                "lr_roc":      cal.lr_roc,
            })

        # Render Jinja2 template
        env = Environment(
            loader=PackageLoader("ai_code_detector", "templates"),
            autoescape=select_autoescape(["html"]),
        )
        tmpl = env.get_template("calibration.html.jinja2")
        html = tmpl.render(
            report=report,
            plots=plot_divs,
        )
        output.write_text(html, encoding="utf-8")
