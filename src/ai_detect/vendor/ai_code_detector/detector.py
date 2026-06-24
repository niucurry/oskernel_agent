"""
DetectCodeGPT algorithm: NPR-based, training-free LLM code detection.

Paper: "One Size Does Not Fit All: Investigating Efficacy of Perplexity
in Detecting LLM-Generated Code", ACM TOSEM 2026.

Public API:
    perturb_code(code, language, seed) -> str
    DetectCodeGPT(provider, k, threshold).detect(code, language) -> DetectionResult

Algorithm (eq. 8):
    LR_orig      = log_rank(code)
    LR_i         = log_rank(perturbed_i)  for i=1..k
    mean_p       = mean(LR_i)
    npr_original = mean_p / LR_orig
    npr_perturbed= mean(mean_p / LR_i)
    score        = npr_original - npr_perturbed

Positive score → code sits near a local minimum of mean log-rank (LM is very
confident) → characteristic of AI-generated text.  Threshold ≈ 0.1.
"""

from __future__ import annotations

import math
import random
import re
import statistics
from typing import Protocol, runtime_checkable

import numpy as np
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class DetectionResult(BaseModel):
    """Outcome of a single DetectCodeGPT.detect() call."""

    score: float
    original_log_rank: float
    mean_perturbed_log_rank: float
    perturbed_log_ranks: list[float] = Field(default_factory=list)
    is_llm_generated: bool
    confidence: float  # in [0, 1]; 0 = uncertain, 1 = very confident


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LogRankProvider(Protocol):
    """Interface required by DetectCodeGPT.  PerplexityCalculator must satisfy this."""

    def compute_log_rank(self, code: str) -> float:
        """Return mean token log-rank for *code*. Lower ≈ more AI-like."""
        ...

    def compute_log_rank_batch(self, codes: list[str]) -> list[float]:
        """Batch variant of compute_log_rank.  Must be faster than k individual calls."""
        ...


# ---------------------------------------------------------------------------
# Perturbation
# ---------------------------------------------------------------------------

# Comment-line prefixes per language (these lines are skipped in op-A)
_COMMENT_STARTS: dict[str, tuple[str, ...]] = {
    "python":     ("#",),
    "java":       ("//", "/*", "*"),
    "go":         ("//", "/*", "*"),
    "c":          ("//", "/*", "*"),
    "cpp":        ("//", "/*", "*"),
    "javascript": ("//", "/*", "*"),
    "typescript": ("//", "/*", "*"),
}

# Python statement-ending tokens after which a blank line must NOT be inserted
# (they open a new indented block, so the body must immediately follow)
_PYTHON_BLOCK_ENDINGS = frozenset({
    ":", ":\\",  # bare colon or colon + backslash continuation
})


def perturb_code(code: str, language: str = "python", seed: int | None = None) -> str:
    """Apply whitespace-only perturbations to *code*.

    Two stochastic operations (each applied with 50% probability per line):

    A. Interior-space insertion
       Pick a random whitespace character that is NOT part of leading indentation
       and insert one extra space next to it.
       Invariant: leading indentation is never touched → Python indentation safe.

    B. Blank-line insertion
       Insert an empty line AFTER the current line.
       Guard: skipped when bracket depth > 0 (inside a multi-line expression),
       when the line ends with '\\' (explicit continuation), or — for Python —
       when the line ends with ':' (block opener).

    Note: spaces may land inside string literals, which is a semantic change
    but never a syntax error and is acceptable for perplexity-based detection.
    """
    if not code:
        return code

    rng = random.Random(seed)
    lang = language.lower()
    comment_starts = _COMMENT_STARTS.get(lang, ("//",))

    # Normalise line endings
    code = code.replace("\r\n", "\n").replace("\r", "\n")
    lines = code.split("\n")
    result: list[str] = []
    bracket_depth = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        is_blank = not stripped
        is_comment = not is_blank and any(stripped.startswith(p) for p in comment_starts)

        # ---- Track bracket depth across the full line ----
        # Simplified: ignores brackets inside string literals.
        # Good enough for typical code; very rarely mis-tracks.
        for ch in line:
            if ch in "([{":
                bracket_depth += 1
            elif ch in ")]}":
                bracket_depth = max(0, bracket_depth - 1)

        # ---- Operation A: interior space insertion ----
        new_line = line
        if not is_blank and not is_comment and rng.random() < 0.5:
            indent_len = len(line) - len(line.lstrip())
            content = line[indent_len:]
            space_positions = [m.start() for m in re.finditer(r" ", content)]
            if space_positions:
                pos = rng.choice(space_positions)
                # Insert one extra space immediately after the chosen space
                new_line = line[:indent_len] + content[: pos + 1] + " " + content[pos + 1 :]

        result.append(new_line)

        # ---- Operation B: blank line after this line ----
        can_insert_blank = (
            bracket_depth == 0
            and not is_blank
            and not stripped.endswith("\\")
            and i < len(lines) - 1  # never after the very last line
            and rng.random() < 0.5
        )
        if can_insert_blank:
            if lang == "python" and stripped.endswith(":"):
                pass  # block opener — body must follow immediately
            else:
                result.append("")

    return "\n".join(result)


