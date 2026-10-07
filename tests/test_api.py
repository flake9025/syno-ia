"""Tests de bout en bout de l'API HTTP (DSM simulé, aucun accès réseau)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api import routes_chat
from app.llm.base import LLMUnavailable
from app.main import create_app
from app.rag.store import Chunk, DocumentRecord
from app.synology.client import DSMError
from app.synology.models import DSMSession, ShareInfo

COMPTES = {
    ("alice", "motdepasse"): DSMSession(sid="sid-alice", account="alice"),
    ("admin", "motdepasse"): DSMSession(sid="sid-admin", account="admin", is_admin=True),
}
PARTAGES = {"sid-alice": ["/documents"], "sid-admin": ["/documents", "/rh"]}
LISIBLES = {
    "sid-alice": {"/documents/public.pdf"},
    "sid-admin": {"/documents/public.pdf", "/rh/salaires.xlsx"},
}


#: Comptes exigeant un second facteur, et le jeton d'appareil que DSM leur délivre.
COMPTES_2FA = {"bob": "motdepasse"}
JETON_APPAREIL = "did-bob-123"


async def fake_login(
    account: str,
    password: str,
    otp_code: str | None = None,
    *,
    device_id: str | None = None,
    trust_device: bool = False,
) -> DSMSession:
    if account in COMPTES_2FA:
        if password != COMPTES_2FA[account]:
            raise DSMError(400)
        # DSM n'accepte la connexion que sur présentation du code ou d'un appareil connu.
        if device_id != JETON_APPAREIL and not otp_code:
            raise DSMError(403)
        session = DSMSession(sid=f"sid-{account}", account=account)
        if trust_device and otp_code:
            session.device_id = JETON_APPAREIL
        return session

    session = COMPTES.get((account, password))
    if session is None:
        raise DSMError(400)
    return session


async def fake_list_shares(sid: str) -> list[ShareInfo]:
    return [
        ShareInfo(path=path, name=path.strip("/"), real_path=f"/volume1{path}")
        for path in PARTAGES.get(sid, [])
    ]


async def fake_stat_paths(sid: str, paths) -> dict[str, bool]:
    lisibles = LISIBLES.get(sid, set())
    return {path: path in lisibles for path in paths}


async def fake_logout(sid: str) -> None:
    return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as test_client:
        context = app.state.context
        context.client.login = fake_login
        context.client.list_shares = fake_list_shares
        context.client.stat_paths = fake_stat_paths
        context.client.logout = fake_logout
        for share, name, text in [
            ("/documents", "public.pdf", "La procédure de sauvegarde du NAS est hebdomadaire."),
            ("/rh", "salaires.xlsx", "Grille confidentielle des salaires 2024."),
        ]:
            context.store.upsert_document(
                DocumentRecord(
                    real_path=f"/volume1{share}/{name}",
                    dsm_path=f"{share}/{name}",
                    share=share,
                    name=name,
                    ext=".pdf",
                    size=1,
                    mtime=1,
                    content_hash="h",
                ),
                [Chunk(text=text, ordinal=0, location="page 1", embedding=np.zeros(3))],
            )
        yield test_client


def connexion(client: TestClient, account: str = "alice", password: str = "motdepasse"):
    return client.post("/api/auth/login", json={"account": account, "password": password})


# ------------------------------------------------------------------ santé
def test_health(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}


def test_info_expose_le_profil_materiel(client: TestClient):
    payload = client.get("/api/info").json()
    assert payload["profile"] in {"micro", "small", "medium", "large"}
    assert "version" in payload


def test_interface_web_servie(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Content-Security-Policy" in response.headers


# ------------------------------------------------------- authentification
def test_connexion_et_cookie(client: TestClient):
    response = connexion(client)
    assert response.status_code == 200
    body = response.json()
    assert body["account"] == "alice"
    assert body["shares"] == ["/documents"]
    cookie = client.cookies.get("syno_ia_session")
    assert cookie and "sid-alice" not in cookie  # le sid DSM ne sort jamais du serveur


def test_mauvais_mot_de_passe(client: TestClient):
    assert connexion(client, password="faux").status_code == 401


def test_acces_refuse_sans_session(client: TestClient):
    assert client.post("/api/chat", json={"question": "test"}).status_code == 401
    assert client.get("/api/documents").status_code == 401


def test_me_anonyme(client: TestClient):
    assert client.get("/api/auth/me").json()["authenticated"] is False


def test_deconnexion(client: TestClient):
    connexion(client)
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").json()["authenticated"] is False


def test_cookie_falsifie_rejete(client: TestClient):
    client.cookies.set("syno_ia_session", "jeton.bidon")
    assert client.get("/api/auth/me").json()["authenticated"] is False


# ------------------------------------------------- appareil de confiance
def connexion_2fa(client: TestClient, **extra):
    charge = {"account": "bob", "password": "motdepasse"}
    charge.update(extra)
    return client.post("/api/auth/login", json=charge)


def test_2fa_exigee_sans_appareil_connu(client: TestClient):
    assert connexion_2fa(client).status_code == 428


def test_appareil_memorise_dispense_du_code(client: TestClient):
    """Après une approbation explicite, le code n'est plus réclamé."""
    reponse = connexion_2fa(client, otp_code="123456", trust_device=True)
    assert reponse.status_code == 200
    assert reponse.json()["device_trusted"] is True
    assert client.cookies.get("syno_ia_device")

    client.post("/api/auth/logout")
    # Le cookie d'appareil survit à la déconnexion : c'est tout l'intérêt.
    suivante = connexion_2fa(client)
    assert suivante.status_code == 200
    assert suivante.json()["device_trusted"] is True


