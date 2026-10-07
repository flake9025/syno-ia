"""Réglages modifiables depuis l'administration, conservés entre deux démarrages.

Les variables d'environnement décrivent l'**installation** (chemins, compte de
service, secrets). Ce fichier porte au contraire les choix d'**exploitation** que
l'utilisateur doit pouvoir changer sans recréer le conteneur : au premier rang,
l'adresse d'un Ollama distant, qui dépend de son réseau et non de l'image.

Seule une liste blanche de clés est acceptée : tout ce qui touche à la sécurité
ou aux chemins reste sous le contrôle exclusif de l'environnement.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

NOM_FICHIER = "overrides.json"

#: Réglages que l'administration a le droit de modifier.
CLES_AUTORISEES = frozenset({"llm_backend", "ollama_url", "llm_model", "llm_timeout_seconds"})

BACKENDS_VALIDES = frozenset({"auto", "llamacpp", "ollama", "openai", "none"})


def fichier(settings) -> Path:
    return settings.data_dir / NOM_FICHIER


def valider(valeurs: dict[str, Any]) -> dict[str, Any]:
    """Filtre et contrôle les réglages reçus, en rejetant ce qui n'est pas conforme."""
    propres: dict[str, Any] = {}
    for cle, valeur in valeurs.items():
        if cle not in CLES_AUTORISEES:
            raise ValueError(f"Réglage non modifiable : {cle}")

        if cle == "llm_backend":
            texte = str(valeur or "auto").lower().strip()
            if texte not in BACKENDS_VALIDES:
                raise ValueError(f"Moteur inconnu : {texte}")
            propres[cle] = texte

        elif cle == "ollama_url":
            texte = str(valeur or "").strip().rstrip("/")
            if texte:
                analyse = urlparse(texte)
                # Un schéma libre permettrait de faire lire des fichiers locaux au NAS.
                if analyse.scheme not in {"http", "https"} or not analyse.hostname:
                    raise ValueError(
                        "L'adresse doit être de la forme http://machine:11434"
                    )
            propres[cle] = texte

        elif cle == "llm_timeout_seconds":
            try:
                nombre = int(valeur)
            except (TypeError, ValueError) as exc:
                raise ValueError("Le délai doit être un nombre de secondes") from exc
            if not 0 <= nombre <= 3600:
                raise ValueError("Le délai doit être compris entre 0 et 3600 secondes")
            propres[cle] = nombre

        else:  # llm_model
            propres[cle] = str(valeur or "").strip()

    return propres


def charger(settings) -> dict[str, Any]:
    """Lit les réglages enregistrés. Un fichier illisible ne doit jamais empêcher
    le démarrage : on le signale et on repart des valeurs d'environnement."""
    cible = fichier(settings)
    if not cible.exists():
        return {}
    try:
        donnees = json.loads(cible.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Réglages enregistrés illisibles (%s) : valeurs par défaut", exc)
        return {}
    if not isinstance(donnees, dict):
        return {}
    try:
        return valider(donnees)
    except ValueError as exc:
        logger.warning("Réglages enregistrés invalides (%s) : valeurs par défaut", exc)
        return {}


def appliquer(settings) -> dict[str, Any]:
    """Recouvre les valeurs d'environnement par les réglages enregistrés."""
    valeurs = charger(settings)
    for cle, valeur in valeurs.items():
        setattr(settings, cle, valeur)
    if valeurs:
        logger.info("Réglages repris de %s : %s", NOM_FICHIER, ", ".join(sorted(valeurs)))
    return valeurs


def enregistrer(settings, valeurs: dict[str, Any]) -> dict[str, Any]:
    """Valide, applique puis persiste les réglages fournis."""
    propres = valider(valeurs)
    fusion = {**charger(settings), **propres}
    cible = fichier(settings)
    cible.parent.mkdir(parents=True, exist_ok=True)
    # Écriture atomique : une coupure ne doit pas laisser un fichier tronqué que
    # le prochain démarrage refuserait de lire.
    provisoire = cible.with_suffix(".tmp")
    provisoire.write_text(json.dumps(fusion, indent=2, ensure_ascii=False), encoding="utf-8")
    provisoire.replace(cible)
    for cle, valeur in propres.items():
        setattr(settings, cle, valeur)
    return fusion
