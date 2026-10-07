"""Réglages persistants et chaîne de moteurs avec repli."""

from __future__ import annotations

import json

import pytest

from app import overrides
from app.llm.base import LLMBackend, LLMUnavailable
from app.llm.chain import ChainBackend


class FauxSettings:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.llm_backend = "auto"
        self.ollama_url = ""
        self.llm_model = ""
        self.llm_timeout_seconds = 120


# ------------------------------------------------------------------ réglages
def test_enregistre_puis_relit(tmp_path):
    settings = FauxSettings(tmp_path)
    overrides.enregistrer(settings, {"ollama_url": "http://192.168.1.10:11434/"})

    assert settings.ollama_url == "http://192.168.1.10:11434"  # barre finale retirée

    relu = FauxSettings(tmp_path)
    overrides.appliquer(relu)
    assert relu.ollama_url == "http://192.168.1.10:11434"


def test_refuse_une_cle_non_autorisee(tmp_path):
    settings = FauxSettings(tmp_path)
    with pytest.raises(ValueError):
        overrides.enregistrer(settings, {"dsm_service_password": "secret"})


@pytest.mark.parametrize(
    "adresse",
    ["file:///etc/passwd", "ftp://machine:11434", "machine:11434", "http://"],
)
def test_refuse_une_adresse_douteuse(tmp_path, adresse):
    """Un schéma libre permettrait de faire lire des fichiers locaux au NAS."""
    settings = FauxSettings(tmp_path)
    with pytest.raises(ValueError):
        overrides.enregistrer(settings, {"ollama_url": adresse})


def test_un_fichier_illisible_ne_bloque_pas_le_demarrage(tmp_path):
    (tmp_path / overrides.NOM_FICHIER).write_text("{ ceci n'est pas du json", encoding="utf-8")
    settings = FauxSettings(tmp_path)
    assert overrides.appliquer(settings) == {}
    assert settings.ollama_url == ""


def test_un_reglage_devenu_invalide_est_ignore(tmp_path):
    (tmp_path / overrides.NOM_FICHIER).write_text(
        json.dumps({"llm_backend": "moteur-inexistant"}), encoding="utf-8"
    )
    settings = FauxSettings(tmp_path)
    assert overrides.appliquer(settings) == {}
    assert settings.llm_backend == "auto"


# --------------------------------------------------------------------- chaîne
class FauxMoteur(LLMBackend):
    def __init__(self, nom: str, jetons: list[str] | None = None, panne: str = "") -> None:
        self.name = nom
        self.model = f"modele-{nom}"
        self._jetons = jetons or []
        self._panne = panne
        self.appele = False

    @property
    def available(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def health(self) -> bool:
        return not self._panne

    async def stream(self, messages, *, temperature=0.2, max_tokens=700):
        self.appele = True
        if self._panne == "avant":
            raise LLMUnavailable(f"{self.name} injoignable")
        for jeton in self._jetons:
            yield jeton
        if self._panne == "pendant":
            raise LLMUnavailable(f"{self.name} coupé en cours")


async def _collecter(chaine: ChainBackend) -> list[str]:
    return [jeton async for jeton in chaine.stream([{"role": "user", "content": "?"}])]


async def test_bascule_sur_le_moteur_suivant_si_le_premier_est_injoignable():
    """Le PC qui héberge Ollama peut être éteint : la réponse ne doit pas être perdue."""
    distant = FauxMoteur("ollama", panne="avant")
    local = FauxMoteur("llamacpp", ["bon", "jour"])
    chaine = ChainBackend([distant, local])

    assert await _collecter(chaine) == ["bon", "jour"]
    assert local.appele
    assert chaine.name == "llamacpp"  # le bandeau doit annoncer le vrai moteur


async def test_n_essaie_pas_le_suivant_une_fois_la_reponse_commencee():
    """Repartir d'un autre modèle ferait se réécrire une réponse déjà affichée."""
    distant = FauxMoteur("ollama", ["déb", "ut"], panne="pendant")
    local = FauxMoteur("llamacpp", ["autre"])
    chaine = ChainBackend([distant, local])

    with pytest.raises(LLMUnavailable):
        await _collecter(chaine)
    assert not local.appele


async def test_le_moteur_prefere_reste_prioritaire():
    distant = FauxMoteur("ollama", ["via-pc"])
    local = FauxMoteur("llamacpp", ["via-nas"])
    chaine = ChainBackend([distant, local])

    assert await _collecter(chaine) == ["via-pc"]
    assert not local.appele


async def test_tous_en_panne_remonte_l_erreur():
    chaine = ChainBackend([FauxMoteur("ollama", panne="avant"), FauxMoteur("b", panne="avant")])
    with pytest.raises(LLMUnavailable):
        await _collecter(chaine)


# ------------------------------------------------------------------- sondage
async def test_le_redacteur_annonce_le_moteur_distant_quand_il_repond():
    """La longueur du prompt se décide avant la recherche : il faut savoir qui rédige."""
    distant = FauxMoteur("ollama", ["ok"])
    local = FauxMoteur("llamacpp", ["ok"])
    chaine = ChainBackend([distant, local])

    assert (await chaine.redacteur()).name == "ollama"


async def test_le_redacteur_designe_le_local_si_le_pc_est_eteint():
    distant = FauxMoteur("ollama", panne="avant")
    local = FauxMoteur("llamacpp", ["ok"])
    chaine = ChainBackend([distant, local])

    assert (await chaine.redacteur()).name == "llamacpp"


async def test_un_sondage_recent_evite_de_rappeler_le_moteur_eteint():
    """Réessayer un PC éteint à chaque question ferait attendre pour rien."""
    distant = FauxMoteur("ollama", panne="avant")
    local = FauxMoteur("llamacpp", ["ok"])
    chaine = ChainBackend([distant, local])

    assert (await chaine.redacteur()).name == "llamacpp"
    distant.appele = False
    assert await _collecter(chaine) == ["ok"]
    assert not distant.appele
