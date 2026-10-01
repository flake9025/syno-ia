"""Démarrage en BM25 puis bascule en hybride : proxy d'embeddings et rattrapage."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from app.rag.backfill import VectorBackfiller
from app.rag.embedder import DeferredEmbedder, Embedder, NullEmbedder
from app.rag.store import Chunk, DocumentRecord, DocumentStore


class FakeEmbedder(Embedder):
    """Moteur déterministe : le vecteur dépend de la longueur du texte."""

    backend = "fake"

    def __init__(self, dimension: int = 4) -> None:
        self.model_name = "fake-model"
        self.dimension = dimension
        self.calls = 0

    def embed_documents(self, texts):
        self.calls += 1
        return [np.full(self.dimension, float(len(text)) or 1.0) for text in texts]

    def embed_query(self, text):
        return np.full(self.dimension, float(len(text)) or 1.0)


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    instance = DocumentStore(tmp_path / "index.db")
    yield instance
    instance.close()


def peupler(store: DocumentStore, nombre: int) -> None:
    record = DocumentRecord(
        real_path="/volume1/documents/guide.txt",
        dsm_path="/documents/guide.txt",
        share="/documents",
        name="guide.txt",
        ext=".txt",
        size=1,
        mtime=1700000000,
        content_hash="hash",
    )
    store.upsert_document(
        record, [Chunk(text=f"fragment numero {i}", ordinal=i) for i in range(nombre)]
    )


# ------------------------------------------------------------------- proxy
def test_proxy_indisponible_avant_chargement():
    proxy = DeferredEmbedder()
    assert proxy.available is False
    assert proxy.state == "pending"
    assert proxy.embed_query("test") is None
    assert proxy.embed_documents(["a", "b"]) == [None, None]


def test_proxy_suit_le_moteur_adopte():
    proxy = DeferredEmbedder()
    proxy.mark_loading()
    assert proxy.state == "loading"

    proxy.adopt(FakeEmbedder(dimension=6))
    assert proxy.available is True
    assert proxy.state == "ready"
    assert proxy.backend == "fake"
    assert proxy.model_name == "fake-model"
    assert proxy.dimension == 6
    assert proxy.describe()["state"] == "ready"
    assert proxy.embed_query("abc").shape == (6,)


def test_proxy_signale_un_chargement_echoue():
    proxy = DeferredEmbedder()
    proxy.adopt(NullEmbedder("modèle introuvable"))
    assert proxy.state == "failed"
    assert proxy.available is False
    assert proxy.describe()["reason"] == "modèle introuvable"


# -------------------------------------------------------------- rattrapage
async def test_rattrapage_vectorise_tous_les_fragments(store: DocumentStore):
    peupler(store, 10)
    embedder = FakeEmbedder()
    backfiller = VectorBackfiller(store, embedder, batch_size=3)

    await backfiller.run()

    assert backfiller.progress.status == "done"
    assert backfiller.progress.done == 10
    assert store.count_chunks_without_vectors(4) == 0
    assert embedder.calls == 4  # lots de 3, 3, 3, 1


async def test_rattrapage_sans_moteur_ne_fait_rien(store: DocumentStore):
    peupler(store, 3)
    backfiller = VectorBackfiller(store, NullEmbedder("désactivé"))

    await backfiller.run()

    assert backfiller.progress.status == "idle"
    assert store.count_chunks_without_vectors(4) == 3


async def test_rattrapage_attend_la_fin_de_l_indexation(store, monkeypatch):
    """Les fragments produits pendant le rattrapage doivent être repris."""
    monkeypatch.setattr("app.rag.backfill._POLL_SECONDS", 0.01)
    peupler(store, 2)
    indexation = {"en_cours": True}
    backfiller = VectorBackfiller(
        store, FakeEmbedder(), batch_size=5, is_indexing=lambda: indexation["en_cours"]
    )

    tache = asyncio.create_task(backfiller.run())
    # Le premier lot est traité, mais la tâche ne doit pas conclure.
    await asyncio.sleep(0.05)
    assert not tache.done()

    peupler(store, 4)  # l'indexation ajoute de nouveaux fragments
    await asyncio.sleep(0.05)
    indexation["en_cours"] = False
    await asyncio.wait_for(tache, timeout=5)

    assert backfiller.progress.status == "done"
    assert store.count_chunks_without_vectors(4) == 0


async def test_rattrapage_abandonne_si_aucun_vecteur_produit(store: DocumentStore):
    """Sans ce garde-fou, le même lot serait repris indéfiniment."""

    class MoteurMuet(FakeEmbedder):
        def embed_documents(self, texts):
            return [None] * len(texts)

    peupler(store, 3)
    backfiller = VectorBackfiller(store, MoteurMuet(), batch_size=2)

    await asyncio.wait_for(backfiller.run(), timeout=5)

    assert backfiller.progress.status == "error"
    assert store.count_chunks_without_vectors(4) == 3


async def test_rattrapage_recalcule_une_dimension_obsolete(store: DocumentStore):
    """Changement de modèle : les anciens vecteurs doivent être remplacés."""
    peupler(store, 3)
    await VectorBackfiller(store, FakeEmbedder(dimension=4)).run()
    assert store.count_chunks_without_vectors(4) == 0

    await VectorBackfiller(store, FakeEmbedder(dimension=8)).run()
    assert store.count_chunks_without_vectors(8) == 0
    assert store.stats()["vectors"] == 3


async def test_rattrapage_interrompu_proprement(store: DocumentStore):
    peupler(store, 50)
    backfiller = VectorBackfiller(store, FakeEmbedder(), batch_size=1)
    backfiller.cancel()

    await asyncio.wait_for(backfiller.run(), timeout=5)

    assert backfiller.progress.status == "cancelled"
