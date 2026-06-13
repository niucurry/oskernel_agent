"""review 模块配置：模型参数来自 config/settings.yaml 的 llm 段；
base_url / api_key 来自环境变量（LLM_BASE_URL / LLM_API_KEY）。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_SETTINGS_PATH = "config/settings.yaml"


class LLMSettings(BaseModel):
    model: str = "deepseek-chat"
    temperature: float = 0.3
    votes: int = 3
    concurrency: int = 5
    max_card_tokens: int = 6000
    context_lines: int = 10
    request_timeout: float = 60.0
    max_retries: int = 3
    min_interval: float = 0.0

    # 运行期从环境变量注入（不入 settings.yaml，避免泄漏密钥）
    base_url: str | None = None
    api_key: str | None = None


@lru_cache(maxsize=4)
def load_llm_settings(path: str | None = None) -> LLMSettings:
    p = Path(path or DEFAULT_SETTINGS_PATH)
    data = {}
    if p.exists():
        data = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("llm", {}) or {}
    s = LLMSettings.model_validate(data)
    s.base_url = os.getenv("LLM_BASE_URL") or s.base_url
    s.api_key = os.getenv("LLM_API_KEY") or s.api_key
    # 允许用环境变量覆盖模型名
    if os.getenv("LLM_MODEL"):
        s.model = os.environ["LLM_MODEL"]
    return s
