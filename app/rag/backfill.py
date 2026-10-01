"""Rattrapage des vecteurs pour les fragments indexés sans embeddings.

L'application démarre en recherche lexicale seule pendant que le moteur
d'embeddings se charge (cf. `DeferredEmbedder`). Les documents indexés durant
cette fenêtre sont écrits sans vecteur. Une fois le moteur prêt, ce module
reprend ces fragments par lots et complète l'index, ce qui fait basculer la
recherche en mode hybride sans réindexer quoi que ce soit : les documents n'ont
pas à être relus ni redécoupés, seul le texte déjà stocké est vectorisé.

Le même mécanisme répare un changement de modèle : les vecteurs dont la
dimension ne correspond plus à celle du moteur courant sont recalculés.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .embedder import Embedder
from .store import DocumentStore

logger = logging.getLogger(__name__)

#: Délai d'attente lorsqu'une indexation est encore en cours de production.
_POLL_SECONDS = 5.0


@dataclass
class BackfillProgress:
    status: str = "idle"  # idle | running | done | error | cancelled
    done: int = 0
    remaining: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    last_error: str = ""

    @property
    def total(self) -> int:
        return self.done + self.remaining

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "done": self.done,
            "remaining": self.remaining,
            "total": self.total,
            "running": self.status == "running",
            "elapsed": round(
                (self.finished_at or time.time()) - self.started_at, 1
            )
            if self.started_at
            else 0.0,
            "last_error": self.last_error,
        }


class VectorBackfiller:
    """Vectorise, lot par lot, les fragments auxquels il manque un embedding."""

    def __init__(
        self,
        store: DocumentStore,
        embedder: Embedder,
        *,
        batch_size: int = 64,
        is_indexing: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.batch_size = max(1, batch_size)
        self.progress = BackfillProgress()
        self._is_indexing = is_indexing or (lambda: False)
        self._cancel = asyncio.Event()

    def cancel(self) -> None:
        self._cancel.set()

    async def run(self) -> None:
        """Complète l'index jusqu'à épuisement, en suivant l'indexation en cours."""
        if not self.embedder.available:
            return
        dimension = self.embedder.dimension
        self.progress = BackfillProgress(status="running", started_at=time.time())
        try:
            while not self._cancel.is_set():
                batch = await asyncio.to_thread(
                    self.store.chunks_without_vectors, dimension, self.batch_size
                )
                if not batch:
                    # L'indexation peut encore produire des fragments sans
                    # vecteur : on ne conclut qu'une fois qu'elle est terminée.
                    if self._is_indexing():
                        await asyncio.sleep(_POLL_SECONDS)
                        continue
                    break

                vectors = await asyncio.to_thread(
                    self.embedder.embed_documents, [text for _, text in batch]
                )
                written = await asyncio.to_thread(
                    self.store.set_chunk_embeddings,
                    list(zip([chunk_id for chunk_id, _ in batch], vectors, strict=True)),
                )
                self.progress.done += written
                if written == 0:
                    # Aucun vecteur produit : insister ferait boucler à l'infini
                    # sur le même lot.
                    raise RuntimeError("le moteur d'embeddings ne renvoie aucun vecteur")
                self.progress.remaining = await asyncio.to_thread(
                    self.store.count_chunks_without_vectors, dimension
                )
                await asyncio.sleep(0)  # laisse respirer les requêtes en cours

            self.progress.status = "cancelled" if self._cancel.is_set() else "done"
            self.progress.remaining = 0 if self.progress.status == "done" else self.progress.remaining
        except asyncio.CancelledError:
            self.progress.status = "cancelled"
            raise
        except Exception as exc:
            self.progress.status = "error"
            self.progress.last_error = str(exc)
            logger.exception("Rattrapage vectoriel interrompu : %s", exc)
        finally:
            self.progress.finished_at = time.time()
            if self.progress.status == "done" and self.progress.done:
                logger.info(
                    "Recherche hybride active : %d fragment(s) vectorisé(s) en %.1fs",
                    self.progress.done,
                    self.progress.finished_at - self.progress.started_at,
                )
