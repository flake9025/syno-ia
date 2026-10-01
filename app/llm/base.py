"""Interface commune à tous les moteurs de génération."""

from __future__ import annotations

from collections.abc import AsyncIterator


class LLMUnavailable(RuntimeError):
    """Le moteur de génération n'est pas utilisable."""


class LLMBackend:
    name = "none"
    model = ""

    @property
    def available(self) -> bool:
        return False

    async def stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.2,
        max_tokens: int = 700,
    ) -> AsyncIterator[str]:
        """Génère la réponse au fil de l'eau."""
        raise NotImplementedError
        yield ""  # pragma: no cover - rend la fonction asynchrone-génératrice

    async def complete(
        self, messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 700
    ) -> str:
        parts = [
            token
            async for token in self.stream(
                messages, temperature=temperature, max_tokens=max_tokens
            )
        ]
        return "".join(parts)

    async def health(self) -> bool:
        return self.available

    async def aclose(self) -> None:
        return None

    def describe(self) -> dict:
        return {"backend": self.name, "model": self.model, "available": self.available}
