"""Tests de bout en bout de l'API HTTP (DSM simulé, aucun accès réseau)."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient

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


async def fake_login(account: str, password: str, otp_code: str | None = None) -> DSMSession:
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


def test_route_api_inconnue(client: TestClient):
    assert client.get("/api/inexistant").status_code == 404
