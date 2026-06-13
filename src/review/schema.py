"""LLM 复核输出的 JSON schema 与解析。"""

from __future__ import annotations

import json
import re
from typing import Literal, Union

from pydantic import BaseModel, Field, field_validator

VERDICTS = ("high_similarity", "likely_clone", "common_pattern", "false_positive")
CLONE_TYPES = ("exact", "renamed", "restructured", "algorithm_only", "none")

# 聚合阶段可能出现的额外状态（非 LLM 直接输出）
AGG_DISPUTED = "disputed"
AGG_PARSE_ERROR = "parse_error"


class EvidenceItem(BaseModel):
    new_lines: Union[str, list[int], int] = Field(..., description="新作品侧涉及行号")
    old_lines: Union[str, list[int], int] = Field(..., description="历史作品侧涉及行号")
    observation: str = Field(..., description="该证据的具体说明")


class Verdict(BaseModel):
    verdict: Literal[VERDICTS]  # type: ignore[valid-type]
    clone_type: Literal[CLONE_TYPES]  # type: ignore[valid-type]
    confidence: float = Field(..., ge=0.0, le=1.0)
    reasoning: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    could_be_coincidence: str = ""
    recommendation_for_reviewer: str = ""

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp(cls, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, v))


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_text(text: str) -> str:
    """从 LLM 文本里抠出 JSON：优先取 ```json``` 代码块，否则取首个 {...} 块。"""
    m = _FENCE.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text.strip()


def parse_verdict(text: str) -> Verdict:
    """解析 LLM 文本为 Verdict；失败抛 ValueError。"""
    raw = _extract_json_text(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 顶层不是对象")
    return Verdict.model_validate(data)
