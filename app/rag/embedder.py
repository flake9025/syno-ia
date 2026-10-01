"""Calcul des embeddings, avec plusieurs moteurs sélectionnés selon le matériel.

* **model2vec** — embeddings *statiques* distillés : une simple table de
  correspondance token → vecteur, moyennée. Aucun réseau de neurones n'est
  exécuté, donc quelques millisecondes par document même sur un Celeron sans
  AVX. C'est le choix par défaut sur un DS218+.
* **fastembed** — inférence ONNX de vrais encodeurs transformeurs (MiniLM, e5) :
  nettement plus précis, mais demande davantage de RAM et de CPU. Seuls les
  modèles du catalogue `TextEmbedding.list_supported_models()` sont acceptés.
* **none** — aucun embedding : la recherche se limite alors à BM25 (FTS5).
  L'application reste pleinement fonctionnelle.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Sequence
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

#: Modèles e5 : ils attendent un préfixe explicite sur les requêtes/passages.
_E5_PREFIXES = ("e5",)

#: Replis utilisés lorsqu'aucun modèle n'est imposé par la configuration.
DEFAULT_MODEL2VEC_MODEL = "minishlab/potion-multilingual-128M"
#: Attention : `fastembed` n'accepte que les modèles de son catalogue
#: (`TextEmbedding.list_supported_models()`). `multilingual-e5-small`, par
#: exemple, n'en fait pas partie.
DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _unit(vector: np.ndarray) -> np.ndarray:
    """Renvoie un vecteur unitaire : les scores de similarité sont des cosinus."""
    vector = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


class Embedder:
    """Interface commune aux moteurs d'embeddings.

    Tous les moteurs renvoient des vecteurs **normalisés** (norme L2 = 1) afin
    qu'un simple produit scalaire vaille une similarité cosinus. Certains
    modèles ONNX (`paraphrase-multilingual-*`) ne normalisent pas leur sortie.
    """

    backend = "none"
    model_name = ""
    dimension = 0

    @property
    def available(self) -> bool:
        return self.dimension > 0

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        raise NotImplementedError

    def embed_query(self, text: str) -> np.ndarray | None:
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "backend": self.backend,
            "model": self.model_name,
            "dimension": self.dimension,
            "available": self.available,
        }


class NullEmbedder(Embedder):
    """Moteur inactif : la recherche repose uniquement sur BM25."""

    backend = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return [None] * len(texts)  # type: ignore[list-item]

    def embed_query(self, text: str) -> np.ndarray | None:
        return None

    def describe(self) -> dict:
        data = super().describe()
        data["reason"] = self.reason
        return data


class DeferredEmbedder(Embedder):
    """Moteur chargé en arrière-plan, remplaçable à chaud.

    Le premier démarrage télécharge le modèle d'embeddings : plusieurs minutes
    sur la liaison d'un NAS domestique. Charger ce modèle pendant le démarrage
    retarderait d'autant la disponibilité du service — assez pour que le
    *healthcheck* Docker abandonne et relance le conteneur, qui recommencerait
    le téléchargement depuis le début, indéfiniment.

    L'application démarre donc immédiatement en recherche lexicale seule (BM25),
    pleinement utilisable, et bascule en recherche hybride dès que le moteur est
    prêt. `pipeline` et `indexer` consultent `available` à chaque appel : la
    bascule ne demande aucune reconstruction de ces composants.
    """

    def __init__(self, reason: str = "chargement en arrière-plan") -> None:
        self._delegate: Embedder = NullEmbedder(reason)
        self._lock = threading.Lock()
        #: pending → loading → ready, ou failed / disabled.
        self.state = "pending"

    # Les attributs sont relus à chaque accès : ils suivent le moteur courant.
    @property
    def backend(self) -> str:  # type: ignore[override]
        return self._delegate.backend

    @property
    def model_name(self) -> str:  # type: ignore[override]
        return self._delegate.model_name

    @property
    def dimension(self) -> int:  # type: ignore[override]
        return self._delegate.dimension

    @property
    def available(self) -> bool:
        return self._delegate.available

    def mark_loading(self) -> None:
        self.state = "loading"

    def adopt(self, embedder: Embedder) -> None:
        """Installe le moteur réellement chargé (ou son repli)."""
        with self._lock:
            self._delegate = embedder
            self.state = "ready" if embedder.available else "failed"

    def disable(self, reason: str) -> None:
        with self._lock:
            self._delegate = NullEmbedder(reason)
            self.state = "disabled"

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        return self._delegate.embed_documents(texts)

    def embed_query(self, text: str) -> np.ndarray | None:
        return self._delegate.embed_query(text)

    def describe(self) -> dict:
        data = self._delegate.describe()
        data["state"] = self.state
        return data


class Model2VecEmbedder(Embedder):
    backend = "model2vec"

    def __init__(self, model_name: str, cache_dir: Path | None = None) -> None:
        from model2vec import StaticModel

        if cache_dir:
            os.environ.setdefault("HF_HOME", str(cache_dir))
        self.model_name = model_name
        self._lock = threading.Lock()
        self._model = StaticModel.from_pretrained(model_name)
        probe = self._model.encode(["test"])
        self.dimension = int(np.asarray(probe).shape[-1])

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        with self._lock:
            vectors = np.asarray(self._model.encode(list(texts)), dtype=np.float32)
        return [_unit(vectors[i]) for i in range(vectors.shape[0])]

    def embed_query(self, text: str) -> np.ndarray | None:
        return self.embed_documents([text])[0]


class FastEmbedEmbedder(Embedder):
    backend = "fastembed"

    def __init__(self, model_name: str, cache_dir: Path | None = None, threads: int = 0) -> None:
        from fastembed import TextEmbedding

        supported = {entry["model"] for entry in TextEmbedding.list_supported_models()}
        if model_name not in supported:
            raise ValueError(
                f"« {model_name} » ne fait pas partie du catalogue fastembed. "
                "Choisissez un modèle listé par TextEmbedding.list_supported_models()."
            )
        self.model_name = model_name
        self._needs_prefix = any(marker in model_name.lower() for marker in _E5_PREFIXES)
        self._lock = threading.Lock()
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=str(cache_dir) if cache_dir else None,
            threads=threads or None,
        )
        probe = list(self._model.embed(["test"]))
        self.dimension = int(np.asarray(probe[0]).shape[-1])

    def _prefixed(self, texts: Sequence[str], kind: str) -> list[str]:
        if not self._needs_prefix:
            return list(texts)
        return [f"{kind}: {text}" for text in texts]

    def embed_documents(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        with self._lock:
            vectors = list(self._model.embed(self._prefixed(texts, "passage")))
        return [_unit(vector) for vector in vectors]

    def embed_query(self, text: str) -> np.ndarray | None:
        with self._lock:
            vectors = list(self._model.embed(self._prefixed([text], "query")))
        return _unit(vectors[0]) if vectors else None


def is_fastembed_model(model_name: str) -> bool:
    """`fastembed` n'accepte que les modèles de son propre catalogue."""
    try:
        from fastembed import TextEmbedding
    except Exception:
        return False
    return any(entry["model"] == model_name for entry in TextEmbedding.list_supported_models())


