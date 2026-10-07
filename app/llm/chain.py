"""Chaîne de moteurs avec repli automatique.

Un Ollama hébergé sur un PC de bureau est bien plus rapide que le NAS, mais il
n'est pas toujours allumé. Vérifier sa présence au démarrage ne suffit donc pas :
la bascule doit se décider **au moment de la question**.

Le repli n'a lieu que si le moteur préféré échoue *avant d'avoir produit le
moindre jeton*. Une fois le texte commencé, on ne recommence pas avec un autre
modèle : l'utilisateur verrait une réponse se réécrire toute seule.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from .base import LLMBackend, LLMUnavailable

logger = logging.getLogger(__name__)

#: Durée pendant laquelle on fait confiance au dernier sondage du moteur préféré.
#: Assez court pour suivre l'allumage du PC, assez long pour ne pas sonder le
#: réseau à chaque question.
CACHE_SONDAGE_SECONDES = 30.0


class ChainBackend(LLMBackend):
    def __init__(self, backends: list[LLMBackend]) -> None:
        if not backends:
            raise ValueError("Une chaîne de moteurs ne peut pas être vide")
        self.backends = backends
        self._active: LLMBackend = backends[0]
        self._sondage_expire = 0.0

    @property
    def name(self) -> str:  # type: ignore[override]
        # Le bandeau doit annoncer le moteur qui répond réellement, pas la chaîne.
        return self._active.name

    async def redacteur(self) -> LLMBackend:
        """Moteur qui rédigera la prochaine réponse, sans rien générer.

        La longueur du prompt doit être décidée **avant** la recherche
        documentaire, et elle dépend entièrement de la machine qui rédige : un PC
        avale 5000 caractères sans broncher là où le NAS y passerait des minutes.
        Il faut donc trancher d'avance, donc sonder.
        """
        if time.monotonic() < self._sondage_expire:
            return self._active

        choisi = self.backends[-1]
        for backend in self.backends:
            if not backend.available:
                continue
            if await backend.health():
                choisi = backend
                break
        self._active = choisi
        self._sondage_expire = time.monotonic() + CACHE_SONDAGE_SECONDES
        return choisi

    @property
    def model(self) -> str:  # type: ignore[override]
        return getattr(self._active, "model", "")

    @property
    def available(self) -> bool:
        return any(backend.available for backend in self.backends)

    async def aclose(self) -> None:
        for backend in self.backends:
            try:
                await backend.aclose()
            except Exception as exc:  # pragma: no cover - fermeture au mieux
                logger.warning("Fermeture de %s : %s", backend.name, exc)

    async def health(self) -> bool:
        for backend in self.backends:
            if await backend.health():
                return True
        return False

    def describe(self) -> dict:
        info = self._active.describe()
        info["chain"] = [backend.name for backend in self.backends]
        return info

    async def stream(
        self, messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 700
    ) -> AsyncIterator[str]:
        utilisables = [backend for backend in self.backends if backend.available]
        if not utilisables:
            raise LLMUnavailable("Aucun moteur de génération disponible")

        # Un sondage récent a déjà écarté les moteurs éteints : inutile de
        # réessayer celui qui vient d'échouer, l'utilisateur attendrait pour rien.
        if time.monotonic() < self._sondage_expire and self._active in utilisables:
            depart = utilisables.index(self._active)
            utilisables = utilisables[depart:]

        derniere: Exception | None = None
        for rang, backend in enumerate(utilisables):
            commence = False
            try:
                flux = backend.stream(messages, temperature=temperature, max_tokens=max_tokens)
                try:
                    async for jeton in flux:
                        if not commence:
                            commence = True
                            self._active = backend
                            if rang:
                                logger.info("Repli sur le moteur %s", backend.name)
                        yield jeton
                finally:
                    await flux.aclose()
                return
            except LLMUnavailable as exc:
                derniere = exc
                if commence:
                    # Panne en cours de rédaction : le texte déjà affiché doit être
                    # conservé tel quel, pas remplacé par celui d'un autre modèle.
                    logger.warning("%s a échoué en cours de réponse : %s", backend.name, exc)
                    raise
                logger.warning(
                    "%s indisponible (%s)%s",
                    backend.name,
                    exc,
                    " : essai du moteur suivant" if rang + 1 < len(utilisables) else "",
                )

        raise LLMUnavailable(str(derniere) if derniere else "Aucun moteur n'a pu répondre")
