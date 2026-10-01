"""Assemblage du contexte : démarrage immédiat en BM25, bascule hybride différée."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.config import Settings
from app.rag.embedder import DeferredEmbedder, Embedder, NullEmbedder
from app.rag.store import Chunk, DocumentRecord
from app.services import build_context


class FauxMoteur(Embedder):
    backend = "faux"

    def __init__(self) -> None:
        self.model_name = "faux-modele"
        self.dimension = 4

    def embed_documents(self, texts):
        return [np.full(4, float(len(text)) or 1.0) for text in texts]

    def embed_query(self, text):
        return np.full(4, float(len(text)) or 1.0)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        app_secret="secret-de-test-0123456789",
        embedding_backend="model2vec",
        embedding_async_load=True,
        llm_backend="none",
        index_on_startup=False,
        index_interval_minutes=0,
        dsm_url="http://dsm.invalid:5000",
        dsm_service_account="",
        dsm_service_password="",
    )


def indexer_un_document(context) -> None:
    context.store.upsert_document(
        DocumentRecord(
            real_path="/volume1/documents/guide.txt",
            dsm_path="/documents/guide.txt",
            share="/documents",
            name="guide.txt",
            ext=".txt",
            size=1,
            mtime=1,
            content_hash="h",
        ),
        [Chunk(text="procédure de sauvegarde", ordinal=0)],
    )


async def test_demarrage_immediat_en_lexical(settings: Settings, monkeypatch):
    """Le modèle ne doit jamais être téléchargé pendant le démarrage."""
    monkeypatch.setattr(
        "app.services.build_embedder",
        lambda *a, **k: pytest.fail("le moteur ne doit pas être chargé au démarrage"),
    )

    context = await build_context(settings)
    try:
        assert isinstance(context.embedder, DeferredEmbedder)
        assert context.embedder.available is False
        assert context.embeddings_pending is True
    finally:
        await context.shutdown()


async def test_bascule_en_hybride_et_rattrapage(settings: Settings, monkeypatch):
    monkeypatch.setattr("app.services.build_embedder", lambda *a, **k: FauxMoteur())

    context = await build_context(settings)
    try:
        indexer_un_document(context)
        assert context.store.count_chunks_without_vectors(4) == 1

        assert context.start_embeddings() is True
        await asyncio.wait_for(context._warmup, timeout=10)

        assert context.embedder.available is True
        assert context.embedder.backend == "faux"
        assert context.embeddings_pending is False
        # Le fragment indexé en mode lexical a été vectorisé sans réindexation.
        assert context.store.count_chunks_without_vectors(4) == 0
        assert context.backfiller.progress.done == 1
        assert context.status()["vectorization"]["status"] == "done"
    finally:
        await context.shutdown()


async def test_echec_de_chargement_laisse_l_application_utilisable(
    settings: Settings, monkeypatch
):
    """Un modèle introuvable ne doit pas empêcher de répondre en BM25."""

    def explose(*args, **kwargs):
        raise RuntimeError("réseau indisponible")

    monkeypatch.setattr("app.services.build_embedder", explose)

    context = await build_context(settings)
    try:
        indexer_un_document(context)
        context.start_embeddings()
        await asyncio.wait_for(context._warmup, timeout=10)

        assert context.embedder.available is False
        assert context.embedder.state == "failed"
        assert context.embeddings_pending is False
        assert any("réseau indisponible" in erreur for erreur in context.startup_errors)
        # La recherche lexicale reste opérationnelle.
        assert len(context.store.search_lexical("sauvegarde", limit=5)) == 1
    finally:
        await context.shutdown()


async def test_repli_sans_moteur_disponible(settings: Settings, monkeypatch):
    monkeypatch.setattr(
        "app.services.build_embedder", lambda *a, **k: NullEmbedder("aucun moteur")
    )

    context = await build_context(settings)
    try:
        context.start_embeddings()
        await asyncio.wait_for(context._warmup, timeout=10)
        assert context.embedder.state == "failed"
        assert context.backfiller.progress.status == "idle"
    finally:
        await context.shutdown()


async def test_relance_apres_echec(settings: Settings, monkeypatch):
    """Un échec réseau ne doit pas condamner l'instance au BM25 jusqu'au redémarrage."""
    tentatives = {"n": 0}

    def capricieux(*args, **kwargs):
        tentatives["n"] += 1
        if tentatives["n"] == 1:
            raise RuntimeError("réseau indisponible")
        return FauxMoteur()

    monkeypatch.setattr("app.services.build_embedder", capricieux)

    context = await build_context(settings)
    try:
        indexer_un_document(context)
        context.start_embeddings()
        await asyncio.wait_for(context._warmup, timeout=10)
        assert context.embedder.available is False

        assert context.start_embeddings() is True
        await asyncio.wait_for(context._warmup, timeout=10)

        assert context.embedder.available is True
        assert context.store.count_chunks_without_vectors(4) == 0
    finally:
        await context.shutdown()


async def test_chargement_synchrone_si_demande(settings: Settings, monkeypatch):
    """`embedding_async_load=false` conserve l'ancien comportement bloquant."""
    monkeypatch.setattr("app.services.build_embedder", lambda *a, **k: FauxMoteur())
    settings = settings.model_copy(update={"embedding_async_load": False})

    context = await build_context(settings)
    try:
        assert not isinstance(context.embedder, DeferredEmbedder)
        assert context.embedder.available is True
        assert context.start_embeddings() is False
    finally:
        await context.shutdown()