def test_sans_approbation_aucun_appareil_memorise(client: TestClient):
    reponse = connexion_2fa(client, otp_code="123456")
    assert reponse.status_code == 200
    assert reponse.json()["device_trusted"] is False
    assert not client.cookies.get("syno_ia_device")
    client.post("/api/auth/logout")
    assert connexion_2fa(client).status_code == 428


def test_decocher_la_case_oublie_lappareil(client: TestClient):
    connexion_2fa(client, otp_code="123456", trust_device=True)
    client.post("/api/auth/logout")

    oubli = connexion_2fa(client, otp_code="123456", trust_device=False)
    assert oubli.status_code == 200
    assert oubli.json()["device_trusted"] is False
    client.post("/api/auth/logout")
    assert connexion_2fa(client).status_code == 428


def test_jeton_dappareil_non_transferable(client: TestClient):
    """Le cookie d'un compte ne doit pas dispenser un autre compte du second facteur."""
    connexion_2fa(client, otp_code="123456", trust_device=True)
    vole = client.cookies.get("syno_ia_device")
    client.post("/api/auth/logout")

    context = client.app.state.context
    assert context.sessions.read_device("bob", vole) == JETON_APPAREIL
    assert context.sessions.read_device("alice", vole) is None


def test_cookie_dappareil_falsifie_ignore(client: TestClient):
    client.cookies.set("syno_ia_device", "charge.bidon")
    assert connexion_2fa(client).status_code == 428


# ------------------------------------------------------------------- RAG
def test_recherche_respecte_les_droits(client: TestClient):
    connexion(client)
    resultats = client.get("/api/search", params={"q": "salaires"}).json()
    assert resultats["sources"] == []

    autorises = client.get("/api/search", params={"q": "sauvegarde"}).json()
    assert [source["name"] for source in autorises["sources"]] == ["public.pdf"]


def test_admin_voit_davantage(client: TestClient):
    connexion(client, "admin")
    resultats = client.get("/api/search", params={"q": "salaires"}).json()
    assert [source["name"] for source in resultats["sources"]] == ["salaires.xlsx"]


