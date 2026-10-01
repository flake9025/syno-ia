"""Chaîne de récupération : recherche hybride puis filtrage par permissions.

Ordre des opérations (important pour la sécurité **et** la performance) :

1. pré-filtrage SQL sur les partages visibles par l'utilisateur ;
2. recherche lexicale (BM25) et sémantique (cosinus) sur ce sous-ensemble ;
3. fusion RRF des deux classements ;
4. **filtrage ACL fichier par fichier** via File Station avec le `sid` de
   l'utilisateur, en remontant la liste jusqu'à obtenir `top_k` résultats
   réellement autorisés ;
5. construction du contexte transmis au LLM.

Aucun extrait n'est renvoyé au navigateur avant l'étape 4.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..synology.acl import AccessController
from ..synology.models import DSMSession
from .embedder import Embedder
from .store import DocumentStore, SearchHit, reciprocal_rank_fusion

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    hits: list[SearchHit]
    context: str
    considered: int
    filtered_out: int
    lexical_only: bool

    @property
    def is_empty(self) -> bool:
        return not self.hits

    def sources(self) -> list[dict]:
        return [hit.to_source(index + 1) for index, hit in enumerate(self.hits)]


class RetrievalPipeline:
    def __init__(
        self,
        store: DocumentStore,
        embedder: Embedder,
        access: AccessController,
        *,
        candidates: int = 60,
        top_k: int = 6,
        context_max_chars: int = 6000,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.access = access
        self.candidates = candidates
        self.top_k = top_k
        self.context_max_chars = context_max_chars

    async def retrieve(
        self,
        session: DSMSession,
        question: str,
        *,
        top_k: int | None = None,
    ) -> RetrievalResult:
        question = (question or "").strip()
        if not question:
            return RetrievalResult([], "", 0, 0, not self.embedder.available)

        top_k = top_k or self.top_k
        allowed_shares = await self.access.readable_share_paths(session)
        if not allowed_shares:
            logger.info("Aucun partage accessible pour %s", session.account)
            return RetrievalResult([], "", 0, 0, not self.embedder.available)

        lexical = await asyncio.to_thread(
            self.store.search_lexical, question, self.candidates, allowed_shares
        )
        semantic: list[SearchHit] = []
        if self.embedder.available:
            embedding = await asyncio.to_thread(self.embedder.embed_query, question)
            if embedding is not None:
                semantic = await asyncio.to_thread(
                    self.store.search_semantic, embedding, self.candidates, allowed_shares
                )

        fused = reciprocal_rank_fusion([lexical, semantic])
        if not fused:
            return RetrievalResult([], "", 0, 0, not semantic)

        authorized, denied = await self._authorize(session, fused, top_k)
        context = self._build_context(authorized)
        return RetrievalResult(
            hits=authorized,
            context=context,
            considered=len(fused),
            filtered_out=denied,
            lexical_only=not semantic,
        )

    async def _authorize(
        self, session: DSMSession, hits: list[SearchHit], top_k: int
    ) -> tuple[list[SearchHit], int]:
        """Valide les permissions par lots jusqu'à réunir `top_k` résultats.

        Renvoie les extraits retenus et le nombre d'extraits **réellement
        refusés** par les ACL. Les extraits jamais examinés — parce que `top_k`
        était déjà atteint — ne sont pas comptés : les signaler comme refusés
        laisserait croire à l'utilisateur qu'on lui cache des documents.
        """
        selected: list[SearchHit] = []
        denied = 0
        checked: dict[str, bool] = {}
        # Un document peut fournir plusieurs fragments : on regroupe par chemin.
        batch_size = max(top_k * 2, 10)
        position = 0
        while position < len(hits) and len(selected) < top_k:
            window = hits[position : position + batch_size]
            position += batch_size
            unknown = [hit.dsm_path for hit in window if hit.dsm_path not in checked]
            if unknown:
                granted = await self.access.filter_paths(session, unknown)
                for path in unknown:
                    checked[path] = path in granted
            for hit in window:
                if checked.get(hit.dsm_path):
                    selected.append(hit)
                    if len(selected) >= top_k:
                        break
                else:
                    denied += 1
        return selected, denied

    def _build_context(self, hits: list[SearchHit]) -> str:
        blocks: list[str] = []
        used = 0
        for index, hit in enumerate(hits, start=1):
            header = f"[{index}] {hit.name}"
            if hit.location:
                header += f" — {hit.location}"
            header += f" ({hit.dsm_path})"
            body = hit.text.strip()
            remaining = self.context_max_chars - used
            if remaining <= 200:
                break
            if len(body) > remaining:
                body = body[:remaining].rsplit(" ", 1)[0] + "…"
            block = f"{header}\n{body}"
            blocks.append(block)
            used += len(block)
        return "\n\n---\n\n".join(blocks)
