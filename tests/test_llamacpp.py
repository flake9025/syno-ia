"""Garde-fous du moteur llama.cpp : arrêt réel et non-concurrence.

Ces comportements touchent au fil natif et ne se voient pas à l'œil nu ; ils ont
pourtant causé la panne « network error » (un abandon qui ne libérait pas le
processeur), d'où ces tests.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.llm import llamacpp
from app.llm.llamacpp import LlamaCppBackend


def _moteur(tmp_path, monkeypatch, **kwargs) -> LlamaCppBackend:
    monkeypatch.setattr(llamacpp, "llama_cpp_available", lambda: True)
    chemin = tmp_path / "modele.gguf"
    chemin.write_bytes(b"GGUF")
    return LlamaCppBackend(chemin, **kwargs)


class FauxLlama:
    """Imite `create_chat_completion` : un flux de jetons produit lentement."""

    def __init__(self, suivi: dict, jetons: int = 1000, pas: float = 0.005) -> None:
        self.suivi = suivi
        self.jetons = jetons
        self.pas = pas

    def create_chat_completion(self, **_kwargs):
        def generateur():
            with self.suivi["verrou"]:
                self.suivi["en_cours"] += 1
                self.suivi["maximum"] = max(self.suivi["maximum"], self.suivi["en_cours"])
            try:
                for i in range(self.jetons):
                    time.sleep(self.pas)
                    yield {"choices": [{"delta": {"content": f"j{i} "}}]}
            finally:
                with self.suivi["verrou"]:
                    self.suivi["en_cours"] -= 1

        return generateur()


def _suivi() -> dict:
    return {"verrou": threading.Lock(), "en_cours": 0, "maximum": 0}


def _brancher(moteur: LlamaCppBackend, suivi: dict, **kwargs) -> None:
    faux = FauxLlama(suivi, **kwargs)
    moteur._ensure_loaded = lambda: faux  # type: ignore[method-assign]


async def test_abandon_arme_l_interruption_native(tmp_path, monkeypatch):
    """Fermer le flux doit armer le drapeau que consulte llama.cpp.

    C'est ce drapeau qui interrompt le calcul *pendant la lecture du prompt* ;
    sans lui, le fil continuerait à saturer les cœurs du NAS.
    """
    moteur = _moteur(tmp_path, monkeypatch)
    suivi = _suivi()
    _brancher(moteur, suivi)

    flux = moteur.stream([{"role": "user", "content": "bonjour"}])
    assert await flux.__anext__() == "j0 "
    await flux.aclose()

    assert moteur._abort is not None
    assert moteur._abort.is_set(), "l'abandon doit être signalé au moteur natif"

    for _ in range(200):  # le fil doit se retirer, pas rester en fond
        if suivi["en_cours"] == 0:
            break
        await asyncio.sleep(0.01)
    assert suivi["en_cours"] == 0


async def test_une_seule_generation_a_la_fois(tmp_path, monkeypatch):
    """Deux questions simultanées ne doivent pas partager le contexte natif."""
    moteur = _moteur(tmp_path, monkeypatch)
    suivi = _suivi()
    _brancher(moteur, suivi, jetons=5)

    async def consommer():
        morceaux = [jeton async for jeton in moteur.stream([{"role": "user", "content": "?"}])]
        return len(morceaux)

    resultats = await asyncio.gather(consommer(), consommer())

    assert resultats == [5, 5]
    assert suivi["maximum"] == 1, "les générations doivent être sérialisées"


async def test_abandon_avant_demarrage_n_execute_rien(tmp_path, monkeypatch):
    """Un abandon pendant l'attente du verrou ne doit pas lancer de calcul.

    On reproduit la séquence exacte de `_sse_tokens` : annuler l'attente en vol,
    attendre que l'annulation aboutisse, puis seulement fermer le flux.
    """
    moteur = _moteur(tmp_path, monkeypatch)
    suivi = _suivi()
    _brancher(moteur, suivi, jetons=3)

    moteur._generate_lock.acquire()
    try:
        flux = moteur.stream([{"role": "user", "content": "?"}])
        attente = asyncio.ensure_future(flux.__anext__())
        await asyncio.sleep(0.05)  # le fil est bloqué sur le verrou
        attente.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attente
    finally:
        moteur._generate_lock.release()
    await flux.aclose()

    await asyncio.sleep(0.1)
    assert suivi["maximum"] == 0, "aucun calcul ne doit avoir démarré"


def test_le_paquet_ne_depasse_pas_le_contexte(tmp_path, monkeypatch):
    moteur = _moteur(tmp_path, monkeypatch, context_size=512, batch_size=4096)
    assert moteur.batch_size == 512

    defaut = _moteur(tmp_path, monkeypatch, context_size=4096)
    assert defaut.batch_size == 512, "la valeur de référence de llama.cpp"
