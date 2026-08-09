"""
Two-stage function-level detection pipeline.

Stage 1 (fast filter):
    Compute log-rank under the reference model.
    - log_rank < LR_LLM   -> label LLM  (high confidence, skip stage 2)
    - log_rank > LR_HUMAN -> label Human (high confidence, skip stage 2)
    - otherwise           -> gray zone, proceed to stage 2

Stage 2 (precise):
    Run DetectCodeGPT NPR scoring.
    - score > NPR_THRESHOLD -> LLM
    - score <= NPR_THRESHOLD -> Human

Language routing (paper RQ1.1):
    C / C++ / Java / Go  -- reliable; no penalty
    Python               -- known poor accuracy; confidence -0.2
    JavaScript / TypeScript -- not tested in paper; treated same as Python

LOC filtering (paper RQ1.3):
    LOC < 20             -> Uncertain (skip detection entirely)
    20 <= LOC < 50        -> detect, but confidence -0.2
    LOC >= 50             -> normal

All thresholds are configurable via AI_DETECT_* environment variables.

Public API:
    pipeline = DetectionPipeline(provider, detector)
    result  = pipeline.process_function(block)        # single
    results = pipeline.process_batch(blocks)           # batch (efficient)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from .detector import DetectCodeGPT, DetectionResult, LogRankProvider
from .models import FunctionBlock, Language

# ---------------------------------------------------------------------------
# Language classification (paper RQ1.1)
# ---------------------------------------------------------------------------

# Low detection reliability per paper, or not tested
_LOW_CONFIDENCE_LANGS: frozenset[Language] = frozenset({
    Language.PYTHON,
    Language.JAVASCRIPT,
    Language.TYPESCRIPT,
})

_LANG_PENALTY_REASON: dict[Language, str] = {
    Language.PYTHON: "detection accuracy known to be lower (paper RQ1.1)",
    Language.JAVASCRIPT: "not benchmarked in paper -- treated conservatively",
    Language.TYPESCRIPT: "not benchmarked in paper -- treated conservatively",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class ThresholdConfig(BaseModel):
    """All tuneable thresholds.  Every field has an AI_DETECT_* env-var override."""

    # Stage 1 log-rank thresholds
    log_rank_llm_threshold: float = 1.5     # below -> LLM
    log_rank_human_threshold: float = 3.0   # above -> Human

    # Stage 2 NPR threshold
    detect_score_threshold: float = 0.1     # above -> LLM

    # LOC gates
    loc_uncertain_below: int = 20           # < this -> Uncertain
    loc_penalty_below: int = 50             # < this (and >= uncertain) -> -penalty

    # Confidence penalties
    loc_short_penalty: float = 0.2
    lang_poor_penalty: float = 0.2

    @classmethod
    def from_env(cls) -> "ThresholdConfig":
        """Build config using defaults, overriding any field set via AI_DETECT_*."""
        _MAP: dict[str, tuple[str, type]] = {
            "AI_DETECT_LR_LLM":            ("log_rank_llm_threshold",   float),
            "AI_DETECT_LR_HUMAN":          ("log_rank_human_threshold",  float),
            "AI_DETECT_NPR_THRESHOLD":     ("detect_score_threshold",    float),
            "AI_DETECT_LOC_UNCERTAIN":     ("loc_uncertain_below",       int),
            "AI_DETECT_LOC_PENALTY_BELOW": ("loc_penalty_below",         int),
            "AI_DETECT_LOC_SHORT_PEN":     ("loc_short_penalty",         float),
            "AI_DETECT_LANG_POOR_PEN":     ("lang_poor_penalty",         float),
        }
        overrides: dict = {}
        for env_var, (field, cast) in _MAP.items():
            raw = os.getenv(env_var)
            if raw is not None:
                try:
                    overrides[field] = cast(raw)
                except (ValueError, TypeError):
                    pass  # bad value -- keep default
        return cls(**overrides)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

class PipelineResult(BaseModel):
    """Detection outcome for one function."""

    function_block: FunctionBlock
    label: Literal["LLM", "Human", "Uncertain"]
    confidence: float = Field(ge=0.0, le=1.0)
    stage: Literal["fast_filter", "detect_code_gpt", "skipped"]
    log_rank: float | None = None
    detect_score: float | None = None
    reasons: list[str] = Field(default_factory=list)
    low_confidence: bool = False  # set when confidence dropped significantly


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class DetectionPipeline:
    """Two-stage LLM-generated code detection pipeline.

    Args:
        provider:  Object satisfying LogRankProvider (e.g. PerplexityCalculator).
        detector:  A DetectCodeGPT instance for gray-zone stage 2 decisions.
        cfg:       ThresholdConfig -- defaults to ThresholdConfig.from_env().
    """

    def __init__(
        self,
        provider: LogRankProvider,
        detector: DetectCodeGPT,
        cfg: ThresholdConfig | None = None,
    ) -> None:
        self.provider = provider
        self.detector = detector
        self.cfg = cfg if cfg is not None else ThresholdConfig.from_env()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_function(self, block: FunctionBlock) -> PipelineResult:
        """Run the full two-stage pipeline on a single FunctionBlock."""
        skip = self._check_loc(block)
        if skip is not None:
            return skip

        try:
            log_rank = self.provider.compute_log_rank(block.source)
        except Exception as exc:
            return _error_result(block, f"log_rank computation failed: {exc!r}")

        stage1 = self._apply_stage1(block, log_rank)
        if stage1 is not None:
            return stage1

        try:
            detect_result = self.detector.detect(
                block.source, language=block.language.value
            )
        except Exception as exc:
            return _error_result(
                block, f"DetectCodeGPT failed: {exc!r}", log_rank=log_rank
            )

        return self._make_stage2_result(block, log_rank, detect_result)

    def process_batch(
        self,
        blocks: list[FunctionBlock],
        show_progress: bool = True,
    ) -> list[PipelineResult]:
        """Process a list of FunctionBlocks using batched log-rank computation.

        Stage 1 log-ranks are computed in ONE provider.compute_log_rank_batch()
        call for all eligible blocks.  Stage 2 DetectCodeGPT runs sequentially
        for each gray-zone function (each internally uses batched perturbations).
        """
        if not blocks:
            return []

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            disable=not show_progress,
        ) as progress:
            return self._run_batch(blocks, progress)

    # ------------------------------------------------------------------
    # Core batch logic
    # ------------------------------------------------------------------

    def _run_batch(
        self,
        blocks: list[FunctionBlock],
        progress: Progress,
    ) -> list[PipelineResult]:
        ordered: dict[int, PipelineResult] = {}

        # ---- LOC pre-filter ----
        eligible_idx: list[int] = []
        for i, block in enumerate(blocks):
            skip = self._check_loc(block)
            if skip is not None:
                ordered[i] = skip
            else:
                eligible_idx.append(i)

        if not eligible_idx:
            return [ordered[i] for i in range(len(blocks))]

        eligible = [blocks[i] for i in eligible_idx]

        # ---- Stage 1: one batched log-rank call ----
        task_s1 = progress.add_task(
            "[cyan]Stage 1 -- Log-Rank", total=len(eligible)
        )
        try:
            log_ranks = self.provider.compute_log_rank_batch(
                [b.source for b in eligible]
            )
        except Exception as exc:
            progress.update(task_s1, completed=len(eligible))
            for i, block in zip(eligible_idx, eligible):
                ordered[i] = _error_result(block, f"batch log_rank failed: {exc!r}")
            return [ordered[i] for i in range(len(blocks))]
        progress.update(task_s1, completed=len(eligible))

        # ---- Route: fast-filter vs gray zone ----
        gray_idx:   list[int]           = []
        gray_blocks: list[FunctionBlock] = []
        gray_lr:    list[float]         = []

        for orig_i, block, lr in zip(eligible_idx, eligible, log_ranks):
            stage1 = self._apply_stage1(block, lr)
            if stage1 is not None:
                ordered[orig_i] = stage1
            else:
                gray_idx.append(orig_i)
                gray_blocks.append(block)
                gray_lr.append(lr)

        # ---- Stage 2: DetectCodeGPT for gray-zone ----
        if gray_blocks:
            task_s2 = progress.add_task(
                "[yellow]Stage 2 -- DetectCodeGPT", total=len(gray_blocks)
            )
            for orig_i, block, lr in zip(gray_idx, gray_blocks, gray_lr):
                try:
                    dr = self.detector.detect(block.source, language=block.language.value)
                    ordered[orig_i] = self._make_stage2_result(block, lr, dr)
                except Exception as exc:
                    ordered[orig_i] = _error_result(
                        block, f"DetectCodeGPT failed: {exc!r}", log_rank=lr
                    )
                progress.advance(task_s2)

        return [ordered[i] for i in range(len(blocks))]

    # ------------------------------------------------------------------
    # Decision helpers
    # ------------------------------------------------------------------

    def _check_loc(self, block: FunctionBlock) -> PipelineResult | None:
        """Return an Uncertain skip result if LOC is below the absolute minimum."""
        if block.loc < self.cfg.loc_uncertain_below:
            return PipelineResult(
                function_block=block,
                label="Uncertain",
                confidence=0.0,
                stage="skipped",
                reasons=[
                    f"LOC={block.loc} < {self.cfg.loc_uncertain_below}: "
                    f"insufficient signal for reliable detection"
                ],
                low_confidence=True,
            )
        return None

    def _apply_stage1(
        self, block: FunctionBlock, log_rank: float
    ) -> PipelineResult | None:
        """Apply stage 1 thresholds.

        Returns a decided PipelineResult when the signal is unambiguous,
        or None when the function falls in the gray zone.
        """
        cfg = self.cfg

        if log_rank < cfg.log_rank_llm_threshold:
            confidence, reasons = self._adjustments(block, base=0.7)
            reasons.insert(
                0,
                f"Stage 1 fast-filter: log_rank={log_rank:.3f} < "
                f"{cfg.log_rank_llm_threshold} -> LLM",
            )
            return _make_result(
                "LLM", "fast_filter", block, confidence, reasons, log_rank=log_rank
            )

        if log_rank > cfg.log_rank_human_threshold:
            confidence, reasons = self._adjustments(block, base=0.7)
            reasons.insert(
                0,
                f"Stage 1 fast-filter: log_rank={log_rank:.3f} > "
                f"{cfg.log_rank_human_threshold} -> Human",
            )
            return _make_result(
                "Human", "fast_filter", block, confidence, reasons, log_rank=log_rank
            )

        return None  # gray zone [1.5, 3.0]

    def _make_stage2_result(
        self,
        block: FunctionBlock,
        log_rank: float,
        detect_result: DetectionResult,
    ) -> PipelineResult:
        cfg = self.cfg
        label: Literal["LLM", "Human", "Uncertain"] = (
            "LLM" if detect_result.is_llm_generated else "Human"
        )
        confidence, reasons = self._adjustments(block, base=detect_result.confidence)
        sign = ">" if detect_result.is_llm_generated else "<="
        reasons.insert(
            0,
            f"Stage 1: log_rank={log_rank:.3f} in gray zone "
            f"[{cfg.log_rank_llm_threshold}, {cfg.log_rank_human_threshold}]",
        )
        reasons.insert(
            1,
            f"Stage 2 DetectCodeGPT: score={detect_result.score:.4f} "
            f"{sign} {cfg.detect_score_threshold} -> {label}",
        )
        return _make_result(
            label, "detect_code_gpt", block, confidence, reasons,
            log_rank=log_rank, detect_score=detect_result.score,
        )

    def _adjustments(
        self, block: FunctionBlock, base: float
    ) -> tuple[float, list[str]]:
        """Return (adjusted_confidence, reason_list) after LOC and language penalties."""
        confidence = base
        reasons: list[str] = []
        cfg = self.cfg

        # LOC penalty
        if cfg.loc_uncertain_below <= block.loc < cfg.loc_penalty_below:
            confidence -= cfg.loc_short_penalty
            reasons.append(
                f"Short function (LOC={block.loc}, "
                f"{cfg.loc_uncertain_below}-{cfg.loc_penalty_below - 1}): "
                f"confidence -{cfg.loc_short_penalty}"
            )

        # Language penalty
        if block.language in _LOW_CONFIDENCE_LANGS:
            confidence -= cfg.lang_poor_penalty
            reasons.append(
                f"Language={block.language.value}: "
                f"{_LANG_PENALTY_REASON[block.language]} "
                f"-> confidence -{cfg.lang_poor_penalty}"
            )

        return max(0.0, min(1.0, confidence)), reasons


# ---------------------------------------------------------------------------
# Helpers (module-level to avoid self noise in type checkers)
# ---------------------------------------------------------------------------

def _make_result(
    label: Literal["LLM", "Human", "Uncertain"],
    stage: Literal["fast_filter", "detect_code_gpt", "skipped"],
    block: FunctionBlock,
    confidence: float,
    reasons: list[str],
    log_rank: float | None = None,
    detect_score: float | None = None,
) -> PipelineResult:
    return PipelineResult(
        function_block=block,
        label=label,
        confidence=confidence,
        stage=stage,
        log_rank=log_rank,
        detect_score=detect_score,
        reasons=reasons,
        low_confidence=confidence < 0.5,
    )


def _error_result(
    block: FunctionBlock,
    msg: str,
    log_rank: float | None = None,
) -> PipelineResult:
    return PipelineResult(
        function_block=block,
        label="Uncertain",
        confidence=0.0,
        stage="skipped",
        log_rank=log_rank,
        reasons=[f"[ERROR] {msg}"],
        low_confidence=True,
    )


# ---------------------------------------------------------------------------
# Legacy high-level runner stub (full repo scan -- implemented separately)
# ---------------------------------------------------------------------------

class PipelineConfig(BaseModel):
    """Config for the full repository scan runner (see PipelineRunner)."""

    repo: Path
    model_id: str = "codellama/CodeLlama-7b-hf"
    device: str = "auto"
    model_cache_dir: Path | None = None
    min_tokens: int = 50
    max_file_bytes: int = 512 * 1024
    workers: int = 1
    output: Path | None = None
    format: str = "html"
    threshold_config: ThresholdConfig = Field(default_factory=ThresholdConfig)


class PipelineRunner:
    """High-level orchestrator: extract -> detect -> aggregate -> report.

    Stub -- implementation deferred until PerplexityCalculator is complete.
    """

    def __init__(self, config: PipelineConfig) -> None:
        self.config = config

    def run(self):
        raise NotImplementedError("PipelineRunner.run() not yet implemented")
