"""Moteur compatible OpenAI (OpenAI, Mistral, Groq, vLLM, LM Studio, LocalAI…)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from .base import LLMBackend, LLMUnavailable

logger = logging.getLogger(__name__)


class OpenAICompatibleBackend(LLMBackend):
    name = "openai"

    def __init__(
        self, base_url: str, api_key: str, model: str, *, timeout: float = 300.0
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            response = await self._client.get("/models", timeout=8.0)
            return response.status_code < 400
        except httpx.HTTPError:
            return False

    async def stream(
        self, messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 700
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        try:
            async with self._client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise LLMUnavailable(f"API {self.base_url} a répondu {response.status_code} : {body}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    token = (choices[0].get("delta") or {}).get("content")
                    if token:
                        yield token
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"API {self.base_url} injoignable : {exc}") from exc
