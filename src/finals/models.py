"""四份决赛报告之间共享的精简事实结构。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator

ReportKind = Literal["summary", "description", "development", "comparison"]
Severity = Literal["info", "low", "medium", "high", "critical"]


class EvidenceRef(BaseModel):
    """可以下钻核对的一条证据。"""

    path: str = ""
    line: int | None = Field(default=None, ge=1)
    excerpt: str = Field(default="", max_length=500)
    url: str = ""


class Finding(BaseModel):
    """面向评委的问题或重要判断。"""

    title: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=360)
    severity: Severity = "info"
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source: ReportKind
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=6)

    @field_validator("title", "detail")
    @classmethod
    def _single_line(cls, value: str) -> str:
        return " ".join(str(value).split())


class ModuleDigest(BaseModel):
    """模块级的一眼可读结论；详细证据仍留在 HTML 报告。"""

    name: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=300)
    similarity_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    evidence_count: int = Field(default=0, ge=0)


class ReportDigest(BaseModel):
    """每份 HTML/PDF 都能写出或消费的统一摘要。"""

    schema_version: int = 1
    repo_id: str = Field(min_length=1, max_length=240)
    kind: ReportKind
    conclusion: str = Field(min_length=1, max_length=240)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    # 描述报告不得因为版面配额静默丢失严重问题或一级子系统。
    findings: list[Finding] = Field(default_factory=list, max_length=64)
    modules: list[ModuleDigest] = Field(default_factory=list, max_length=64)
    metrics: dict[str, int | float | str | bool | None] = Field(default_factory=dict)
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def decision_findings(self, limit: int = 5) -> list[Finding]:
        """按严重度和置信度选出摘要页真正需要的判断。"""
        rank = {"critical": 5, "high": 4, "medium": 3, "low": 2, "info": 1}
        return sorted(
            self.findings,
            key=lambda item: (-rank[item.severity], -item.confidence, item.title),
        )[: max(0, limit)]
