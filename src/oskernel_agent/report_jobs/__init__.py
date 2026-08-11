"""Report orchestration — single entry point for all four deliverable kinds."""

from __future__ import annotations

from ._runner import run, ReportKind, KindResult, JobResult

__all__ = ["run", "ReportKind", "KindResult", "JobResult"]
