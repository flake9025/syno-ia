"""Chaîne complète de récupération, ACL comprises."""

from __future__ import annotations

import numpy as np
import pytest

from app.rag.embedder import NullEmbedder
from app.rag.pipeline import RetrievalPipeline
from app.rag.store import Chunk, DocumentRecord, DocumentStore
from app.synology.acl import AccessController
from app.synology.models import DSMSession, ShareInfo


class FakeClient:
    def __init__(self, shares: list[str], readable: set[str]):
        self._shares = shares
        self._readable = readable

    async def list_shares(self, sid: str) -> list[ShareInfo]:
        return [ShareInfo(path=path, name=path.strip("/")) for path in self._shares]

    async def stat_paths(self, sid: str, paths):
        return {path: path in self._readable for path in paths}


class ConstantEmbedder(NullEmbedder):
    """Embeddings déterministes pour tester la voie sémantique sans réseau."""

    backend = "test"
    dimension = 3

    def embed_documents(self, texts):
        return [np.array([len(text) % 7, 1.0, 2.0], dtype=np.float32) for text in texts]

    def embed_query(self, text):
        return np.array([len(text) % 7, 1.0, 2.0], dtype=np.float32)


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    instance = DocumentStore(tmp_path / "pipeline.db")
    for share, name, text in [
        ("/documents", "public.pdf", "La procédure de sauvegarde hebdomadaire du NAS Synology"),
        ("/documents", "interne.pdf", "La procédure de sauvegarde des serveurs internes"),
        ("/rh", "salaires.xlsx", "Grille des salaires et procédure de révision annuelle"),
    ]:
        instance.upsert_document(
            DocumentRecord(
                real_path=f"/volume1{share}/{name}",
                dsm_path=f"{share}/{name}",
                share=share,
                name=name,
                ext=".pdf",
                size=100,
                mtime=1,
                content_hash="h",
            ),
            [Chunk(text=text, ordinal=0, location="page 1", embedding=np.array([1.0, 1.0, 1.0]))],
        )
    yield instance
    instance.close()


def build(store: DocumentStore, shares: list[str], readable: set[str], embedder=None):
    access = AccessController(FakeClient(shares, readable), ttl=60)
    return RetrievalPipeline(store, embedder or NullEmbedder(), access, candidates=20, top_k=5)


async def test_seuls_les_documents_autorises_sont_retournes(store: DocumentStore):
    pipeline = build(store, ["/documents"], {"/documents/public.pdf"})
    result = await pipeline.retrieve(DSMSession(sid="s", account="alice"), "procédure sauvegarde")
    assert [hit.name for hit in result.hits] == ["public.pdf"]
    assert result.filtered_out >= 1


async def test_aucun_partage_aucun_resultat(store: DocumentStore):
    pipeline = build(store, [], set())
    result = await pipeline.retrieve(DSMSession(sid="s", account="bob"), "procédure")
    assert result.is_empty
    assert result.context == ""


async def test_le_contexte_contient_les_marqueurs_de_citation(store: DocumentStore):
    pipeline = build(store, ["/documents", "/rh"], {
        "/documents/public.pdf", "/documents/interne.pdf", "/rh/salaires.xlsx",
    })
    result = await pipeline.retrieve(DSMSession(sid="s", account="chef"), "procédure")
    assert "[1]" in result.context
    assert "page 1" in result.context
    assert len(result.hits) == 3
    assert result.sources()[0]["index"] == 1


async def test_le_contexte_est_borne(store: DocumentStore):
    pipeline = build(store, ["/documents", "/rh"], {
        "/documents/public.pdf", "/documents/interne.pdf", "/rh/salaires.xlsx",
    })
    pipeline.context_max_chars = 150
    result = await pipeline.retrieve(DSMSession(sid="s", account="chef"), "procédure")
    assert len(result.context) <= 400  # en-têtes compris


async def test_voie_semantique_activee(store: DocumentStore):
    pipeline = build(store, ["/documents"], {"/documents/public.pdf"}, ConstantEmbedder())
    result = await pipeline.retrieve(DSMSession(sid="s", account="alice"), "sauvegarde")
    assert not result.lexical_only


async def test_question_vide(store: DocumentStore):
    pipeline = build(store, ["/documents"], {"/documents/public.pdf"})
    result = await pipeline.retrieve(DSMSession(sid="s", account="alice"), "   ")
    assert result.is_empty


async def test_le_compteur_ne_retient_que_les_vrais_refus(store: DocumentStore):
    """`filtered_out` alimente « n extraits écartés faute de droits ».

    Avec un `top_k` plus petit que le nombre de candidats, les extraits
    excédentaires ne sont jamais examinés : les compter comme refusés
    ferait croire à l'utilisateur qu'on lui cache des documents.
    """
    pipeline = build(store, ["/documents", "/rh"], {
        "/documents/public.pdf", "/documents/interne.pdf", "/rh/salaires.xlsx",
    })
    pipeline.top_k = 1

    result = await pipeline.retrieve(DSMSession(sid="s", account="chef"), "procédure")

    assert len(result.hits) == 1
    assert result.considered == 3
    assert result.filtered_out == 0


async def test_le_compteur_signale_les_refus_reels(store: DocumentStore):
    pipeline = build(store, ["/documents", "/rh"], {"/documents/public.pdf"})
    result = await pipeline.retrieve(DSMSession(sid="s", account="alice"), "procédure")
    assert [hit.name for hit in result.hits] == ["public.pdf"]
    assert result.filtered_out == 2
