"""Persistance de la clé de signature entre deux démarrages.

Une clé tirée au hasard à chaque lancement invalidait silencieusement les
appareils de confiance : après chaque mise à jour du conteneur, DSM réclamait
de nouveau un code 2FA alors que l'utilisateur avait coché « mémoriser ».
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings
from app.security import SessionStore


@pytest.fixture(autouse=True)
def sans_cle_denvironnement(monkeypatch):
    """conftest impose APP_SECRET à toute la suite ; ici on teste son absence."""
    monkeypatch.delenv("APP_SECRET", raising=False)


def reglages(tmp_path, **extra) -> Settings:
    return Settings(data_dir=tmp_path, _env_file=None, **extra)


def test_la_cle_est_conservee_entre_deux_demarrages(tmp_path):
    premier = reglages(tmp_path)
    premier.ensure_secret()

    second = reglages(tmp_path)
    second.ensure_secret()

    assert premier.app_secret
    assert second.app_secret == premier.app_secret
    assert premier.secret_path.exists()


def test_deux_installations_distinctes_ont_des_cles_differentes(tmp_path):
    """Un secret identique partout rendrait les cookies rejouables d'un NAS à l'autre."""
    un = reglages(tmp_path / "a")
    un.ensure_secret()
    deux = reglages(tmp_path / "b")
    deux.ensure_secret()

    assert un.app_secret != deux.app_secret


def test_un_appareil_memorise_survit_au_redemarrage(tmp_path):
    """Le scénario réel : mise à jour du conteneur, puis reconnexion sans 2FA."""
    avant = reglages(tmp_path)
    avant.ensure_secret()
    cookie = SessionStore(avant.app_secret).sign_device("marie", "jeton-dsm-abc")

    apres = reglages(tmp_path)
    apres.ensure_secret()

    assert SessionStore(apres.app_secret).read_device("marie", cookie) == "jeton-dsm-abc"


def test_une_cle_fournie_par_lenvironnement_lemporte(tmp_path):
    """Seul moyen de partager la même clé entre plusieurs instances."""
    settings = reglages(tmp_path, app_secret="cle-explicite")
    settings.ensure_secret()

    assert settings.app_secret == "cle-explicite"
    assert not settings.secret_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows n'expose pas les droits POSIX")
def test_la_cle_nest_pas_lisible_par_les_autres_comptes(tmp_path):
    """Ce fichier signe les sessions : il ne doit pas fuiter vers un autre compte."""
    settings = reglages(tmp_path)
    settings.ensure_secret()

    assert settings.secret_path.exists()
    mode = settings.secret_path.stat().st_mode & 0o077
    assert mode == 0, "le fichier de clé ne doit pas être lisible par autrui"


def test_un_volume_en_lecture_seule_ne_bloque_pas_le_demarrage(tmp_path, monkeypatch):
    """Mieux vaut des sessions éphémères qu'une application qui refuse de démarrer."""
    settings = reglages(tmp_path)

    def interdit(*args, **kwargs):
        raise OSError("volume en lecture seule")

    monkeypatch.setattr("pathlib.Path.write_text", interdit)
    settings.ensure_secret()

    assert settings.app_secret  # une clé reste disponible, simplement non persistée


def test_une_cle_vide_est_refusee():
    """Un HMAC à clé vide est recalculable par n'importe qui."""
    with pytest.raises(ValueError):
        SessionStore("")
