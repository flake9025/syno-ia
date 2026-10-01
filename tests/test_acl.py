"""Contrôle d'accès : le filtrage doit être strictement *fail-closed*."""

from __future__ import annotations

import pytest

from app.synology.acl import AccessController
from app.synology.client import DSMError
from app.synology.models import DSMSession, ShareInfo


class FakeClient:
    """Client DSM simulé : chaque utilisateur possède ses partages et ses fichiers."""

    def __init__(self, shares: dict[str, list[str]], files: dict[str, set[str]]):
        self._shares = shares
        self._files = files
        self.stat_calls = 0
        self.share_calls = 0
        self.fail_stat = False
        self.expire = False

    async def list_shares(self, sid: str) -> list[ShareInfo]:
        self.share_calls += 1
        if self.expire:
            raise DSMError(106)
        return [
            ShareInfo(path=path, name=path.strip("/"), real_path=f"/volume1{path}")
            for path in self._shares.get(sid, [])
        ]

    async def stat_paths(self, sid: str, paths):
        self.stat_calls += 1
        if self.expire:
            raise DSMError(106)
        if self.fail_stat:
            raise DSMError(402)
        allowed = self._files.get(sid, set())
        return {path: path in allowed for path in paths}


def session(sid: str, account: str = "alice") -> DSMSession:
    return DSMSession(sid=sid, account=account)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient(
        shares={"sid-alice": ["/documents"], "sid-bob": ["/documents", "/rh"]},
        files={
            "sid-alice": {"/documents/public.pdf"},
            "sid-bob": {"/documents/public.pdf", "/rh/salaires.xlsx"},
        },
    )


async def test_partage_non_visible_est_refuse(client: FakeClient):
    controller = AccessController(client, ttl=60)
    autorises = await controller.filter_paths(
        session("sid-alice"), ["/rh/salaires.xlsx", "/documents/public.pdf"]
    )
    assert autorises == {"/documents/public.pdf"}


async def test_acl_par_fichier_dans_un_partage_visible(client: FakeClient):
    """Alice voit /documents mais n'a pas accès à tous ses fichiers."""
    controller = AccessController(client, ttl=60)
    autorises = await controller.filter_paths(
        session("sid-alice"), ["/documents/public.pdf", "/documents/confidentiel.pdf"]
    )
    assert autorises == {"/documents/public.pdf"}


async def test_utilisateur_privilegie(client: FakeClient):
    controller = AccessController(client, ttl=60)
    autorises = await controller.filter_paths(
        session("sid-bob", "bob"), ["/rh/salaires.xlsx", "/documents/public.pdf"]
    )
    assert autorises == {"/rh/salaires.xlsx", "/documents/public.pdf"}


async def test_erreur_dsm_refuse_tout(client: FakeClient):
    """Une panne de vérification ne doit jamais ouvrir l'accès."""
    client.fail_stat = True
    controller = AccessController(client, ttl=60)
    assert await controller.filter_paths(session("sid-alice"), ["/documents/public.pdf"]) == set()


async def test_session_expiree_est_propagee(client: FakeClient):
    client.expire = True
    controller = AccessController(client, ttl=60)
    with pytest.raises(DSMError):
        await controller.filter_paths(session("sid-alice"), ["/documents/public.pdf"])


async def test_mise_en_cache_des_verdicts(client: FakeClient):
    controller = AccessController(client, ttl=300)
    for _ in range(3):
        await controller.filter_paths(session("sid-alice"), ["/documents/public.pdf"])
    assert client.stat_calls == 1
    assert client.share_calls == 1


async def test_invalidation_du_cache(client: FakeClient):
    controller = AccessController(client, ttl=300)
    await controller.filter_paths(session("sid-alice"), ["/documents/public.pdf"])
    controller.invalidate("sid-alice")
    await controller.filter_paths(session("sid-alice"), ["/documents/public.pdf"])
    assert client.stat_calls == 2


async def test_mode_non_strict_saute_la_verification_fichier(client: FakeClient):
    controller = AccessController(client, ttl=60, strict=False)
    autorises = await controller.filter_paths(
        session("sid-alice"), ["/documents/confidentiel.pdf", "/rh/salaires.xlsx"]
    )
    assert autorises == {"/documents/confidentiel.pdf"}
    assert client.stat_calls == 0


async def test_sans_partage_aucun_acces():
    controller = AccessController(FakeClient({}, {}), ttl=60)
    assert await controller.filter_paths(session("sid-inconnu"), ["/documents/a.pdf"]) == set()
