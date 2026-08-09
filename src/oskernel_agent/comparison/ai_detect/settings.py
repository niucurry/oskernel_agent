"""加载 config/settings.yaml 的 ai_detect 段（AI 生成代码检测配置）。

所有字段都可被 AI_DETECT_* 环境变量覆盖（与上游 ThresholdConfig.from_env 对齐），便于宿主机
单独跑模型时临时调参，无需改配置文件。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_SETTINGS_PATH = "config/settings.yaml"


class AIDetectSettings(BaseModel):
    model_id: str = "bigcode/starcoder2-3b"
    device: str = "auto"
    engine: str = "transformers"          # transformers / vllm
    batch_size: int = 8
    # P2：Stage2 扰动次数是 AI 检测耗时大头，默认从 50 降到 20（精度/速度折中；
    # 需要更高精度可经 AI_DETECT_K 或 settings.yaml 调回）。
    k_perturbations: int = 20
    min_loc: int = 20
    # 默认阈值针对 starcoder2-3b 标定（真人 log-rank 均值≈0.625）；工具原默认 1.5/3.0 不可用。
    # 阈值与打分模型强相关，换模型需重新标定。详见 config/settings.yaml 注释。
    log_rank_llm_threshold: float = 0.31
    log_rank_human_threshold: float = 0.31
    detect_score_threshold: float = 0.20
    git_blame: bool = True
    suspicious_min_confidence: float = 0.7
    max_functions: int = 0                # >0 时只检测前 N 个函数（限额/冒烟）

    # 环境变量覆盖表：env → (字段, 类型)
    _ENV_MAP = {
        "AI_DETECT_MODEL": ("model_id", str),
        "AI_DETECT_DEVICE": ("device", str),
        "AI_DETECT_ENGINE": ("engine", str),
        "AI_DETECT_BATCH_SIZE": ("batch_size", int),
        "AI_DETECT_K": ("k_perturbations", int),
        "AI_DETECT_MIN_LOC": ("min_loc", int),
        "AI_DETECT_LR_LLM": ("log_rank_llm_threshold", float),
        "AI_DETECT_LR_HUMAN": ("log_rank_human_threshold", float),
        "AI_DETECT_NPR_THRESHOLD": ("detect_score_threshold", float),
        "AI_DETECT_MAX_FUNCTIONS": ("max_functions", int),
    }

    def apply_env(self) -> "AIDetectSettings":
        data = self.model_dump()
        for env_var, (field, cast) in self._ENV_MAP.items():
            raw = os.getenv(env_var)
            if raw is None:
                continue
            try:
                data[field] = cast(raw)
            except (ValueError, TypeError):
                pass  # 坏值忽略，保留原值
        return AIDetectSettings(**data)


@lru_cache(maxsize=4)
def load_ai_detect_settings(path: str | None = None) -> AIDetectSettings:
    p = Path(path or DEFAULT_SETTINGS_PATH)
    data: dict = {}
    if p.exists():
        loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        data = loaded.get("ai_detect", {}) or {}
    return AIDetectSettings.model_validate(data).apply_env()