def test_liste_des_documents_filtree(client: TestClient):
    connexion(client)
    documents = client.get("/api/documents").json()["documents"]
    assert [doc["name"] for doc in documents] == ["public.pdf"]


def test_totaux_documents_limites_aux_partages_autorises(client: TestClient):
    """Le bandeau de l'interface ne doit pas trahir l'existence des partages interdits."""
    connexion(client)
    total_alice = client.get("/api/documents").json()["total"]
    connexion(client, "admin")
    total_admin = client.get("/api/documents").json()["total"]
    assert total_alice["documents"] == 1
    assert total_admin["documents"] > total_alice["documents"]
    assert total_admin["chunks"] > total_alice["chunks"]


def test_health_ne_divulgue_pas_la_taille_de_l_index(client: TestClient):
    """Route publique : aucune statistique d'index ne doit y transiter."""
    payload = client.get("/api/health").json()
    assert "documents" not in payload
    assert "chunks" not in payload


def test_telechargement_interdit(client: TestClient):
    connexion(client)
    response = client.get("/api/document", params={"path": "/rh/salaires.xlsx"})
    assert response.status_code == 403


def test_chat_en_flux(client: TestClient):
    connexion(client)
    with client.stream("POST", "/api/chat", json={"question": "sauvegarde"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        corps = "".join(response.iter_text())
    assert "event: sources" in corps
    assert "event: done" in corps
    assert "public.pdf" in corps
    assert "salaires" not in corps


def test_chat_sans_document_autorise(client: TestClient):
    connexion(client)
    with client.stream("POST", "/api/chat", json={"question": "salaires"}) as response:
        corps = "".join(response.iter_text())
    assert "event: done" in corps


async def test_battement_de_coeur_pendant_un_silence(monkeypatch: pytest.MonkeyPatch):
    """Un flux muet émet des commentaires SSE : sans eux, la connexion serait coupée."""
    monkeypatch.setattr(routes_chat, "HEARTBEAT_SECONDS", 0.01)

    async def generation_lente() -> AsyncIterator[str]:
        await asyncio.sleep(0.08)
        yield "bonjour"

    recus: list[str] = []
    reponse: list[str] = []
    flux = routes_chat._sse_tokens(generation_lente(), reponse, routes_chat._Timing())
    async for morceau in flux:
        recus.append(morceau)

    assert recus.count(": ping\n\n") >= 2
    assert recus[-1] == 'event: token\ndata: {"text": "bonjour"}\n\n'
    assert reponse == ["bonjour"]


async def test_battement_de_coeur_absent_si_les_jetons_affluent(
    monkeypatch: pytest.MonkeyPatch,
):
    """Une génération fluide ne doit produire aucun commentaire parasite."""
    monkeypatch.setattr(routes_chat, "HEARTBEAT_SECONDS", 10.0)

    async def generation_rapide() -> AsyncIterator[str]:
        for mot in ("un", "deux"):
            yield mot

    reponse: list[str] = []
    flux = routes_chat._sse_tokens(generation_rapide(), reponse, routes_chat._Timing())
    recus = [m async for m in flux]

    assert all(not m.startswith(":") for m in recus)
    assert reponse == ["un", "deux"]


async def test_erreur_de_generation_remontee(monkeypatch: pytest.MonkeyPatch):
    """Le relais ne doit pas avaler l'échec du moteur, sinon le repli extractif saute."""
    monkeypatch.setattr(routes_chat, "HEARTBEAT_SECONDS", 0.01)

    async def generation_cassee() -> AsyncIterator[str]:
        yield "debut"
        await asyncio.sleep(0.03)
        raise LLMUnavailable("modèle absent")

    reponse: list[str] = []
    flux = routes_chat._sse_tokens(generation_cassee(), reponse, routes_chat._Timing())
    with pytest.raises(LLMUnavailable):
        async for _ in flux:
            pass
    assert reponse == ["debut"]


async def test_generation_interrompue_au_dela_du_delai():
    """Passé le délai, on garde ce qui a été produit plutôt que d'attendre indéfiniment."""
    import time as _time

    async def generation_interminable() -> AsyncIterator[str]:
        yield "premier"
        while True:
            await asyncio.sleep(0.02)
            yield "encore"

    reponse: list[str] = []
    chrono = routes_chat._Timing()
    flux = routes_chat._sse_tokens(
        generation_interminable(), reponse, chrono, _time.monotonic() + 0.2
    )
    recus = [m async for m in flux]

    assert chrono.truncated is True
    assert chrono.snapshot()["truncated"] is True
    assert reponse[0] == "premier"
    assert len(recus) == len(reponse)


def test_chat_annonce_le_moteur_et_les_durees(client: TestClient):
    """L'utilisateur doit pouvoir vérifier quel moteur a répondu et en combien de temps."""
    connexion(client)
    with client.stream("POST", "/api/chat", json={"question": "sauvegarde"}) as response:
        corps = "".join(response.iter_text())

    evenements = {}
    for bloc in corps.split("\n\n"):
        nom = charge = None
        for ligne in bloc.split("\n"):
            if ligne.startswith("event:"):
                nom = ligne[6:].strip()
            elif ligne.startswith("data:"):
                charge = ligne[5:].strip()
        if nom and charge:
            evenements[nom] = json.loads(charge)

    assert "engine" in evenements["sources"]
    assert evenements["sources"]["retrieval_ms"] >= 0

    final = evenements["done"]
    assert final["engine"]["backend"] == "extractive"
    assert final["timing"]["tokens"] > 0
    assert final["timing"]["total_ms"] >= final["timing"]["first_token_ms"]


class LLMMuet:
    """Moteur qui ne rend jamais la main : simule un NAS qui s'enlise."""

    available = True
    name = "llamacpp"

    def describe(self) -> dict:
        return {"backend": "llamacpp", "model": "modele-lent.gguf"}

    async def aclose(self) -> None:
        return None

    async def stream(self, messages, **kwargs) -> AsyncIterator[str]:
        await asyncio.sleep(3600)
        yield "jamais produit"  # pragma: no cover - le délai tombe avant


def test_delai_depasse_sans_jeton_bascule_en_extractif(client: TestClient):
    """Plutôt qu'une bulle vide, l'utilisateur reçoit les passages trouvés."""
    connexion(client)
    context = client.app.state.context
    context.llm = LLMMuet()
    context.settings.llm_timeout_seconds = 1

    with client.stream("POST", "/api/chat", json={"question": "sauvegarde"}) as response:
        corps = "".join(response.iter_text())

    assert "Les passages trouvés" in corps or "passages trouvés" in corps
    final = json.loads(corps.rsplit("data:", 1)[1].strip())
    assert final["engine"]["backend"] == "extractive"
    assert final["timing"]["truncated"] is False
    assert final["answer"].strip()


def test_sans_llm_la_reponse_est_immediate(client: TestClient):
    """La case décochée doit court-circuiter la rédaction, pas l'attendre."""
    connexion(client)
    context = client.app.state.context
    context.llm = LLMMuet()  # rendrait la main dans une heure si on l'appelait
    context.settings.llm_timeout_seconds = 600

    with client.stream(
        "POST", "/api/chat", json={"question": "sauvegarde", "use_llm": False}
    ) as response:
        corps = "".join(response.iter_text())

    final = json.loads(corps.rsplit("data:", 1)[1].strip())
    assert final["engine"]["backend"] == "extractive"
    assert final["answer"].strip()


# ----------------------------------------------------------------- admin
def test_admin_reserve(client: TestClient):
    connexion(client)
    assert client.get("/api/admin/status").status_code == 403


def test_status_admin(client: TestClient):
    connexion(client, "admin")
    payload = client.get("/api/admin/status").json()
    assert payload["index"]["documents"] == 2
    assert "hardware" in payload


def test_admin_peut_purger_lindex(client: TestClient):
    connexion(client, "admin")
    assert client.post("/api/admin/index/clear").status_code == 200
    connexion(client, "admin")
    assert client.get("/api/admin/status").json()["index"]["documents"] == 0


def test_suppression_modele_libere_lespace(client: TestClient):
    from app.hardware import LLM_BY_PROFILE

    connexion(client, "admin")
    models_dir = client.app.state.context.settings.models_dir
    models_dir.mkdir(parents=True, exist_ok=True)
    fichier = models_dir / LLM_BY_PROFILE["micro"].filename
    fichier.write_bytes(b"gguf" * 64)

    reponse = client.request("DELETE", "/api/admin/models", params={"profile": "micro"})
    assert reponse.status_code == 200
    assert reponse.json()["freed_bytes"] == 256
    assert not fichier.exists()


def test_suppression_modele_absent_ou_inconnu(client: TestClient):
    connexion(client, "admin")
    absent = client.request("DELETE", "/api/admin/models", params={"profile": "large"})
    assert absent.status_code == 404
    inconnu = client.request("DELETE", "/api/admin/models", params={"profile": "geant"})
    assert inconnu.status_code == 400


def test_suppression_modele_reservee_aux_admins(client: TestClient):
    connexion(client)
    refuse = client.request("DELETE", "/api/admin/models", params={"profile": "micro"})
    assert refuse.status_code == 403


def test_route_api_inconnue(client: TestClient):
    assert client.get("/api/inexistant").status_code == 404


# ------------------------------------------------------- génération déportée
def test_remote_llm_suggere_l_adresse_du_poste_consultant(client: TestClient):
    """Le NAS voit l'IP du PC qui consulte : autant l'offrir toute prête."""
    connexion(client, "admin")
    payload = client.get(
        "/api/admin/llm/remote", headers={"X-Forwarded-For": "192.168.1.42"}
    ).json()
    assert payload["suggested_url"] == "http://192.168.1.42:11434"


def test_remote_llm_ne_suggere_rien_en_local(client: TestClient):
    connexion(client, "admin")
    payload = client.get(
        "/api/admin/llm/remote", headers={"X-Forwarded-For": "127.0.0.1"}
    ).json()
    assert payload["suggested_url"] == ""


def test_remote_llm_refuse_une_adresse_douteuse(client: TestClient):
    connexion(client, "admin")
    refus = client.post("/api/admin/llm/remote", json={"ollama_url": "file:///etc/passwd"})
    assert refus.status_code == 400


def test_remote_llm_persiste_le_reglage(client: TestClient):
    """L'adresse doit survivre à la recréation du conteneur, donc sortir de l'image."""
    connexion(client, "admin")
    enregistre = client.post(
        "/api/admin/llm/remote",
        json={"ollama_url": "http://192.168.1.42:11434/", "llm_model": "qwen2.5:7b"},
    )
    assert enregistre.status_code == 200

    settings = client.app.state.context.settings
    assert settings.ollama_url == "http://192.168.1.42:11434"
    assert json.loads((settings.data_dir / "overrides.json").read_text(encoding="utf-8")) == {
        "ollama_url": "http://192.168.1.42:11434",
        "llm_model": "qwen2.5:7b",
    }


def test_remote_llm_reserve_aux_admins(client: TestClient):
    connexion(client)
    assert client.get("/api/admin/llm/remote").status_code == 403
    assert client.post("/api/admin/llm/remote", json={"ollama_url": ""}).status_code == 403


def test_test_remote_signale_une_adresse_injoignable(client: TestClient):
    connexion(client, "admin")
    payload = client.post(
        "/api/admin/llm/remote/test", json={"ollama_url": "http://127.0.0.1:1"}
    ).json()
    assert payload["reachable"] is False
    assert "OLLAMA_HOST=0.0.0.0" in payload["detail"]

