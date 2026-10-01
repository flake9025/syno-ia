"""Index SQLite : persistance, recherche lexicale, recherche vectorielle, RRF."""

from __future__ import annotations

import numpy as np
import pytest

from app.rag.store import (
    Chunk,
    DocumentRecord,
    DocumentStore,
    SearchHit,
    build_fts_query,
    reciprocal_rank_fusion,
)


@pytest.fixture
def store(tmp_path) -> DocumentStore:
    instance = DocumentStore(tmp_path / "index.db")
    yield instance
    instance.close()


def add(store: DocumentStore, name: str, texts: list[str], share: str = "/documents", dim: int = 4):
    record = DocumentRecord(
        real_path=f"/volume1/documents/{name}",
        dsm_path=f"{share}/{name}",
        share=share,
        name=name,
        ext=".txt",
        size=len(" ".join(texts)),
        mtime=1700000000,
        content_hash="hash",
    )
    chunks = [
        Chunk(text=text, ordinal=i, location=f"page {i + 1}", embedding=np.full(dim, i + 1.0))
        for i, text in enumerate(texts)
    ]
    return store.upsert_document(record, chunks)


def test_upsert_et_statistiques(store: DocumentStore):
    add(store, "guide.txt", ["Procédure de sauvegarde du NAS", "Restauration des données"])
    stats = store.stats()
    assert stats["documents"] == 1
    assert stats["chunks"] == 2
    assert stats["vectors"] == 2


def test_reindexation_remplace_les_fragments(store: DocumentStore):
    add(store, "guide.txt", ["un", "deux", "trois"])
    add(store, "guide.txt", ["nouveau contenu"])
    stats = store.stats()
    assert stats["documents"] == 1
    assert stats["chunks"] == 1


def test_recherche_lexicale(store: DocumentStore):
    add(store, "backup.txt", ["La procédure de sauvegarde hebdomadaire du serveur"])
    add(store, "recette.txt", ["Gâteau au chocolat et aux amandes"])
    hits = store.search_lexical("sauvegarde", limit=10)
    assert len(hits) == 1
    assert hits[0].name == "backup.txt"
    assert hits[0].lexical_rank == 0


def test_recherche_lexicale_insensible_aux_accents(store: DocumentStore):
    add(store, "doc.txt", ["Procédure de télétravail"])
    assert store.search_lexical("procedure", limit=5)
    assert store.search_lexical("PROCÉDURE", limit=5)


def test_filtrage_par_partage(store: DocumentStore):
    add(store, "public.txt", ["contenu partagé"], share="/documents")
    add(store, "prive.txt", ["contenu partagé"], share="/rh")
    autorises = store.search_lexical("partagé", limit=10, shares={"/documents"})
    assert [hit.share for hit in autorises] == ["/documents"]
    assert store.search_lexical("partagé", limit=10, shares=set()) == []


def test_recherche_semantique(store: DocumentStore):
    add(store, "a.txt", ["premier fragment", "second fragment"], dim=4)
    resultats = store.search_semantic(np.array([2.0, 2.0, 2.0, 2.0]), limit=5)
    assert resultats
    assert resultats[0].semantic_rank == 0


def test_recherche_semantique_ignore_les_dimensions_incompatibles(store: DocumentStore):
    add(store, "a.txt", ["fragment"], dim=4)
    assert store.search_semantic(np.array([1.0, 1.0]), limit=5) == []


def test_suppression_des_documents_absents(store: DocumentStore):
    add(store, "a.txt", ["alpha"])
    add(store, "b.txt", ["beta"])
    removed = store.delete_missing(
        ["/volume1/documents/a.txt"], roots=["/volume1/documents"]
    )
    assert removed == 1
    assert store.stats()["documents"] == 1


def test_documents_hors_racine_sont_conserves(store: DocumentStore):
    add(store, "a.txt", ["alpha"])
    removed = store.delete_missing([], roots=["/volume1/autre"])
    assert removed == 0


def test_requete_fts_est_echappee():
    assert build_fts_query('recherche "injection" OR') == '"recherche"* OR "injection"* OR "or"*'
    assert build_fts_query("!!!") == ""


