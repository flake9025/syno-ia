"""Moteur local llama.cpp (GGUF), exécuté dans le conteneur.

Sur un NAS modeste, le modèle est chargé **paresseusement** au premier usage afin
de ne pas immobiliser la RAM tant que personne ne pose de question, et déchargé
sur demande depuis le panneau d'administration.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from .base import LLMBackend, LLMUnavailable

logger = logging.getLogger(__name__)

_SENTINEL = object()


def llama_cpp_available() -> bool:
    try:
        import llama_cpp  # noqa: F401
    except Exception:
        return False
    return True


class LlamaCppBackend(LLMBackend):
    name = "llamacpp"

    def __init__(
        self,
        model_path: Path,
        *,
        context_size: int = 4096,
        threads: int = 0,
        model_label: str = "",
    ) -> None:
        if not llama_cpp_available():
            raise LLMUnavailable(
                "llama-cpp-python n'est pas installé dans cette image "
                "(construisez l'image avec WITH_LOCAL_LLM=1)."
            )
        if not model_path.exists():
            raise LLMUnavailable(f"Modèle GGUF introuvable : {model_path}")
        self.model_path = model_path
        self.model = model_label or model_path.name
        self.context_size = context_size
        self.threads = threads or 0
        self._llama = None
        self._load_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.model_path.exists()

    def _ensure_loaded(self):
        if self._llama is not None:
            return self._llama
        with self._load_lock:
            if self._llama is None:
                from llama_cpp import Llama

                logger.info("Chargement du modèle %s…", self.model_path.name)
                self._llama = Llama(
                    model_path=str(self.model_path),
                    n_ctx=self.context_size,
                    n_threads=self.threads or None,
                    n_batch=64,
                    use_mlock=False,
                    use_mmap=True,
                    verbose=False,
                )
                logger.info("Modèle %s chargé", self.model_path.name)
        return self._llama

    def unload(self) -> None:
        with self._load_lock:
            self._llama = None

    async def aclose(self) -> None:
        # Libère la projection mémoire du GGUF : indispensable avant de supprimer
        # le fichier, et pour rendre la RAM quand le moteur est remplacé.
        self.unload()

    async def stream(
        self, messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 700
    ) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        output: queue.Queue = queue.Queue(maxsize=64)

        def _worker() -> None:
            try:
                llama = self._ensure_loaded()
                stream = llama.create_chat_completion(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=True,
                )
                for part in stream:
                    delta = (part.get("choices") or [{}])[0].get("delta") or {}
                    token = delta.get("content")
                    if token:
                        output.put(token)
            except Exception as exc:  # pragma: no cover - dépend du runtime natif
                output.put(LLMUnavailable(f"llama.cpp : {exc}"))
            finally:
                output.put(_SENTINEL)

        thread = threading.Thread(target=_worker, name="llamacpp", daemon=True)
        thread.start()
        try:
            while True:
                item = await loop.run_in_executor(None, output.get)
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            thread.join(timeout=1.0)

    async def health(self) -> bool:
        return self.available

    def describe(self) -> dict:
        data = super().describe()
        data.update(
            {
                "model_path": str(self.model_path),
                "loaded": self._llama is not None,
                "context_size": self.context_size,
                "threads": self.threads,
            }
        )
        return data
