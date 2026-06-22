"""LLM 客户端接口与 OpenAI 兼容异步实现。

接口设计成可注入，便于用 mock 客户端做单元测试（不联网）。
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from loguru import logger

from .config import LLMSettings


@runtime_checkable
class LLMClient(Protocol):
    async def complete(self, messages: list[dict], temperature: float) -> str:
        """给定 messages 返回模型文本输出。"""
        ...


class OpenAICompatClient:
    """OpenAI 兼容 API（DeepSeek/Qwen 等）异步客户端，带限速与网络重试。"""

    def __init__(self, settings: LLMSettings):
        from openai import AsyncOpenAI

        if not settings.api_key:
            raise RuntimeError("缺少 LLM_API_KEY 环境变量，无法调用真实 LLM")
        self.settings = settings
        self.model = settings.model
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url or None,
            timeout=settings.request_timeout,
        )
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _throttle(self) -> None:
        if self.settings.min_interval <= 0:
            return
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._last_call + self.settings.min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = asyncio.get_event_loop().time()

    async def complete(self, messages: list[dict], temperature: float) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            await self._throttle()
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                )
                return resp.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001 — 网络/限速错误重试
                last_exc = exc
                backoff = min(2 ** attempt, 10)
                logger.warning("LLM 调用失败（第 {} 次）：{}；{}s 后重试", attempt, exc, backoff)
                await asyncio.sleep(backoff)
        raise RuntimeError(f"LLM 调用在 {self.settings.max_retries} 次重试后仍失败: {last_exc}")