# ---------------------------------------------------------------------------
# NPR formula helpers
# ---------------------------------------------------------------------------

_EPS = 1e-8  # guard against division by zero when LR ≈ 0


def _compute_npr(
    lr_orig: float,
    lr_perturbed: list[float],
) -> tuple[float, float, float]:
    """Return (score, npr_original, npr_perturbed) per paper eq. 8.

    score = (mean_p / lr_orig)  -  mean(mean_p / lr_i)
    """
    mean_p = float(np.mean(lr_perturbed))
    npr_original = mean_p / (lr_orig + _EPS)
    npr_perturbed = float(np.mean([mean_p / (lr_i + _EPS) for lr_i in lr_perturbed]))
    return npr_original - npr_perturbed, npr_original, npr_perturbed


def _score_to_confidence(score: float, threshold: float) -> float:
    """Map NPR score distance from the decision boundary to [0, 1].

    Uses a sigmoid centered on `threshold`; returns 0 at the boundary,
    approaches 1 far from it.
    """
    distance = score - threshold
    p_ai = 1.0 / (1.0 + math.exp(-10.0 * distance))  # sigmoid, steepness=10
    return min(1.0, 2.0 * abs(p_ai - 0.5))


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------


class DetectCodeGPT:
    """NPR-based LLM-generated code detector (paper eq. 8).

    Args:
        provider:        Object satisfying LogRankProvider (e.g. PerplexityCalculator).
        k:               Total number of perturbations (default 50).
        threshold:       NPR score above which code is labelled AI-generated (default 0.1).
        batch_size:      Perturbations scored per LM forward pass.
        early_stop_std:  If running std of LR_perturbed drops below this after the
                         first batch, no more batches are issued.
    """

    def __init__(
        self,
        provider: LogRankProvider,
        k: int = 50,
        threshold: float = 0.1,
        batch_size: int = 10,
        early_stop_std: float = 0.01,
    ) -> None:
        self.provider = provider
        self.k = k
        self.threshold = threshold
        self.batch_size = batch_size
        self.early_stop_std = early_stop_std

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, code: str, language: str = "python") -> DetectionResult:
        """Run the full NPR detection pipeline on *code*.

        Returns a DetectionResult with score, log-rank values, label, and confidence.
        """
        # Step 1 — original log-rank
        lr_orig = self.provider.compute_log_rank(code)

        # Step 2 — generate k perturbations (deterministic seeds → reproducible)
        perturbations = [
            perturb_code(code, language=language, seed=i) for i in range(self.k)
        ]

        # Step 3 — batch-score with adaptive early stopping
        lr_perturbed = self._batch_score(perturbations)

        # Step 4 — NPR formula
        score, _, _ = _compute_npr(lr_orig, lr_perturbed)

        # Step 5 — label + confidence
        is_ai = score > self.threshold
        confidence = _score_to_confidence(score, self.threshold)

        return DetectionResult(
            score=score,
            original_log_rank=lr_orig,
            mean_perturbed_log_rank=float(np.mean(lr_perturbed)),
            perturbed_log_ranks=lr_perturbed,
            is_llm_generated=is_ai,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _batch_score(self, perturbations: list[str]) -> list[float]:
        """Score perturbations in batches; stop early when variance is stable.

        Processes `batch_size` samples at a time.  After the first full batch,
        checks whether std(lr_so_far) < early_stop_std.  If so, the mean has
        converged and further samples add little information.
        """
        lr_so_far: list[float] = []

        for start in range(0, len(perturbations), self.batch_size):
            batch = perturbations[start : start + self.batch_size]
            lr_so_far.extend(self.provider.compute_log_rank_batch(batch))

            # Early stopping: check after the first complete batch
            if len(lr_so_far) >= self.batch_size:
                std = float(np.std(lr_so_far))
                if std < self.early_stop_std:
                    break

        return lr_so_far
