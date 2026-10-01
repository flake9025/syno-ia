"""Client DSM : encodage des appels, codes d'erreur et robustesse de `getinfo`."""

from __future__ import annotations

import json

import httpx
import pytest

from app.synology.client import DSMClient, DSMError

API_INFO = {
    "success": True,
    "data": {
        "SYNO.API.Auth": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 7},
        "SYNO.FileStation.List": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
        "SYNO.FileStation.Info": {"path": "entry.cgi", "minVersion": 1, "maxVersion": 2},
    },
}


def make_client(handler) -> DSMClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="http://dsm.test:5000")
    return DSMClient("http://dsm.test:5000", client=http)


def params_of(request: httpx.Request) -> dict[str, str]:
    if request.method == "POST":
        return dict(httpx.QueryParams(request.content.decode()))
    return dict(request.url.params)


def ok(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def ko(code: int, errors: list | None = None) -> httpx.Response:
    error: dict = {"code": code}
    if errors:
        error["errors"] = errors
    return httpx.Response(200, json={"success": False, "error": error})


# --------------------------------------------------------------- codes d'erreur
def test_le_meme_code_a_deux_sens_selon_lapi():
    """402 : « permission refusée » sous Auth, « système occupé » sous File Station."""
    assert "Permission" in DSMError(402, api="SYNO.API.Auth").message
    assert "occupé" in DSMError(402, api="SYNO.FileStation.List").message


def test_otp_seulement_pour_lauthentification():
    assert DSMError(403, api="SYNO.API.Auth").needs_otp
    assert not DSMError(403, api="SYNO.FileStation.List").needs_otp


def test_codes_de_session_expiree():
    # 150 : l'IP de sortie du conteneur a changé depuis la connexion.
    assert DSMError(150).is_session_expired
    assert DSMError(106).is_session_expired
    assert not DSMError(408, api="SYNO.FileStation.List").is_session_expired


def test_privilege_dapplication():
    exc = DSMError(160, api="SYNO.FileStation.List")
    assert exc.is_app_privilege
    assert "File Station" in exc.message


def test_refus_dacces_filestation():
    assert DSMError(403, api="SYNO.FileStation.List").is_permission_denied
    assert DSMError(407, api="SYNO.FileStation.List").is_permission_denied
    assert not DSMError(408, api="SYNO.FileStation.List").is_permission_denied


# ------------------------------------------------------------------- connexion
async def test_login_encode_les_parametres_attendus():
    vus: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        recu = params_of(request)
        if recu["api"] == "SYNO.API.Auth":
            vus.update(recu)
            return ok({"sid": "SID-1", "device_id": "DID-7"})
        return ok({"is_manager": False})

    client = make_client(handler)
    session = await client.login("alice", "secret")
    await client.aclose()

    assert session.sid == "SID-1"
    # DSM 7 renvoie « device_id » là où DSM 6 renvoie « did ».
    assert session.device_id == "DID-7"
    assert vus["session"] == "FileStation"  # requis par toutes les API File Station
    assert vus["format"] == "sid"
    assert vus["version"] == "6"
    assert "otp_code" not in vus


async def test_login_detecte_ladministrateur():
    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        if params_of(request)["api"] == "SYNO.API.Auth":
            return ok({"sid": "SID-ADMIN"})
        return ok({"is_manager": True})

    client = make_client(handler)
    session = await client.login("admin", "secret")
    await client.aclose()
    assert session.is_admin


async def test_login_propage_le_code_dsm():
    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        return ko(403)

    client = make_client(handler)
    with pytest.raises(DSMError) as info:
        await client.login("alice", "secret")
    await client.aclose()
    assert info.value.needs_otp


async def test_nas_injoignable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = make_client(handler)
    with pytest.raises(DSMError) as info:
        await client.login("alice", "secret")
    await client.aclose()
    assert info.value.code == 100


# -------------------------------------------------------------------- partages
async def test_list_shares_lit_real_path_et_adv_right():
    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        recu = params_of(request)
        # `additional` doit être encodé en tableau JSON (exigence DSM).
        assert json.loads(recu["additional"]) == ["real_path", "perm", "mount_point_type"]
        return ok({
            "shares": [
                {
                    "path": "/documents",
                    "name": "documents",
                    "additional": {
                        "real_path": "/volume1/documents",
                        "perm": {"acl": {"read": True, "write": False}},
                    },
                },
                {
                    "path": "/archives",
                    "name": "archives",
                    "additional": {
                        "real_path": "/volume1/archives",
                        "perm": {
                            "acl": {"read": True},
                            "adv_right": {"disable_list": True, "disable_download": True},
                        },
                    },
                },
            ]
        })

    client = make_client(handler)
    shares = await client.list_shares("SID")
    await client.aclose()

    assert [share.path for share in shares] == ["/documents", "/archives"]
    assert shares[0].readable
    # « Interdire la navigation » retire le partage du périmètre consultable.
    assert not shares[1].readable


# --------------------------------------------------------------------- getinfo
async def test_stat_paths_lot_nominal():
    appels = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        recu = params_of(request)
        appels.append(json.loads(recu["path"]))
        return ok({
            "files": [
                {"path": "/documents/a.pdf", "name": "a.pdf"},
                {"path": "/documents/b.pdf", "name": "b.pdf"},
            ]
        })

    client = make_client(handler)
    verdicts = await client.stat_paths("SID", ["/documents/a.pdf", "/documents/b.pdf"])
    await client.aclose()

    assert verdicts == {"/documents/a.pdf": True, "/documents/b.pdf": True}
    assert len(appels) == 1  # un seul appel groupé quand tout va bien


async def test_stat_paths_reprend_chemin_par_chemin_si_le_lot_echoue():
    """Un fichier supprimé depuis l'indexation ne doit pas masquer les autres."""
    lots = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        demandes = json.loads(params_of(request)["path"])
        lots.append(demandes)
        if len(demandes) > 1:
            return ko(408, errors=[{"code": 408, "path": "/documents/disparu.pdf"}])
        if demandes == ["/documents/disparu.pdf"]:
            return ko(408)
        return ok({"files": [{"path": demandes[0], "name": "ok.pdf"}]})

    client = make_client(handler)
    verdicts = await client.stat_paths(
        "SID", ["/documents/a.pdf", "/documents/disparu.pdf", "/documents/b.pdf"]
    )
    await client.aclose()

    assert verdicts == {
        "/documents/a.pdf": True,
        "/documents/disparu.pdf": False,
        "/documents/b.pdf": True,
    }
    assert len(lots) == 4  # 1 lot en échec + 3 reprises unitaires


async def test_stat_paths_refuse_sur_acl_en_lecture_seule_negative():
    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        return ok({
            "files": [
                {
                    "path": "/documents/secret.pdf",
                    "name": "secret.pdf",
                    "additional": {"perm": {"acl": {"read": False}}},
                }
            ]
        })

    client = make_client(handler)
    verdicts = await client.stat_paths("SID", ["/documents/secret.pdf"])
    await client.aclose()
    assert verdicts == {"/documents/secret.pdf": False}


async def test_stat_paths_refuse_les_chemins_absents_de_la_reponse():
    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        return ok({"files": [{"path": "/documents/a.pdf", "name": "a.pdf"}]})

    client = make_client(handler)
    verdicts = await client.stat_paths("SID", ["/documents/a.pdf", "/documents/fantome.pdf"])
    await client.aclose()
    assert verdicts["/documents/a.pdf"] is True
    assert verdicts["/documents/fantome.pdf"] is False


async def test_stat_paths_propage_une_session_expiree():
    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        return ko(106)

    client = make_client(handler)
    with pytest.raises(DSMError):
        await client.stat_paths("SID", ["/documents/a.pdf", "/documents/b.pdf"])
    await client.aclose()


async def test_stat_paths_propage_un_defaut_de_privilege_dapplication():
    """Inutile d'insister chemin par chemin si File Station est interdit au compte."""
    appels = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "query.cgi" in request.url.path:
            return httpx.Response(200, json=API_INFO)
        appels.append(1)
        return ko(160)

    client = make_client(handler)
    with pytest.raises(DSMError) as info:
        await client.stat_paths("SID", ["/documents/a.pdf", "/documents/b.pdf"])
    await client.aclose()
    assert info.value.is_app_privilege
    assert len(appels) == 1


async def test_stat_paths_sans_chemin():
    client = make_client(lambda request: httpx.Response(200, json=API_INFO))
    assert await client.stat_paths("SID", []) == {}
    await client.aclose()