def test_fusion_rrf_privilegie_les_resultats_communs():
    def hit(chunk_id: int) -> SearchHit:
        return SearchHit(
            chunk_id=chunk_id, document_id=chunk_id, text="", ordinal=0, location="",
            real_path="", dsm_path="", share="", name="", mtime=0,
        )

    lexical = [hit(1), hit(2), hit(3)]
    semantic = [hit(3), hit(4), hit(1)]
    fused = reciprocal_rank_fusion([lexical, semantic])
    # Le fragment 1 (rang 0 puis 2) et le 3 (rang 2 puis 0) doivent devancer les autres.
    assert {fused[0].chunk_id, fused[1].chunk_id} == {1, 3}
    assert fused[0].score > fused[-1].score


def test_liste_des_documents_filtre_par_partage(store: DocumentStore):
    add(store, "a.txt", ["alpha"], share="/documents")
    add(store, "b.txt", ["beta"], share="/rh")
    assert len(store.list_documents(shares={"/documents"})) == 1
    assert store.list_documents(shares=set()) == []
    assert len(store.list_documents(shares=None)) == 2


# ------------------------------------------------- rattrapage des vecteurs
def add_sans_vecteur(store: DocumentStore, name: str, texts: list[str], share: str = "/documents"):
    record = DocumentRecord(
        real_path=f"/volume1/documents/{name}",
        dsm_path=f"{share}/{name}",
        share=share,
        name=name,
        ext=".txt",
        size=1,
        mtime=1700000000,
        content_hash="hash",
    )
    chunks = [Chunk(text=text, ordinal=i, location="") for i, text in enumerate(texts)]
    return store.upsert_document(record, chunks)


def test_fragments_sans_vecteur_sont_reperes(store: DocumentStore):
    add_sans_vecteur(store, "brut.txt", ["un", "deux"])
    assert store.count_chunks_without_vectors(4) == 2
    attente = store.chunks_without_vectors(4, limit=10)
    assert [text for _, text in attente] == ["un", "deux"]


def test_vecteurs_de_dimension_obsolete_sont_a_refaire(store: DocumentStore):
    """Changer de modèle d'embeddings doit provoquer un recalcul, pas un index muet."""
    add(store, "guide.txt", ["alpha", "beta"], dim=4)
    assert store.count_chunks_without_vectors(4) == 0
    assert store.count_chunks_without_vectors(8) == 2


def test_rattrapage_active_la_recherche_semantique(store: DocumentStore):
    add_sans_vecteur(store, "brut.txt", ["sauvegarde du NAS", "restauration"])
    assert store.search_semantic(np.full(4, 1.0), limit=5) == []

    attente = store.chunks_without_vectors(4, limit=10)
    ecrits = store.set_chunk_embeddings(
        [(chunk_id, np.full(4, 1.0) if "NAS" in text else np.array([1.0, 0.0, 0.0, 0.0]))
         for chunk_id, text in attente]
    )
    assert ecrits == 2
    assert store.count_chunks_without_vectors(4) == 0

    hits = store.search_semantic(np.full(4, 1.0), limit=5)
    assert hits and "NAS" in hits[0].text


def test_rattrapage_preserve_l_index_lexical(store: DocumentStore):
    """Le trigger FTS est restreint à « text » : ces écritures ne doivent rien casser."""
    add_sans_vecteur(store, "brut.txt", ["procédure de sauvegarde hebdomadaire"])
    attente = store.chunks_without_vectors(4, limit=10)
    store.set_chunk_embeddings([(attente[0][0], np.full(4, 1.0))])
    resultats = store.search_lexical("sauvegarde", limit=5)
    assert len(resultats) == 1


def test_modification_du_texte_resynchronise_le_fts(store: DocumentStore):
    """Garde-fou : restreindre le trigger ne doit pas désynchroniser l'index."""
    add_sans_vecteur(store, "brut.txt", ["ancienne formulation"])
    chunk_id = store.chunks_without_vectors(4, limit=1)[0][0]
    with store._lock, store._conn:
        store._conn.execute(
            "UPDATE chunks SET text = ? WHERE id = ?", ("nouvelle formulation", chunk_id)
        )
    assert store.search_lexical("ancienne", limit=5) == []
    assert len(store.search_lexical("nouvelle", limit=5)) == 1


def test_vecteurs_absents_sont_ignores(store: DocumentStore):
    add_sans_vecteur(store, "brut.txt", ["texte"])
    chunk_id = store.chunks_without_vectors(4, limit=1)[0][0]
    assert store.set_chunk_embeddings([(chunk_id, None)]) == 0
