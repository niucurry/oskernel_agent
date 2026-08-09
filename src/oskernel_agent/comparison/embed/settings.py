"""加载 config/settings.yaml 的嵌入/向量库配置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_SETTINGS_PATH = "config/settings.yaml"


class EmbeddingSettings(BaseModel):
    model_name: str = "Salesforce/codet5p-110m-embedding"
    max_length: int = 512
    window_size: int = 512
    window_stride: int = 384
    batch_size: int = 16
    device: str = "auto"


class QdrantSettings(BaseModel):
    url: str = "http://localhost:6333"
    collection: str = "os_functions"


class RetrievalSettings(BaseModel):
    top_k: int = 20


class Settings(BaseModel):
    embedding: EmbeddingSettings = EmbeddingSettings()
    qdrant: QdrantSettings = QdrantSettings()
    retrieval: RetrievalSettings = RetrievalSettings()


@lru_cache(maxsize=4)
def load_settings(path: str | None = None) -> Settings:
    p = Path(path or DEFAULT_SETTINGS_PATH)
    if not p.exists():
        return Settings()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Settings.model_validate(data)
