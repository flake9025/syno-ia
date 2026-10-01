"""Sélection des moteurs d'embeddings."""

from __future__ import annotations

import numpy as np
import pytest

from app.hardware import EMBEDDING_BY_PROFILE
from app.rag.embedder import (
    DEFAULT_FASTEMBED_MODEL,
    DEFAULT_MODEL2VEC_MODEL,
    Embedder,
    NullEmbedder,
    _unit,
    build_embedder,
    is_fastembed_model,
)


def test_backend_desactive():
    embedder = build_embedder("none", "")
    assert isinstance(embedder, NullEmbedder)
    assert not embedder.available
    assert embedder.embed_query("question") is None
    assert embedder.embed_documents(["a", "b"]) == [None, None]


def test_repli_sur_moteur_absent():
    """Un moteur inutilisable ne doit jamais empêcher l'application de démarrer."""
    embedder = build_embedder("fastembed", "modele/inexistant-xyz")
    assert isinstance(embedder, Embedder)
    # model2vec peut être indisponible hors ligne : le repli ultime est NullEmbedder.
    assert embedder.backend in {"model2vec", "none"}


@pytest.mark.parametrize(
    "model", [model for backend, model in EMBEDDING_BY_PROFILE.values() if backend == "fastembed"]
)
def test_les_modeles_fastembed_du_catalogue_existent(model: str):
    """fastembed refuse tout modèle hors de sa propre liste : on la vérifie ici."""
    fastembed = pytest.importorskip("fastembed")
    supported = {entry["model"] for entry in fastembed.TextEmbedding.list_supported_models()}
    assert model in supported


def test_le_modele_fastembed_par_defaut_existe():
    fastembed = pytest.importorskip("fastembed")
    supported = {entry["model"] for entry in fastembed.TextEmbedding.list_supported_models()}
    assert DEFAULT_FASTEMBED_MODEL in supported


def test_les_profils_couvrent_les_quatre_niveaux():
    assert set(EMBEDDING_BY_PROFILE) == {"micro", "small", "medium", "large"}
    assert all(backend in {"model2vec", "fastembed"} for backend, _ in EMBEDDING_BY_PROFILE.values())
    assert EMBEDDING_BY_PROFILE["micro"][1] == DEFAULT_MODEL2VEC_MODEL


def test_description_du_moteur_inactif():
    description = NullEmbedder("hors ligne").describe()
    assert description["backend"] == "none"
    assert description["reason"] == "hors ligne"
    assert description["dimension"] == 0


def test_le_moteur_inactif_ne_casse_pas_numpy():
    """Le reste du code doit pouvoir traiter l'absence de vecteur sans exception."""
    vecteurs = NullEmbedder().embed_documents(["x"])
    assert np.asarray([v for v in vecteurs if v is not None], dtype=np.float32).size == 0


def test_normalisation_en_vecteur_unitaire():
    """Les modèles ONNX type MiniLM ne normalisent pas : le moteur doit le faire."""
    brut = np.array([3.0, 4.0], dtype=np.float32)
    unitaire = _unit(brut)
    assert float(np.linalg.norm(unitaire)) == pytest.approx(1.0)
    assert float(np.dot(unitaire, unitaire)) == pytest.approx(1.0)


def test_normalisation_dun_vecteur_nul():
    assert np.allclose(_unit(np.zeros(4, dtype=np.float32)), 0.0)


def test_detection_du_catalogue_fastembed():
    pytest.importorskip("fastembed")
    assert is_fastembed_model(DEFAULT_FASTEMBED_MODEL)
    assert not is_fastembed_model("intfloat/multilingual-e5-small")
    assert not is_fastembed_model("modele/inexistant-xyz")


def test_auto_privilegie_le_moteur_statique():
    """potion figure dans les deux catalogues : sans AVX, le statique gagne."""
    pytest.importorskip("fastembed")
    assert is_fastembed_model(DEFAULT_MODEL2VEC_MODEL)
    embedder = build_embedder("auto", DEFAULT_MODEL2VEC_MODEL)
    assert embedder.backend in {"model2vec", "none"}
