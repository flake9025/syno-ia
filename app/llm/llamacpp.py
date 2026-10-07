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
        batch_size: int = 512,
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
        # Un paquet plus grand que la fenêtre de contexte n'a aucun sens.
        self.batch_size = max(32, min(batch_size or 512, context_size))
        self._llama = None
        self._load_lock = threading.Lock()
        # llama.cpp ne supporte pas deux générations simultanées sur le même
        # contexte : on les sérialise, ce qui rend aussi l'interruption sans
        # ambiguïté (elle vise toujours la génération en cours).
        self._generate_lock = threading.Lock()
        self._abort: threading.Event | None = None
        self._abort_cb = None

    @property
    def available(self) -> bool:
        return self.model_path.exists()

    def _ensure_loaded(self):
        if self._llama is not None:
            return self._llama
        with self._load_lock:
            if self._llama is None:
                from llama_cpp import Llama

                logger.info(
                    "Chargement du modèle %s (contexte %d, %s fil(s), paquet %d)…",
                    self.model_path.name,
                    self.context_size,
                    self.threads or "auto",
                    self.batch_size,
                )
                self._llama = Llama(
                    model_path=str(self.model_path),
                    n_ctx=self.context_size,
                    n_threads=self.threads or None,
                    n_batch=self.batch_size,
                    n_ubatch=self.batch_size,
                    use_mlock=False,
                    use_mmap=True,
                    verbose=False,
                )
                logger.info("Modèle %s chargé", self.model_path.name)
                self._install_abort_callback(self._llama)
        return self._llama

    def _install_abort_callback(self, llama) -> None:
        """Branche l'interruption native de llama.cpp.

        Sans elle, l'abandon d'une génération n'est constaté qu'entre deux jetons.
        Or un dépassement de délai survient le plus souvent *pendant la lecture du
        prompt*, avant le premier jeton : le fil continuerait alors à saturer les
        cœurs plusieurs minutes, affamant le serveur web au point que la réponse
        de repli ne partirait jamais.
        """
        import llama_cpp

        setter = getattr(llama_cpp, "llama_set_abort_callback", None)
        fabrique = getattr(llama_cpp, "ggml_abort_callback", None)
        contexte = getattr(llama, "ctx", None)
        if setter is None or fabrique is None or contexte is None:
            logger.warning(
                "llama.cpp sans interruption native : un abandon ne libérera "
                "le processeur qu'au jeton suivant"
            )
            return

        def _doit_abandonner(_donnees) -> bool:
            drapeau = self._abort
            return bool(drapeau is not None and drapeau.is_set())

        # ctypes ne retient pas la fonction : sans cette référence, le ramasse-miettes
        # la libérerait et llama.cpp appellerait une adresse morte.
        self._abort_cb = fabrique(_doit_abandonner)
        try:
            setter(contexte, self._abort_cb, None)
        except Exception as exc:  # pragma: no cover - dépend du runtime natif
            self._abort_cb = None
            logger.warning("Interruption native indisponible (%s)", exc)

    def unload(self) -> None:
        with self._load_lock:
            self._llama = None
            # Le contexte natif disparaît avec le modèle : garder la fonction de
            # rappel n'aurait plus de sens.
            self._abort_cb = None

    async def aclose(self) -> None:
        # Libère la projection mémoire du GGUF : indispensable avant de supprimer
        # le fichier, et pour rendre la RAM quand le moteur est remplacé.
        self.unload()

    async def stream(
        self, messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 700
    ) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        output: queue.Queue = queue.Queue(maxsize=64)
        cancelled = threading.Event()

        def _worker() -> None:
            # Une seule génération à la fois : llama.cpp partage un contexte unique,
            # et l'interruption ne doit viser que la génération réellement en cours.
            with self._generate_lock:
                if cancelled.is_set():
                    output.put(_SENTINEL)
                    return
                self._abort = cancelled
                try:
                    llama = self._ensure_loaded()
                    stream = llama.create_chat_completion(
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stream=True,
                    )
                    for part in stream:
                        if cancelled.is_set():
                            break
                        delta = (part.get("choices") or [{}])[0].get("delta") or {}
                        token = delta.get("content")
                        if token:
                            output.put(token)
                except Exception as exc:  # pragma: no cover - dépend du runtime natif
                    # Une interruption volontaire remonte comme une erreur de décodage :
                    # ce n'est pas une panne, et plus personne n'écoute.
                    if not cancelled.is_set():
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
            # Abandon (client parti, délai dépassé) : sans ce drapeau, llama.cpp
            # continuerait à produire des jetons que plus personne ne lit, en
            # monopolisant les cœurs du NAS jusqu'à `max_tokens`.
            cancelled.set()
            while True:  # débloque un `put` resté en attente sur une file pleine
                try:
                    output.get_nowait()
                except queue.Empty:
                    break
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.warning(
                    "Le fil llama.cpp ne s'est pas arrêté en 2 s : le processeur "
                    "restera chargé jusqu'à la fin du calcul en cours"
                )

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
