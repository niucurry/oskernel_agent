"""
Pydantic data models shared across all modules.

Defines the canonical data structures that flow through the pipeline:
  SourceFile → FunctionChunk → ScoredFunction → FileResult → RepoReport
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field


class Language(str, Enum):
    PYTHON = "python"
    JAVA = "java"
    GO = "go"
    C = "c"
    CPP = "cpp"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    RUST = "rust"


class Label(str, Enum):
    HUMAN = "HUMAN"
    AI_SUSPECTED = "AI_SUSPECTED"
    SKIPPED = "SKIPPED"  # too short / parse error


class FunctionChunk(BaseModel):
    """A single extracted function/method from a source file."""

    name: str
    qualified_name: str = ""  # e.g. "ClassName.method_name"
    source: str
    start_line: int
    end_line: int
    loc: int = 0  # effective lines (non-blank, non-comment)
    language: Language
    file_path: Path


# Public alias used by extractor's external API
FunctionBlock = FunctionChunk


class ScoredFunction(BaseModel):
    """FunctionChunk annotated with detection scores."""

    chunk: FunctionChunk
    token_count: int = 0

    # Stage 1
    log_rank_score: float | None = None  # mean log-rank; lower → more AI-like
    passed_coarse_filter: bool = False

    # Stage 2
    log_prob: float | None = None
    perturbation_log_probs: list[float] = Field(default_factory=list)
    detect_score: float | None = None  # (log_p - mean_perturb) / std_perturb

    label: Label = Label.SKIPPED
    confidence: float = 0.0  # in [0, 1]


class FileResult(BaseModel):
    """Aggregated detection result for one source file."""

    path: Path
    language: Language
    total_functions: int = 0
    analysed_functions: int = 0
    ai_suspected: int = 0
    suspicion_rate: float = 0.0
    functions: list[ScoredFunction] = Field(default_factory=list)


class RepoSummary(BaseModel):
    total_files: int = 0
    total_functions: int = 0
    analysed_functions: int = 0
    ai_suspected: int = 0
    suspicion_rate: float = 0.0


class RepoReport(BaseModel):
    """Top-level report for an entire repository scan."""

    repo: Path
    scanned_at: str  # ISO-8601
    model_id: str
    config: dict = Field(default_factory=dict)
    summary: RepoSummary = Field(default_factory=RepoSummary)
    files: list[FileResult] = Field(default_factory=list)
