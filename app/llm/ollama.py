"""Moteur Ollama (serveur distant ou conteneur voisin sur le NAS)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from .base import LLMBackend, LLMUnavailable

logger = logging.getLogger(__name__)


class OllamaBackend(LLMBackend):
    name = "ollama"

    def __init__(self, base_url: str, model: str, *, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.model)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            response = await self._client.get("/api/tags", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        try:
            response = await self._client.get("/api/tags", timeout=10.0)
            response.raise_for_status()
            return [model["name"] for model in response.json().get("models", [])]
        except (httpx.HTTPError, KeyError, ValueError):
            return []

    async def stream(
        self, messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 700
    ) -> AsyncIterator[str]:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")[:300]
                    raise LLMUnavailable(f"Ollama a répondu {response.status_code} : {body}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("error"):
                        raise LLMUnavailable(f"Ollama : {chunk['error']}")
                    token = (chunk.get("message") or {}).get("content")
                    if token:
                        yield token
                    if chunk.get("done"):
                        break
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"Ollama injoignable sur {self.base_url} : {exc}") from exc