def plan_attempts(backend: str, model_name: str) -> list[tuple[str, str]]:
    """Ordonne les couples (moteur, modèle) à essayer, du préféré au repli.

    Fonction pure : elle n'instancie rien, ce qui la rend testable hors ligne.
    """
    backend = (backend or "auto").lower()
    if backend == "none":
        return []
    if backend == "model2vec":
        return [("model2vec", model_name or DEFAULT_MODEL2VEC_MODEL)]
    if backend == "fastembed":
        return [
            ("fastembed", model_name or DEFAULT_FASTEMBED_MODEL),
            ("model2vec", DEFAULT_MODEL2VEC_MODEL),
        ]
    # « auto » : normalement le profil matériel a déjà tranché (cf.
    # services.build_context). En dernier recours, on privilégie le moteur le
    # plus léger — certains modèles (potion) figurent dans les deux catalogues,
    # et la variante statique est bien plus rapide sans AVX.
    attempts = [("model2vec", model_name or DEFAULT_MODEL2VEC_MODEL)]
    if model_name and is_fastembed_model(model_name):
        attempts.append(("fastembed", model_name))
    attempts.append(("model2vec", DEFAULT_MODEL2VEC_MODEL))
    return attempts


def build_embedder(
    backend: str,
    model_name: str,
    *,
    cache_dir: Path | None = None,
    threads: int = 0,
) -> Embedder:
    """Instancie le moteur demandé, avec repli progressif en cas d'échec."""
    if (backend or "auto").lower() == "none":
        return NullEmbedder("désactivé par configuration")

    errors: list[str] = []
    seen: set[tuple[str, str]] = set()
    for engine, name in plan_attempts(backend, model_name):
        if (engine, name) in seen:
            continue
        seen.add((engine, name))
        try:
            if engine == "model2vec":
                embedder = Model2VecEmbedder(name, cache_dir=cache_dir)
            else:
                embedder = FastEmbedEmbedder(name, cache_dir=cache_dir, threads=threads)
            logger.info(
                "Embeddings : %s / %s (dimension %d)",
                engine,
                embedder.model_name,
                embedder.dimension,
            )
            return embedder
        except Exception as exc:
            errors.append(f"{engine}: {exc}")
            logger.warning("Moteur d'embeddings %s indisponible (%s)", engine, exc)

    logger.warning("Aucun moteur d'embeddings disponible — recherche lexicale seule (BM25)")
    return NullEmbedder("; ".join(errors) or "aucun moteur disponible")
