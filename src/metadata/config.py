"""metadata 模块配置（config/settings.yaml 的 metadata 段）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

DEFAULT_SETTINGS_PATH = "config/settings.yaml"


class MetadataSettings(BaseModel):
    string_generic_repo_threshold: int = 5
    baseline_sim_threshold: float = 0.85
    # 双侧同基线（query 与 candidate 命中同一基线函数）下限；双侧信号远强于单侧，故低于 baseline_sim_threshold
    baseline_bilateral_threshold: float = 0.75
    # 公共/框架代码广度过滤：一个 query 函数高相似(>=sim)命中 >=repo 个不同历史仓库 → 判公共代码
    common_code_repo_threshold: int = 5
    common_code_sim_threshold: float = 0.9
    contest_start: str = "2024-05-01"
    early_impl_days: int = 3
    early_impl_min_lines: int = 30
    large_commit_lines: int = 2000
    vague_message_patterns: list[str] = Field(
        default_factory=lambda: [r"^\s*init", r"^\s*add\s+files?", r"^\s*update"]
    )


@lru_cache(maxsize=4)
def load_metadata_settings(path: str | None = None) -> MetadataSettings:
    p = Path(path or DEFAULT_SETTINGS_PATH)
    data = {}
    if p.exists():
        data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("metadata", {}) or {}
    return MetadataSettings.model_validate(data)
