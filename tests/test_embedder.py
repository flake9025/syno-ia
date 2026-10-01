"""Sélection des moteurs d'embeddings.

Les tests qui téléchargent réellement un modèle sont marqués `network` et
exclus de l'intégration continue (`-m "not network"`).
"""

from __future__ import annotations

import numpy as np
import pytest

from app.hardware import EMBEDDING_BY_PROFILE
from app.rag import embedder as embedder_module
from app.rag.embedder import (
    DEFAULT_FASTEMBED_MODEL,
    DEFAULT_MODEL2VEC_MODEL,
    NullEmbedder,
    _unit,
    build_embedder,
    is_fastembed_model,
    plan_attempts,
)

# --------------------------------------------------------------- catalogue


@pytest.mark.parametrize(
    "model", [model for backend, model in EMBEDDING_BY_PROFILE.values() if backend == "fastembed"]
)
def test_les_modeles_fastembed_du_catalogue_existent(model: str):
    """fastembed refuse tout modèle hors de sa propre liste : on la vérifie ici."""
    pytest.importorskip("fastembed")
    assert is_fastembed_model(model)


def test_le_modele_fastembed_par_defaut_existe():
    pytest.importorskip("fastembed")
    assert is_fastembed_model(DEFAULT_FASTEMBED_MODEL)


def test_modele_hors_catalogue_refuse():
    pytest.importorskip("fastembed")
    assert not is_fastembed_model("intfloat/multilingual-e5-small")
    assert not is_fastembed_model("modele/inexistant-xyz")


def test_les_profils_couvrent_les_quatre_niveaux():
    assert set(EMBEDDING_BY_PROFILE) == {"micro", "small", "medium", "large"}
    assert all(backend in {"model2vec", "fastembed"} for backend, _ in EMBEDDING_BY_PROFILE.values())
    assert EMBEDDING_BY_PROFILE["micro"][1] == DEFAULT_MODEL2VEC_MODEL


# ------------------------------------------------------ plan de sélection


def test_plan_moteur_desactive():
    assert plan_attempts("none", "") == []


def test_plan_model2vec_sans_repli_lourd():
    assert plan_attempts("model2vec", "") == [("model2vec", DEFAULT_MODEL2VEC_MODEL)]


def test_plan_fastembed_replie_sur_statique():
    plan = plan_attempts("fastembed", "")
    assert plan[0] == ("fastembed", DEFAULT_FASTEMBED_MODEL)
    assert plan[-1] == ("model2vec", DEFAULT_MODEL2VEC_MODEL)


def test_plan_auto_privilegie_le_moteur_statique():
    """potion figure dans les deux catalogues : sans AVX, le statique gagne."""
    pytest.importorskip("fastembed")
    assert is_fastembed_model(DEFAULT_MODEL2VEC_MODEL)
    assert plan_attempts("auto", DEFAULT_MODEL2VEC_MODEL)[0][0] == "model2vec"


def test_plan_auto_ignore_fastembed_hors_catalogue():
    pytest.importorskip("fastembed")
    assert all(engine == "model2vec" for engine, _ in plan_attempts("auto", "modele/inexistant"))


# ------------------------------------------------------------ instanciation


def test_backend_desactive():
    embedder = build_embedder("none", "")
    assert isinstance(embedder, NullEmbedder)
    assert not embedder.available
    assert embedder.embed_query("question") is None
    assert embedder.embed_documents(["a", "b"]) == [None, None]


def test_repli_ultime_si_aucun_moteur_ne_demarre(monkeypatch):
    """Une panne de tous les moteurs ne doit jamais empêcher le démarrage."""

    def boom(*args, **kwargs):
        raise RuntimeError("moteur indisponible")

    monkeypatch.setattr(embedder_module, "Model2VecEmbedder", boom)
    monkeypatch.setattr(embedder_module, "FastEmbedEmbedder", boom)

    embedder = build_embedder("fastembed", DEFAULT_FASTEMBED_MODEL)
    assert isinstance(embedder, NullEmbedder)
    assert not embedder.available
    assert "moteur indisponible" in embedder.describe()["reason"]


def test_repli_du_premier_moteur_vers_le_second(monkeypatch):
    class FauxStatique(NullEmbedder):
        backend = "model2vec"
        dimension = 256

        def __init__(self, model_name, cache_dir=None):
            self.model_name = model_name

    def boom(*args, **kwargs):
        raise RuntimeError("ONNX absent")

    monkeypatch.setattr(embedder_module, "FastEmbedEmbedder", boom)
    monkeypatch.setattr(embedder_module, "Model2VecEmbedder", FauxStatique)

    embedder = build_embedder("fastembed", DEFAULT_FASTEMBED_MODEL)
    assert embedder.backend == "model2vec"
    assert embedder.model_name == DEFAULT_MODEL2VEC_MODEL


def test_aucun_essai_redondant(monkeypatch):
    """Le repli ne doit pas retenter un couple (moteur, modèle) déjà échoué."""
    essais: list[tuple[str, str]] = []

    def trace_statique(model_name, cache_dir=None):
        essais.append(("model2vec", model_name))
        raise RuntimeError("ko")

    monkeypatch.setattr(embedder_module, "Model2VecEmbedder", trace_statique)
    build_embedder("model2vec", DEFAULT_MODEL2VEC_MODEL)
    assert essais == [("model2vec", DEFAULT_MODEL2VEC_MODEL)]


# --------------------------------------------------------------- vecteurs


def test_normalisation_en_vecteur_unitaire():
    """Les modèles ONNX type MiniLM ne normalisent pas : le moteur doit le faire."""
    unitaire = _unit(np.array([3.0, 4.0], dtype=np.float32))
    assert float(np.linalg.norm(unitaire)) == pytest.approx(1.0)
    assert float(np.dot(unitaire, unitaire)) == pytest.approx(1.0)


def test_normalisation_dun_vecteur_nul():
    assert np.allclose(_unit(np.zeros(4, dtype=np.float32)), 0.0)


def test_description_du_moteur_inactif():
    description = NullEmbedder("hors ligne").describe()
    assert description["backend"] == "none"
    assert description["reason"] == "hors ligne"
    assert description["dimension"] == 0


def test_le_moteur_inactif_ne_casse_pas_numpy():
    """Le reste du code doit pouvoir traiter l'absence de vecteur sans exception."""
    vecteurs = NullEmbedder().embed_documents(["x"])
    assert np.asarray([v for v in vecteurs if v is not None], dtype=np.float32).size == 0


# ------------------------------------------- intégration (téléchargements)


@pytest.mark.network
@pytest.mark.parametrize(
    ("backend", "model"),
    [("model2vec", DEFAULT_MODEL2VEC_MODEL), ("fastembed", DEFAULT_FASTEMBED_MODEL)],
)
def test_vecteurs_unitaires_et_classement_correct(backend: str, model: str):
    embedder = build_embedder(backend, model)
    assert embedder.backend == backend

    documents = embedder.embed_documents(
        ["Les congés payés sont de 25 jours par an.", "Recette de la tarte aux pommes."]
    )
    question = embedder.embed_query("combien de jours de congés ?")

    for vecteur in [*documents, question]:
        assert float(np.linalg.norm(vecteur)) == pytest.approx(1.0, abs=1e-4)
    assert np.dot(question, documents[0]) > np.dot(question, documents[1])
